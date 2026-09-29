"""Custom CDS model definition for product"""

import logging
import typing

from opensearchpy import Keyword
from maas_cds.model.anomaly_mixin import AnomalyMixin
from maas_cds.model.deletable_mixin import DeletableMixin
from maas_cds.model.dynamic_partition_mixin import DynamicPartitionMixin
from maas_cds.model.product_datatake_mixin import ProductDatatakeMixin
from maas_cds.model import generated
import maas_cds.lib.parsing_name.utils as utils


from maas_cds.lib.dateutils import get_microseconds_delta

__all__ = ["CdsProduct"]

LOGGER = logging.getLogger("CdsProduct")

# attributes written by mark_as_deleted for each deleted service
DELETION_FIELD_SUFFIXES = (
    "_is_deleted",
    "_deletion_issue",
    "_deletion_date",
    "_deletion_cause",
)


class CdsProduct(
    DeletableMixin,
    DynamicPartitionMixin,
    AnomalyMixin,
    ProductDatatakeMixin,
    generated.CdsProduct,
):
    """CdsProduct custom"""

    cams_tickets = Keyword(multi=True)

    # CAMS ticket propagated from the product's datatake (datatake.last_attached_ticket).
    # Kept distinct from ``cams_tickets`` which holds direct product-level correlations.
    datatake_cams_ticket = Keyword()

    _PARTITION_FIELDS = [
        "PRIP_.*_publication_date",  # Format to keep
        "prip_publication_date",  # current -
        "AUXIP_.*_publication_date",  # Format to keep
        "auxip_publication_date",  # current -
        "DD_.*_publication_date",  # Format to keep
        "ddip_publication_date",  # previous
        "dddas_publication_date",  # current -
        "ddcreodias_publication_date",  # previous s2 repro
        "MPCIP_.*_publication_date",  # Format to keep
        "LTA_.*_publication_date",  # Format to keep
    ]

    @property
    def publication_date(self):
        return self.prip_publication_date

    def deletion_trace(self) -> typing.Tuple[bool, typing.Optional[str]]:
        """Trace whether this product has been (or is being) deleted.

        Reads the deletion counters (``nb_dd_deleted`` / ``nb_lta_deleted``) and the
        per-interface ``*_deletion_issue`` fields populated by
        :meth:`mark_as_deleted`.

        Returns:
            tuple: ``(to_be_deleted, deletion_issue)`` where ``to_be_deleted`` is
                True when the product has been deleted from at least one interface,
                and ``deletion_issue`` is the related Jira issue key if available.
        """
        nb_deleted = (getattr(self, "nb_dd_deleted", 0) or 0) + (
            getattr(self, "nb_lta_deleted", 0) or 0
        )

        if not nb_deleted:
            return False, None

        deletion_issue = None
        for field, value in self.to_dict().items():
            if field.endswith("_deletion_issue") and value:
                deletion_issue = value
                break

        return True, deletion_issue

    def deletion_trace_by_interface(
        self,
    ) -> typing.Dict[str, typing.Tuple[bool, typing.Optional[str]]]:
        """Trace deletion split by interface type (``DD`` / ``LTA``).

        Same logic as :meth:`deletion_trace` but keeps the two interfaces apart:
        the ``nb_dd_deleted`` / ``nb_lta_deleted`` counters tell whether the product
        has been deleted from that interface, and the first matching
        ``{interface}_*_deletion_issue`` field gives the related Jira issue key.

        Returns:
            dict: ``{"DD": (deleted, issue), "LTA": (deleted, issue)}`` where
                ``deleted`` is True when the product has been deleted from that
                interface and ``issue`` is the related Jira issue key if available.
        """
        counters = {
            "DD": getattr(self, "nb_dd_deleted", 0) or 0,
            "LTA": getattr(self, "nb_lta_deleted", 0) or 0,
        }

        issues = {"DD": None, "LTA": None}
        for field, value in self.to_dict().items():
            if not value or not field.endswith("_deletion_issue"):
                continue
            for interface in issues:
                if issues[interface] is None and field.startswith(f"{interface}_"):
                    issues[interface] = value

        return {
            interface: (bool(counters[interface]), issues[interface])
            for interface in counters
        }

    @property
    def product_type_with_timeliness(self):
        if self.timeliness is None or self.product_type is None:
            return None
        return f"{self.timeliness}_{self.product_type}"

    def datatake_id_is_missing(self) -> bool:
        """Returns true if datatake id is missing or None

        Returns:
            boolean: datatake is missing
        """
        return (
            self.datatake_id is None
            or self.datatake_id == utils.DATATAKE_ID_MISSING_VALUE
        )

    def get_datatake_id(self) -> str | None:
        """Return the associate datatake id of the product

        Returns:
            str: associate cds-datatake id
        """

        return (
            f"{self.satellite_unit}-{self.datatake_id}"
            if not self.datatake_id_is_missing()
            else None
        )

    def get_compute_key(self):
        """Return the compute key of the product

        The key is unique for a calcul

        If the product is not associate with calcul the method should return None

        """

        LOGGER.warning(
            "get_compute_key - Must be surcharged in sub class specific for each mission : %s",
            self.name,
        )
        return None

    def data_for_datatake(self):
        """When a datatake is not already create from the mp
        this method allow to create the associate datatake from the product

        Returns:
            dict: data for create the datatake. Default None
        """
        return None

    def calculate_dd_timeliness(self, dd_service_name, dd_attrs):
        """Calculate from PRIP to DD timeliness"""

        dd_attr_config = dd_attrs.get(dd_service_name)

        if dd_attr_config is None:
            LOGGER.error(
                "Unknown dd service name : %s. Cannot calculate dd timeliness for product : %s.",
                dd_service_name,
                self.name,
            )
            return

        dd_publication_date = getattr(self, dd_attr_config["publication_date"])

        if self.prip_publication_date and dd_publication_date:
            setattr(
                self,
                dd_attr_config["from_prip_timeliness"],
                get_microseconds_delta(dd_publication_date, self.prip_publication_date),
            )

    def deleted_service_ids(self, interface_type: str) -> typing.List[str]:
        """Service identifiers currently flagged deleted for an interface type.

        Discovered from the document itself rather than from
        issue.deletion_interfaces, so that a ticket whose interface list was
        edited after marking can still be rolled back entirely.

        Args:
            interface_type (str): service type (currently DD or LTA)

        Returns:
            List[str]: service identifiers
        """
        prefix = f"{interface_type}_"

        suffix = "_is_deleted"

        return [
            name[len(prefix) : -len(suffix)]
            for name in self.to_dict()
            if name.startswith(prefix) and name.endswith(suffix)
        ]

    def recompute_deleted_counter(self, interface_type: str):
        """Recount the deletions of an interface type from the document itself.

        Args:
            interface_type (str): service type (currently DD or LTA)
        """
        nb_deleted_value = sum(
            1
            for name, value in self.to_dict().items()
            if name.startswith(f"{interface_type}_")
            and name.endswith("_is_deleted")
            and value
        )

        setattr(self, f"nb_{interface_type.lower()}_deleted", nb_deleted_value)

    def mark_as_deleted(self, issue: "DeletionIssue", service_ids: typing.List[str]):
        """Populate attributes to reflect deletion from interfaces.

        Args:
            issue (DeletionIssue): issue
            service_ids (List[str]): service identifiers to mark deleted
        """
        for service_id in service_ids:
            prefix = f"{issue.interface_type}_{service_id}"

            setattr(self, f"{prefix}_is_deleted", True)

            setattr(self, f"{prefix}_deletion_issue", issue.key)

            setattr(
                self,
                f"{prefix}_deletion_date",
                issue.deletion_date,
            )

            setattr(
                self,
                f"{prefix}_deletion_cause",
                issue.deletion_cause,
            )

        # Count deleted per service type
        self.recompute_deleted_counter(issue.interface_type)

    def unmark_as_deleted(
        self, issue: "DeletionIssue", service_ids: typing.List[str] = None
    ) -> bool:
        """Revert the deletion attributes set by `issue`.

        Only the services whose deletion issue is `issue` are cleared: a product
        deleted from an interface by another ticket keeps its flags.

        Args:
            issue (DeletionIssue): issue whose marks must be reverted
            service_ids (List[str], optional): services to consider. Defaults to
                every service currently flagged for the issue interface type.

        Returns:
            bool: True if the product was modified
        """
        if service_ids is None:
            service_ids = self.deleted_service_ids(issue.interface_type)

        changed = False

        for service_id in service_ids:
            prefix = f"{issue.interface_type}_{service_id}"

            if getattr(self, f"{prefix}_deletion_issue", None) != issue.key:
                continue

            for suffix in DELETION_FIELD_SUFFIXES:
                self.remove_field(f"{prefix}{suffix}")

            changed = True

        if changed:
            self.recompute_deleted_counter(issue.interface_type)

        return changed
