#!/usr/bin/env python3
"""

Migration script to migrate CDS from 2.21 to 2.22

Each S1 mission planning report lists its first AIS datatake hours after its first
SAR one. Until 2.22 the consolidation of a report deleted the AIS datatakes of the
previous report in between and only S1C got them back (by a datatake_id range,
which broke when the S1C orbit reached 10000): the AI_RAW products sensed there had
no datatake. And COMPUTE_AI_RELATED copied the datatake of the AI_RAW before
COMPUTE_COMPLETENESS attached it, so the AI completeness had none. This patch brings
the existing documents in line, with the 2.22 code:

1. the AIS datatakes the consolidation of each report deleted are recreated from the
   previous report (PostConsolidateMpFileEngine.get_ais_to_recreate), from the first
   datatake of the unit;
2. every AI_RAW goes to the nearest AIS datatake of its sensing
   (CdsProductS1.is_ai_datatake_candidate / get_nearest_datatake);
3. all the AIS datatakes are computed again, only the changed ones are written;
4. the AI completeness takes the datatake and timeliness of its AI_RAW.

Run it after the engines run 2.22: an older post-consolidation keeps deleting the
S1D AIS datatakes. In dry run nothing is written: steps 2 and 4 use the datatakes
step 1 would recreate, step 3 only counts. It can run again: what is in line already
is skipped.
"""

import bisect
import datetime
import logging
import sys

import opensearchpy.connection.connections as db_connections
from opensearchpy.helpers import parallel_bulk

from maas_engine.update.migration import MaasMigrator, migration_main

from maas_cds import model
from maas_cds.engines.reports.post_consolidate_mp_file import (
    PostConsolidateMpFileEngine,
)
from maas_cds.lib.config_manager import MaasConfigManager
from maas_cds.lib.parsing_name import utils
from maas_cds.model import (
    CdsAiProductionCompleteness,
    CdsDatatake,
    CdsDatatakeS1,
    CdsProductS1,
    MpProduct,
)
from maas_cds.model.configuration import OcsConfigDataflow

AIS = "AIS"

AI_RAW = "AI_RAW__0_"

# COMPUTE_COMPLETENESS arguments in production (default.json)
COMPLETENESS_TOLERANCE = {
    "S1": {
        "local": {
            "WV_.*0.": -759000,
            "WV_.*1.": -13500000,
            "WV_.*2.": -12843000,
            "Z.*0.": -1000000,
            "EN.*0.": -100000,
            "S.*0.": -1000000,
            "S.*1.": -2400000,
            "S.*2.": -2000000,
            "EW_.*0.": -135000,
            "EW_.*1.": 230000,
            "IW_.*0.": -512000,
            "IW_.*1.": 150000,
        },
        "slice": {
            "IW_OCN__2.": -7400000,
            "S._OCN__2.": -8800000,
            "EW_OCN__2.": -8200000,
            "EW_SLC__1.": -8200000,
        },
    }
}
MISSING_PERIODS_MAXIMAL_OFFSET = {
    "S1": {
        "local": {
            "WV_.*0.": 6000000,
            "S.*0.": 6000000,
            "EW_.*0.": 6000000,
            "IW_.*0.": 6000000,
        }
    },
    "S2": {"local": {"MSI_L0__DS": 10000000}},
}

DATATAKE_SOURCE = [
    "datatake_id",
    "satellite_unit",
    "timeliness",
    "observation_time_start",
    "observation_time_stop",
]

# the AIS datatakes before the end of an AI_RAW window, at most
NEAREST_CANDIDATES = 20

# the S1 completeness status of a datatake (S2 has final_completeness_status)
SENSING_STATUS = "sensing_global_status"

SCROLL = "1h"
DATATAKE_PAGE_SIZE = 100


