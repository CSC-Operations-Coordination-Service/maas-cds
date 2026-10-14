"""Tests of the deletion consolidation and of its reconciliation with attachments"""

import copy
from unittest.mock import patch

import pytest
from maas_engine.exceptions import CannotProcessMessageException, HandleMessageException
from maas_model import MAASMessage

from maas_cds.engines.reports.product_deletion import DeletionConsolidatorEngine
from maas_cds.model import CdsDeletionIssue, CdsInterfaceProductDeletion, CdsPublication

from data.deletion_data_test import (
    make_deletion,
    make_issue,
    make_product,
    make_publication,
    zulu,
)


def build_engine(deletions, deletables):
    """An engine whose two database accesses are replaced by fixtures"""
    engine = DeletionConsolidatorEngine()

    engine.load_deletions = lambda issue: deletions

    engine.deletable_iterator = lambda names, service_type, service_ids: [
        deletable
        for deletable in deletables
        if engine.matched_name(deletable, service_type) in set(names)
    ]

    return engine


def actions_of(engine, issue):
    """The bulk actions of a CdsDeletionIssue message, as a lazy iterator"""
    engine.payload = MAASMessage(document_class="CdsDeletionIssue")
    engine.input_documents = [issue]

    return engine.action_iterator()


def run(engine, issue):
    return list(actions_of(engine, issue))


def run_deletions(engine, deletions, issues):
    """Run a CdsInterfaceProductDeletion message carrying `deletions`"""
    engine.payload = MAASMessage(document_class="CdsInterfaceProductDeletion")
    engine.input_documents = deletions

    with patch(
        "maas_cds.engines.reports.product_deletion.CdsDeletionIssue.mget_by_ids",
        return_value=issues,
    ):
        return list(engine.action_iterator())


def spy_lookups(engine):
    """Record the names of every product and publication lookup"""
    lookups = []

    lookup = engine.deletable_iterator

    def spy(names, service_type, service_ids):
        lookups.append(list(names))
        return lookup(names, service_type, service_ids)

    engine.deletable_iterator = spy

    return lookups


class FakeIndex:
    """Serves fresh copies of its documents, as a search does, and applies bulk
    actions with their optimistic locking"""

    def __init__(self, documents):
        self.documents = {
            (document.meta.index, document.meta.id): document for document in documents
        }

        self.conflicts = 0

    def plug(self, engine):
        """Replace the two database lookups of an engine"""
        engine.load_deletions = lambda issue: [
            copy.deepcopy(document)
            for document in self.documents.values()
            if isinstance(document, CdsInterfaceProductDeletion)
            and document.jira_issue == issue.key
        ]

        def deletable_iterator(names, service_type, service_ids):
            return [
                copy.deepcopy(document)
                for document in self.documents.values()
                if not isinstance(document, CdsInterfaceProductDeletion)
                and engine.matched_name(document, service_type) in set(names)
                and (
                    not isinstance(document, CdsPublication)
                    or document.service_id in service_ids
                )
            ]

        engine.deletable_iterator = deletable_iterator

        return engine

    def bulk(self, _client, actions, **_kwargs):
        """Stand-in for parallel_bulk on a message smaller than one bulk chunk:
        every action is planned before the first one is applied"""
        for action in list(actions):
            yield self.apply(action)

    def apply(self, action):
        key = (action["_index"], action["_id"])

        details = {"_index": key[0], "_id": key[1]}

        if action["_op_type"] == "delete":
            del self.documents[key]

            return True, {"delete": details | {"status": 200, "result": "deleted"}}

        current = self.documents[key]

        if action["if_seq_no"] != current.meta.seq_no:
            self.conflicts += 1

            return False, {"index": details | {"status": 409}}

        updated = current.__class__(**action["_source"])
        updated.meta.index, updated.meta.id = key
        updated.meta.seq_no = current.meta.seq_no + 1
        updated.meta.primary_term = current.meta.primary_term

        self.documents[key] = updated

        return True, {"index": details | {"status": 200, "result": "updated"}}

    def run(self, issues):
        """Process a CdsDeletionIssue message with a new engine, as the consumer does"""
        engine = self.plug(DeletionConsolidatorEngine())

        payload = MAASMessage(document_class="CdsDeletionIssue")
        engine.payload = payload
        engine.input_documents = issues

        with patch("maas_engine.engine.data.parallel_bulk", self.bulk), patch(
            "maas_engine.engine.data.db_connections"
        ), patch.object(engine, "get_input_model", return_value=CdsDeletionIssue):
            return list(engine.run("update.cds-deletion-issue", payload))


