"""Custom CDS model definition for s1 product"""

from datetime import timedelta
import logging

from maas_cds.lib.parsing_name import utils
from maas_cds.model.product import CdsProduct, DynamicPartitionMixin
from maas_cds.model.datatake_s1 import CdsDatatakeS1
from opensearchpy import Q

__all__ = ["CdsProductS1"]


LOGGER = logging.getLogger("CdsModelProductS1")


class CdsProductS1(CdsProduct):
    """CdsProduct specific for sentinel 1"""

    # the datatake an attached AI_RAW has just left, not stored
    detached_datatake_id = None

    def get_detached_compute_key(self):
        """Compute key of the datatake this AI_RAW has just left, if any"""
        if self.detached_datatake_id is None:
            return None

        return (f"{self.satellite_unit}-{self.detached_datatake_id}", self.product_type)

    def has_known_timeliness(self):
        """The AI_RAW timeliness comes from its packet store, when the PRIP gives it"""
        return (
            self.timeliness is not None
            and self.timeliness != utils.TIMELINESS_NULL_VALUE
        )

    def get_ai_datatake_window(self):
        """Sensing window of an AI_RAW in which its AIS datatake is searched

        An AI_RAW can span several AIS datatakes, or be a few seconds split just
        after the end of its datatake: any AIS datatake overlapping the sensing
        widened by MATCHING_DELTA_PRODUCTS is a candidate, the nearest wins.

        Returns:
            tuple(datetime, datetime): start and end of the window
        """
        delta = timedelta(seconds=CdsDatatakeS1.MATCHING_DELTA_PRODUCTS)

        return self.sensing_start_date - delta, self.sensing_end_date + delta

    def is_ai_datatake_candidate(self, datatake):
        """Same rule as the AI_RAW datatake search, for a datatake in memory

        Args:
            datatake (CdsDatatakeS1): an AIS datatake of the same satellite unit

        Returns:
            bool: the datatake can hold this AI_RAW
        """
        start_date, end_date = self.get_ai_datatake_window()

        return (
            datatake.observation_time_start <= end_date
            and datatake.observation_time_stop >= start_date
            and (
                not self.has_known_timeliness()
                or datatake.timeliness == self.timeliness
            )
        )

    def get_nearest_datatake(self, datatakes):
        """The datatake sharing the most sensing with the product, the closest
        one when none overlaps it

        Args:
            datatakes (list(CdsDatatakeS1)): candidate datatakes

        Returns:
            CdsDatatakeS1: the nearest datatake, None without candidate
        """
        return max(
            datatakes,
            key=lambda d: (
                min(self.sensing_end_date, d.observation_time_stop)
                - max(self.sensing_start_date, d.observation_time_start)
            ).total_seconds(),
            default=None,
        )

    def attach_to_datatake(self, datatake):
        """Attach the product to a datatake, remember the one it leaves

        Args:
            datatake (CdsDatatakeS1): the datatake of the product
        """
        if (
            not self.datatake_id_is_missing()
            and self.datatake_id != datatake.datatake_id
        ):
            self.detached_datatake_id = self.datatake_id

        setattr(self, "datatake_id", datatake.datatake_id)
        setattr(self, "timeliness", datatake.timeliness)

    def get_compute_key(self):
        """override specific method for sentinel 1

        Returns:
            tuple: a tuple as key allowing us to group product computation
        """

        # We match all product between observation ± delta in seconds

        # Corner case
        if CdsDatatakeS1.is_product_type_without_datatake_id(self.product_type):
            if self.product_type == "AI_RAW__0_":

                start_date, end_date = self.get_ai_datatake_window()

                ai_filters = [
                    Q("term", satellite_unit=self.satellite_unit),
                    Q(
                        "range",
                        observation_time_start={"lte": end_date},
                    ),
                    Q(
                        "range",
                        observation_time_stop={"gte": start_date},
                    ),
                ]

                if self.has_known_timeliness():
                    ai_filters.append(
                        Q("term", timeliness=self.timeliness),
                    )

                search = (
                    CdsDatatakeS1.search()
                    .filter(Q("bool", filter=ai_filters))
                    .filter("term", instrument_mode="AIS")
                )

            if "ERRMAT" in self.product_type:
                start_date = self.sensing_end_date + timedelta(
                    seconds=CdsDatatakeS1.MATCHING_DELTA_PRODUCTS
                )
                end_date = self.sensing_end_date - timedelta(
                    seconds=CdsDatatakeS1.MATCHING_DELTA_PRODUCTS
                )
                search = (
                    CdsDatatakeS1.search()
                    .filter(
                        Q(
                            "bool",
                            filter=[
                                Q(
                                    "range",
                                    observation_time_start={"lte": start_date},
                                ),
                                Q(
                                    "range",
                                    observation_time_stop={"gte": end_date},
                                ),
                            ],
                        )
                    )
                    .filter("term", satellite_unit=self.satellite_unit)
                    .filter("term", instrument_mode="RFC")
                    .filter("term", polarization=f"D{self.product_type[2]}")
                )

            datatake_doc = list(search.execute())
            if datatake_doc:
                if len(datatake_doc) > 1:
                    LOGGER.warning(
                        "Too much datatake for product %s using the nearest one",
                        self.name,
                    )

                datatake_doc = self.get_nearest_datatake(datatake_doc)

                self.attach_to_datatake(datatake_doc)

            else:
                LOGGER.warning("No datatake for product %s", self.name)
                return None
            return (datatake_doc.meta.id, self.product_type)

        # Protective behaviour
        # This is based on dataflow
        if (
            not self.get_datatake_id()
            and self.product_level
            not in DynamicPartitionMixin.PRODUCT_LEVEL_THAT_MAKE_SENSE
            or self.instrument_mode
            not in (
                "SM",
                "IW",
                "EW",
                "WV",
                "RFC",
                "ZI",
                "ZE",
                "ZW",
                "AN",
                "ZS",
                "AI",
            )
            or self.product_type in CdsDatatakeS1.EXCLUDES_PRODUCTED_TYPES
        ):
            return None

        return (self.get_datatake_id(), self.product_type)
