"""Tests of the sensing completeness stored in a S3pSession"""

from unittest.mock import patch

import pytest

from maas_cds.lib.config_manager import MaasConfigManager
from maas_cds.model.configuration import MaasConfigCompletenessS3
from maas_cds.model.s3p_session import S3pSession

SESSION_NAME = "SVL__DCS_03_S3A_20260402064724052724_dat"

# Expected values of the configuration below, in microseconds
TM_EXPECTED = 101 * 60 * 1000000

DO_EXPECTED = 101 * 60 * 1000000

SR_EXPECTED = 44 * 60 * 1000000

CR_EXPECTED = 44 * 60 * 1000000

# The product types a downlink session is expected to carry: the level 0 ones of
# the configuration below, plus OL_0_CR____ which is only known by the session
EXPECTED_PRODUCT_TYPES = [
    "DO_0_NAV___",
    "OL_0_CR____",
    "SR_0_SRA___",
    "TM_0_NAT___",
]


def granule(product_type, validitystart, validitystop, timeliness="NR", flux="NRT"):
    """A l0pp granule as stored in a session by the consolidation engine

    All the granules are transferred in NR, whatever the timeliness their
    products are published with.
    """
    return {
        "product_name": (
            f"S3A_{product_type}_20260402T050553_20260402T064744"
            f"_20260402T064923_6111______________SVL_O_{timeliness}_OPE.ISIP"
        ),
        "product_type": product_type,
        "timeliness": timeliness,
        "flux": flux,
        "validitystart": validitystart,
        "validitystop": validitystop,
        "delivery_date_to_eum": validitystop,
    }


@pytest.fixture(autouse=True)
def completeness_s3_configuration():
    """Load a S3 completeness configuration in the manager singleton

    Mirrors the shape of the deployed configuration: TM_0_NAT___ and DO_0_NAV___
    only exist in AL, SR_0_SRA___ has both a NR and a ST record, and the L1
    product types are configured but never part of a downlink session.

    The configuration cache is shared by all the tests of the session: drop the
    S3 entry so this stub is the one used, whatever ran before.
    """
    MaasConfigManager.CACHE.pop("MaasConfigCompletenessS3", None)

    with patch(
        "maas_cds.model.configuration.MaasConfigCompletenessS3.load"
    ) as mock_load:
        mock_load.return_value = [
            MaasConfigCompletenessS3(
                **{
                    "latest": True,
                    "key": "v1",
                    "records": [
                        {
                            "timeliness": "AL",
                            "sensing_in_minutes": 101,
                            "product_type": "TM_0_NAT___",
                        },
                        {
                            "timeliness": "AL",
                            "sensing_in_minutes": 101,
                            "product_type": "DO_0_NAV___",
                        },
                        {
                            "timeliness": "NR",
                            "sensing_in_minutes": 44,
                            "product_type": "SR_0_SRA___",
                        },
                        {
                            # a distinct sensing to spot which record is used
                            "timeliness": "ST",
                            "sensing_in_minutes": 101,
                            "product_type": "SR_0_SRA___",
                        },
                        {
                            "timeliness": "NR",
                            "sensing_in_minutes": 44,
                            "product_type": "OL_1_EFR___",
                        },
                    ],
                }
            )
        ]

        MaasConfigManager(config_model_class=[MaasConfigCompletenessS3()])

        yield

    MaasConfigManager.CACHE.pop("MaasConfigCompletenessS3", None)


@pytest.fixture
def session():
    """An empty session"""
    return S3pSession.from_session_name(SESSION_NAME)


def test_expected_product_types(session):
    """Only the level 0 product types are expected in a session"""
    assert session.expected_product_types() == EXPECTED_PRODUCT_TYPES