def assert_one_action_per_document(actions):
    """Two actions on one document would conflict on the optimistic locking"""
    keys = [(action["_index"], action["_id"]) for action in actions]

    assert len(keys) == len(set(keys)), f"duplicated bulk actions: {keys}"


def test_nothing_superseded_marks_as_before():
    """The nominal path is unchanged when a ticket has a single attachment"""
    issue = make_issue(attachment_ids=["10"])

    deletions = [
        make_deletion("PRODUCT_A", attachment_id="10", created=zulu(1)),
        make_deletion("PRODUCT_B", attachment_id="10", created=zulu(1)),
    ]

    products = [make_product("PRODUCT_A"), make_product("PRODUCT_B")]

    actions = run(build_engine(deletions, products), issue)

    assert_one_action_per_document(actions)
    assert len(actions) == 2

    for action in actions:
        assert action["_source"]["LTA_Werum_is_deleted"] is True
        assert action["_source"]["LTA_Werum_deletion_issue"] == "OMCS-1234"
        assert action["_source"]["nb_lta_deleted"] == 1


def test_superseded_attachment_rolls_back_dropped_products():
    """A new version of the list is attached, dropping A and B, adding C"""
    issue = make_issue(attachment_ids=["11"])

    deletions = [
        make_deletion("PRODUCT_A", attachment_id="10", created=zulu(1)),
        make_deletion("PRODUCT_B", attachment_id="10", created=zulu(1)),
        make_deletion("PRODUCT_C", attachment_id="11", created=zulu(2)),
    ]

    products = [
        make_product("PRODUCT_A"),
        make_product("PRODUCT_B"),
        make_product("PRODUCT_C"),
    ]

    # A and B were marked by the superseded attachment
    products[0].mark_as_deleted(issue, ["Werum"])
    products[1].mark_as_deleted(issue, ["Werum"])

    actions = run(build_engine(deletions, products), issue)

    assert_one_action_per_document(actions)

    by_id = {action["_id"]: action for action in actions}

    for name in ("PRODUCT_A", "PRODUCT_B"):
        source = by_id[name]["_source"]
        assert "LTA_Werum_is_deleted" not in source
        assert "LTA_Werum_deletion_issue" not in source
        assert source["nb_lta_deleted"] == 0

    assert by_id["PRODUCT_C"]["_source"]["LTA_Werum_is_deleted"] is True

    # the deletions of the superseded attachment are dropped
    deleted = [action for action in actions if action.get("_op_type") == "delete"]
    assert sorted(action["_id"] for action in deleted) == [
        "PRODUCT_A-10",
        "PRODUCT_B-10",
    ]


def test_product_listed_by_both_attachments_yields_no_action():
    """The conflict case: B is in the superseded list and in the current one.

    It must neither be emitted twice, which would conflict on the optimistic
    locking, nor be rolled back.
    """
    issue = make_issue(attachment_ids=["11"])

    deletions = [
        make_deletion("PRODUCT_A", attachment_id="10", created=zulu(1)),
        make_deletion("PRODUCT_B", attachment_id="10", created=zulu(1)),
        make_deletion("PRODUCT_B", attachment_id="11", created=zulu(2)),
        make_deletion("PRODUCT_C", attachment_id="11", created=zulu(2)),
    ]

    product_b = make_product("PRODUCT_B")
    product_b.mark_as_deleted(issue, ["Werum"])

    product_a = make_product("PRODUCT_A")
    product_a.mark_as_deleted(issue, ["Werum"])

    products = [product_a, product_b, make_product("PRODUCT_C")]

    actions = run(build_engine(deletions, products), issue)

    assert_one_action_per_document(actions)

    updated = [action for action in actions if action.get("_op_type") != "delete"]

    # B is unmarked then marked back identically: no write at all
    assert sorted(action["_id"] for action in updated) == ["PRODUCT_A", "PRODUCT_C"]


