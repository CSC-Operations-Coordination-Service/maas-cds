"""Custom CDS model definition for product deletion"""

from maas_model import ZuluDate

from maas_cds.model import generated

__all__ = [
    "ProductDeletion",
    "CdsInterfaceProductDeletion",
    "CdsDeletionIssue",
]


class CdsInterfaceProductDeletion(generated.CdsInterfaceProductDeletion):
    """
    CdsInterfaceProductDeletion override
    """


class ProductDeletion(generated.ProductDeletion):
    """ProductDeletion custom"""

    @property
    def jira_issue(self) -> str:
        """Get the issue name contained in the report name

        Returns:
            str: The issue name
        """
        return self.reportName.split("_")[0]


class CdsDeletionIssue(generated.CdsDeletionIssue):
    """CdsDeletionIssue override"""

    @property
    def attachment_filenames(self) -> set:
        """File names currently attached to the ticket.

        Returns:
            set: unprefixed file names, empty when the ticket predates the
                collection of attachment identities
        """
        return {
            name.strip() for name in (self.attachments or []) if name and name.strip()
        }

    @property
    def current_attachment_ids(self) -> set:
        """Identifiers of the attachments currently on the ticket.

        An identifier changes at every upload, so it is the only reliable way to
        tell a superseded attachment from the current one.

        Returns:
            set: attachment identifiers, empty when the ticket predates the
                collection of attachment identities
        """
        return {
            str(attachment_id).strip()
            for attachment_id in (self.attachment_ids or [])
            if attachment_id is not None and str(attachment_id).strip()
        }
