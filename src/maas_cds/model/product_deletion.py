"""Custom CDS model definition for product deletion"""

from maas_model import ZuluDate

from maas_cds.model import generated

__all__ = [
    "ProductDeletion",
    "CdsInterfaceProductDeletion",
    "CdsDeletionIssue",
]


def as_list(value) -> list:
    """Get a keyword field of several values as a list

    The JSON extractor of the collector stores a single match as the value
    itself, not as a list of one: a ticket with one attachment has its
    identifier as a plain string.

    Args:
        value: the field as read from the index

    Returns:
        list: the values, empty when the field is not set
    """
    if value is None:
        return []

    if isinstance(value, (str, int)):
        return [value]

    return list(value)


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
            name.strip() for name in as_list(self.attachments) if name and name.strip()
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
            for attachment_id in as_list(self.attachment_ids)
            if attachment_id is not None and str(attachment_id).strip()
        }