def test_emptied_attachment_rolls_everything_back():
    """A re-upload that dropped every product still emits the rollback.

    This is the case the previous dirty check was blind to: it kept every key of
    the initial state, so a pure field removal compared equal.
    """
    issue = make_issue(attachment_ids=["11"])

    deletions = [
        make_deletion("PRODUCT_A", attachment_id="10", created=zulu(1)),
        make_deletion("PRODUCT_B", attachment_id="11", created=zulu(2)),
    ]

    product_a = make_product("PRODUCT_A")
    product_a.mark_as_deleted(issue, ["Werum"])

    actions = run(build_engine(deletions, [product_a, make_product("PRODUCT_B")]), issue)

    rollback = next(action for action in actions if action["_id"] == "PRODUCT_A")

    assert "LTA_Werum_is_deleted" not in rollback["_source"]
    assert rollback["_source"]["nb_lta_deleted"] == 0


def test_detached_attachment_promotes_the_previous_one():
    issue = make_issue(attachment_ids=["10"])

    deletions = [
        make_deletion("PRODUCT_A", attachment_id="10", created=zulu(1)),
        make_deletion("PRODUCT_B", attachment_id="11", created=zulu(2)),
    ]

    product_b = make_product("PRODUCT_B")
    product_b.mark_as_deleted(issue, ["Werum"])

    actions = run(build_engine(deletions, [make_product("PRODUCT_A"), product_b]), issue)

    by_id = {action["_id"]: action for action in actions}

    assert by_id["PRODUCT_A"]["_source"]["LTA_Werum_is_deleted"] is True
    assert "LTA_Werum_is_deleted" not in by_id["PRODUCT_B"]["_source"]


def test_legacy_deletions_are_never_rolled_back():
    """Deletions collected before attachment identities are grandfathered in"""
    issue = make_issue(attachment_ids=["11"])

    deletions = [
        make_deletion("PRODUCT_LEGACY"),
        make_deletion("PRODUCT_B", attachment_id="11", created=zulu(2)),
    ]

    legacy = make_product("PRODUCT_LEGACY")
    legacy.mark_as_deleted(issue, ["Werum"])

    actions = run(build_engine(deletions, [legacy, make_product("PRODUCT_B")]), issue)

    by_id = {action["_id"]: action for action in actions}

    # unchanged, so not even emitted
    assert "PRODUCT_LEGACY" not in by_id
    assert legacy.LTA_Werum_is_deleted is True


def test_publications_are_rolled_back_too():
    issue = make_issue(attachment_ids=["11"])

    deletions = [
        make_deletion("PRODUCT_A", attachment_id="10", created=zulu(1)),
        make_deletion("PRODUCT_B", attachment_id="11", created=zulu(2)),
    ]

    publication_a = make_publication("PRODUCT_A")
    publication_a.mark_as_deleted(issue)

    deletables = [make_publication("PRODUCT_B"), publication_a]

    actions = run(build_engine(deletions, deletables), issue)

    by_id = {action["_id"]: action for action in actions}

    assert "deletion_issue" not in by_id["Werum-PRODUCT_A"]["_source"]
    assert by_id["Werum-PRODUCT_B"]["_source"]["deletion_issue"] == "OMCS-1234"


