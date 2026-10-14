"""Custom CDS model definition for cds completeness splitted for S5"""

import logging
import typing

from maas_cds.lib.orbit_id_strategy import S5DatatakeIdStrategy
from maas_cds.model import generated


from maas_cds.model.cds_completeness.cds_completeness_splitted import (
    CdsCompletenessSplitted,
)

__all__ = ["CdsCompletenessSplittedS5"]

LOGGER = logging.getLogger("CdsCompletenessSplittedS5")


class CdsCompletenessSplittedS5(CdsCompletenessSplitted):
    """Document handling cds S5 completeness"""

    MISSION = "S5"

    ORBIT_ID_STRATEGY = S5DatatakeIdStrategy

    DATATAKE_ID_REGEX_FORMAT = S5DatatakeIdStrategy.ID_REGEX_FORMAT

    # Product type triggering the missing orbit check: the S5 datatake holding
    # it is the reference to spot the orbits absent from the database
    MISSING_ORBIT_PRODUCT_TYPE = "L1B_RA_BD1"

    MISSING_ORBIT_TIMELINESS = "OFFL"

    def find_brother_products_scan(self):
        """Specific method to query productsfor S5 Completeness

        Returns:
            Generator: Publication of implied product
        """
        search_iter = (
            generated.CdsPublication.search()
            .filter("term", satellite_unit=self.satellite_unit)
            .filter("term", datatake_id=self.datatake_id)
            .filter("term", timeliness=self.timeliness)
            .filter("term", product_type=self.product_type)
            .filter("term", service_type=self.service_type)
            .filter("term", service_id=self.service_id)
            .params(ignore=404)
            .scan()
        )

        return search_iter

    def is_compute_key_to_check_missing_orbit(self):
        """Frame method to specify the check if the product launch a
        missing orbit check/generation

        Returns:
            bool: True if this product enable a check
        """
        return (
            self.product_type == self.MISSING_ORBIT_PRODUCT_TYPE
            and self.timeliness == self.MISSING_ORBIT_TIMELINESS
        )

    @classmethod
    def generate_datatake_ids_list_between_2_ids(
        cls, datatake_ref_1: str, datatake_ref_2: str
    ) -> typing.List[str]:
        """Function which return a list of all S5 datatakes_id string which are
        between 2 S5 datatake_ids given as arguments

        Args:
            datatake_ref_1 (str): 1st datatake_id reference
            datatake_ref_2 (str): 2nd datatake_id reference

        Raises:
            ValueError: ValueError raised if datatake_ids does not respect the expected format

        Returns:
            typing.List[str]: The list of datatake_ids between the 2 references
        """
        return cls.ORBIT_ID_STRATEGY.ids_between(datatake_ref_1, datatake_ref_2)

    @classmethod
    def sort_datatake_id(
        cls, datatake_id_1: str, datatake_id_2: str
    ) -> typing.Tuple[str, str]:
        """This function sort 2 datatake ids in ascending order

        Args:
            datatake_id_1 (str): datatake_id string nb1
            datatake_id_2 (str): datatake_id string nb2

        Raises:
            ValueError: ValueError raised if datatake_id does not respect the expected format

        Returns:
            Tuple[str, str]: The 2 datatake_ids string sorted in ascending order in a tuple
        """
        return cls.ORBIT_ID_STRATEGY.sort_ids(datatake_id_1, datatake_id_2)
