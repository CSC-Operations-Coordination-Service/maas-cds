"""Tests of the deletion consolidation and of its reconciliation with attachments"""

from unittest.mock import patch

import pytest
from maas_model import MAASMessage

from maas_cds.engines.reports.product_deletion import DeletionConsolidatorEngine

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


def run(engine, issue):
    engine.payload = MAASMessage(document_class="CdsDeletionIssue")
    engine.input_documents = [issue]

    return list(engine.action_iterator())


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


def test_payload_of_deletions_reconciles_the_whole_ticket():
    """A message carrying a subset of the rows must still see the full ticket"""
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

    with patch(
        "maas_cds.engines.reports.product_deletion.CdsDeletionIssue.mget_by_ids",
        return_value=[issue],
    ) as mock_mget:
        actions = list(engine.action_iterator())

    mock_mget.assert_called_once_with(["OMCS-1234"])

    # the row absent from the payload was still reconciled
    rollback = next(action for action in actions if action["_id"] == "PRODUCT_A")
    assert "LTA_Werum_is_deleted" not in rollback["_source"]


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

    actions = run(engine, issue)

    assert actions[0]["_index"] == "cds-product-2019-01"

    # pylint: disable=protected-access
    assert ("cds-product-2019-01", "PRODUCT_A") in engine._index_id_document_map