def test_payload_of_deletions_reconciles_the_whole_ticket_when_an_attachment_is_stale():
    """A message carrying a subset of the rows still sees the full ticket when an
    attachment is superseded: the rollback needs every row"""
    issue = make_issue(attachment_ids=["11"])

    deletions = [
        make_deletion("PRODUCT_A", attachment_id="10", created=zulu(1)),
        make_deletion("PRODUCT_B", attachment_id="11", created=zulu(2)),
    ]

    product_a = make_product("PRODUCT_A")
    product_a.mark_as_deleted(issue, ["Werum"])

    engine = build_engine(deletions, [product_a, make_product("PRODUCT_B")])
    engine.payload = MAASMessage(document_class="CdsInterfaceProductDeletion")
    # only the surviving row is in the payload
    engine.input_documents = [deletions[1]]

    lookups = spy_lookups(engine)

    with patch(
        "maas_cds.engines.reports.product_deletion.CdsDeletionIssue.mget_by_ids",
        return_value=[issue],
    ) as mock_mget:
        actions = list(engine.action_iterator())

    mock_mget.assert_called_once_with(["OMCS-1234"])

    assert lookups == [["PRODUCT_A", "PRODUCT_B"]]

    # the row absent from the payload was still reconciled
    rollback = next(action for action in actions if action["_id"] == "PRODUCT_A")
    assert "LTA_Werum_is_deleted" not in rollback["_source"]


def test_payload_of_deletions_only_looks_up_its_own_names():
    """Nothing to roll back: a message of rows only marks its own names, the other
    rows of the ticket come with their own message"""
    issue = make_issue(attachment_ids=["10"])

    names = ["PRODUCT_A", "PRODUCT_B", "PRODUCT_C"]

    deletions = [
        make_deletion(name, attachment_id="10", created=zulu(1)) for name in names
    ]

    engine = build_engine(deletions, [make_product(name) for name in names])

    lookups = spy_lookups(engine)

    actions = run_deletions(engine, [deletions[1]], [issue])

    assert lookups == [["PRODUCT_B"]]

    assert [action["_id"] for action in actions] == ["PRODUCT_B"]


def test_payload_of_deletions_scopes_each_ticket_to_its_own_rows():
    first = make_issue(key="OMCS-1")
    second = make_issue(key="OMCS-2", deletion_interfaces=["Exprivia"])

    rows = {
        "OMCS-1": [
            make_deletion("PRODUCT_A", jira_issue="OMCS-1"),
            make_deletion("PRODUCT_B", jira_issue="OMCS-1"),
        ],
        "OMCS-2": [
            make_deletion("PRODUCT_C", jira_issue="OMCS-2"),
            make_deletion("PRODUCT_D", jira_issue="OMCS-2"),
        ],
    }

    engine = build_engine([], [make_product(f"PRODUCT_{name}") for name in "ABCD"])
    engine.load_deletions = lambda issue: rows[issue.key]

    lookups = spy_lookups(engine)

    run_deletions(engine, [rows["OMCS-1"][0], rows["OMCS-2"][1]], [first, second])

    assert lookups == [["PRODUCT_A"], ["PRODUCT_D"]]


def test_payload_of_issues_looks_up_the_whole_ticket():
    issue = make_issue(attachment_ids=["10"])

    names = ["PRODUCT_A", "PRODUCT_B", "PRODUCT_C"]

    deletions = [
        make_deletion(name, attachment_id="10", created=zulu(1)) for name in names
    ]

    engine = build_engine(deletions, [make_product(name) for name in names])

    lookups = spy_lookups(engine)

    run(engine, issue)

    assert lookups == [names]


def test_unknown_document_class_is_rejected():
    engine = DeletionConsolidatorEngine()
    engine.payload = MAASMessage(document_class="CdsProduct")

    with pytest.raises(TypeError):
        list(engine.action_iterator())


def test_deletable_iterator_chunks_terms_queries():
    """Product names are chunked to stay below the terms query limit"""
    engine = DeletionConsolidatorEngine(terms_chunk_size=2)

    names = [f"PRODUCT_{index}" for index in range(5)]

    with patch(
        "maas_cds.engines.reports.product_deletion.CdsProduct.search"
    ) as product_search, patch(
        "maas_cds.engines.reports.product_deletion.CdsPublication.search"
    ) as publication_search:
        product_search.return_value.filter.return_value.params.return_value.scan.return_value = (
            []
        )
        publication_search.return_value.filter.return_value.filter.return_value.filter.return_value.params.return_value.scan.return_value = (
            []
        )

        list(engine.deletable_iterator(names, "LTA", ["Werum"]))

    assert product_search.call_count == 3
    assert publication_search.call_count == 3


