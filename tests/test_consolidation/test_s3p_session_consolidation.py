"""End to end consolidation of a S3 downlink session

From the raw logs of the three S3P streams to the document indexed in
``s3p-session``:

- the CADU polling agent gives the acquisition window of the session,
- the thin layer gives the L0PP granules with the sensing they cover,
- the circulation agent gives the CADU chunks, the housekeeping telemetry and
  the delivery of each granule to EUMETSAT.

The test ends on the complete document, granules and completeness included, so
the shape of what the consolidation indexes is readable in one place.

The session is a small one on purpose, with the three outcomes a completeness
can have: two product types cover the whole orbit (Complete), one covers half
of what it should (Partial), and the two remaining expected ones have no
granule at all (Missing).
"""

from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest

import maas_cds.model as model

from maas_cds.engines.reports.s3p_session import S3pSessionConsolidatorEngine
from maas_cds.lib.config_manager import MaasConfigManager
from maas_cds.model.configuration import MaasConfigCompletenessS3
from maas_cds.model.s3p_session import S3pSession
from maas_model.date_utils import datestr_to_utc_datetime as zulu

# S3A_<acquisition timestamp><downlink orbit>
SESSION_NAME = "S3A_20260402064724052724"

TIMELINESS_KEY = f"SVL__DCS_03_{SESSION_NAME}_dat"

# The downlink: 10 minutes of CADU transfer
ACQUISITION_START = "2026-04-02T06:47:24.000Z"

ACQUISITION_STOP = "2026-04-02T06:57:24.000Z"

# Every granule of a session is transferred to EUMETSAT from the same queue, so
# they all start being pushed at the same time
TRANSFER_START = "2026-04-02T07:04:55.000Z"

# One orbit of sensing, in microseconds
EXPECTED_101 = 101 * 60 * 1000000

EXPECTED_44 = 44 * 60 * 1000000

# The middle of the sensing the session carries, 05:06:44 -> 06:47:44: where a
# product type with no granule is placed on the time axis
MISSING_SENSING_DATE = "2026-04-02T05:57:14.000Z"


def compact(timestamp):
    """20260402T050644, the way a S3 product name holds a date"""
    return timestamp[:19].replace("-", "").replace(":", "")


def granule_name(product_type, validitystart, validitystop):
    """Name of a L0PP granule, in the ISIP flavour of the S3 PDGS

    The sensing period is part of the name, so two granules of the same product
    type are two different products.
    """
    return (
        f"S3A_{product_type}_{compact(validitystart)}_{compact(validitystop)}"
        "_20260402T064923_6111______________SVL_O_NR_OPE.ISIP"
    )


# The granules of the session: product type, sensing period, size and the time
# the circulation agent finished pushing them to EUMETSAT.
#
#   TM_0_HKM__G  05:06:44 -> 06:47:44          101 min, the whole orbit
#   TM_0_NAT__G  05:06:44 -> 06:40:00 and
#                06:00:00 -> 06:47:44          101 min once the overlap is dropped
#   OL_0_EFR__G  05:30:00 -> 05:52:00           22 min of the 44 expected
GRANULES = [
    (
        "TM_0_HKM__G",
        "2026-04-02T05:06:44.000Z",
        "2026-04-02T06:47:44.000Z",
        500000,
        "2026-04-02T07:05:00.000Z",
    ),
    (
        "TM_0_NAT__G",
        "2026-04-02T05:06:44.000Z",
        "2026-04-02T06:40:00.000Z",
        1500000,
        "2026-04-02T07:05:10.000Z",
    ),
    (
        "TM_0_NAT__G",
        "2026-04-02T06:00:00.000Z",
        "2026-04-02T06:47:44.000Z",
        2500000,
        "2026-04-02T07:05:20.000Z",
    ),
    (
        "OL_0_EFR__G",
        "2026-04-02T05:30:00.000Z",
        "2026-04-02T05:41:00.000Z",
        6500000,
        "2026-04-02T07:06:00.000Z",
    ),
    (
        "OL_0_EFR__G",
        "2026-04-02T05:41:00.000Z",
        "2026-04-02T05:52:00.000Z",
        9500000,
        "2026-04-02T07:06:30.000Z",
    ),
]

HKRAW_NAME = "S3A_OPER__HK__RAW___20260402T064733_20260402T065339_O52724_0001.TGZ"

