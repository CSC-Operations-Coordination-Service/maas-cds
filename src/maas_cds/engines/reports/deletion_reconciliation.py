"""Reconciliation of the deletions of a ticket with its current attachments"""

from typing import List, Tuple

from maas_cds.lib.deletion_grouping import (
    DEFAULT_INTERFACE_DICT,
    AttachmentInfo,
    build_groups,
    resolve_service_id,
)
from maas_cds.model import CdsDeletionIssue, CdsInterfaceProductDeletion


# fields needed to reconcile: everything else is left in the index
DELETION_SOURCE_FIELDS = [
    "attachment_created",
    "attachment_id",
    "effective_product_name",
    "reportName",
]


class DeletionReconciliationMixin:
    """Tell which deletions of a ticket come from a superseded attachment.

    A deletion ticket carries one attachment per service, listing the products to
    delete. An attachment is superseded when a newer file is attached, when the
    same file is re-uploaded with a product line removed, or when it is detached
    from the ticket. The deletions it produced must then be rolled back.

    Superseding is detected through the identity of the JIRA attachment each row
    was extracted from, which changes at every upload even when the file name and
    the file content are identical.
    """

    interface_dict = DEFAULT_INTERFACE_DICT

    def load_deletions(self, issue: CdsDeletionIssue) -> List:
        """Get every deletion of a ticket.

        The whole set is needed, never the subset carried by the payload: telling
        a superseded attachment from the current one requires seeing them all.

        Args:
            issue (CdsDeletionIssue): the deletion ticket

        Returns:
            List[CdsInterfaceProductDeletion]: deletions of the ticket
        """
        search = (
            CdsInterfaceProductDeletion.search()
            .filter("term", jira_issue=issue.key)
            # attachments of the other interface type belong to another ticket flow
            .filter("term", interface_type=issue.interface_type)
            .source(DELETION_SOURCE_FIELDS)
        )

        return list(search.params(size=2000, ignore=404).scan())

    def split_attachments(
        self, deletions: List, issue: CdsDeletionIssue
    ) -> Tuple[set, set]:
        """Split the attachments that produced the deletions in current and stale

        Args:
            deletions (List[CdsInterfaceProductDeletion]): deletions of the ticket
            issue (CdsDeletionIssue): the deletion ticket

        Returns:
            tuple: (current attachment identifiers, stale attachment identifiers)
        """
        attachments = {}

        for deletion in deletions:
            attachment_id = getattr(deletion, "attachment_id", None)

            if attachment_id is None:
                # deletion collected before attachment identities were recorded:
                # its provenance is unknown, so it is never declared stale
                continue

            created = getattr(deletion, "attachment_created", None)

            attachment = attachments.get(attachment_id)

            if attachment is None:
                attachment = attachments[attachment_id] = AttachmentInfo(
                    attachment_id,
                    created=created,
                    report_name=getattr(deletion, "reportName", "") or "",
                )
            elif created is not None and (
                attachment.created is None or created > attachment.created
            ):
                attachment.created = created

        if len(attachments) < 2 and not issue.current_attachment_ids:
            # a single known attachment and no ground truth on the ticket: nothing
            # can be superseded
            return set(attachments), set()

        for attachment in attachments.values():
            attachment.service_id = resolve_service_id(
                attachment.report_name, issue, self.interface_dict, self.logger
            )

        current, stale = build_groups(list(attachments.values()), issue)

        return set(current), set(stale)

    @staticmethod
    def deletion_names(deletions: List, attachment_ids: set, keep_legacy: bool) -> set:
        """Collect the product names of a subset of attachments

        Args:
            deletions (List[CdsInterfaceProductDeletion]): deletions of the ticket
            attachment_ids (set): attachments to collect
            keep_legacy (bool): include the deletions with no attachment identity

        Returns:
            set: effective product names
        """
        names = set()

        for deletion in deletions:
            attachment_id = getattr(deletion, "attachment_id", None)

            if attachment_id is None:
                if not keep_legacy:
                    continue
            elif attachment_id not in attachment_ids:
                continue

            # deletions not tracked yet have no effective product name, and a null
            # in a terms query is rejected by opensearch
            if deletion.effective_product_name:
                names.add(deletion.effective_product_name)

        return names