def test_deletable_iterator_skips_empty_names():
    """An empty terms query is rejected by opensearch"""
    engine = DeletionConsolidatorEngine()

    with patch(
        "maas_cds.engines.reports.product_deletion.CdsProduct.search"
    ) as product_search:
        assert list(engine.deletable_iterator([], "LTA", ["Werum"])) == []

    product_search.assert_not_called()


def test_reports_are_keyed_on_the_index_the_document_comes_from():
    """The bulk result reports back meta.index, so the map must use it.

    Recomputing partition_index_name resolves a publication date and raises when
    none is set, and disagrees with the bulk action whenever a document sits in a
    partition its current dates no longer point to.
    """
    issue = make_issue(attachment_ids=["10"])

    deletions = [make_deletion("PRODUCT_A", attachment_id="10", created=zulu(1))]

    product = make_product("PRODUCT_A", index="cds-product-2019-01")

    engine = build_engine(deletions, [product])
    engine.send_reports = True

    actions = run(engine, issue)

    assert actions[0]["_index"] == "cds-product-2019-01"

    # pylint: disable=protected-access
    assert ("cds-product-2019-01", "PRODUCT_A") in engine._index_id_document_map


def test_documents_are_not_kept_for_reports_when_reports_are_off():
    """The report map would hold every document written by the run"""
    issue = make_issue(attachment_ids=["10"])

    deletions = [make_deletion("PRODUCT_A", attachment_id="10", created=zulu(1))]

    engine = build_engine(deletions, [make_product("PRODUCT_A")])

    assert len(run(engine, issue)) == 1

    # pylint: disable=protected-access
    assert not engine._index_id_document_map


def test_ticket_above_max_documents_is_written_chunk_by_chunk():
    """max_documents bounds a chunk, not a ticket: a big ticket takes longer, and
    is processed completely"""
    issue = make_issue(attachment_ids=["10"])

    names = [f"PRODUCT_{index:02d}" for index in range(10)]

    deletions = [
        make_deletion(name, attachment_id="10", created=zulu(1)) for name in names
    ]

    deletables = [make_product(name) for name in names] + [
        make_publication(name) for name in names
    ]

    engine = build_engine(deletions, deletables)
    engine.terms_chunk_size = 2
    # exactly what a chunk holds: 2 products and their 2 publications
    engine.max_documents = 4

    actions = run(engine, issue)

    assert_one_action_per_document(actions)
    assert len(actions) == 20


def test_each_chunk_is_written_and_dropped_before_the_next_lookup():
    """Memory is bounded by the chunk: its actions are yielded, and its documents
    released, before the next names are looked up"""
    issue = make_issue(attachment_ids=["10"])

    names = [f"PRODUCT_{index}" for index in range(5)]

    deletions = [
        make_deletion(name, attachment_id="10", created=zulu(1)) for name in names
    ]

    engine = build_engine(deletions, [make_product(name) for name in names])
    engine.terms_chunk_size = 2

    lookups = []

    lookup = engine.deletable_iterator

    def spy(chunk, service_type, service_ids):
        # pylint: disable=protected-access
        lookups.append((list(chunk), len(engine._decisions)))
        return lookup(chunk, service_type, service_ids)

    engine.deletable_iterator = spy

    actions = actions_of(engine, issue)

    first_chunk = [next(actions), next(actions)]

    # the next chunk is not looked up before the first one is handed to the bulk
    assert [chunk for chunk, _ in lookups] == [names[:2]]
    assert [action["_id"] for action in first_chunk] == names[:2]

    rest = list(actions)

    assert [chunk for chunk, _ in lookups] == [names[:2], names[2:4], names[4:]]

    # nothing of the previous chunk is held when the next one is looked up
    assert [held for _, held in lookups] == [0, 0, 0]

    # pylint: disable=protected-access
    assert not engine._decisions

    assert [action["_id"] for action in rest] == names[2:]


