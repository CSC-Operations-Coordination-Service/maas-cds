"""mixin do deletable entities"""

import typing


class DeletableMixin:
    """Deletion bookkeeping shared by CdsProduct and CdsPublication.

    Deletion flags are set from a deletion ticket and its attached product list.
    When an attachment is superseded, the flags it set must be rolled back, hence
    the mark / unmark pair.
    """

    def remove_field(self, name: str) -> bool:
        """Remove a field from the document source.

        Uses delattr rather than setting the field to None: to_bulk_action emits
        an index operation carrying the whole to_dict() as source, so a name
        absent from the document is really removed from the index, while a None
        would write an explicit null.

        Args:
            name (str): field name

        Returns:
            bool: True if the field was present
        """
        try:
            delattr(self, name)
        except (AttributeError, KeyError):
            return False

        return True

    def mark_as_deleted(self, issue, service_ids: typing.List[str]):
        """Populate attributes to reflect deletion from interfaces."""
        raise NotImplementedError()

    def unmark_as_deleted(self, issue, service_ids: typing.List[str] = None) -> bool:
        """Revert the attributes set by `issue`.

        Returns:
            bool: True if the document was modified
        """
        raise NotImplementedError()
