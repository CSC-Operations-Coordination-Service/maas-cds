"""Tests of the pure attachment grouping helpers"""

import datetime

from maas_cds.lib.deletion_grouping import (
    UNRESOLVED_PREFIX,
    AttachmentInfo,
    build_groups,
    chunked,
    extract_service_id,
    resolve_service_id,
    strip_issue_prefix,
)

from data.deletion_data_test import make_issue


def zulu(day, hour=0):
    return datetime.datetime(2024, 5, day, hour, tzinfo=datetime.timezone.utc)


def attachment(attachment_id, created=None, report_name="", service_id=None):
    return AttachmentInfo(
        attachment_id, created=created, report_name=report_name, service_id=service_id
    )


def test_strip_issue_prefix():
    assert (
        strip_issue_prefix("OMCS-1234_LTA_Werum_DelList.csv", "OMCS-1234")
        == "LTA_Werum_DelList.csv"
    )

    # attachement_prefix disabled: the report name has no prefix to strip
    assert (
        strip_issue_prefix("LTA_Werum_DelList.csv", "OMCS-1234")
        == "LTA_Werum_DelList.csv"
    )

    assert strip_issue_prefix("", "OMCS-1234") == ""


def test_extract_service_id_nominal():
    assert extract_service_id("OMCS-1234_LTA_Werum_DelList.csv") == "Werum"

    # CDSE is the alias of the DAS service
    assert extract_service_id("OMCS-5678_CDSE_DelList.xlsx") == "DAS"

    assert extract_service_id("OMCS-5678_DHUS_S3_DelList.csv") == "DHUS"


def test_extract_service_id_canonical_value():
    """a file named with the service identifier rather than its alias resolves"""
    assert extract_service_id("OMCS-5678_DD_DAS_DelList.csv") == "DAS"


def test_extract_service_id_underscored_service():
    """S5P_DLR is not a single token: it is reached through its alias"""
    assert extract_service_id("OMCS-1234_LTA_DLR_DelList.csv") == "S5P_DLR"


def test_extract_service_id_is_case_insensitive():
    assert extract_service_id("OMCS-1234_lta_WERUM_dellist.CSV") == "Werum"


def test_extract_service_id_no_match():
    assert extract_service_id("products.csv") is None
    assert extract_service_id("") is None


def test_resolve_service_id_falls_back_on_single_interface():
    issue = make_issue(deletion_interfaces=["Acri"])

    assert resolve_service_id("products.csv", issue) == "Acri"


def test_resolve_service_id_unresolved_are_never_grouped_together():
    """The data loss guard: two unparsable attachments must not compete.

    Sharing a group would make the older one stale and roll back deletions that
    are still valid.
    """
    issue = make_issue(deletion_interfaces=["Werum", "Acri"])

    first = resolve_service_id("products.csv", issue)
    second = resolve_service_id("list.csv", issue)

    assert first.startswith(UNRESOLVED_PREFIX)
    assert second.startswith(UNRESOLVED_PREFIX)
    assert first != second


def test_build_groups_single_attachment():
    issue = make_issue(attachment_ids=["10"])

    current, stale = build_groups(
        [attachment("10", created=zulu(1), service_id="Werum")], issue
    )

    assert current == ["10"]
    assert stale == []


def test_build_groups_newer_supersedes_older():
    """A new version of a file is attached: the previous one loses"""
    issue = make_issue(attachment_ids=["10", "11"])

    current, stale = build_groups(
        [
            attachment("10", created=zulu(1), service_id="Werum"),
            attachment("11", created=zulu(2), service_id="Werum"),
        ],
        issue,
    )

    assert current == ["11"]
    assert stale == ["10"]


def test_build_groups_keeps_one_attachment_per_service():
    """A ticket carrying one file per service keeps them all"""
    issue = make_issue(
        deletion_interfaces=["Werum", "Acri"], attachment_ids=["10", "11"]
    )

    current, stale = build_groups(
        [
            attachment("10", created=zulu(1), service_id="Werum"),
            attachment("11", created=zulu(2), service_id="Acri"),
        ],
        issue,
    )

    assert sorted(current) == ["10", "11"]
    assert stale == []


def test_build_groups_detached_attachment_is_stale():
    """An attachment removed from the ticket loses whatever its date"""
    issue = make_issue(attachment_ids=["10"])

    current, stale = build_groups(
        [
            attachment("10", created=zulu(1), service_id="Werum"),
            attachment("11", created=zulu(2), service_id="Werum"),
        ],
        issue,
    )

    # 11 is the most recent but is no longer attached: 10 is promoted back
    assert current == ["10"]
    assert stale == ["11"]


def test_build_groups_without_ground_truth_is_inert_on_detachment():
    """A ticket not collected since attachment identities are recorded.

    Its attachment_ids is empty, which must not be read as 'everything was
    detached'.
    """
    issue = make_issue(attachment_ids=[])

    current, stale = build_groups(
        [
            attachment("10", created=zulu(1), service_id="Werum"),
            attachment("11", created=zulu(2), service_id="Werum"),
        ],
        issue,
    )

    # supersession still applies inside the service group, detachment does not
    assert current == ["11"]
    assert stale == ["10"]


def test_build_groups_all_detached():
    issue = make_issue(attachment_ids=["99"])

    current, stale = build_groups(
        [attachment("10", created=zulu(1), service_id="Werum")], issue
    )

    assert current == []
    assert stale == ["10"]


def test_build_groups_tie_break_is_numeric():
    """JIRA identifiers are numeric strings: '10' is newer than '9'"""
    issue = make_issue(attachment_ids=["9", "10"])

    current, stale = build_groups(
        [
            attachment("9", created=zulu(1), service_id="Werum"),
            attachment("10", created=zulu(1), service_id="Werum"),
        ],
        issue,
    )

    assert current == ["10"]
    assert stale == ["9"]


def test_build_groups_tie_break_without_created():
    """A missing creation date must not be compared against a datetime"""
    issue = make_issue(attachment_ids=["9", "10"])

    current, stale = build_groups(
        [
            attachment("9", created=None, service_id="Werum"),
            attachment("10", created=zulu(1), service_id="Werum"),
        ],
        issue,
    )

    assert current == ["10"]
    assert stale == ["9"]


def test_chunked():
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunked([], 2)) == []
