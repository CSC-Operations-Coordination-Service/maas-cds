"""Tests of the S3 session KPI against a real consolidated document

S3B downlink orbit 042640, captured from the ``s3p-session`` index. It is a
nominal session: the 11 level 0 product types are all there and each of them
covers a bit more than the sensing expected of an orbit.

The document was consolidated before the sensing completeness existed, so it
carries the granule KPI but none of the completeness ones. That makes it a
double check: recomputing the KPI from the raw granules must give back exactly
the values production stored, and the completeness that comes on top must read
100% everywhere.
"""

from unittest.mock import patch

import pytest

from data.s3p_session_data_test import S3B_SESSION_042640

from maas_cds.lib.config_manager import MaasConfigManager
from maas_cds.model.cds_s3_completeness import CdsS3Completeness
from maas_cds.model.configuration import MaasConfigCompletenessS3
from maas_cds.model.s3p_session import S3pSession

# One orbit of sensing, in microseconds, for the two families of S3 instruments
EXPECTED_101 = 101 * 60 * 1000000

EXPECTED_44 = 44 * 60 * 1000000

# The product types the session carries, with the sensing each of them covers.
# Computed by hand from the granule validity periods of the document: the
# granules of a product type are contiguous, so it is the span between the
# earliest validitystart and the latest validitystop.
#
# All of them exceed the expected orbit sensing, which is exactly what a
# nominal session looks like: the acquisition overlaps the neighbour orbits.
SENSING_VALUES = {
    # 01:16:50 -> 02:59:22, a single granule per orbit
    "DO_0_DOP___": (6152000000, EXPECTED_101),
    # 01:16:52 -> 02:59:12
    "DO_0_NAV___": (6140000000, EXPECTED_101),
    # 01:16:50 -> 02:59:22
    "GN_0_GNS___": (6152000000, EXPECTED_101),
    # 01:16:50 -> 02:59:22
    "MW_0_MWR___": (6152000000, EXPECTED_101),
    # 02:00:45 -> 02:57:19, out of the completeness configuration: the expected
    # value comes from S3pSession.SESSION_SENSING_IN_MINUTES
    "OL_0_CR____": (3394000000, EXPECTED_44),
    # 01:16:19 -> 02:00:45 in 23 granules
    "OL_0_EFR___": (2666000000, EXPECTED_44),
    # 01:09:39 -> 02:51:50 in 20 granules
    "SL_0_SLT___": (6131000000, EXPECTED_101),
    # 01:14:39 -> 02:56:50 in 11 granules
    "SR_0_SRA___": (6131000000, EXPECTED_101),
    # 01:16:51 -> 02:59:22
    "TM_0_HKM2__": (6151000000, EXPECTED_101),
    # 01:16:50 -> 02:59:22, the granule triggering the missing orbit detection
    "TM_0_HKM___": (6152000000, EXPECTED_101),
    # 01:16:51 -> 02:59:22, in 2 overlapping granules counted once
    "TM_0_NAT___": (6151000000, EXPECTED_101),
}


@pytest.fixture(autouse=True)
def completeness_s3_configuration():
    """Load the deployed S3 completeness configuration in the manager singleton

    Built from the product types of :class:`CdsS3Completeness` rather than from
    a stub of their own, so this test cannot drift from the sensing values the
    rest of the S3 completeness uses.
    """
    MaasConfigManager.CACHE.pop("MaasConfigCompletenessS3", None)

    records = [
        {
            "product_type": product_type,
            "timeliness": timeliness,
            "sensing_in_minutes": properties["sensing"],
        }
        for product_type, properties in CdsS3Completeness.S3_PRODUCTS_TYPES.items()
        if properties["active"]
        for timeliness in properties["timeliness"]
    ]

    with patch(
        "maas_cds.model.configuration.MaasConfigCompletenessS3.load"
    ) as mock_load:
        mock_load.return_value = [
            MaasConfigCompletenessS3(
                **{"latest": True, "key": "v1", "records": records}
            )
        ]

        MaasConfigManager(config_model_class=[MaasConfigCompletenessS3()])

        yield

    MaasConfigManager.CACHE.pop("MaasConfigCompletenessS3", None)


@pytest.fixture
def session():
    """The consolidated session, as read back from the index"""
    document = S3pSession(**S3B_SESSION_042640)
    document.meta.id = S3B_SESSION_042640["downlink_session"]

    return document


