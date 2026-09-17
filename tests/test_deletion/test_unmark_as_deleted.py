"""Tests of the deletion rollback on products and publications"""

from data.deletion_data_test import make_issue, make_product, make_publication


def test_mark_then_unmark_is_a_round_trip():
    """A rollback must leave no trace but a reset counter"""
    issue = make_issue()

    product = make_product("S1A_PRODUCT")

    before = product.to_dict()

    product.mark_as_deleted(issue, ["Werum"])

    assert product.LTA_Werum_is_deleted is True
    assert product.LTA_Werum_deletion_issue == "OMCS-1234"
    assert product.nb_lta_deleted == 1

    assert product.unmark_as_deleted(issue) is True

    assert product.to_dict() == dict(before, nb_lta_deleted=0)


def test_unmark_handles_underscored_service_id():
    """S5P_DLR contains the separator used to build the attribute names"""
    issue = make_issue(deletion_interfaces=["S5P_DLR"])

    product = make_product("S5P_PRODUCT", mission="S5")

    product.mark_as_deleted(issue, ["S5P_DLR"])

    assert product.deleted_service_ids("LTA") == ["S5P_DLR"]

    assert product.unmark_as_deleted(issue) is True
    assert "LTA_S5P_DLR_is_deleted" not in product.to_dict()


def test_unmark_only_clears_the_marks_of_its_own_issue():
    """The core of the rollback: a product deleted by two tickets keeps the other"""
    werum_issue = make_issue(key="OMCS-1", deletion_interfaces=["Werum"])
    acri_issue = make_issue(key="OMCS-2", deletion_interfaces=["Acri"])

    product = make_product("S1A_PRODUCT")

    product.mark_as_deleted(werum_issue, ["Werum"])
    product.mark_as_deleted(acri_issue, ["Acri"])

    assert product.nb_lta_deleted == 2

    assert product.unmark_as_deleted(werum_issue) is True

    assert "LTA_Werum_is_deleted" not in product.to_dict()
    assert product.LTA_Acri_is_deleted is True
    assert product.LTA_Acri_deletion_issue == "OMCS-2"
    assert product.nb_lta_deleted == 1


def test_unmark_untouched_product_is_a_noop():
    issue = make_issue()

    product = make_product("S1A_PRODUCT")

    before = product.to_dict()

    assert product.unmark_as_deleted(issue) is False
    assert product.to_dict() == before


def test_unmark_is_idempotent():
    issue = make_issue()

    product = make_product("S1A_PRODUCT")
    product.mark_as_deleted(issue, ["Werum"])
    product.unmark_as_deleted(issue)

    assert product.unmark_as_deleted(issue) is False


def test_rolled_back_fields_are_absent_not_null():
    """A None would be written as an explicit null in the index source"""
    issue = make_issue()

    product = make_product("S1A_PRODUCT")
    product.mark_as_deleted(issue, ["Werum"])
    product.unmark_as_deleted(issue)

    source = product.to_bulk_action()["_source"]

    assert "LTA_Werum_is_deleted" not in source
    assert "LTA_Werum_deletion_issue" not in source
    assert [name for name, value in source.items() if value is None] == []


def test_remove_field_on_absent_declared_field():
    """deletion_issue is declared on CdsPublication: delattr would raise"""
    publication = make_publication("S1A_PRODUCT")

    assert publication.remove_field("deletion_issue") is False


def test_publication_mark_then_unmark():
    issue = make_issue()

    publication = make_publication("S1A_PRODUCT")

    before = publication.to_dict()

    publication.mark_as_deleted(issue)

    assert publication.deletion_issue == "OMCS-1234"

    assert publication.unmark_as_deleted(issue) is True
    assert publication.to_dict() == before


def test_publication_unmark_only_clears_its_own_issue():
    publication = make_publication("S1A_PRODUCT")

    publication.mark_as_deleted(make_issue(key="OMCS-1"))

    assert publication.unmark_as_deleted(make_issue(key="OMCS-2")) is False
    assert publication.deletion_issue == "OMCS-1"


def test_publication_unattributed_marks_are_left_alone():
    """Publications marked before the issue was recorded cannot be attributed"""
    publication = make_publication("S1A_PRODUCT")
    publication.deletion_date = "2020-01-01T00:00:00.000Z"
    publication.deletion_cause = "legacy"

    assert publication.unmark_as_deleted(make_issue()) is False
    assert publication.deletion_cause == "legacy"