def test_completeness_session_only_product_type(session):
    """A product type the session carries without being published as such

    The OL_0_CR granules are published as OL_0_CR0___ / OL_0_CR1___, so the
    completeness configuration holds no OL_0_CR____ record: its nominal sensing
    is the one declared by the session.
    """
    session.l0pp_granules = [
        granule("OL_0_CR___G", "2026-04-02T06:00:00.000Z", "2026-04-02T06:44:00.000Z"),
    ]

    session.compute_completeness(session.l0pp_granules)

    assert session.completeness_for("OL_0_CR____")["expected"] == CR_EXPECTED
    assert session.completeness_for("OL_0_CR____")["percentage"] == 100
    assert session.completeness_for("OL_0_CR____")["status"] == "Complete"


def test_completeness_session_only_product_type_overriden_by_the_config(session):
    """A configuration record wins over the sensing declared by the session"""
    with patch.dict(
        session.SESSION_SENSING_IN_MINUTES, {"SR_0_SRA___": 101}, clear=False
    ):
        session.l0pp_granules = [
            granule(
                "SR_0_SRA__G", "2026-04-02T06:00:00.000Z", "2026-04-02T06:44:00.000Z"
            ),
        ]

        session.compute_completeness(session.l0pp_granules)

    # the 44 minutes of the configuration, not the 101 declared above
    assert session.completeness_for("SR_0_SRA___")["expected"] == SR_EXPECTED


def test_completeness_complete(session):
    """A session whose granules cover the expected sensing is complete

    TM_0_NAT___ is only configured in AL while its granules are transferred in
    NR: the expected value is the one of its only record.
    """
    session.l0pp_granules = [
        # 101 minutes of TM_0_NAT in 2 contiguous granules
        granule("TM_0_NAT__G", "2026-04-02T05:06:44.000Z", "2026-04-02T06:00:00.000Z"),
        granule("TM_0_NAT__G", "2026-04-02T06:00:00.000Z", "2026-04-02T06:47:44.000Z"),
    ]

    session.compute_completeness(session.l0pp_granules)

    assert session.completeness_for("TM_0_NAT___")["value"] == TM_EXPECTED
    assert session.completeness_for("TM_0_NAT___")["expected"] == TM_EXPECTED
    assert session.completeness_for("TM_0_NAT___")["value_adjusted"] == TM_EXPECTED
    assert session.completeness_for("TM_0_NAT___")["percentage"] == 100
    assert session.completeness_for("TM_0_NAT___")["status"] == "Complete"


def test_completeness_partial(session):
    """A session missing sensing is partial"""
    session.l0pp_granules = [
        # half of the expected 44 minutes
        granule("SR_0_SRA__G", "2026-04-02T06:00:00.000Z", "2026-04-02T06:22:00.000Z"),
    ]

    session.compute_completeness(session.l0pp_granules)

    assert session.completeness_for("SR_0_SRA___")["value"] == SR_EXPECTED / 2
    assert session.completeness_for("SR_0_SRA___")["percentage"] == 50
    assert session.completeness_for("SR_0_SRA___")["status"] == "Partial"


def test_completeness_prefers_the_nr_expected_record(session):
    """A product type configured with several timeliness uses its NR record"""
    session.l0pp_granules = [
        granule("SR_0_SRA__G", "2026-04-02T06:00:00.000Z", "2026-04-02T06:44:00.000Z"),
    ]

    session.compute_completeness(session.l0pp_granules)

    # 44 minutes of the NR record, not the 101 of the ST one
    assert session.completeness_for("SR_0_SRA___")["expected"] == SR_EXPECTED
    assert session.completeness_for("SR_0_SRA___")["percentage"] == 100


def test_completeness_missing_product_type(session):
    """A product type absent from the session reads 0%"""
    session.l0pp_granules = [
        granule("TM_0_NAT__G", "2026-04-02T05:06:44.000Z", "2026-04-02T06:47:44.000Z"),
    ]

    session.compute_completeness(session.l0pp_granules)

    assert session.completeness_for("SR_0_SRA___")["value"] == 0
    assert session.completeness_for("SR_0_SRA___")["expected"] == SR_EXPECTED
    assert session.completeness_for("SR_0_SRA___")["percentage"] == 0
    assert session.completeness_for("SR_0_SRA___")["status"] == "Missing"

    assert session.completeness_for("DO_0_NAV___")["status"] == "Missing"


