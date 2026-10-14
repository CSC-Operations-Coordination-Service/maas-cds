"""Tests of the deletion consolidation and of its reconciliation with attachments"""

import copy
import json
import random
from unittest.mock import patch

import pytest
from maas_engine.exceptions import CannotProcessMessageException, HandleMessageException
from maas_model import MAASMessage
from opensearchpy.serializer import JSONSerializer

from maas_cds.engines.reports.product_deletion import DeletionConsolidatorEngine
from maas_cds.model import (
    CdsDeletionIssue,
    CdsInterfaceProductDeletion,
    CdsProduct,
    CdsPublication,
)

from data.deletion_data_test import (
    make_deletion,
    make_issue,
    make_product,
    make_publication,
    zulu,
)


def listed(deletion, keys, interface_type, names):
    """Whether load_listings would return a deletion"""
    return (
        deletion.jira_issue in keys
        and deletion.interface_type == interface_type
        and deletion.effective_product_name in set(names)
    )


def build_engine(deletions, deletables):
    """An engine whose database accesses are replaced by fixtures"""
    engine = DeletionConsolidatorEngine()

    engine.load_deletions = lambda issue: [
        deletion
        for deletion in deletions
        if deletion.jira_issue == issue.key
        and deletion.interface_type == issue.interface_type
    ]

    engine.load_listings = lambda keys, interface_type, names: [
        deletion
        for deletion in deletions
        if listed(deletion, keys, interface_type, names)
    ]

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


def indexed(document, index=None, identifier=None, seq_no=None):
    """A document as a search or an mget returns it: its source went through JSON,
    so the fields the model does not declare, such as the deletion dates of a
    product, are strings, while the declared dates of a ticket are datetimes"""
    meta = document.meta

    return document.__class__.from_opensearch(
        {
            "_index": index or getattr(meta, "index", None) or "cds-deletion-issue",
            "_id": identifier or meta.id,
            "_source": json.loads(JSONSerializer().dumps(document.to_dict())),
            "_seq_no": seq_no or getattr(meta, "seq_no", None) or 1,
            "_primary_term": getattr(meta, "primary_term", None) or 1,
        }
    )


