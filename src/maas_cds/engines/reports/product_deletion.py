"""Deletion consolidation"""

from typing import Generator, List

from maas_engine.engine.rawdata import DataEngine
from maas_engine.exceptions import HandleMessageException

from maas_cds.engines.reports.deletion_reconciliation import DeletionReconciliationMixin
from maas_cds.engines.reports.mission_mixin import MissionMixinEngine
from maas_cds.lib.deletion_grouping import DEFAULT_INTERFACE_DICT, chunked
from maas_cds.model import (
    CdsDeletionIssue,
    CdsProduct,
    CdsPublication,
)


class DeletionConsolidatorEngine(
    DeletionReconciliationMixin, MissionMixinEngine, DataEngine
):
    """Consolidate CdsInterfaceProductDeletion / CdsDeletionIssue

    Marks the products and publications listed by the current attachments of a
    deletion ticket as deleted, and rolls back the ones listed only by a
    superseded attachment.

    Inherits from :class:`MissionMixinEngine` so that the products and
    publications touched are reported on mission-dedicated routing keys
    (``update.cds-product-s2``, ``update.cds-publication-s2``, ...) with their
    mission-specific document class. This is what triggers the completeness
    recompute (COMPUTE_COMPLETENESS / COMPUTE_COMPLETENESS_V2) of the datatake
    the products belong to.
    """

    ENGINE_ID = "CONSOLIDATE_DELETION"

    # pylint: disable=R0913
    # legit: constructors have many argument for configuration
    def __init__(
        self,
        args=None,
        send_reports=False,
        interface_dict=None,
        max_documents=50000,
        terms_chunk_size=1024,
    ):
        super().__init__(args, send_reports=send_reports)

        self.interface_dict = interface_dict or DEFAULT_INTERFACE_DICT

        # guard against loading a whole index in memory on a pathological ticket
        self.max_documents = max_documents

        self.terms_chunk_size = terms_chunk_size

        # (index, identifier) -> (document, state before this run)
        self._decisions = {}

        self._stale_actions = []

    # pylint: enable=R0913

    def action_iterator(self):
        """Mark and unmark the documents impacted by the payload tickets"""
        self._decisions = {}

        self._stale_actions = []

        for issue in self.resolve_issues():
            self.plan_issue(issue)

        self.logger.info(
            "Deletion run touched %d documents, %d stale deletions dropped",
            len(self._decisions),
            len(self._stale_actions),
        )

        yield from self.emit()

    def resolve_issues(self) -> List[CdsDeletionIssue]:
        """Get the tickets to reconcile, whatever the payload document class.

        Both payload shapes converge here: reconciliation always needs the whole
        deletion set of a ticket, never the subset carried by the message.

        Returns:
            List[CdsDeletionIssue]: tickets
        """
        document_class = self.payload.document_class

        if document_class == "CdsDeletionIssue":
            return self.input_documents

        if document_class == "CdsInterfaceProductDeletion":
            keys = sorted(
                {
                    deletion.jira_issue
                    for deletion in self.input_documents
                    if deletion.jira_issue
                }
            )

            issues = [issue for issue in CdsDeletionIssue.mget_by_ids(keys) if issue]

            if missing := set(keys) - {issue.key for issue in issues}:
                self.logger.warning(
                    "Issues %s do not exist (yet): skipping deletion", sorted(missing)
                )

            return issues

        raise TypeError(
            f"Unexpected input document class for deletion: {document_class}"
        )

    def plan_issue(self, issue: CdsDeletionIssue):
        """Decide the fate of every document impacted by a ticket

        Args:
            issue (CdsDeletionIssue): the deletion ticket
        """
        deletions = self.load_deletions(issue)

        if not deletions:
            self.logger.info("No deletion for issue %s: skipping", issue.key)
            return

        current_ids, stale_ids = self.split_attachments(deletions, issue)

        # deletions with no attachment identity predate the feature: they are
        # grandfathered in with the current set
        current_names = self.deletion_names(deletions, current_ids, keep_legacy=True)

        stale_names = self.deletion_names(deletions, stale_ids, keep_legacy=False)

        # only the products dropped from the ticket need a rollback
        stale_names -= current_names

        if not stale_ids:
            self.logger.debug(
                "[%s] %d products from %d attachment(s), nothing superseded",
                issue.key,
                len(current_names),
                len(current_ids),
            )
        else:
            self.logger.info(
                "[%s] %d products kept from %d attachment(s), "
                "%d products rolled back from %d superseded attachment(s) %s",
                issue.key,
                len(current_names),
                len(current_ids),
                len(stale_names),
                len(stale_ids),
                sorted(stale_ids),
            )

        if stale_ids and not current_ids:
            # highest blast radius of the whole reconciliation: make it visible
            self.logger.warning(
                "[%s] every attachment is superseded or detached: "
                "rolling back the whole ticket",
                issue.key,
            )

        for deletable in self.deletable_iterator(
            sorted(stale_names | current_names),
            issue.interface_type,
            issue.deletion_interfaces,
        ):
            document = self.register(deletable)

            if stale_names:
                # revert first so that a product listed by both a superseded and a
                # current attachment ends up in the state the current one dictates
                document.unmark_as_deleted(issue)

            if self.matched_name(document, issue.interface_type) in current_names:
                document.mark_as_deleted(issue, issue.deletion_interfaces)

        for deletion in deletions:
            if getattr(deletion, "attachment_id", None) in stale_ids:
                self._stale_actions.append(deletion.to_bulk_action("delete"))

    def register(self, document):
        """Get the canonical instance for a document of this run.

        A document listed by both a superseded and a current attachment is fetched
        once and mutated once: bulk actions carry an optimistic locking clause, so
        a second action on the same document would systematically conflict.

        Args:
            document (MAASDocument): a freshly fetched document

        Returns:
            MAASDocument: the instance to mutate, possibly already mutated
        """
        # the bulk action targets meta.index, so that is what identifies a document
        key = (document.meta.index, document.meta.id)

        decision = self._decisions.get(key)

        if decision is None:
            if len(self._decisions) >= self.max_documents:
                raise HandleMessageException(
                    f"{self.ENGINE_ID}: more than {self.max_documents} documents "
                    "impacted in a single run"
                )

            decision = self._decisions[key] = (document, document.to_dict())

        return decision[0]

    def report(self, document):
        """Override to index the report map on the index the document comes from.

        The base implementation recomputes partition_index_name, which for a
        product resolves a publication date and raises when none is set. Every
        document here comes from a search, and the bulk action targets its
        meta.index, which is also what the bulk result reports back: keying on
        anything else silently drops the report.

        Args:
            document (MAASDocument): document to report
        """
        index_name = getattr(document.meta, "index", None)

        if index_name is None:
            super().report(document)
            return

        # pylint: disable=protected-access
        self._index_id_document_map[(index_name, document.meta.id)] = document
        # pylint: enable=protected-access

    def emit(self) -> Generator:
        """Yield at most one bulk action per document, stale deletions last

        Yields:
            dict: bulk actions
        """
        for document, initial_dict in self._decisions.values():
            # a plain comparison is required: the union operator used to keep every
            # key of the initial state, which made the check blind to the removal
            # of a field, hence to a rollback
            if document.to_dict() == initial_dict:
                continue

            self.report(document)

            yield document.to_bulk_action()

        yield from self._stale_actions

    @staticmethod
    def matched_name(document, interface_type: str) -> str:
        """Get the name the deletable_iterator query matched on

        Args:
            document (MAASDocument): product or publication
            interface_type (str): service type (currently DD or LTA)

        Returns:
            str: the matched product name
        """
        if interface_type == "DD" and isinstance(document, CdsProduct):
            return document.dddas_name

        return document.name

    def deletable_iterator(
        self, products_names: List[str], service_type: str, service_ids: List[str]
    ) -> Generator:
        """Get a generator of deletable documents (products and publications) based
        on criteria arguments

        Args:
            products_names (List[str]): products to look up
            service_type (str): service type (currently DD or LTA)
            service_ids (List[str]): service identifiers

        Yields:
            MAASDocument: products then publications, chunk by chunk
        """
        self.logger.debug(
            "Get %d deletables for %s / %s",
            len(products_names),
            service_type,
            service_ids,
        )

        if not products_names:
            # avoid building terms queries with empty values, that opensearch
            # rejects with "No value specified for terms query"
            return

        # This will for CDSE only need to remap this with dd_attrs
        name_field = "dddas_name" if service_type == "DD" else "name"

        for chunk in chunked(products_names, self.terms_chunk_size):
            yield from (
                CdsProduct.search()
                .filter("terms", **{name_field: chunk})
                .params(
                    version=True,
                    seq_no_primary_term=True,
                    ignore=404,
                    size=len(chunk),
                )
                .scan()
            )

            yield from (
                CdsPublication.search()
                .filter(
                    "term",
                    service_type=service_type,
                )
                .filter(
                    "terms",
                    service_id=service_ids,
                )
                .filter(
                    "terms",
                    name=chunk,
                )
                .params(
                    version=True,
                    seq_no_primary_term=True,
                    ignore=404,
                    size=len(chunk),
                )
                .scan()
            )
