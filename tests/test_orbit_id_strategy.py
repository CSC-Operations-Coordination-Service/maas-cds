"""Tests of the orbit identifier strategies shared by the missing orbit detections"""

import pytest

from maas_cds.lib.orbit_id_strategy import (
    S3DatatakeIdStrategy,
    S3DownlinkOrbitStrategy,
    S5DatatakeIdStrategy,
)


def test_s3_datatake_ids_between():
    """The S3 datatake identifiers between two references, cycle wrap included"""
    assert not S3DatatakeIdStrategy.ids_between("S3B-069-007", "S3B-069-007")

    expected = [
        "S3B-068-381",
        "S3B-068-382",
        "S3B-068-383",
        "S3B-068-384",
        "S3B-068-385",
        "S3B-069-001",
    ]

    assert S3DatatakeIdStrategy.ids_between("S3B-069-002", "S3B-068-380") == expected

    # the references are given in any order
    assert S3DatatakeIdStrategy.ids_between("S3B-068-380", "S3B-069-002") == expected

    # and can be read from the most recent orbit to the oldest one
    assert (
        S3DatatakeIdStrategy.ids_between("S3B-069-002", "S3B-068-380", descending=True)
        == expected[::-1]
    )


def test_s3_datatake_ids_between_consecutive_orbits():
    """Two consecutive datatakes have nothing in between"""
    assert not S3DatatakeIdStrategy.ids_between("S3B-069-001", "S3B-068-385")

    assert S3DatatakeIdStrategy.ids_between("S3B-069-001", "S3B-068-384") == [
        "S3B-068-385",
    ]


@pytest.mark.parametrize(
    "datatake_id_1, datatake_id_2",
    [
        ("Sj3B-069-001", "S3B-068-385"),
        ("S3B-069-001", "S3B-08-385"),
        ("S3B-069-001", "S3B-08-45"),
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