class FakeIndex:
    """Serves fresh copies of its documents, as a search does, and applies bulk
    actions with their optimistic locking. Documents and tickets are read back as
    the index returns them, see indexed()."""

    def __init__(self, documents):
        self.documents = {
            (document.meta.index, document.meta.id): indexed(document)
            for document in documents
        }

        self.conflicts = 0

        # engine of the last run
        self.engine = None

    def plug(self, engine):
        """Replace the database lookups of an engine"""
        engine.load_deletions = lambda issue: [
            copy.deepcopy(document)
            for document in self.documents.values()
            if isinstance(document, CdsInterfaceProductDeletion)
            and document.jira_issue == issue.key
        ]

        engine.load_listings = lambda keys, interface_type, names: [
            copy.deepcopy(document)
            for document in self.documents.values()
            if isinstance(document, CdsInterfaceProductDeletion)
            and listed(document, keys, interface_type, names)
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
            if self.documents.pop(key, None) is None:
                return False, {"delete": details | {"status": 404}}

            return True, {"delete": details | {"status": 200, "result": "deleted"}}

        current = self.documents[key]

        if action["if_seq_no"] != current.meta.seq_no:
            self.conflicts += 1

            return False, {"index": details | {"status": 409}}

        self.documents[key] = indexed(
            current.__class__(**action["_source"]),
            index=key[0],
            identifier=key[1],
            seq_no=current.meta.seq_no + 1,
        )

        return True, {"index": details | {"status": 200, "result": "updated"}}

    def run(self, issues, rows=None, **options):
        """Process a message with a new engine, as the consumer does: a
        CdsDeletionIssue message of `issues`, or a CdsInterfaceProductDeletion
        message of the stored `rows` when given, whose tickets are `issues`"""
        engine = self.engine = self.plug(DeletionConsolidatorEngine(**options))

        issues = {issue.key: indexed(issue) for issue in issues}

        if rows is None:
            model = CdsDeletionIssue
            engine.input_documents = list(issues.values())
        else:
            model = CdsInterfaceProductDeletion
            engine.input_documents = [
                copy.deepcopy(self.documents[(row.meta.index, row.meta.id)])
                for row in rows
            ]

        payload = MAASMessage(document_class=model.__name__)
        engine.payload = payload

        with patch("maas_engine.engine.data.parallel_bulk", self.bulk), patch(
            "maas_engine.engine.data.db_connections"
        ), patch.object(engine, "get_input_model", return_value=model), patch(
            "maas_cds.engines.reports.product_deletion.CdsDeletionIssue.mget_by_ids",
            side_effect=lambda keys: [issues.get(key) for key in keys],
        ):
            return list(engine.run("update.x", payload))


class EagerIndex(FakeIndex):
    """Stand-in for parallel_bulk when it keeps up with the lookups: each action
    is applied before the next one is planned"""

    def bulk(self, _client, actions, **_kwargs):
        for action in actions:
            yield self.apply(action)


def run_until_acked(index, issues, max_runs=8, rows=None):
    """Process a message again as long as the consumer requeues it

    Returns:
        int: runs needed to acknowledge it, None when it never is
    """
    for runs in range(1, max_runs + 1):
        try:
            index.run(issues, rows=rows)
        except HandleMessageException:
            continue

        return runs

    return None


def ticket_row(name, key, service="Werum", attachment_id=None, created=None):
    """A deletion of ticket `key`, whose identifier is distinct from the other
    tickets ones"""
    deletion = make_deletion(
        name,
        attachment_id=attachment_id,
        created=created,
        jira_issue=key,
        report_name=f"{key}_LTA_{service}_DelList.csv",
    )

    deletion.meta.id = f"{name}-{key}-{attachment_id}"

    return deletion


def deletion_issue_of(document, service_id="Werum"):
    """The ticket a product or a publication is marked by on a service"""
    if isinstance(document, CdsPublication):
        return getattr(document, "deletion_issue", None)

    return getattr(document, f"LTA_{service_id}_deletion_issue", None)


DELETABLES = {"product": make_product, "publication": make_publication}


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

    engine = build_engine(deletions, products)

    actions = run(engine, issue)

    assert_one_action_per_document(actions)

    by_id = {action["_id"]: action for action in actions}

    for name in ("PRODUCT_A", "PRODUCT_B"):
        source = by_id[name]["_source"]
        assert "LTA_Werum_is_deleted" not in source
        assert "LTA_Werum_deletion_issue" not in source
        assert source["nb_lta_deleted"] == 0

    assert by_id["PRODUCT_C"]["_source"]["LTA_Werum_is_deleted"] is True

    # the deletions of the superseded attachment are dropped once the bulk of the
    # documents is applied, see delete_stale_rows()
    assert not [action for action in actions if action.get("_op_type") == "delete"]

    # pylint: disable=protected-access
    assert sorted(action["_id"] for action in engine._stale_rows) == [
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

    actions = run(
        build_engine(deletions, [product_a, make_product("PRODUCT_B")]), issue
    )

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

    actions = run(
        build_engine(deletions, [make_product("PRODUCT_A"), product_b]), issue
    )

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

    # pylint: disable=protected-access
    assert not engine._stale_rows


@pytest.mark.parametrize("index_class", [FakeIndex, EagerIndex])
def test_document_of_two_tickets_is_written_once_and_completed_by_the_retry(
    index_class,
):
    """Two tickets of one message touch the same product.

    Only the chunk being planned is in memory, so the second ticket fetches the
    product again, most likely before the bulk applies the first write: a second
    write would conflict, and as bulk chunks run in parallel, either write could
    be the one lost. The second ticket leaves the product to the retry of the
    message, with the rows of its superseded attachment, which still list the
    rollback.
    """
    exprivia = make_issue(key="OMCS-1", deletion_interfaces=["Exprivia"])
    werum = make_issue(attachment_ids=["11"])

    # marked by the superseded attachment of the Werum ticket
    product_a = make_product("PRODUCT_A")
    product_a.mark_as_deleted(werum, ["Werum"])

    index = index_class(
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


@pytest.mark.parametrize("index_class", [FakeIndex, EagerIndex])
@pytest.mark.parametrize("kind", ["product", "publication"])
@pytest.mark.parametrize(
    "count, owner",
    [(2, None), (2, 0), (2, 1), (3, None), (3, 0), (3, 1), (3, 2)],
)
def test_tickets_listing_a_name_leave_its_mark_to_the_last_one(
    index_class, kind, count, owner
):
    """The mark of a service is single-valued: as when every ticket was applied in
    turn to the document, the last ticket of the message that lists a name owns
    it. Were the earlier tickets to mark it too, the document, written once per
    run, would be left to the retry, where the first ticket overwrites the mark
    again: from three tickets on, the message would be requeued forever."""
    issues = [make_issue(key=f"OMCS-{number}") for number in range(1, count + 1)]

    document = DELETABLES[kind]("PRODUCT_A")

    if owner is not None:
        document.mark_as_deleted(issues[owner], ["Werum"])

    index = index_class(
        [document] + [ticket_row("PRODUCT_A", issue.key) for issue in issues]
    )

    # written once, by the last ticket, whatever the timing of the bulk
    assert run_until_acked(index, issues) == 1

    assert index.conflicts == 0

    marked = index.documents[(document.meta.index, document.meta.id)]

    assert deletion_issue_of(marked) == issues[-1].key

    if kind == "product":
        assert marked.nb_lta_deleted == 1


@pytest.mark.parametrize("index_class", [FakeIndex, EagerIndex])
@pytest.mark.parametrize("kind", ["product", "publication"])
def test_later_ticket_rolling_a_name_back_leaves_it_to_an_earlier_one(
    index_class, kind
):
    """OMCS-2 marked A, then superseded its list with one that drops A, while
    OMCS-1, before it in the message, still lists A. A ticket that only rolls a
    name back does not own it: A ends marked by OMCS-1, as when both tickets are
    applied in turn, and is not rolled back for good with the stale row gone."""
    earlier = make_issue(key="OMCS-1")
    later = make_issue(key="OMCS-2", attachment_ids=["21"])

    document = DELETABLES[kind]("PRODUCT_A")
    document.mark_as_deleted(later, ["Werum"])

    stale_row = ticket_row("PRODUCT_A", "OMCS-2", attachment_id="20", created=zulu(1))

    index = index_class(
        [
            document,
            make_product("PRODUCT_B"),
            ticket_row("PRODUCT_A", "OMCS-1"),
            stale_row,
            ticket_row("PRODUCT_B", "OMCS-2", attachment_id="21", created=zulu(2)),
        ]
    )

    runs = run_until_acked(index, [earlier, later])

    # the rollback may meet the mark of OMCS-1 not applied yet: left to the retry
    assert runs in (1, 2)

    assert index.conflicts == 0

    marked = index.documents[(document.meta.index, document.meta.id)]

    assert deletion_issue_of(marked) == "OMCS-1"

    if kind == "product":
        assert marked.LTA_Werum_is_deleted is True
        assert marked.nb_lta_deleted == 1

    assert (stale_row.meta.index, stale_row.meta.id) not in index.documents


def test_ticket_listed_twice_in_a_message_is_applied_once():
    """Its first place would leave its marks to the second one, which reads the
    documents before the first write lands and finds nothing to change"""
    issue = make_issue(attachment_ids=["11"])

    product_a = make_product("PRODUCT_A")
    product_a.mark_as_deleted(issue, ["Werum"])

    stale_row = make_deletion("PRODUCT_B", attachment_id="10", created=zulu(1))

    index = FakeIndex(
        [
            product_a,
            stale_row,
            make_deletion("PRODUCT_A", attachment_id="11", created=zulu(2)),
        ]
    )

    assert run_until_acked(index, [issue, issue]) == 1

    assert index.documents[("cds-product-2024-05", "PRODUCT_A")].LTA_Werum_is_deleted

    # and its stale rows are deleted once
    assert (stale_row.meta.index, stale_row.meta.id) not in index.documents


@pytest.mark.parametrize("index_class", [FakeIndex, EagerIndex])
def test_tickets_on_several_services_of_a_product_complete_it_in_turn(index_class):
    """Only the services a later ticket marks are left to it: the Werum mark of
    OMCS-2 and the Exprivia mark of OMCS-3 change the same product, so the second
    one waits for the retry"""
    issues = [
        make_issue(key="OMCS-1"),
        make_issue(key="OMCS-2"),
        make_issue(key="OMCS-3", deletion_interfaces=["Exprivia"]),
    ]

    index = index_class(
        [
            make_product("PRODUCT_A"),
            ticket_row("PRODUCT_A", "OMCS-1"),
            ticket_row("PRODUCT_A", "OMCS-2"),
            ticket_row("PRODUCT_A", "OMCS-3", service="Exprivia"),
        ]
    )

    assert run_until_acked(index, issues) == 2

    assert index.conflicts == 0

    product = index.documents[("cds-product-2024-05", "PRODUCT_A")]

    assert product.LTA_Werum_deletion_issue == "OMCS-2"
    assert product.LTA_Exprivia_deletion_issue == "OMCS-3"
    assert product.nb_lta_deleted == 2


@pytest.mark.parametrize("index_class", [FakeIndex, EagerIndex])
@pytest.mark.parametrize("second_type", ["LTA", "DD"])
def test_document_marked_again_as_read_back_from_the_index_is_unchanged(
    index_class, second_type
):
    """The index returns the deletion dates of a product as strings, the ticket
    gives a datetime: a product marked again by its ticket must still compare
    unchanged. Otherwise the first ticket writes it on every run, the second one
    always leaves it to the retry, and the message is requeued forever."""
    second = (
        make_issue(key="OMCS-2", deletion_interfaces=["Exprivia"])
        if second_type == "LTA"
        else make_issue(key="OMCS-2", interface_type="DD", deletion_interfaces=["DHUS"])
    )

    index = index_class(
        [
            make_product("PRODUCT_A", dddas_name="PRODUCT_A"),
            ticket_row("PRODUCT_A", "OMCS-1"),
            make_deletion(
                "PRODUCT_A",
                jira_issue="OMCS-2",
                interface_type=second_type,
                report_name=f"OMCS-2_{second_type}_DelList.csv",
            ),
        ]
    )

    assert run_until_acked(index, [make_issue(key="OMCS-1"), second]) == 2

    assert index.conflicts == 0

    product = index.documents[("cds-product-2024-05", "PRODUCT_A")]

    assert product.LTA_Werum_deletion_issue == "OMCS-1"

    if second_type == "LTA":
        assert product.LTA_Exprivia_deletion_issue == "OMCS-2"
    else:
        assert product.DD_DHUS_deletion_issue == "OMCS-2"

    # written once per ticket
    assert product.meta.seq_no == 3


def test_later_tickets_are_read_for_the_names_of_the_chunk_only():
    """The rows of the later tickets are read chunk by chunk, for the names the
    ticket marks, so that a message of many tickets holds no more than a chunk of
    them. A message of one ticket, the usual case, reads nothing more."""
    first = make_issue(key="OMCS-1")
    second = make_issue(key="OMCS-2")
    other_type = make_issue(key="OMCS-3", interface_type="DD")

    names = ["PRODUCT_A", "PRODUCT_B", "PRODUCT_C"]

    deletions = [ticket_row(name, "OMCS-1") for name in names] + [
        ticket_row("PRODUCT_B", "OMCS-2"),
        make_deletion("PRODUCT_C", jira_issue="OMCS-3", interface_type="DD"),
    ]

    listings = []

    def build():
        engine = build_engine(deletions, [make_product(name) for name in names])
        engine.terms_chunk_size = 2

        load_listings = engine.load_listings

        def spy(keys, interface_type, chunk):
            listings.append((list(keys), interface_type, list(chunk)))
            return load_listings(keys, interface_type, chunk)

        engine.load_listings = spy

        return engine

    run(build(), first)

    assert not listings

    engine = build()
    engine.payload = MAASMessage(document_class="CdsDeletionIssue")
    engine.input_documents = [first, second, other_type]

    actions = list(engine.action_iterator())

    # a ticket of the other interface type sets other fields
    assert listings == [
        (["OMCS-2"], "LTA", ["PRODUCT_A", "PRODUCT_B"]),
        (["OMCS-2"], "LTA", ["PRODUCT_C"]),
    ]

    by_id = {action["_id"]: action["_source"] for action in actions}

    assert by_id["PRODUCT_A"]["LTA_Werum_deletion_issue"] == "OMCS-1"
    assert by_id["PRODUCT_B"]["LTA_Werum_deletion_issue"] == "OMCS-2"
    assert by_id["PRODUCT_C"]["LTA_Werum_deletion_issue"] == "OMCS-1"


@pytest.mark.parametrize("index_class", [FakeIndex, EagerIndex])
def test_documents_written_for_the_earlier_tickets_are_bounded(index_class):
    """The keys of the documents written for the earlier tickets of a message are
    kept, see flush(). Past max_documents, the rest of the message is left to its
    retry, which finds them unchanged and gets further."""
    first = make_issue(key="OMCS-1")
    second = make_issue(key="OMCS-2", deletion_interfaces=["Exprivia"])

    names = [f"PRODUCT_{index}" for index in range(6)]

    index = index_class(
        [make_product(name) for name in names]
        + [ticket_row(name, "OMCS-1") for name in names]
        + [ticket_row(name, "OMCS-2", service="Exprivia") for name in names]
    )

    held = []

    for _ in range(8):
        try:
            index.run([first, second], max_documents=2, terms_chunk_size=2)
        except HandleMessageException:
            # pylint: disable=protected-access
            held.append(len(index.engine._written))
            continue

        break

    # a chunk of OMCS-1 per run, then the last 2 Exprivia marks left to the retry
    assert held == [2, 2, 2]

    assert index.conflicts == 0

    for name in names:
        product = index.documents[("cds-product-2024-05", name)]

        assert product.LTA_Werum_deletion_issue == "OMCS-1"
        assert product.LTA_Exprivia_deletion_issue == "OMCS-2"
        assert product.nb_lta_deleted == 2


def failing_once(index_class, failures):
    """An index whose first write of some documents fails with a status

    Args:
        index_class (type): FakeIndex or EagerIndex
        failures (dict): (index, identifier) -> status of its first write
    """

    class FailingIndex(index_class):
        """Fails some writes once"""

        def apply(self, action):
            key = (action["_index"], action["_id"])

            if action["_op_type"] != "delete" and key in failures:
                details = {"_index": key[0], "_id": key[1]}

                return False, {"index": details | {"status": failures.pop(key)}}

            return super().apply(action)

    return FailingIndex


@pytest.mark.parametrize("index_class", [FakeIndex, EagerIndex])
@pytest.mark.parametrize("status", [409, 429, 503])
def test_failed_rollback_keeps_the_stale_rows_for_the_retry(index_class, status):
    """A bulk item can fail: a conflict with another engine, an overloaded node.
    The rows of the superseded attachment are dropped only once every write is
    applied, so that the retry still finds the rollback to do."""
    issue = make_issue(attachment_ids=["11"])

    product_a = make_product("PRODUCT_A")
    product_a.mark_as_deleted(issue, ["Werum"])

    stale_row = make_deletion("PRODUCT_A", attachment_id="10", created=zulu(1))

    product_key = ("cds-product-2024-05", "PRODUCT_A")
    stale_key = (stale_row.meta.index, stale_row.meta.id)

    index = failing_once(index_class, {product_key: status})(
        [
            product_a,
            make_product("PRODUCT_C"),
            stale_row,
            make_deletion("PRODUCT_C", attachment_id="11", created=zulu(2)),
        ]
    )

    with pytest.raises(HandleMessageException):
        index.run([issue])

    assert index.documents[product_key].LTA_Werum_is_deleted is True
    assert stale_key in index.documents

    assert index.run([issue]) == []

    assert "LTA_Werum_is_deleted" not in index.documents[product_key].to_dict()
    assert stale_key not in index.documents


@pytest.mark.parametrize("index_class", [FakeIndex, EagerIndex])
def test_failed_mark_of_a_whole_ticket_reconcile_is_retried(index_class):
    """A message of rows reconciles the whole ticket when an attachment is stale.
    A mark of a name outside the payload that fails is retried with the whole
    ticket: the stale rows that call for it are still there."""
    issue = make_issue(attachment_ids=["11"])

    product_1 = make_product("PRODUCT_1")
    product_1.mark_as_deleted(issue, ["Werum"])

    rows = [
        make_deletion("PRODUCT_1", attachment_id="10", created=zulu(1)),
        make_deletion("PRODUCT_1", attachment_id="11", created=zulu(2)),
        make_deletion("PRODUCT_2", attachment_id="11", created=zulu(2)),
    ]

    product_2 = ("cds-product-2024-05", "PRODUCT_2")

    index = failing_once(index_class, {product_2: 429})(
        [product_1, make_product("PRODUCT_2")] + rows
    )

    # the collector sends the row of PRODUCT_1 of the new attachment
    assert run_until_acked(index, [issue], rows=rows[1:2]) == 2

    assert index.documents[product_2].LTA_Werum_deletion_issue == "OMCS-1234"

    assert (rows[0].meta.index, rows[0].meta.id) not in index.documents


@pytest.mark.parametrize("index_class", [FakeIndex, EagerIndex])
def test_owner_of_a_mark_does_not_depend_on_the_runs_of_a_message(index_class):
    """OMCS-2 superseded its list, so it is reconciled whole and owns PRODUCT_1,
    which OMCS-1 lists too. A DD ticket of the same product forces a retry: the
    stale rows of OMCS-2 are kept until the message is acknowledged, so the retry
    sees the same tickets and OMCS-2 keeps the mark."""
    first = make_issue(key="OMCS-1", deletion_interfaces=["Exprivia"])
    second = make_issue(
        key="OMCS-2", deletion_interfaces=["Exprivia"], attachment_ids=["11"]
    )
    third = make_issue(key="OMCS-3", interface_type="DD", deletion_interfaces=["DHUS"])

    rows = [
        ticket_row("PRODUCT_1", "OMCS-1", service="Exprivia"),
        ticket_row("PRODUCT_2", "OMCS-2", "Exprivia", "10", created=zulu(1)),
        ticket_row("PRODUCT_1", "OMCS-2", "Exprivia", "11", created=zulu(2)),
        ticket_row("PRODUCT_4", "OMCS-2", "Exprivia", "11", created=zulu(2)),
        make_deletion(
            "DPRODUCT_1",
            jira_issue="OMCS-3",
            interface_type="DD",
            report_name="OMCS-3_DD_DHUS_DelList.csv",
        ),
    ]

    products = [
        make_product(name, dddas_name=f"D{name}")
        for name in ("PRODUCT_1", "PRODUCT_2", "PRODUCT_4")
    ]

    index = index_class(products + rows)

    runs = run_until_acked(
        index, [first, second, third], rows=[rows[0], rows[3], rows[4]]
    )

    assert runs == 2

    product = index.documents[("cds-product-2024-05", "PRODUCT_1")]

    assert product.LTA_Exprivia_deletion_issue == "OMCS-2"
    assert product.DD_DHUS_deletion_issue == "OMCS-3"

    assert (rows[1].meta.index, rows[1].meta.id) not in index.documents


SERVICES = ["Werum", "Exprivia"]


def random_message(seed):
    """Tickets on one or both services of PRODUCT_A, that list it, roll it back or
    leave it alone, and the product and publications as marked by any of them"""
    rng = random.Random(seed)

    issues = []

    rows = []

    behaviours = {}

    for number in range(1, rng.randint(2, 4) + 1):
        key = f"OMCS-{number}"

        services = rng.sample(SERVICES, rng.randint(1, 2))

        behaviour = behaviours[key] = rng.choice(["marks", "rolls back", "ignores"])

        if behaviour == "rolls back":
            issues.append(
                make_issue(key=key, deletion_interfaces=services, attachment_ids=["1"])
            )

            rows.append(ticket_row("PRODUCT_A", key, services[0], "0", created=zulu(1)))
            rows.append(ticket_row("PRODUCT_B", key, services[0], "1", created=zulu(2)))
        else:
            issues.append(make_issue(key=key, deletion_interfaces=services))

            name = "PRODUCT_A" if behaviour == "marks" else "PRODUCT_B"

            rows.append(ticket_row(name, key, services[0]))

    deletables = [make_product("PRODUCT_A")] + [
        make_publication("PRODUCT_A", service_id=service) for service in SERVICES
    ]

    for service in SERVICES:
        owner = rng.choice(
            [None] + [issue for issue in issues if service in issue.deletion_interfaces]
        )

        if owner is not None:
            deletables[0].mark_as_deleted(owner, [service])
            deletables[1 + SERVICES.index(service)].mark_as_deleted(owner)

    return issues, behaviours, deletables, rows


def applied_in_turn(issues, behaviours, deletables):
    """The tickets applied in turn to one instance of each document"""
    deletables = copy.deepcopy(deletables)

    for issue in issues:
        found = [
            deletable
            for deletable in deletables
            if isinstance(deletable, CdsProduct)
            or deletable.service_id in issue.deletion_interfaces
        ]

        for deletable in found:
            if behaviours[issue.key] == "rolls back":
                deletable.unmark_as_deleted(issue)

            if behaviours[issue.key] == "marks":
                deletable.mark_as_deleted(issue, issue.deletion_interfaces)

    return deletables


def deletion_state(deletables):
    """The deletion fields of PRODUCT_A and of its publications"""
    product, *publications = deletables

    return {
        "nb_lta_deleted": getattr(product, "nb_lta_deleted", 0) or 0,
        "product": {
            service: deletion_issue_of(product, service) for service in SERVICES
        },
        "publications": [
            deletion_issue_of(publication) for publication in publications
        ],
    }


def message_problem(index_class, seed):
    """Process the random message of `seed` until it is acknowledged

    Returns:
        str: what went wrong, None when nothing did
    """
    issues, behaviours, deletables, rows = random_message(seed)

    expected = deletion_state(applied_in_turn(issues, behaviours, deletables))

    index = index_class(copy.deepcopy(deletables) + rows)

    runs = run_until_acked(index, issues)

    if runs is None or runs > len(issues):
        return f"acknowledged after {runs} runs for {len(issues)} tickets"

    if index.conflicts:
        return f"{index.conflicts} conflicts"

    if [key for key in index.documents if key[1].endswith("-0")]:
        return "stale rows left"

    reached = deletion_state(
        [
            index.documents[(deletable.meta.index, deletable.meta.id)]
            for deletable in deletables
        ]
    )

    if reached != expected:
        return f"{reached} instead of {expected}"

    return None


@pytest.mark.parametrize("index_class", [FakeIndex, EagerIndex])
def test_message_ends_as_its_tickets_applied_in_turn(index_class):
    """Whatever the tickets of a message do to a name, and the timing of the bulk,
    the message is acknowledged within a run per ticket, with no conflict and no
    stale row left, in the state its tickets applied in turn would give"""
    problems = {
        seed: problem
        for seed in range(200)
        if (problem := message_problem(index_class, seed))
    }

    assert not problems