def test_missing_product_type_is_placed_on_the_time_axis(session):
    """A product type with no granule gets a sensing period of no duration

    Without a date nothing places it next to the product types the session did
    carry: the middle of the sensing of the session is used, with the same
    start and stop so it claims no sensing at all.
    """
    session.l0pp_granules = [
        # 101 minutes, 05:06:44 -> 06:47:44
        granule("TM_0_NAT__G", "2026-04-02T05:06:44.000Z", "2026-04-02T06:47:44.000Z"),
    ]

    session.compute_completeness(session.l0pp_granules)

    entry = session.completeness_for("SR_0_SRA___")

    assert entry["value"] == 0
    assert entry["status"] == "Missing"
    assert entry["sensing_start_date"] == "2026-04-02T05:57:14.000Z"
    assert entry["sensing_stop_date"] == entry["sensing_start_date"]


def test_missing_product_type_falls_back_on_the_acquisition(session):
    """A session with no granule at all is placed by its acquisition window"""
    session.acquisition_start_time = "2026-04-02T06:47:24.000Z"
    session.acquisition_stop_time = "2026-04-02T06:57:24.000Z"

    session.compute_completeness([])

    entry = session.completeness_for("TM_0_NAT___")

    assert entry["sensing_start_date"] == "2026-04-02T06:52:24.000Z"
    assert entry["sensing_stop_date"] == entry["sensing_start_date"]


def test_missing_product_type_of_an_undated_session(session):
    """Nothing to place it with, so no date is invented"""
    session.compute_completeness([])

    entry = session.completeness_for("TM_0_NAT___")

    assert entry["status"] == "Missing"
    assert "sensing_start_date" not in entry
    assert "sensing_stop_date" not in entry


def test_completeness_ignores_the_upper_levels(session):
    """The L1 and L2 product types are not part of a session"""
    session.compute_completeness([])

    assert session.completeness_for("OL_1_EFR___") is None


def test_completeness_overlapping_granules(session):
    """Overlapping granules are counted once and the value is adjusted"""
    session.l0pp_granules = [
        # 44 minutes covered twice: the sensing value must not be doubled
        granule("SR_0_SRA__G", "2026-04-02T06:00:00.000Z", "2026-04-02T06:44:00.000Z"),
        granule("SR_0_SRA__G", "2026-04-02T06:10:00.000Z", "2026-04-02T06:44:00.000Z"),
        # and 1 extra minute to exceed the expected value
        granule("SR_0_SRA__G", "2026-04-02T06:44:00.000Z", "2026-04-02T06:45:00.000Z"),
    ]

    session.compute_completeness(session.l0pp_granules)

    assert (
        session.completeness_for("SR_0_SRA___")["value"] == SR_EXPECTED + 60 * 1000000
    )
    assert session.completeness_for("SR_0_SRA___")["value_adjusted"] == SR_EXPECTED
    assert session.completeness_for("SR_0_SRA___")["percentage"] == 100
    assert session.completeness_for("SR_0_SRA___")["status"] == "Complete"