class PatchV2_22_0(MaasMigrator):
    """Migration tool to migrate from 2.21 to 2.22"""

    def __init__(self, args):
        super().__init__(args)

        self.engine = None

        # satellite_unit -> datatake_id -> AIS datatake recreated
        self.recreated = {}

        # AI_RAW name -> (datatake_id, timeliness) once attached
        self.ai_raw_datatakes = {}

        # id of the datatakes recreated or that gained or lost an AI_RAW
        self.datatakes_to_compute = set()

    def setup(self):
        """override"""
        super().setup()

        # as COMPUTE_COMPLETENESS
        MaasConfigManager(config_model_class=[OcsConfigDataflow()])

        CdsDatatake.COMPLETENESS_TOLERANCE = COMPLETENESS_TOLERANCE
        CdsDatatake.MISSING_PERIODS_MAXIMAL_OFFSET = MISSING_PERIODS_MAXIMAL_OFFSET

        # as the raw-only-mp configuration
        PostConsolidateMpFileEngine.MODEL_MODULE = model
        self.engine = PostConsolidateMpFileEngine(
            raw_data_type="MpProduct",
            consolidated_data_type="CdsDatatake",
            raw_data_time_start_field_name="observation_time_start",
            consolidated_data_time_start_field_name="observation_time_start",
        )

        # -v is for the patch: one line per request would hide it
        logging.getLogger("opensearch").setLevel(logging.WARNING)

    def get_ais_satellite_units(self):
        """The S1 units with AIS datatakes in their mission planning"""
        search = (
            MpProduct.search()
            .filter("prefix", satellite_id="S1")
            .filter("term", instrument_mode=AIS)
            .params(ignore=404)
        )
        search.aggs.bucket("units", "terms", field="satellite_id", size=20)

        return sorted(
            bucket.key for bucket in search[0].execute().aggregations.units.buckets
        )

    def get_report_names(self, satellite_unit):
        """The mission planning reports of a unit, in order"""
        search = (
            MpProduct.search()
            .filter("term", satellite_id=satellite_unit)
            .params(ignore=404)
        )
        search.aggs.bucket("reports", "terms", field="reportName", size=10000)

        return sorted(
            bucket.key for bucket in search[0].execute().aggregations.reports.buckets
        )

    def get_first_datatake_start(self, satellite_unit):
        """Observation start of the first datatake of a unit: the mission planning
        before it is a test one"""
        search = (
            CdsDatatake.search()
            .filter("term", satellite_unit=satellite_unit)
            .params(ignore=404)
        )
        search.aggs.metric("first", "min", field="observation_time_start")

        value = search[0].execute().aggregations.first["value"]

        if value is None:
            return None

        return datetime.datetime.fromtimestamp(value / 1000, tz=datetime.UTC)

    @MaasMigrator.migration_action_iterator
    def ais_datatake_iterator(self):
        """Recreate the AIS datatakes the mission planning consolidation deleted"""
        for satellite_unit in self.get_ais_satellite_units():
            first_datatake_start = self.get_first_datatake_start(satellite_unit)
            report_names = self.get_report_names(satellite_unit)
            recreated = self.recreated.setdefault(satellite_unit, {})

            self.logger.info(
                "[AIS][%s] - %s reports, first datatake at %s",
                satellite_unit,
                len(report_names),
                first_datatake_start,
            )

            if first_datatake_start is None:
                continue

            for previous_report_name, report_name in zip(
                report_names, report_names[1:]
            ):
                for mp_product in self.engine.get_ais_to_recreate(
                    satellite_unit, report_name, previous_report_name
                ):
                    if (
                        mp_product.observation_time_start < first_datatake_start
                        or mp_product.datatake_id in recreated
                    ):
                        continue

                    datatake = self.engine.consolidate_CdsDatatake_from_MpProduct(
                        mp_product
                    )

                    if datatake is None:
                        continue

                    self.logger.info(
                        "[AIS] - %s recreated from %s (%s)",
                        datatake.key,
                        previous_report_name,
                        datatake.observation_time_start,
                    )

                    recreated[mp_product.datatake_id] = datatake
                    self.datatakes_to_compute.add(datatake.key)

                    yield datatake.to_bulk_action()

            self.logger.info(
                "[AIS][%s] - %s datatakes recreated", satellite_unit, len(recreated)
            )

    def load_ais_datatakes(self):
        """The AIS datatakes an AI_RAW can be attached to, with the recreated ones

        Returns:
            dict: satellite_unit -> AIS datatakes by observation start
        """
        datatakes = {}

        for datatake in (
            CdsDatatakeS1.search()
            .filter("prefix", satellite_unit="S1")
            .filter("term", instrument_mode=AIS)
            .source(DATATAKE_SOURCE)
            .params(ignore=404, scroll=SCROLL)
            .scan()
        ):
            datatakes.setdefault(datatake.satellite_unit, {})[
                datatake.datatake_id
            ] = datatake

        # not written in dry run
        for satellite_unit, recreated in self.recreated.items():
            for datatake_id, datatake in recreated.items():
                datatakes.setdefault(satellite_unit, {}).setdefault(
                    datatake_id, datatake
                )

        return {
            satellite_unit: sorted(
                unit_datatakes.values(),
                key=lambda datatake: datatake.observation_time_start,
            )
            for satellite_unit, unit_datatakes in datatakes.items()
        }

    @staticmethod
    def search_candidate_datatakes(datatakes, starts, product):
        """The AIS datatakes CdsProductS1.get_compute_key searches for an AI_RAW

        Args:
            datatakes (list(CdsDatatakeS1)): the AIS datatakes of the unit, by start
            starts (list(datetime)): their observation start
            product (CdsProductS1): the AI_RAW to attach

        Returns:
            list(CdsDatatakeS1): the candidate datatakes
        """
        _, end_date = product.get_ai_datatake_window()

        last = bisect.bisect_right(starts, end_date)

        return [
            datatake
            for datatake in datatakes[max(0, last - NEAREST_CANDIDATES) : last]
            if product.is_ai_datatake_candidate(datatake)
        ]

    @MaasMigrator.migration_action_iterator
    def ai_raw_iterator(self):
        """Attach every AI_RAW to the nearest AIS datatake of its sensing"""
        datatakes = self.load_ais_datatakes()
        starts = {
            satellite_unit: [
                datatake.observation_time_start for datatake in unit_datatakes
            ]
            for satellite_unit, unit_datatakes in datatakes.items()
        }

        search = (
            CdsProductS1.search()
            .filter("term", mission="S1")
            .filter("term", product_type=AI_RAW)
            .params(ignore=404)
        )

        self.logger.info(
            "[AI_RAW] - %s documents to check against %s AIS datatakes",
            search.count(),
            sum(len(unit_datatakes) for unit_datatakes in datatakes.values()),
        )

        attached = moved = still_missing = 0

        for product in search.params(seq_no_primary_term=True, scroll=SCROLL).scan():
            datatake = None

            if product.sensing_start_date and product.sensing_end_date:
                datatake = product.get_nearest_datatake(
                    self.search_candidate_datatakes(
                        datatakes.get(product.satellite_unit, []),
                        starts.get(product.satellite_unit, []),
                        product,
                    )
                )

            initial_dict = product.to_dict()
            was_missing = product.datatake_id_is_missing()

            if datatake is not None:
                product.attach_to_datatake(datatake)

            self.ai_raw_datatakes[product.name] = (
                product.datatake_id,
                product.timeliness,
            )

            if product.datatake_id_is_missing():
                still_missing += 1
                continue

            changes = {
                field: getattr(product, field)
                for field in ("datatake_id", "timeliness")
                if getattr(product, field) != initial_dict.get(field)
            }

            if not changes:
                continue

            if "datatake_id" in changes:
                self.datatakes_to_compute.add(
                    f"{product.satellite_unit}-{product.datatake_id}"
                )

                if was_missing:
                    attached += 1
                else:
                    moved += 1
                    self.datatakes_to_compute.add(product.get_detached_compute_key()[0])

                self.logger.info(
                    "[AI_RAW] - %s: %s -> %s",
                    product.name,
                    initial_dict.get("datatake_id"),
                    product.datatake_id,
                )

            yield {
                "_op_type": "update",
                "_index": product.meta.index,
                "_id": product.meta.id,
                "if_seq_no": product.meta.seq_no,
                "if_primary_term": product.meta.primary_term,
                "doc": changes | {"updateTime": datetime.datetime.now(tz=datetime.UTC)},
            }

        self.logger.info(
            "[AI_RAW] - %s attached, %s moved, %s without AIS datatake",
            attached,
            moved,
            still_missing,
        )

    @MaasMigrator.migration_action_iterator
    def datatake_iterator(self):
        """Compute the AIS datatakes again

        All of them, not only the ones recreated or that gained or lost an AI_RAW
        in this run: the AI_RAW a stopped run attached are on their datatake for
        the next one, which would not compute it. Only the changed ones are written.
        """
        search = (
            CdsDatatakeS1.search()
            .filter("prefix", satellite_unit="S1")
            .filter("term", instrument_mode=AIS)
            .params(ignore=404)
        )

        self.logger.info(
            "[Datatake] - %s datatakes to compute, %s recreated or gained or lost"
            " an AI_RAW",
            search.count(),
            len(self.datatakes_to_compute),
        )

        if self.args.dry_run:
            return

        for datatake in search.params(
            version=True,
            seq_no_primary_term=True,
            scroll=SCROLL,
            size=DATATAKE_PAGE_SIZE,
        ).scan():
            # as the engine does on a mission planning update
            original_datatake_dict = datatake.to_dict()
            original_status = original_datatake_dict.get(SENSING_STATUS)

            datatake.purge_dynamic_fields()
            datatake.missing_periods = None

            yield from datatake.load_data_before_compute()

            datatake.compute_completeness()

            if original_datatake_dict == datatake.to_dict():
                continue

            self.logger.info(
                "[Datatake] - %s: %s -> %s",
                datatake.key,
                original_status,
                datatake.to_dict().get(SENSING_STATUS),
            )

            yield datatake.to_bulk_action()

    @MaasMigrator.migration_action_iterator
    def ai_completeness_iterator(self):
        """Give the AI completeness the datatake and timeliness of its AI_RAW"""
        search = (
            CdsAiProductionCompleteness.search()
            .filter("exists", field="input_name")
            .params(ignore=404)
        )

        self.logger.info("[AI completeness] - %s documents to check", search.count())

        updated = 0

        for document in search.params(seq_no_primary_term=True, scroll=SCROLL).scan():
            datatake_id, timeliness = self.ai_raw_datatakes.get(
                document.input_name, (None, None)
            )

            if datatake_id in (None, utils.DATATAKE_ID_MISSING_VALUE):
                continue

            changes = {
                field: value
                for field, value in (
                    ("datatake_id", datatake_id),
                    ("timeliness", timeliness),
                )
                if getattr(document, field, None) != value
            }

            if not changes:
                continue

            updated += 1

            yield {
                "_op_type": "update",
                "_index": document.meta.index,
                "_id": document.meta.id,
                "if_seq_no": document.meta.seq_no,
                "if_primary_term": document.meta.primary_term,
                "doc": changes | {"updateTime": datetime.datetime.now(tz=datetime.UTC)},
            }

        self.logger.info("[AI completeness] - %s documents get a datatake", updated)

    def bulk_exec(self, iterator):
        """override: a failed action is logged and the patch goes on

        The engines run during the patch: a version conflict is a document an
        engine wrote after the patch read it, with the 2.22 code.
        """
        for success, info in parallel_bulk(
            db_connections.get_connection(),
            iterator,
            refresh=True,
            raise_on_error=False,
            request_timeout=self.args.es_timeout,
        ):
            if not success:
                self.logger.error(info)

    def run(self):
        """override"""
        # each step reads what the previous one wrote (bulk_exec refreshes)
        self.bulk_exec(self.ais_datatake_iterator())

        self.bulk_exec(self.ai_raw_iterator())

        self.bulk_exec(self.datatake_iterator())

        self.bulk_exec(self.ai_completeness_iterator())


def run():
    migration_main(sys.argv[1:], PatchV2_22_0)


if __name__ == "__main__":
    run()
