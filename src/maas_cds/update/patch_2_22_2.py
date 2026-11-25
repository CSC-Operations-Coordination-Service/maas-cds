#!/usr/bin/env python3
"""

Migration script to migrate CDS from 2.22.1 to 2.22.2

An AI_RAW is attached to one AIS datatake only, the one sharing the most sensing
with it. Until 2.22.2 a neighbour it also covered did not count it: a short AIS
datatake inside a long AI_RAW stayed at 0 %, or without completeness at all (19 of
the 21 S1 AIS datatakes below 50 % from 2026-11-11 to 2026-11-24). From 2.22.2 an
AIS datatake also counts the AI_RAW of the same timeliness attached to another
datatake, clipped to its observation (CdsDatatakeS1.get_ai_raw_of_other_datatakes).

The engines only compute a datatake again when one of its products arrives. This
patch computes the AIS datatakes an AI_RAW attached elsewhere overlaps
(CdsProductS1.counts_for_other_ai_datatake), and writes the changed ones. Nothing
else changes: no AI_RAW moves, no datatake is created.

Run it after `completeness-s1` runs 2.22.2. In dry run nothing is written, every
datatake is still computed and the change of its AI_RAW completeness logged. It can
run again: a datatake already in line is not written.
"""

import bisect
import logging
import sys

from maas_engine.update.migration import MaasMigrator, migration_main

from maas_cds.lib.config_manager import MaasConfigManager
from maas_cds.model import CdsDatatake, CdsDatatakeS1, CdsProductS1
from maas_cds.model.configuration import OcsConfigDataflow
from maas_cds.update.patch_2_22_0 import (
    AI_RAW,
    AIS,
    COMPLETENESS_TOLERANCE,
    DATATAKE_PAGE_SIZE,
    DATATAKE_SOURCE,
    MISSING_PERIODS_MAXIMAL_OFFSET,
    SCROLL,
    PatchV2_22_0,
)

# the AIS datatakes before the end of an AI_RAW, at most: a 75 min AI_RAW covers
# about 5 of them
OVERLAP_CANDIDATES = 20

PRODUCT_SOURCE = [
    "name",
    "satellite_unit",
    "datatake_id",
    "timeliness",
    "sensing_start_date",
    "sensing_end_date",
    "prip_id",
]

AI_RAW_PERCENTAGE = f"{AI_RAW}_local_percentage"


class PatchV2_22_2(MaasMigrator):
    """Migration tool to migrate from 2.22.1 to 2.22.2"""

    def __init__(self, args):
        super().__init__(args)

        # key of the AIS datatakes an AI_RAW attached elsewhere overlaps
        self.datatakes_to_compute = set()

    def setup(self):
        """override"""
        super().setup()

        # as COMPUTE_COMPLETENESS
        MaasConfigManager(config_model_class=[OcsConfigDataflow()])

        CdsDatatake.COMPLETENESS_TOLERANCE = COMPLETENESS_TOLERANCE
        CdsDatatake.MISSING_PERIODS_MAXIMAL_OFFSET = MISSING_PERIODS_MAXIMAL_OFFSET

        # -v is for the patch: one line per request would hide it
        logging.getLogger("opensearch").setLevel(logging.WARNING)

    def load_ais_datatakes(self):
        """The AIS datatakes, by satellite unit and observation start

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
            datatakes.setdefault(datatake.satellite_unit, []).append(datatake)

        for unit_datatakes in datatakes.values():
            unit_datatakes.sort(key=lambda datatake: datatake.observation_time_start)

        return datatakes

    @staticmethod
    def search_overlapped_datatakes(datatakes, starts, product):
        """The AIS datatakes an AI_RAW counts for, besides its own

        Args:
            datatakes (list(CdsDatatakeS1)): the AIS datatakes of the unit, by start
            starts (list(datetime)): their observation start
            product (CdsProductS1): an AI_RAW

        Returns:
            list(CdsDatatakeS1): the datatakes
        """
        last = bisect.bisect_left(starts, product.sensing_end_date)

        return [
            datatake
            for datatake in datatakes[max(0, last - OVERLAP_CANDIDATES) : last]
            if product.counts_for_other_ai_datatake(datatake)
        ]

    def find_datatakes_to_compute(self):
        """Find the AIS datatakes an AI_RAW attached elsewhere overlaps"""
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
            .filter("exists", field="prip_id")
            .source(PRODUCT_SOURCE)
            .params(ignore=404)
        )

        self.logger.info(
            "[AI_RAW] - %s documents to check against %s AIS datatakes",
            search.count(),
            sum(len(unit_datatakes) for unit_datatakes in datatakes.values()),
        )

        for product in search.params(scroll=SCROLL).scan():
            if not (product.sensing_start_date and product.sensing_end_date):
                continue

            for datatake in self.search_overlapped_datatakes(
                datatakes.get(product.satellite_unit, []),
                starts.get(product.satellite_unit, []),
                product,
            ):
                key = f"{datatake.satellite_unit}-{datatake.datatake_id}"

                if key not in self.datatakes_to_compute:
                    self.logger.debug(
                        "[AI_RAW] - %s (%s) also covers %s",
                        product.name,
                        product.datatake_id,
                        key,
                    )

                self.datatakes_to_compute.add(key)

        self.logger.info(
            "[AI_RAW] - %s AIS datatakes covered by an AI_RAW of another one",
            len(self.datatakes_to_compute),
        )

    @MaasMigrator.migration_action_iterator
    def datatake_iterator(self):
        """Compute the AIS datatakes an AI_RAW attached elsewhere overlaps

        Also in dry run, to log the change: only the actions are skipped.
        """
        keys = sorted(self.datatakes_to_compute)
        changed = 0

        for page_start in range(0, len(keys), DATATAKE_PAGE_SIZE):
            page = keys[page_start : page_start + DATATAKE_PAGE_SIZE]

            for datatake in (
                CdsDatatakeS1.search()
                .filter("ids", values=page)
                .params(
                    ignore=404,
                    version=True,
                    seq_no_primary_term=True,
                    scroll=SCROLL,
                    size=DATATAKE_PAGE_SIZE,
                )
                .scan()
            ):
                # as the engine does on a mission planning update
                original_datatake_dict = datatake.to_dict()

                datatake.purge_dynamic_fields()
                datatake.missing_periods = None

                yield from datatake.load_data_before_compute()

                datatake.compute_completeness()

                if original_datatake_dict == datatake.to_dict():
                    continue

                changed += 1

                self.logger.info(
                    "[Datatake] - %s %s: AI_RAW %s %% -> %s %%",
                    datatake.key,
                    datatake.timeliness,
                    original_datatake_dict.get(AI_RAW_PERCENTAGE),
                    datatake.to_dict().get(AI_RAW_PERCENTAGE),
                )

                yield datatake.to_bulk_action()

        self.logger.info("[Datatake] - %s of %s datatakes changed", changed, len(keys))

    # a failed action is logged and the patch goes on (version conflicts)
    bulk_exec = PatchV2_22_0.bulk_exec

    def run(self):
        """override"""
        self.find_datatakes_to_compute()

        self.bulk_exec(self.datatake_iterator())


def run():
    migration_main(sys.argv[1:], PatchV2_22_2)


if __name__ == "__main__":
    run()