CADU_NAMES = [
    f"DCS_03_{SESSION_NAME}_ch1_DSIB.xml",
    f"DCS_03_{SESSION_NAME}_ch2_DSIB.xml",
]


# The document the consolidation indexes, once the three streams are in.
#
# Sensing, in seconds:      value  expected
#   TM_0_HKM___              6060      6060   the whole orbit, one granule
#   TM_0_NAT___              6060      6060   two overlapping granules, counted once
#   OL_0_EFR___              1320      2640   half of what an orbit carries
#   SR_0_SRA___                 0      6060   expected, but no granule
#   OL_0_CR____                 0      2640   idem, sensing declared by the session
#                           -----     -----
#   global                  13440     23460   57.29%
EXPECTED_DOCUMENT = {
    # identity, derived from the session name alone
    "downlink_session": SESSION_NAME,
    "satellite_id": "S3A",
    "downlink_orbit": "052724",
    # the CADU transfer window, from the CADU polling agent
    "acquisition_start_time": ACQUISITION_START,
    "acquisition_stop_time": ACQUISITION_STOP,
    "timeliness_key": TIMELINESS_KEY,
    # the CADU chunks, from the circulation agent
    "cadu_files": [
        {
            "cadu_name": CADU_NAMES[0],
            "cadu_delivery_in": "2026-04-02T06:58:00.000Z",
            # the delivery out of a known chunk lands on the session below
            "cadu_delivery_out": None,
        },
        {
            "cadu_name": CADU_NAMES[1],
            "cadu_delivery_in": "2026-04-02T06:58:00.000Z",
            "cadu_delivery_out": None,
        },
    ],
    "cadu_delivery_out": "2026-04-02T06:58:30.000Z",
    # the housekeeping telemetry, delivered to EUMETSAT on its own
    "hkraw_name": HKRAW_NAME,
    "hkraw_size": 3500070,
    "hkraw_delivery_time": "2026-04-02T07:02:00.000Z",
    # the granule KPI: all 5 granules delivered, the last one at 07:06:30
    "delivery_to_eum_completeness": 1.0,
    "delivery_to_eum_timeliness": 546.0,
    "delivery_to_eum_timeliness_from_acq_start": 1146.0,
    # the last raw data generation is the end of the sensing, before the
    # downlink is even over: from the acquisition stop it reads negative
    "generation_timeliness_from_acq_start": 20.0,
    "generation_timeliness_from_acq_stop": -580.0,
    # the sensing completeness, one entry per product type expected in a
    # session, ordered by product type
    "completeness": [
        {
            "product_type": "OL_0_CR____",
            "value": 0,
            "expected": EXPECTED_44,
            "value_adjusted": 0,
            "percentage": 0.0,
            "status": "Missing",
            # no granule: a period of no duration, in the middle of the
            # 05:06:44 -> 06:47:44 the session carries
            "sensing_start_date": MISSING_SENSING_DATE,
            "sensing_stop_date": MISSING_SENSING_DATE,
        },
        {
            "product_type": "OL_0_EFR___",
            "value": 1320000000,
            "expected": EXPECTED_44,
            "value_adjusted": 1320000000,
            "percentage": 50.0,
            "status": "Partial",
            "sensing_start_date": "2026-04-02T05:30:00.000Z",
            "sensing_stop_date": "2026-04-02T05:52:00.000Z",
        },
        {
            "product_type": "SR_0_SRA___",
            "value": 0,
            "expected": EXPECTED_101,
            "value_adjusted": 0,
            "percentage": 0.0,
            "status": "Missing",
            "sensing_start_date": MISSING_SENSING_DATE,
            "sensing_stop_date": MISSING_SENSING_DATE,
        },
        {
            "product_type": "TM_0_HKM___",
            "value": 6060000000,
            "expected": EXPECTED_101,
            "value_adjusted": 6060000000,
            "percentage": 100.0,
            "status": "Complete",
            "sensing_start_date": "2026-04-02T05:06:44.000Z",
            "sensing_stop_date": "2026-04-02T06:47:44.000Z",
        },
        {
            "product_type": "TM_0_NAT___",
            "value": 6060000000,
            "expected": EXPECTED_101,
            "value_adjusted": 6060000000,
            "percentage": 100.0,
            "status": "Complete",
            # the two granules overlap: one period, counted once
            "sensing_start_date": "2026-04-02T05:06:44.000Z",
            "sensing_stop_date": "2026-04-02T06:47:44.000Z",
        },
    ],
    # and their aggregate over the session
    "global_value": 13440000000,
    "global_expected": 23460000000,
    "global_value_adjusted": 13440000000,
    "global_percentage": 13440000000 / 23460000000 * 100,
    "global_status": "Partial",
    # the granules, in the order the thin layer logged them
    "l0pp_granules": [
        {
            "product_name": (
                "S3A_TM_0_HKM__G_20260402T050644_20260402T064744"
                "_20260402T064923_6111______________SVL_O_NR_OPE.ISIP"
            ),
            "product_type": "TM_0_HKM__G",
            "timeliness": "NR",
            "flux": "NRT",
            "validitystart": "2026-04-02T05:06:44.000Z",
            "validitystop": "2026-04-02T06:47:44.000Z",
            "raw_data_generation_time": "2026-04-02T06:47:44.000Z",
            "thin_layer_log_date": "2026-04-02T07:00:00.000Z",
            "delivery_start_date_to_eum": "2026-04-02T07:04:55.000Z",
            "delivery_date_to_eum": "2026-04-02T07:05:00.000Z",
            "transfer_duration_to_eum": 5.0,
            "transfer_bandwith_to_eum": 100000.0,
            "filesize": 500000,
        },
        {
            "product_name": (
                "S3A_TM_0_NAT__G_20260402T050644_20260402T064000"
                "_20260402T064923_6111______________SVL_O_NR_OPE.ISIP"
            ),
            "product_type": "TM_0_NAT__G",
            "timeliness": "NR",
            "flux": "NRT",
            "validitystart": "2026-04-02T05:06:44.000Z",
            "validitystop": "2026-04-02T06:40:00.000Z",
            "raw_data_generation_time": "2026-04-02T06:40:00.000Z",
            "thin_layer_log_date": "2026-04-02T07:00:00.000Z",
            "delivery_start_date_to_eum": "2026-04-02T07:04:55.000Z",
            "delivery_date_to_eum": "2026-04-02T07:05:10.000Z",
            "transfer_duration_to_eum": 15.0,
            "transfer_bandwith_to_eum": 100000.0,
            "filesize": 1500000,
        },
        {
            "product_name": (
                "S3A_TM_0_NAT__G_20260402T060000_20260402T064744"
                "_20260402T064923_6111______________SVL_O_NR_OPE.ISIP"
            ),
            "product_type": "TM_0_NAT__G",
            "timeliness": "NR",
            "flux": "NRT",
            "validitystart": "2026-04-02T06:00:00.000Z",
            "validitystop": "2026-04-02T06:47:44.000Z",
            "raw_data_generation_time": "2026-04-02T06:47:44.000Z",
            "thin_layer_log_date": "2026-04-02T07:00:00.000Z",
            "delivery_start_date_to_eum": "2026-04-02T07:04:55.000Z",
            "delivery_date_to_eum": "2026-04-02T07:05:20.000Z",
            "transfer_duration_to_eum": 25.0,
            "transfer_bandwith_to_eum": 100000.0,
            "filesize": 2500000,
        },
        {
            "product_name": (
                "S3A_OL_0_EFR__G_20260402T053000_20260402T054100"
                "_20260402T064923_6111______________SVL_O_NR_OPE.ISIP"
            ),
            "product_type": "OL_0_EFR__G",
            "timeliness": "NR",
            "flux": "NRT",
            "validitystart": "2026-04-02T05:30:00.000Z",
            "validitystop": "2026-04-02T05:41:00.000Z",
            "raw_data_generation_time": "2026-04-02T05:41:00.000Z",
            "thin_layer_log_date": "2026-04-02T07:00:00.000Z",
            "delivery_start_date_to_eum": "2026-04-02T07:04:55.000Z",
            "delivery_date_to_eum": "2026-04-02T07:06:00.000Z",
            "transfer_duration_to_eum": 65.0,
            "transfer_bandwith_to_eum": 100000.0,
            "filesize": 6500000,
        },
        {
            "product_name": (
                "S3A_OL_0_EFR__G_20260402T054100_20260402T055200"
                "_20260402T064923_6111______________SVL_O_NR_OPE.ISIP"
            ),
            "product_type": "OL_0_EFR__G",
            "timeliness": "NR",
            "flux": "NRT",
            "validitystart": "2026-04-02T05:41:00.000Z",
            "validitystop": "2026-04-02T05:52:00.000Z",
            "raw_data_generation_time": "2026-04-02T05:52:00.000Z",
            "thin_layer_log_date": "2026-04-02T07:00:00.000Z",
            "delivery_start_date_to_eum": "2026-04-02T07:04:55.000Z",
            "delivery_date_to_eum": "2026-04-02T07:06:30.000Z",
            "transfer_duration_to_eum": 95.0,
            "transfer_bandwith_to_eum": 100000.0,
            "filesize": 9500000,
        },
    ],
}


