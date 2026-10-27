"""Tests of the orbit identifier strategies shared by the missing orbit detections"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from maas_cds.lib.orbit_id_strategy import (
    S3DatatakeIdStrategy,
    S3DownlinkOrbitStrategy,
    S5DatatakeIdStrategy,
)


def test_s3_datatake_ids_between():
    """The S3 datatake identifiers between two references of the same cycle"""
    assert not S3DatatakeIdStrategy.ids_between("S3B-069-007", "S3B-069-007")

    expected = [
        "S3B-069-003",
        "S3B-069-004",
        "S3B-069-005",
    ]

    assert S3DatatakeIdStrategy.ids_between("S3B-069-006", "S3B-069-002") == expected

    # the references are given in any order
    assert S3DatatakeIdStrategy.ids_between("S3B-069-002", "S3B-069-006") == expected

    # and can be read from the most recent orbit to the oldest one
    assert (
        S3DatatakeIdStrategy.ids_between("S3B-069-006", "S3B-069-002", descending=True)
        == expected[::-1]
    )


def test_s3_datatake_ids_between_cycles():
    """The length of a cycle is unknown: only the orbits of the latest cycle,
    from its first one, are listed"""
    assert S3DatatakeIdStrategy.ids_between("S3B-069-003", "S3B-068-380") == [
        "S3B-069-001",
        "S3B-069-002",
    ]

    # whatever the last orbit of the previous cycle
    assert S3DatatakeIdStrategy.ids_between("S3C-004-003", "S3C-003-110") == [
        "S3C-004-001",
        "S3C-004-002",
    ]

    # skipped cycles are unknown too
    assert S3DatatakeIdStrategy.ids_between("S3B-071-002", "S3B-068-380") == [
        "S3B-071-001",
    ]


def test_s3_datatake_ids_between_consecutive_orbits():
    """Two consecutive datatakes have nothing in between"""
    assert not S3DatatakeIdStrategy.ids_between("S3B-069-001", "S3B-068-385")

    # the end of the previous cycle is unknown
    assert not S3DatatakeIdStrategy.ids_between("S3B-069-001", "S3B-068-384")

    assert not S3DatatakeIdStrategy.ids_between("S3B-069-008", "S3B-069-007")


@pytest.mark.parametrize(
    "datatake_id_1, datatake_id_2",
    [
        ("Sj3B-069-001", "S3B-068-385"),
        ("S3B-069-001", "S3B-08-385"),
        ("S3B-069-001", "S3B-08-45"),
        # tagged identifier of an irregular numbering period
        ("S3C-004-001-COM", "S3C-004-002"),
    ],
)
def test_s3_datatake_ids_bad_format(datatake_id_1, datatake_id_2):
    """An identifier out of the expected format is rejected"""
    with pytest.raises(ValueError):
        S3DatatakeIdStrategy.ids_between(datatake_id_1, datatake_id_2)

    with pytest.raises(ValueError):
        S3DatatakeIdStrategy.sort_ids(datatake_id_1, datatake_id_2)


def test_s3_datatake_sort_ids():
    """The datatake identifiers are sorted on their orbit, not as strings"""
    assert S3DatatakeIdStrategy.sort_ids("S3B-069-001", "S3B-068-385") == (
        "S3B-068-385",
        "S3B-069-001",
    )

    assert S3DatatakeIdStrategy.sort_ids("S3B-068-385", "S3B-069-001") == (
        "S3B-068-385",
        "S3B-069-001",
    )


def test_s3_datatake_sort_ids_relative_orbit_over_385():
    """The rank does not rely on a number of orbits per cycle"""
    assert S3DatatakeIdStrategy.sort_ids("S3B-069-001", "S3B-068-400") == (
        "S3B-068-400",
        "S3B-069-001",
    )


def test_s3_datatake_is_orbit_id():
    """Only the nominal identifiers take part in the orbit arithmetic"""
    assert S3DatatakeIdStrategy.is_orbit_id("S3C-003-111")
    assert not S3DatatakeIdStrategy.is_orbit_id("S3C-004-001-COM")
    assert not S3DatatakeIdStrategy.is_orbit_id(None)


ORBIT = S3DatatakeIdStrategy.ORBIT_DURATION

NOW = datetime(2026, 10, 26)


def test_s3_datatake_missing_ids():
    """The identifiers missing between an orbit and the previous one in time"""
    assert S3DatatakeIdStrategy.missing_ids(
        "S3C-003-111", NOW - 4 * ORBIT, "S3C-003-115", NOW, descending=True
    ) == ["S3C-003-114", "S3C-003-113", "S3C-003-112"]

    # a new cycle starts at its first orbit
    assert S3DatatakeIdStrategy.missing_ids(
        "S3C-003-380", NOW - 5 * ORBIT, "S3C-004-003", NOW
    ) == ["S3C-004-001", "S3C-004-002"]

    # the start of an orbit may move a bit from one orbit to the next
    assert S3DatatakeIdStrategy.missing_ids(
        "S3C-003-111",
        NOW - 4 * ORBIT + timedelta(minutes=30),
        "S3C-003-115",
        NOW,
    ) == ["S3C-003-112", "S3C-003-113", "S3C-003-114"]


@pytest.mark.parametrize(
    "previous_id, previous_start, current_id",
    [
        # the numbering goes backwards: end of the S3C commissioning
        ("S3C-009-189", NOW - ORBIT, "S3C-003-111"),
        # same identifier
        ("S3C-003-111", NOW - ORBIT, "S3C-003-111"),
        # irregular numbering period, tagged
        ("S3C-009-189-COM", NOW - ORBIT, "S3C-003-111"),
        ("S3C-004-001", NOW - ORBIT, "S3C-004-003-COM"),
        # more missing identifiers than elapsed orbits
        ("S3C-003-100", NOW - 3 * ORBIT, "S3C-003-111"),
    ],
)
def test_s3_datatake_missing_ids_untrusted(previous_id, previous_start, current_id):
    """Nothing is missing when the numbering can not explain the gap"""
    assert not S3DatatakeIdStrategy.missing_ids(
        previous_id, previous_start, current_id, NOW
    )


NOMINAL_ORBIT = S3DatatakeIdStrategy.NOMINAL_CYCLE_DURATION / 385

REFERENCES = {"S3C": ("S3C-003-111", NOW)}


@pytest.mark.parametrize(
    "datatake_id, start, matches",
    [
        ("S3C-003-111", NOW, True),
        # the reference, as the product, starts anywhere in its orbit
        ("S3C-003-115", NOW + 4 * NOMINAL_ORBIT - timedelta(minutes=90), True),
        ("S3C-003-115", NOW + 4 * NOMINAL_ORBIT + timedelta(minutes=90), True),
        # a new cycle every 385 orbits
        ("S3C-004-001", NOW + 275 * NOMINAL_ORBIT, True),
        ("S3C-002-111", NOW - 385 * NOMINAL_ORBIT, True),
        # commissioning: identifiers the nominal numbering reuses later
        ("S3C-004-001", NOW - timedelta(days=45), False),
        ("S3C-009-189", NOW - NOMINAL_ORBIT, False),
        # the numbering was not following the orbit before the commissioning
        ("S3C-003-110", NOW - timedelta(days=60), False),
        # no reference, nothing to compare with
        ("S3A-004-001", NOW - timedelta(days=45), True),
    ],
)
def test_s3_datatake_matches_sensing_time(datatake_id, start, matches):
    """An identifier is the orbit counted from the reference of its satellite"""
    with patch.dict(S3DatatakeIdStrategy.ORBIT_REFERENCES, REFERENCES):
        assert S3DatatakeIdStrategy.matches_sensing_time(datatake_id, start) is matches


def test_s3_datatake_tag_abnormal():
    """Only an abnormal orbit identifier is tagged"""
    with patch.dict(S3DatatakeIdStrategy.ORBIT_REFERENCES, REFERENCES):
        tagged = S3DatatakeIdStrategy.tag_abnormal("S3C-009-189", NOW - NOMINAL_ORBIT)

        assert tagged == "S3C-009-189-COM"
        assert not S3DatatakeIdStrategy.is_orbit_id(tagged)

        assert S3DatatakeIdStrategy.tag_abnormal("S3C-003-111", NOW) == "S3C-003-111"

        # tagged once
        assert S3DatatakeIdStrategy.tag_abnormal(tagged, NOW) == tagged


def test_s5_datatake_missing_ids_without_time_check():
    """Without orbit duration, the gap is not checked against the time"""
    assert S5DatatakeIdStrategy.missing_ids(
        "S5P-12345", NOW - timedelta(seconds=1), "S5P-12348", NOW
    ) == ["S5P-12346", "S5P-12347"]


def test_s5_datatake_ids_between():
    """The S5 datatake identifiers hold a single absolute orbit counter"""
    assert not S5DatatakeIdStrategy.ids_between("S5P-12345", "S5P-12345")

    assert not S5DatatakeIdStrategy.ids_between("S5P-12345", "S5P-12346")

    assert S5DatatakeIdStrategy.ids_between("S5P-12348", "S5P-12345") == [
        "S5P-12346",
        "S5P-12347",
    ]

    # the padding of the counter is kept
    assert S5DatatakeIdStrategy.ids_between("S5P-01002", "S5P-00999") == [
        "S5P-01000",
        "S5P-01001",
    ]


def test_s5_datatake_ids_bad_format():
    """An identifier out of the expected format is rejected"""
    with pytest.raises(ValueError):
        S5DatatakeIdStrategy.ids_between("S5P-1234", "S5P-12345")


def test_downlink_orbits_between():
    """The S3 downlink orbits are a bare, zero padded counter"""
    assert not S3DownlinkOrbitStrategy.ids_between("052724", "052724")

    assert not S3DownlinkOrbitStrategy.ids_between("052724", "052725")

    assert S3DownlinkOrbitStrategy.ids_between("052724", "052721") == [
        "052722",
        "052723",
    ]

    assert S3DownlinkOrbitStrategy.ids_between("052724", "052721", descending=True) == [
        "052723",
        "052722",
    ]


def test_downlink_orbits_bad_format():
    """An orbit out of the expected format is rejected"""
    with pytest.raises(ValueError):
        S3DownlinkOrbitStrategy.ids_between("52724", "052721")

    with pytest.raises(ValueError):
        S3DownlinkOrbitStrategy.ids_between("O52724", "052721")