def test_max_documents_rejects_the_message_and_keeps_the_stale_rows():
    """A chunk matching far more documents than its names is rejected, never
    requeued forever. The rows of the superseded attachment stay: they list
    rollbacks that did not run."""
    issue = make_issue(attachment_ids=["11"])

    deletions = [
        make_deletion("PRODUCT_A", attachment_id="10", created=zulu(1)),
        make_deletion("PRODUCT_B", attachment_id="10", created=zulu(1)),
        make_deletion("PRODUCT_C", attachment_id="11", created=zulu(2)),
    ]

    products = [make_product("PRODUCT_A"), make_product("PRODUCT_B")]

    for product in products:
        product.mark_as_deleted(issue, ["Werum"])

    # the last chunk holds a single name, yet matches 3 documents
    deletables = products + [
        make_product("PRODUCT_C"),
        make_publication("PRODUCT_C"),
        make_publication("PRODUCT_C", index="cds-publication-2024-06"),
    ]

    engine = build_engine(deletions, deletables)
    engine.terms_chunk_size = 2
    engine.max_documents = 2

    actions = []

    with pytest.raises(CannotProcessMessageException):
        for action in actions_of(engine, issue):
            actions.append(action)

    # the first chunk was written
    assert [action["_id"] for action in actions] == ["PRODUCT_A", "PRODUCT_B"]

    assert not [action for action in actions if action["_op_type"] == "delete"]


def test_document_of_two_tickets_is_written_once_and_completed_by_the_retry():
    """Two tickets of one message touch the same product.

    Only the chunk being planned is in memory, so the second ticket fetches the
    product again, before the bulk applies the first write: a second write would
    conflict, and as bulk chunks run in parallel, either write could be the one
    lost. The second ticket leaves the product to the retry of the message, with
    the rows of its superseded attachment, which still list the rollback.
    """
    exprivia = make_issue(key="OMCS-1", deletion_interfaces=["Exprivia"])
    werum = make_issue(attachment_ids=["11"])

    # marked by the superseded attachment of the Werum ticket
    product_a = make_product("PRODUCT_A")
    product_a.mark_as_deleted(werum, ["Werum"])

    index = FakeIndex(
        [
            product_a,
            make_product("PRODUCT_B"),
            make_deletion(
                "PRODUCT_A",
                jira_issue="OMCS-1",
                report_name="OMCS-1_LTA_Exprivia_DelList.csv",
            ),
            make_deletion("PRODUCT_A", attachment_id="10", created=zulu(1)),
            make_deletion("PRODUCT_B", attachment_id="11", created=zulu(2)),
        ]
    )

    product_key = ("cds-product-2024-05", "PRODUCT_A")
    stale_row_key = ("cds-interface-product-deletion", "PRODUCT_A-10")

    with pytest.raises(HandleMessageException):
        index.run([exprivia, werum])

    # one write per document and per run: nothing conflicted
    assert index.conflicts == 0

    assert index.documents[product_key].LTA_Exprivia_is_deleted is True
    assert index.documents[("cds-product-2024-05", "PRODUCT_B")].LTA_Werum_is_deleted

    # the rollback waits for the retry, and so does the row that lists it
    assert index.documents[product_key].LTA_Werum_is_deleted is True
    assert stale_row_key in index.documents

    # the retry reads the product with the first write applied
    assert index.run([exprivia, werum]) == []

    assert index.conflicts == 0

    product = index.documents[product_key]

    assert product.LTA_Exprivia_is_deleted is True
    assert product.LTA_Exprivia_deletion_issue == "OMCS-1"
    assert "LTA_Werum_is_deleted" not in product.to_dict()
    assert product.nb_lta_deleted == 1

    assert stale_row_key not in index.documents