@pytest.fixture(autouse=True)
def completeness_s3_configuration():
    """Load a S3 completeness configuration in the manager singleton

    Mirrors the deployed one: TM_0_NAT___ only exists in AL while its granules
    are transferred in NR, and OL_1_EFR___ is a L1 product type a downlink
    session never carries. OL_0_CR____ is deliberately absent: the session
    declares its sensing itself.
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
                            "product_type": "TM_0_HKM___",
                            "timeliness": "NR",
                            "sensing_in_minutes": 101,
                        },
                        {
                            "product_type": "TM_0_NAT___",
                            "timeliness": "AL",
                            "sensing_in_minutes": 101,
                        },
                        {
                            "product_type": "SR_0_SRA___",
                            "timeliness": "NR",
                            "sensing_in_minutes": 101,
                        },
                        {
                            "product_type": "OL_0_EFR___",
                            "timeliness": "NR",
                            "sensing_in_minutes": 44,
                        },
                        {
                            "product_type": "OL_1_EFR___",
                            "timeliness": "NR",
                            "sensing_in_minutes": 44,
                        },
                    ],
                }
            )
        ]

        MaasConfigManager(config_model_class=[MaasConfigCompletenessS3()])

        yield

    MaasConfigManager.CACHE.pop("MaasConfigCompletenessS3", None)


def cadu_polling_documents():
    """The two logs framing the CADU transfer of the session"""
    return [
        model.S3pMetricsRestCaduPollingAgent(
            domain="Data Import",
            code="IMP",
            action="IN",
            filename=SESSION_NAME,
            creationtime=zulu(ACQUISITION_START),
            log_date=zulu(ACQUISITION_START),
        ),
        model.S3pMetricsRestCaduPollingAgent(
            domain="Timeliness",
            code="TL",
            action="REF",
            timelinessKey=TIMELINESS_KEY,
            eventtime=zulu(ACQUISITION_STOP),
            log_date=zulu(ACQUISITION_STOP),
        ),
    ]


def thin_layer_documents():
    """One log per L0PP granule produced for the session"""
    return [
        model.S3pMetricsThinLayer(
            eventname="LOPP",
            timelinessKey=TIMELINESS_KEY,
            filename=granule_name(product_type, validitystart, validitystop),
            validitystart=zulu(validitystart),
            validitystop=zulu(validitystop),
            # the raw data of a granule is complete at the end of its sensing
            generationtime=zulu(validitystop),
            log_date=zulu("2026-04-02T07:00:00.000Z"),
        )
        for product_type, validitystart, validitystop, _, _ in GRANULES
    ]


def circulation_documents():
    """The CADU chunks, the housekeeping telemetry and the granule deliveries"""
    documents = [
        model.S3pMetricsCirculationAgent(
            domain="Data Circulation",
            code="DC",
            action=action,
            filename=cadu_name,
            log_date=log_date,
        )
        for cadu_name in CADU_NAMES
        for action, log_date in (
            ("IN", zulu("2026-04-02T06:58:00.000Z")),
            ("OUT", zulu("2026-04-02T06:58:30.000Z")),
        )
    ]

    documents.append(
        model.S3pMetricsCirculationAgent(
            domain="Data Circulation",
            code="DC",
            action="OUT",
            filename=HKRAW_NAME,
            tourl=f"sftp://s3cgs@vids.eumetsat.int/out/toEUMFOS/S3A/{HKRAW_NAME}",
            filesize=3500070,
            log_date=zulu("2026-04-02T07:02:00.000Z"),
        )
    )

    documents.extend(
        model.S3pMetricsCirculationAgent(
            domain="Data Circulation",
            code="DC",
            action="OUT",
            filename=granule_name(product_type, validitystart, validitystop),
            # the first location of the tourl gives the flux: here the nominal one
            tourl=(
                "ftp://user:password@s3-opeidcs01/data/to_MRN/to_IDC_NEW/from_ACQ"
                f"/High/{granule_name(product_type, validitystart, validitystop)}"
            ),
            filesize=filesize,
            queueid="102978",
            log_date=zulu(delivery_date),
        )
        for product_type, validitystart, validitystop, filesize, delivery_date in GRANULES
    )

    return documents


def search_mock(results):
    """A search chain returning the given results whatever it is filtered on"""
    search = MagicMock()
    search.filter.return_value = search
    search.sort.return_value = search
    search.params.return_value = search
    search.execute.return_value = results

    return search


def consolidate(raw_data_type, raw_documents, session_cache):
    """Run one consolidation pass over a stream of raw logs"""
    S3pSessionConsolidatorEngine.MODEL_MODULE = model

    engine = S3pSessionConsolidatorEngine(raw_data_type=raw_data_type)
    engine.local_session_cache = session_cache
    engine.input_documents = raw_documents

    # a session standing for a missing downlink orbit is another concern
    with patch.object(S3pSession, "get_previous_orbit_document", return_value=None):
        return list(engine.action_iterator())


def build_session():
    """Consolidate the three streams, in the order the logs reach the chain"""
    session_cache = {}

    # the session is new: nothing to read back from the index
    with patch.object(S3pSession, "get_by_id", return_value=None):
        consolidate(
            "S3pMetricsRestCaduPollingAgent", cadu_polling_documents(), session_cache
        )

        consolidate("S3pMetricsThinLayer", thin_layer_documents(), session_cache)

        # the circulation agent resolves the session of a granule through the
        # thin layer log of that granule, and the one of the housekeeping
        # telemetry through the downlink orbit of its name
        with patch.object(
            model.S3pMetricsThinLayer,
            "search",
            return_value=search_mock([SimpleNamespace(timelinessKey=TIMELINESS_KEY)]),
        ), patch.object(
            model.generated.S3pSession,
            "search",
            return_value=search_mock([SimpleNamespace(downlink_session=SESSION_NAME)]),
        ), patch.object(
            model.S3pMetricsCirculationAgent,
            "search",
            return_value=search_mock([SimpleNamespace(log_date=zulu(TRANSFER_START))]),
        ):
            consolidate(
                "S3pMetricsCirculationAgent", circulation_documents(), session_cache
            )

    return session_cache[SESSION_NAME]


@pytest.fixture
def session():
    """The session, consolidated from the raw logs of the three streams"""
    return build_session()


def test_session_identity(session):
    """The session is keyed on its name, and the orbit comes from it"""
    assert session.meta.id == SESSION_NAME
    assert session.downlink_session == SESSION_NAME
    assert session.satellite_id == "S3A"
    assert session.downlink_orbit == "052724"


def test_granules_are_consolidated(session):
    """Every thin layer log gave a granule, and every granule was delivered"""
    assert len(session.l0pp_granules) == len(GRANULES)

    assert [granule.product_type for granule in session.l0pp_granules] == [
        product_type for product_type, _, _, _, _ in GRANULES
    ]

    assert all(granule.delivery_date_to_eum for granule in session.l0pp_granules)


def test_completeness_of_the_session(session):
    """The three outcomes a completeness can have, on one session"""
    # the whole orbit, in one granule and in two overlapping ones
    assert session.completeness_for("TM_0_HKM___")["status"] == "Complete"
    assert session.completeness_for("TM_0_NAT___")["status"] == "Complete"
    assert session.completeness_for("TM_0_NAT___")["value"] == EXPECTED_101

    # half of the 44 minutes expected
    assert session.completeness_for("OL_0_EFR___")["percentage"] == 50
    assert session.completeness_for("OL_0_EFR___")["status"] == "Partial"

    # expected in a session, but no granule carried them: a period of no
    # duration keeps them on the time axis next to the others
    for product_type in ("SR_0_SRA___", "OL_0_CR____"):
        entry = session.completeness_for(product_type)

        assert entry["status"] == "Missing"
        assert entry["sensing_start_date"] == MISSING_SENSING_DATE
        assert entry["sensing_stop_date"] == MISSING_SENSING_DATE

    # the L1 product type of the configuration is not part of a session
    assert session.completeness_for("OL_1_EFR___") is None


def test_consolidated_document(session):
    """The complete document the consolidation indexes"""
    document = session.to_dict()

    # stamped at consolidation time, so not part of the expected document
    assert document.pop("updateTime")

    assert document == EXPECTED_DOCUMENT
