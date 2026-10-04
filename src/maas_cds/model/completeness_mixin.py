"""Reusable completeness computation logic

The expected value resolution and the value / percentage / status arithmetic are
the same whatever the document holding the completeness: a completeness document
dedicated to a single (product_type, timeliness) couple like
:class:`CdsCompletenessSplitted`, or a document aggregating several of them like
:class:`S3pSession`.

This mixin gathers that logic, parametrized by product_type and timeliness
instead of reading them on the document itself.
"""

import logging

from collections import namedtuple

from maas_cds.lib import tolerance
from maas_cds.lib.config_manager import MaasConfigManager
from maas_cds.lib.periodutils import (
    compute_total_sensing_product,
    compute_total_sensing_period,
)
from maas_cds.lib.status import evaluate_completeness_status
from maas_cds.model.enumeration import CompletenessScope

__all__ = ["CompletenessMixin", "CompletenessValues"]


LOGGER = logging.getLogger("CompletenessMixin")


# All the values of a single completeness, as stored on a document
CompletenessValues = namedtuple(
    "CompletenessValues",
    ("value", "expected", "value_adjusted", "percentage", "status"),
)


class CompletenessMixin:
    """Completeness computation methods shared by the completeness holders"""

    # Mission used to find the completeness configuration
    MISSION = None

    COMPLETENESS_TOLERANCE = {}

    @property
    def completeness_log_id(self):
        """Reference of the document, for logging purpose only

        Returns:
            str: the datatake identifier if any, the document identifier otherwise
        """
        return getattr(self, "datatake_id", None) or getattr(self.meta, "id", None)

    def completeness_configuration(self) -> list:
        """Records of the mission completeness configuration

        Returns:
            list: the configuration records, empty if the configuration of the
                mission is not loaded
        """
        configs_completeness = MaasConfigManager().get_config(
            f"MaasConfigCompleteness{self.MISSION}"
        )

        if not configs_completeness:
            LOGGER.warning(
                "[%s] - No MaasConfigCompleteness%s configuration loaded",
                self.completeness_log_id,
                self.MISSION,
            )
            return []

        return configs_completeness[0].records

    def get_expected_value_for(self, product_type: str, timeliness: str) -> int:
        """Expected sensing value of a (product_type, timeliness) couple

        Args:
            product_type (str): the product type
            timeliness (str): the timeliness

        Returns:
            int: the expected value in microseconds, 0 if it cannot be resolved
        """
        matching_items = [
            item
            for item in self.completeness_configuration()
            if item.product_type == product_type and item.timeliness == timeliness
        ]

        if not matching_items:
            LOGGER.warning(
                "[%s] - No matching config item found for product_type=%s, timeliness=%s",
                self.completeness_log_id,
                product_type,
                timeliness,
            )
            return 0

        if len(matching_items) > 1:
            LOGGER.warning(
                "[%s] - Matching two many config item found for product_type=%s, timeliness=%s",
                self.completeness_log_id,
                product_type,
                timeliness,
            )

        return self.expected_value_from_record(matching_items[0])

    def get_expected_value_for_product_type(
        self, product_type: str, preferred_timeliness: str = None
    ) -> int:
        """Expected sensing value of a product type, whatever its timeliness

        For the holders aggregating all the timeliness of a product type: the
        expected sensing of a product type does not depend on the timeliness it
        is published with, and the configuration only holds the timeliness the
        product type actually has (ex: TM_0_NAT___ only exists in AL).

        Args:
            product_type (str): the product type
            preferred_timeliness (str): the record to pick when the product type
                is configured with several timeliness

        Returns:
            int: the expected value in microseconds, 0 if it cannot be resolved
        """
        matching_items = [
            item
            for item in self.completeness_configuration()
            if item.product_type == product_type
        ]

        if not matching_items:
            LOGGER.warning(
                "[%s] - No matching config item found for product_type=%s",
                self.completeness_log_id,
                product_type,
            )
            return 0

        preferred_items = [
            item for item in matching_items if item.timeliness == preferred_timeliness
        ]

        return self.expected_value_from_record((preferred_items or matching_items)[0])

    def expected_value_from_record(self, config_record) -> int:
        """Expected sensing value held by a configuration record

        Args:
            config_record: a record of the mission completeness configuration

        Returns:
            int: the expected value in microseconds, tolerance included
        """
        return self.expected_value_from_sensing_in_minutes(
            config_record.product_type, config_record.sensing_in_minutes
        )

    def expected_value_from_sensing_in_minutes(
        self, product_type: str, sensing_in_minutes: int
    ) -> int:
        """Expected sensing value of a product type from its nominal sensing

        Args:
            product_type (str): the product type, to resolve the tolerance
            sensing_in_minutes (int): the nominal sensing duration in minutes

        Returns:
            int: the expected value in microseconds, tolerance included
        """
        expected_value = sensing_in_minutes * 60 * 1000000

        LOGGER.debug(
            "Expected value for %s are %s",
            product_type,
            expected_value,
        )

        tolerance_value = tolerance.get_completeness_tolerance(
            self.COMPLETENESS_TOLERANCE,
            # some completeness holders only know the mission at class level
            getattr(self, "mission", None) or self.MISSION,
            CompletenessScope.LOCAL,
            product_type,
        )

        return expected_value + tolerance_value

    @staticmethod
    def compute_completeness_values(
        sensing_value: int, expected_value: int
    ) -> CompletenessValues:
        """Derive all the completeness values from a sensing value and an expected one

        Args:
            sensing_value (int): the computed value (most of the time a sensing duration)
            expected_value (int): the expected value

        Returns:
            CompletenessValues: all the values to store, None if there is no
                expected value to compare the sensing value with
        """
        if not expected_value:
            return None

        # avoid value superior to expected
        value_adjusted = min(sensing_value, expected_value)

        percentage = value_adjusted / expected_value * 100

        return CompletenessValues(
            value=sensing_value,
            expected=expected_value,
            value_adjusted=value_adjusted,
            percentage=percentage,
            status=evaluate_completeness_status(percentage),
        )

    @staticmethod
    def compute_sensing_value(periods: list) -> tuple:
        """Compute the sensing value covered by a list of periods

        Overlapping periods are counted once.

        Args:
            periods (list(Period)): the periods to take into account, in any order

        Returns:
            tuple(int, Period): the sensing value in microseconds and the period
                covered by all the periods
        """
        sorted_periods = sorted(periods, key=lambda period: period.start)

        return (
            compute_total_sensing_product(sorted_periods),
            compute_total_sensing_period(sorted_periods),
        )

    def store_completeness_value(self, attr_name: str, value):
        """Store a computed completeness value on the document

        Frame method allowing implementations to store the values elsewhere.

        Args:
            attr_name (str): the attribute name
            value: the value to store
        """
        setattr(self, attr_name, value)

    def set_completeness_attributes(
        self,
        key_field: str,
        scope: CompletenessScope,
        values: CompletenessValues,
    ):
        """Store all the values of a completeness as flat attributes

        Uses the same naming convention as CdsDatatake so the dashboards have a
        single one: ``<key_field>_<scope>_<value name>``

        Args:
            key_field (str): key field value, None for a scope holding a single
                completeness (ex: the global completeness of a session)
            scope (CompletenessScope): scope value (local, global)
            values (CompletenessValues): the values to store
        """
        prefix = f"{key_field}_" if key_field else ""

        for value_name, value in values._asdict().items():
            self.store_completeness_value(f"{prefix}{scope.value}_{value_name}", value)
