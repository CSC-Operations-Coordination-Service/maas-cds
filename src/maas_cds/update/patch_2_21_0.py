#!/usr/bin/env python3
"""

Migration script to migrate CDS from 2.20 to 2.21

The S2C mission planning is about 30s late on the real sensing. Until 2.21 the
S2C datastrips and publications were attached by their sensing without that
shift: some of them went to the previous datatake, and the granules of their
product group stayed without datatake. From 2.21 the engines apply the shift and
check the datatake of the datastrips again. This patch brings the S2C documents
sensed since PATCH_2_21_0_FROM (default 2026-09-01) in line, with the 2.21 code:

1. the datastrips move to the datatake covering their shifted sensing
   (COMPUTE_COMPLETENESS, CdsProductS2.check_datatake_id);
2. each datatake owns the product groups of its datastrips and takes their
   granules and tiles (CdsDatatakeS2.load_data_before_compute); all the datatakes
   of the period are then computed again, as on a mission planning update, and
   only the changed ones are written: a run stopped after the re-attach leaves
   no datatake behind for the next one;
3. the publications move to the nearest datatake of their shifted sensing
   (consolidation, find_nearest_datatake).

The per service completeness of the datatakes whose publications moved is only
logged: its S2 compute fails from 2.15 (CdsCompleteness.find_brother_products_scan
has no include_deleted), the 2.14 completeness-v2-s2 computes it.

Run it after the engines attaching S2 documents run 2.21: an older one attaches
the new ones without the shift. In dry run nothing is written: step 2 reads what
step 1 writes, so it only counts what it would compute.
"""

import bisect
import datetime
import logging
import os
import sys

import opensearchpy.connection.connections as db_connections
from opensearchpy.helpers import parallel_bulk

from maas_engine.update.migration import MaasMigrator, migration_main
from maas_model import datestr_to_utc_datetime

from maas_cds.engines.reports.base import BaseProductConsolidatorEngine
from maas_cds.lib.config_manager import MaasConfigManager
from maas_cds.lib.parsing_name import utils
from maas_cds.lib.queryutils.find_datatake_from_sensing import S2C_MP_SHIFT_SECONDS
from maas_cds.model import (
    CdsCompleteness,
    CdsDatatake,
    CdsDatatakeS2,
    CdsProductS2,
    CdsPublication,
)
from maas_cds.model.configuration import OcsConfigDataflow

SATELLITE_UNIT = "S2C"

DEFAULT_FROM = "2026-09-01T00:00:00.000Z"

# COMPUTE_COMPLETENESS arguments in production: no S2 tolerance, missing
# periods of the L0 datastrips
MISSING_PERIODS_MAXIMAL_OFFSET = {"S2": {"local": {"MSI_L0__DS": 10000000}}}

# what the consolidation fills from the datatake (fill_from_datatake)
PUBLICATION_DATATAKE_FIELDS = [
    "datatake_id",
    "timeliness",
    "absolute_orbit",
    "instrument_mode",
]

PUBLICATION_SOURCE = PUBLICATION_DATATAKE_FIELDS + [
    "mission",
    "satellite_unit",
    "product_type",
    "service_type",
    "service_id",
    "sensing_start_date",
    "sensing_end_date",
]

# the datatakes overlapping the search window of a publication, at most
NEAREST_CANDIDATES = 10

# The scans below query and write for each hit: a page of the default scroll
# (1000 hits, 5 minutes) outlives its shard contexts (ScanError on 2026-11-04,
# 1000 datatakes in more than 5 minutes)
SCROLL = "1h"
DATATAKE_PAGE_SIZE = 100