def test_completeness_global(session):
    """The global completeness aggregates the local ones of the session"""
    session.l0pp_granules = [
        # complete TM
        granule("TM_0_NAT__G", "2026-04-02T05:06:44.000Z", "2026-04-02T06:47:44.000Z"),
        # half of the SR
        granule("SR_0_SRA__G", "2026-04-02T06:00:00.000Z", "2026-04-02T06:22:00.000Z"),
        # and no DO_0_NAV nor OL_0_CR granule at all
    ]

    session.compute_completeness(session.l0pp_granules)

    assert (
        session.global_expected == TM_EXPECTED + SR_EXPECTED + DO_EXPECTED + CR_EXPECTED
    )
    assert session.global_value_adjusted == TM_EXPECTED + SR_EXPECTED / 2
    assert session.global_percentage == pytest.approx(
        (TM_EXPECTED + SR_EXPECTED / 2)
        / (TM_EXPECTED + SR_EXPECTED + DO_EXPECTED + CR_EXPECTED)
        * 100
    )
    assert session.global_status == "Partial"


def test_completeness_product_type_out_of_configuration(session):
    """A granule of a product type nothing expects is ignored"""
    session.l0pp_granules = [
        granule("MW_0_MWR__G", "2026-04-02T06:00:00.000Z", "2026-04-02T06:44:00.000Z"),
    ]

    session.compute_completeness(session.l0pp_granules)

    assert session.completeness_for("MW_0_MWR___") is None

    # the expected product types are still reported as missing
    assert session.completeness_for("TM_0_NAT___")["status"] == "Missing"


def test_completeness_legacy_granule_without_timeliness(session):
    """A granule indexed before this computation is still usable

    Such granules hold no timeliness, which is not needed to compute the
    completeness of a session.
    """
    legacy_granule = granule(
        "TM_0_NAT__G", "2026-04-02T05:06:44.000Z", "2026-04-02T06:47:44.000Z"
    )
    del legacy_granule["timeliness"]

    session.l0pp_granules = [legacy_granule]

    assert session.granule_product_type(session.l0pp_granules[0]) == "TM_0_NAT___"

    session.compute_completeness(session.l0pp_granules)

    assert session.completeness_for("TM_0_NAT___")["percentage"] == 100


def test_completeness_unusable_granules(session):
    """Granules without a usable validity period are skipped"""
    no_period = granule("TM_0_NAT__G", None, None)

    inconsistent_period = granule(
        "SR_0_SRA__G", "2026-04-02T06:44:00.000Z", "2026-04-02T06:00:00.000Z"
    )

    session.l0pp_granules = [no_period, inconsistent_period]

    session.compute_completeness(session.l0pp_granules)

    assert session.completeness_for("TM_0_NAT___")["status"] == "Missing"
    assert session.completeness_for("SR_0_SRA___")["status"] == "Missing"
    assert session.global_percentage == 0


def test_compute_kpi_computes_the_completeness(session):
    """The completeness is part of the KPI computed by the consolidation"""
    session.acquisition_start_time = "2026-04-02T05:06:44.000Z"
    session.acquisition_stop_time = "2026-04-02T06:47:44.000Z"
    session.l0pp_granules = [
        granule("TM_0_NAT__G", "2026-04-02T05:06:44.000Z", "2026-04-02T06:47:44.000Z"),
    ]

    session.compute_kpi()

    # the granule count KPI is still computed
    assert session.delivery_to_eum_completeness == 1

    # and the sensing one too
    assert session.completeness_for("TM_0_NAT___")["percentage"] == 100


def test_compute_kpi_without_configuration(session):
    """A missing configuration does not break the other KPI

    Only the product types whose sensing the session declares itself are still
    computed.
    """
    MaasConfigManager.CACHE.pop("MaasConfigCompletenessS3", None)

    session.acquisition_start_time = "2026-04-02T05:06:44.000Z"
    session.acquisition_stop_time = "2026-04-02T06:47:44.000Z"
    session.l0pp_granules = [
        granule("TM_0_NAT__G", "2026-04-02T05:06:44.000Z", "2026-04-02T06:47:44.000Z"),
    ]

    session.compute_kpi()

    assert session.delivery_to_eum_completeness == 1

    assert [entry["product_type"] for entry in session.completeness] == ["OL_0_CR____"]

    assert session.completeness_for("TM_0_NAT___") is None