def test_session_is_the_expected_document(session):
    """Guard on the fixture: the identity the rest of the tests rely on"""
    assert session.satellite_id == "S3B"
    assert session.downlink_orbit == "042640"
    assert len(session.l0pp_granules) == 63


def test_expected_product_types(session):
    """A session is expected to carry the level 0 product types, and only those"""
    assert session.expected_product_types() == sorted(SENSING_VALUES)


def test_compute_kpi_reproduces_the_granule_kpi(session):
    """Recomputing the KPI gives back what production stored in the document"""
    session.compute_kpi()

    # every granule reached EUM
    assert session.delivery_to_eum_completeness == 1

    # latest delivery to EUM (03:15:06.447) against the acquisition stop
    assert session.delivery_to_eum_timeliness == 499.447

    # ... and against the acquisition start
    assert session.delivery_to_eum_timeliness_from_acq_start == 952.62

    # latest raw data generation (02:59:22) against the acquisition boundaries
    assert session.generation_timeliness_from_acq_start == 8.173
    assert session.generation_timeliness_from_acq_stop == -445


def test_compute_kpi_leaves_the_qrt_kpi_unset(session):
    """No granule of this session was circulated over the QRT flux"""
    session.compute_kpi()

    assert not [key for key in session.to_dict() if key.endswith("_qrt")]


@pytest.mark.parametrize(
    "product_type, sensing_value, expected_value",
    [(product_type, *values) for product_type, values in SENSING_VALUES.items()],
)
def test_local_completeness(session, product_type, sensing_value, expected_value):
    """Each product type covers the sensing expected of an orbit"""
    session.compute_kpi()

    values = {
        name: getattr(session, f"{product_type}_local_{name}")
        for name in ("value", "expected", "value_adjusted", "percentage", "status")
    }

    assert values == {
        "value": sensing_value,
        "expected": expected_value,
        # the acquisition overlaps the neighbour orbits: the value is capped
        "value_adjusted": expected_value,
        "percentage": 100,
        "status": "Complete",
    }


def test_global_completeness(session):
    """The global completeness aggregates the local ones of every product type"""
    session.compute_kpi()

    total_expected = sum(expected for _, expected in SENSING_VALUES.values())

    assert session.global_value == total_expected
    assert session.global_expected == total_expected
    assert session.global_value_adjusted == total_expected
    assert session.global_percentage == 100
    assert session.global_status == "Complete"


def test_no_completeness_outside_the_expected_product_types(session):
    """The granule flavour of a product type never leaks into the document"""
    session.compute_kpi()

    completeness_types = {
        key.split("_local_")[0] for key in session.to_dict() if "_local_" in key
    }

    assert completeness_types == set(SENSING_VALUES)


def test_session_triggers_the_missing_orbit_detection(session):
    """It holds a TM_0_HKM__G granule, so it looks back for the missing orbits"""
    assert session.is_session_to_check_missing_orbit()


def test_missing_orbit_detection_from_this_session(session):
    """The two orbits between this session and 042637 get a session of their own"""
    previous = S3pSession.from_session_name("S3B_20260703012851042637")
    previous.acquisition_start_time = "2026-07-03T01:28:51.000Z"
    previous.acquisition_stop_time = "2026-07-03T01:36:24.000Z"

    session.compute_kpi()

    with patch.object(S3pSession, "get_previous_orbit_document", return_value=previous):
        missing_sessions = session.generate_missing_sessions()

    assert [missing.meta.id for missing in missing_sessions] == [
        "S3B_MISSING_______042639",
        "S3B_MISSING_______042638",
    ]

    for missing in missing_sessions:
        assert missing.is_missing is True
        assert missing.satellite_id == "S3B"

        # the very product types the real session reads 100% for read 0% here
        assert missing.TM_0_HKM____local_percentage == 0
        assert missing.global_status == "Missing"

    # spread over the 3 orbits between the two acquisition starts (01:28:51 ->
    # 02:59:13.827), so a step of 1807.609 s
    assert [
        missing.acquisition_start_time.isoformat() for missing in missing_sessions
    ] == [
        "2026-07-03T02:29:06.218000+00:00",
        "2026-07-03T01:58:58.609000+00:00",
    ]
