"""Deletion consolidation"""

from typing import Dict, Generator, List, Optional, Sequence, Set, Tuple

from maas_engine.engine.rawdata import DataEngine
from maas_engine.exceptions import (
    CannotProcessMessageException,
    HandleMessageException,
)

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

    The names of a ticket are handled in chunks of ``terms_chunk_size``: the
    documents of a chunk are looked up, changed, handed to the bulk, then
    released. Besides the deletion rows of the current ticket, a run holds the
    documents of its current chunk and the keys of about ``max_documents``
    documents written for the earlier tickets of the message, see flush(): the
    products and publications of a whole ticket or message are never held at once.

    When several tickets of a message list a name, the last one marks it, as if
    every ticket was applied in turn to the same document, see later_marks().

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

        # safety nets against what a run would hold in memory at once:
        # - a chunk is expected to match about one product and one publication per
        #   service for each name. Far more means a pathological match: the message
        #   is rejected.
        # - the keys of the documents written for the earlier tickets of the
        #   message are kept, see flush(). Past max_documents, the rest of the
        #   message is left to its retry, which finds them unchanged.
        # A big ticket or message only takes longer and is never limited.
        self.max_documents = max_documents

        # names looked up, then written, at once
        self.terms_chunk_size = terms_chunk_size

        # (index, identifier) -> (document, state before this run), for the chunk
        # being planned only
        self._decisions = {}

        # documents written for an earlier ticket of the message, see flush()
        self._written = set()

        # documents left to the retry of the message, see flush()
        self._deferred = 0

        # the rest of the message is left to its retry, see reconcile_issue()
        self._truncated = False

        # names of the payload per ticket, see payload_scopes()
        self._scopes = None

        # ticket key -> (current, stale) attachment identifiers, for the later
        # tickets of the message, see later_marks()
        self._attachments = {}

    # pylint: enable=R0913

    def run(self, routing_key, payload):
        """Override to requeue the message when a part of it was left to its retry

        The exception is raised once the bulk is over, so that every other action
        is applied first.
        """
        reports = list(super().run(routing_key, payload))

        if self._truncated:
            raise HandleMessageException(
                f"{self.ENGINE_ID}: {len(self._written)} documents written for the "
                "first tickets of the message, the rest is left to its retry"
            )

        if self._deferred:
            raise HandleMessageException(
                f"{self.ENGINE_ID}: {self._deferred} documents impacted by several "
                "tickets of the message are left to its retry"
            )

        yield from reports

    def action_iterator(self):
        """Mark and unmark the documents impacted by the payload tickets"""
        self._decisions = {}

        self._written = set()

        self._deferred = 0

        self._truncated = False

        self._attachments = {}

        issues = self.resolve_issues()

        self._scopes = self.payload_scopes()

        for position, issue in enumerate(issues):
            if self._truncated:
                self.logger.info(
                    "%d tickets of the message left to its retry",
                    len(issues) - position,
                )
                return

            yield from self.reconcile_issue(
                issue, scope=self.issue_scope(issue), later=issues[position + 1 :]
            )

    def resolve_issues(self) -> List[CdsDeletionIssue]:
        """Get the tickets to reconcile, whatever the payload document class.

        Both payload shapes converge here: telling a superseded attachment from
        the current one needs the whole deletion set of a ticket, never the subset
        carried by the message.

        Returns:
            List[CdsDeletionIssue]: tickets
        """
        document_class = self.payload.document_class

        if document_class == "CdsDeletionIssue":
            # a ticket listed twice is applied once, at its last place: the first
            # one would leave its marks to the second, see later_marks()
            last = {
                issue.key: position
                for position, issue in enumerate(self.input_documents)
            }

            return [
                issue
                for position, issue in enumerate(self.input_documents)
                if last[issue.key] == position
            ]

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

    def payload_scopes(self) -> Optional[Dict[str, Set[str]]]:
        """Get the names carried by a payload of deletions, per ticket

        Returns:
            dict: ticket key -> effective product names, None for a payload of
                tickets, that reconciles every name
        """
        if self.payload.document_class != "CdsInterfaceProductDeletion":
            return None

        scopes = {}

        for deletion in self.input_documents:
            if deletion.jira_issue and deletion.effective_product_name:
                scopes.setdefault(deletion.jira_issue, set()).add(
                    deletion.effective_product_name
                )

        return scopes

    def issue_scope(self, issue: CdsDeletionIssue) -> Optional[Set[str]]:
        """Get the names of the payload for a ticket

        Args:
            issue (CdsDeletionIssue): the deletion ticket

        Returns:
            set: effective product names, None for all
        """
        return None if self._scopes is None else self._scopes.get(issue.key, set())

    def marked_names(
        self,
        deletions: List,
        current_ids: set,
        stale_ids: set,
        scope: Optional[Set[str]],
    ) -> Set[str]:
        """Get the names a ticket marks

        Args:
            deletions (List[CdsInterfaceProductDeletion]): deletions of the ticket
            current_ids (set): current attachment identifiers
            stale_ids (set): stale attachment identifiers
            scope (set): names of the payload for this ticket, None for all

        Returns:
            set: effective product names
        """
        # deletions with no attachment identity predate the feature: they are
        # grandfathered in with the current set
        names = self.deletion_names(deletions, current_ids, keep_legacy=True)

        if scope is not None and not stale_ids:
            # nothing to roll back: a message of deletions only marks its own
            # names, the other rows of the ticket come with their own message.
            # A stale attachment needs the whole ticket, once: its rows are
            # deleted afterwards
            names &= scope

        return names

    def reconcile_issue(
        self,
        issue: CdsDeletionIssue,
        scope: Optional[Set[str]],
        later: Sequence[CdsDeletionIssue] = (),
    ) -> Generator:
        """Decide the fate of every document impacted by a ticket, chunk by chunk

        Args:
            issue (CdsDeletionIssue): the deletion ticket
            scope (set): names of the payload for this ticket, None for all
            later (list): tickets after this one in the message

        Yields:
            dict: bulk actions, documents first, then stale deletions
        """
        deletions = self.load_deletions(issue)

        if not deletions:
            self.logger.info("No deletion for issue %s: skipping", issue.key)
            return

        current_ids, stale_ids = self.split_attachments(deletions, issue)

        current_names = self.marked_names(deletions, current_ids, stale_ids, scope)

        # only the products dropped from the ticket need a rollback
        stale_names = (
            self.deletion_names(deletions, stale_ids, keep_legacy=False) - current_names
        )

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

        # a document written for the last ticket is not met again
        remember = bool(later)

        deferred = self._deferred

        written = 0

        for chunk in chunked(
            sorted(stale_names | current_names), self.terms_chunk_size
        ):
            if remember and self._written and len(self._written) >= self.max_documents:
                # the keys kept for the next tickets are bounded too. The retry
                # finds every document written so far unchanged, so it gets further
                self._truncated = True

                self.logger.info(
                    "[%s] %d documents written, %d for the message: the rest of it "
                    "is left to its retry, with the stale deletions",
                    issue.key,
                    written,
                    len(self._written),
                )
                return

            self.plan_chunk(
                issue,
                chunk,
                current_names,
                rollback=bool(stale_names),
                later=later,
            )

            for action in self.flush(issue, remember):
                written += 1
                yield action

        if self._deferred > deferred:
            # a rollback left to the retry still needs the rows that list it
            self.logger.info(
                "[%s] %d documents written, %d left to the retry with the stale "
                "deletions",
                issue.key,
                written,
                self._deferred - deferred,
            )
            return

        dropped = 0

        for deletion in deletions:
            if getattr(deletion, "attachment_id", None) in stale_ids:
                dropped += 1
                yield deletion.to_bulk_action("delete")

        self.logger.info(
            "[%s] %d documents written, %d stale deletions dropped",
            issue.key,
            written,
            dropped,
        )

    # pylint: disable=R0913
    def plan_chunk(
        self,
        issue: CdsDeletionIssue,
        names: List[str],
        current_names: Set[str],
        rollback: bool,
        later: Sequence[CdsDeletionIssue] = (),
    ):
        """Look up the documents of a chunk of names and change them in memory

        Args:
            issue (CdsDeletionIssue): the deletion ticket
            names (List[str]): names of the chunk
            current_names (set): names the current attachments list
            rollback (bool): the ticket has products to roll back
            later (list): tickets after this one in the message
        """
        self._decisions = {}

        taken = self.later_marks(
            later,
            [name for name in names if name in current_names],
            issue.interface_type,
        )

        for deletable in self.deletable_iterator(
            names,
            issue.interface_type,
            issue.deletion_interfaces,
        ):
            document = self.register(deletable)

            if rollback:
                # revert first so that a product listed by both a superseded and a
                # current attachment ends up in the state the current one dictates
                document.unmark_as_deleted(issue)

            name = self.matched_name(document, issue.interface_type)

            if name not in current_names:
                continue

            # a publication is scoped to the single service it was looked up for
            services = (
                [document.service_id]
                if isinstance(document, CdsPublication)
                else issue.deletion_interfaces
            )

            # the services a later ticket of the message marks are left to it
            services = [
                service_id
                for service_id in services
                if service_id not in taken.get(name, ())
            ]

            if services:
                document.mark_as_deleted(issue, services)

    # pylint: enable=R0913

    def later_marks(
        self,
        later: Sequence[CdsDeletionIssue],
        names: List[str],
        interface_type: str,
    ) -> Dict[str, Set[str]]:
        """Get the services on which a later ticket of the message marks a name

        The marks of a service are single-valued, on a product as on a
        publication: the last ticket of the message that lists a name marks it,
        as when every ticket was applied in turn to the same document. An earlier
        ticket marking it too would be overwritten by the later one, and as a
        document written twice is left to the retry, see flush(), where the
        earlier ticket would overwrite it back, the message would be requeued
        forever from three tickets on.

        A later ticket that only rolls the name back does not count: an earlier
        ticket that still lists the name marks it.

        Only the rows of the later tickets that list the names are read, so that a
        message of many tickets holds no more than a chunk of them.

        Args:
            later (list): tickets after the current one in the message
            names (List[str]): names the current ticket marks
            interface_type (str): service type of the current ticket

        Returns:
            dict: name -> services marked by a later ticket
        """
        # a ticket of the other interface type sets other fields
        tickets = {
            issue.key: issue
            for issue in later
            if issue.interface_type == interface_type
        }

        if not tickets or not names:
            return {}

        listings = {}

        for deletion in self.load_listings(sorted(tickets), interface_type, names):
            listings.setdefault(deletion.jira_issue, []).append(deletion)

        taken = {}

        for key, deletions in listings.items():
            issue = tickets[key]

            current_ids, stale_ids = self.ticket_attachments(issue)

            for name in self.marked_names(
                deletions, current_ids, stale_ids, self.issue_scope(issue)
            ):
                taken.setdefault(name, set()).update(issue.deletion_interfaces)

        return taken

    def ticket_attachments(self, issue: CdsDeletionIssue) -> Tuple[set, set]:
        """Split the attachments of a later ticket of the message, once per run

        Args:
            issue (CdsDeletionIssue): the deletion ticket

        Returns:
            tuple: (current attachment identifiers, stale attachment identifiers)
        """
        if issue.key not in self._attachments:
            self._attachments[issue.key] = self.split_attachments(
                self.load_deletions(issue), issue
            )

        return self._attachments[issue.key]

    def register(self, document):
        """Get the canonical instance for a document of the chunk.

        A document listed by both a superseded and a current attachment is fetched
        once and mutated once: bulk actions carry an optimistic locking clause, so
        a second action on the same document would systematically conflict. A
        document matches a single name, so it belongs to a single chunk.

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
                # rejected: a requeue would fail the same way forever
                raise CannotProcessMessageException(
                    f"{self.ENGINE_ID}: more than {self.max_documents} documents "
                    f"match {self.terms_chunk_size} names at most: raise "
                    "max_documents in the engine configuration to process it"
                )

            decision = self._decisions[key] = (document, document.to_dict())

        return decision[0]

    def flush(self, issue: CdsDeletionIssue, remember: bool) -> Generator:
        """Yield at most one bulk action per changed document of the chunk, then
        release the chunk

        A document already written for an earlier ticket of the message is left to
        the retry of the message. It was fetched again, most likely before the bulk
        applied the first write, so a second write would conflict, and bulk chunks
        run in parallel: either write could be the one lost, with the stale
        deletions of its ticket applied anyway. The retry reads it with the first
        write applied, which it then finds unchanged: marking and unmarking give
        the same result when repeated.

        Args:
            issue (CdsDeletionIssue): the deletion ticket
            remember (bool): record the documents written, for the next tickets

        Yields:
            dict: bulk actions
        """
        decisions, self._decisions = self._decisions, {}

        for key, (document, initial_dict) in decisions.items():
            # a plain comparison is required: the union operator used to keep every
            # key of the initial state, which made the check blind to the removal
            # of a field, hence to a rollback
            if document.to_dict() == initial_dict:
                continue

            if key in self._written:
                self.logger.info(
                    "[%s] %s already written for another ticket of the message: "
                    "left to the retry",
                    issue.key,
                    key,
                )
                self._deferred += 1
                continue

            if remember:
                # keys only: the documents themselves are released with the chunk
                self._written.add(key)

            if self.send_reports:
                # the report map would otherwise hold every document of the run
                self.report(document)

            yield document.to_bulk_action()

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