class PatchV2_21_0(MaasMigrator):
    """Migration tool to migrate from 2.20 to 2.21"""

    def __init__(self, args):
        super().__init__(args)

        self.from_date = datestr_to_utc_datetime(
            os.environ.get("PATCH_2_21_0_FROM", DEFAULT_FROM)
        )
        self.to_date = datetime.datetime.now(tz=datetime.UTC)

        # datatake_id of the datatakes that gained or lost a product
        self.datatakes_to_compute = set()

        # (service_type, service_id) -> datatake_id that gained or lost a publication
        self.completeness_to_compute = {}

    def setup(self):
        """override"""
        super().setup()

        # as COMPUTE_COMPLETENESS: the duplicated pairs per interface come from
        # the dataflow
        MaasConfigManager(config_model_class=[OcsConfigDataflow()])

        CdsDatatake.COMPLETENESS_TOLERANCE = None
        CdsDatatake.MISSING_PERIODS_MAXIMAL_OFFSET = MISSING_PERIODS_MAXIMAL_OFFSET

        # -v is for the patch: one line per request would hide it
        logging.getLogger("opensearch").setLevel(logging.WARNING)

        self.logger.info(
            "Patching the %s documents sensed from %s to %s",
            SATELLITE_UNIT,
            self.from_date,
            self.to_date,
        )

    def sensing_search(self, model):
        """Search the S2C documents sensed in the period"""
        return (
            model.search()
            .filter("term", mission="S2")
            .filter("term", satellite_unit=SATELLITE_UNIT)
            .filter(
                "range",
                sensing_start_date={"gte": self.from_date, "lte": self.to_date},
            )
        )

    def datatake_search(self):
        """Search the S2C datatakes observed in the period"""
        return (
            CdsDatatakeS2.search()
            .filter("term", mission="S2")
            .filter("term", satellite_unit=SATELLITE_UNIT)
            .filter(
                "range",
                observation_time_start={"gte": self.from_date, "lte": self.to_date},
            )
        )

    @MaasMigrator.migration_action_iterator
    def datastrip_iterator(self):
        """Move the S2C datastrips to the datatake of their shifted sensing"""
        search = (
            self.sensing_search(CdsProductS2)
            .filter("terms", product_type=CdsDatatakeS2.get_datastrip_product_types())
            .exclude("term", datatake_id=utils.DATATAKE_ID_MISSING_VALUE)
            .params(ignore=404)
        )

        self.logger.info("[Datastrip] - %s documents to check", search.count())

        for product in search.params(
            version=True, seq_no_primary_term=True, scroll=SCROLL
        ).scan():
            product.check_datatake_id()

            if product.detached_datatake_id is None:
                continue

            self.logger.info(
                "[Datastrip] - %s moves from %s to %s",
                product.name,
                product.detached_datatake_id,
                product.datatake_id,
            )

            self.datatakes_to_compute.update(
                {product.detached_datatake_id, product.datatake_id}
            )

            yield product.to_bulk_action()

    @MaasMigrator.migration_action_iterator
    def product_group_iterator(self):
        """Attach the granules and tiles to the datatake of their product group"""
        search = self.datatake_search().params(ignore=404)

        self.logger.info("[Datatake] - %s documents to check", search.count())

        for datatake in search.params(scroll=SCROLL, size=DATATAKE_PAGE_SIZE).scan():
            product_group_ids = datatake.search_datastrip_product_group_ids()

            if product_group_ids != sorted(datatake.product_group_ids or []):
                self.datatakes_to_compute.add(datatake.datatake_id)

            if not product_group_ids:
                continue

            # the datatakes the products leave, as the re-attach does not tell
            datatake.product_group_ids = product_group_ids
            left_datatake_ids = {
                product.datatake_id
                for product in datatake.find_related_document_not_attached()
                .source(["datatake_id"])
                .scan()
            }

            if not left_datatake_ids:
                continue

            self.logger.info(
                "[Datatake] - %s takes products of %s",
                datatake.datatake_id,
                sorted(left_datatake_ids),
            )

            self.datatakes_to_compute.add(datatake.datatake_id)
            self.datatakes_to_compute.update(
                left_datatake_ids - {utils.DATATAKE_ID_MISSING_VALUE, None}
            )

            yield from datatake.load_data_before_compute()

    @MaasMigrator.migration_action_iterator
    def datatake_iterator(self):
        """Compute the S2C datatakes of the period again

        All of them, not only the ones that gained or lost a product in this run:
        the products a stopped run re-attached are on their datatake for the
        next run, which would not compute it.
        """
        search = self.datatake_search().params(ignore=404)

        self.logger.info(
            "[Datatake] - %s datatakes to compute, %s gained or lost a product",
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
            original_status = getattr(datatake, "final_completeness_status", None)

            datatake.purge_dynamic_fields()
            datatake.missing_periods = None

            yield from datatake.load_data_before_compute()

            datatake.compute_completeness()

            if original_datatake_dict == datatake.to_dict():
                continue

            self.logger.info(
                "[Datatake] - %s: %s -> %s",
                datatake.datatake_id,
                original_status,
                datatake.final_completeness_status,
            )

            yield datatake.to_bulk_action()

    def load_datatake_windows(self):
        """The S2C datatakes a publication of the period can be attached to

        Returns:
            list(CdsDatatakeS2): the datatakes, by observation start
        """
        margin = datetime.timedelta(days=1)

        datatakes = list(
            CdsDatatakeS2.search()
            .filter("term", mission="S2")
            .filter("term", satellite_unit=SATELLITE_UNIT)
            .filter(
                "range",
                observation_time_start={
                    "gte": self.from_date - margin,
                    "lte": self.to_date + margin,
                },
            )
            .source(
                PUBLICATION_DATATAKE_FIELDS
                + [
                    "mission",
                    "satellite_unit",
                    "observation_time_start",
                    "observation_time_stop",
                ]
            )
            .params(ignore=404)
            .scan()
        )

        datatakes.sort(key=lambda datatake: datatake.observation_time_start)

        return datatakes

    @staticmethod
    def search_candidate_datatakes(datatakes, starts, publication):
        """The datatakes the consolidation searches for a publication

        Same window as BaseProductConsolidatorEngine._get_datatake_S2_map

        Args:
            datatakes (list(CdsDatatakeS2)): the datatakes, by observation start
            starts (list(datetime)): their observation start
            publication (CdsPublication): the publication to attach

        Returns:
            list(CdsDatatakeS2): the candidate datatakes
        """
        tolerance = datetime.timedelta(
            seconds=BaseProductConsolidatorEngine.S2_DATATAKE_ATTACHEMENT_DELTA
        )
        shift = datetime.timedelta(seconds=S2C_MP_SHIFT_SECONDS)

        last = bisect.bisect_right(
            starts, publication.sensing_end_date + 2 * tolerance + shift
        )

        return [
            datatake
            for datatake in datatakes[max(0, last - NEAREST_CANDIDATES) : last]
            if datatake.observation_time_stop
            >= publication.sensing_start_date - 2 * tolerance
        ]

    @MaasMigrator.migration_action_iterator
    def publication_iterator(self):
        """Move the S2C publications to the nearest datatake of their shifted sensing"""
        datatakes = self.load_datatake_windows()
        starts = [datatake.observation_time_start for datatake in datatakes]

        tolerance = datetime.timedelta(
            seconds=BaseProductConsolidatorEngine.S2_DATATAKE_ATTACHEMENT_DELTA
        )

        search = (
            self.sensing_search(CdsPublication)
            .filter("prefix", product_type="MSI")
            .source(PUBLICATION_SOURCE)
            .params(ignore=404)
        )

        self.logger.info(
            "[Publication] - %s documents to check against %s datatakes",
            search.count(),
            len(datatakes),
        )

        moved = 0

        for publication in search.params(
            seq_no_primary_term=True, scroll=SCROLL
        ).scan():
            nearest_datatake = publication.find_nearest_datatake(
                self.search_candidate_datatakes(datatakes, starts, publication),
                tolerance,
            )

            # as the consolidation: no datatake found, nothing changes
            if nearest_datatake is None:
                continue

            previous_datatake_id = publication.datatake_id
            initial_dict = publication.to_dict()

            publication.fill_from_datatake(nearest_datatake)

            changes = {
                field: getattr(publication, field, None)
                for field in PUBLICATION_DATATAKE_FIELDS
                if getattr(publication, field, None) != initial_dict.get(field)
            }

            if not changes:
                continue

            if publication.datatake_id != previous_datatake_id:
                moved += 1

                self.completeness_to_compute.setdefault(
                    (publication.service_type, publication.service_id), set()
                ).update(
                    {previous_datatake_id, publication.datatake_id}
                    - {utils.DATATAKE_ID_MISSING_VALUE, None}
                )

            yield {
                "_op_type": "update",
                "_index": publication.meta.index,
                "_id": publication.meta.id,
                "if_seq_no": publication.meta.seq_no,
                "if_primary_term": publication.meta.primary_term,
                "doc": changes | {"updateTime": datetime.datetime.now(tz=datetime.UTC)},
            }

        self.logger.info("[Publication] - %s publications change datatake", moved)

    def log_completeness_to_compute(self):
        """Log the per service completeness that gained or lost a publication"""
        for (service_type, service_id), datatake_ids in sorted(
            self.completeness_to_compute.items()
        ):
            index = CdsCompleteness(
                mission="S2",
                satellite_unit=SATELLITE_UNIT,
                service_type=service_type,
                service_id=service_id,
            ).partition_index_name

            if not self.es_conn.indices.exists(index):
                self.logger.info(
                    "[Completeness][%s] - no per service completeness", index
                )
                continue

            self.logger.info(
                "[Completeness][%s] - %s datatakes to compute again: %s",
                index,
                len(datatake_ids),
                " ".join(sorted(datatake_ids)),
            )

    def bulk_exec(self, iterator):
        """override: a failed action is logged and the patch goes on

        The engines run during the patch: a version conflict is a document an
        engine wrote after the patch read it, with the 2.21 code.
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
        self.bulk_exec(self.datastrip_iterator())

        self.bulk_exec(self.product_group_iterator())

        self.bulk_exec(self.datatake_iterator())

        self.bulk_exec(self.publication_iterator())

        self.log_completeness_to_compute()


def run():
    migration_main(sys.argv[1:], PatchV2_21_0)


if __name__ == "__main__":
    run()
