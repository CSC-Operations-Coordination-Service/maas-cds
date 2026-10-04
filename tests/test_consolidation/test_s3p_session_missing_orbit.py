"""Tests of the creation of the sessions of the missing S3 downlink orbits

A downlink session nobody ever heard of is not a session at 0%: it is no
document at all, invisible on the dashboards. The consolidation of a session
holding the trigger product type looks back for the orbits with no session and
creates one for each of them.
"""

import datetime

from unittest.mock import patch

import pytest

import maas_cds.model as model

from maas_cds.engines.reports.s3p_session import S3pSessionConsolidatorEngine
from maas_cds.lib.config_manager import MaasConfigManager
from maas_cds.model.configuration import MaasConfigCompletenessS3
from maas_cds.model.s3p_session import S3pSession, to_datetime

# S3A_<acquisition timestamp><downlink orbit>
SESSION_NAME = "S3A_20260402064724052724"

PREVIOUS_SESSION_NAME = "S3A_20260402053024052721"


def granule(product_type, validitystart, validitystop):
    """A l0pp granule as stored in a session by the consolidation engine"""
    return {
        "product_name": (
            f"S3A_{product_type}_20260402T050553_20260402T064744"
            "_20260402T064923_6111______________SVL_O_NR_OPE.ISIP"
        ),
        "product_type": product_type,
        "validitystart": validitystart,
        "validitystop": validitystop,
    }


@pytest.fixture(autouse=True)
def completeness_s3_configuration():
    """Load a minimal S3 completeness configuration in the manager singleton"""
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
                            "timeliness": "NR",
                            "sensing_in_minutes": 101,
                            "product_type": "TM_0_HKM___",
                        },
                        {
                            "timeliness": "AL",
                            "sensing_in_minutes": 101,
                            "product_type": "TM_0_NAT___",
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
    """A session holding the granule triggering the missing orbit detection"""
    document = S3pSession.from_session_name(SESSION_NAME)
    document.acquisition_start_time = "2026-04-02T06:47:24.000Z"
    document.acquisition_stop_time = "2026-04-02T06:57:24.000Z"
    document.l0pp_granules = [
        granule(
            S3pSession.MISSING_ORBIT_PRODUCT_TYPE,
            "2026-04-02T05:06:44.000Z",
            "2026-04-02T06:47:44.000Z",
        ),
    ]
    return document


@pytest.fixture
def previous_session():
    """The session of the orbit 052721, 3 orbits before the one of the fixture above"""
    document = S3pSession.from_session_name(PREVIOUS_SESSION_NAME)
    document.acquisition_start_time = "2026-04-02T01:44:24.000Z"
    document.acquisition_stop_time = "2026-04-02T01:54:24.000Z"
    return document


def test_session_name_holds_the_downlink_orbit(session):
    """The fixtures use real session names: the orbit is their last 6 characters"""
    assert session.satellite_id == "S3A"
    assert session.downlink_orbit == "052724"


def test_is_session_to_check_missing_orbit(session):
    """Only the session holding the trigger product type looks back"""
    assert session.is_session_to_check_missing_orbit()

    session.l0pp_granules = [
        granule("SR_0_SRA__G", "2026-04-02T06:00:00.000Z", "2026-04-02T06:44:00.000Z"),
    ]

    assert not session.is_session_to_check_missing_orbit()


def test_is_session_to_check_missing_orbit_without_granule(session):
    """A session with no granule yet does not look back"""
    session.l0pp_granules = []

    assert not session.is_session_to_check_missing_orbit()


def test_missing_session_name():
    """The name of a missing session only depends on its downlink orbit"""
    name = S3pSession.missing_session_name("S3A", "052723")

    assert name == "S3A_MISSING_______052723"

    # it has the shape of a real name, so it is parsed the same way
    assert len(name) == len(SESSION_NAME)

    rebuilt = S3pSession.from_session_name(name)

    assert rebuilt.satellite_id == "S3A"
    assert rebuilt.downlink_orbit == "052723"


def test_generate_missing_sessions(session, previous_session):
    """The orbits between the previous session and this one get a session"""
    with patch.object(
        S3pSession, "get_previous_orbit_document", return_value=previous_session
    ):
        missing_sessions = session.generate_missing_sessions()

    assert [missing.downlink_orbit for missing in missing_sessions] == [
        "052723",
        "052722",
    ]

    assert [missing.meta.id for missing in missing_sessions] == [
        "S3A_MISSING_______052723",
        "S3A_MISSING_______052722",
    ]

    for missing in missing_sessions:
        assert missing.is_missing is True
        assert missing.satellite_id == "S3A"
        assert missing.downlink_session == missing.meta.id


def test_generate_missing_sessions_completeness(session, previous_session):
    """A missing session reads 0% for every product type expected in a session"""
    with patch.object(
        S3pSession, "get_previous_orbit_document", return_value=previous_session
    ):
        missing_sessions = session.generate_missing_sessions()

    missing = missing_sessions[0]

    assert missing.completeness_for("TM_0_HKM___")["value"] == 0
    assert missing.completeness_for("TM_0_HKM___")["percentage"] == 0
    assert missing.completeness_for("TM_0_HKM___")["status"] == "Missing"

    assert missing.global_percentage == 0
    assert missing.global_status == "Missing"

    assert missing.delivery_to_eum_completeness == 0


def test_generate_missing_sessions_are_dated(session, previous_session):
    """The missing sessions are spread over the gap, so they are visible in time"""
    with patch.object(
        S3pSession, "get_previous_orbit_document", return_value=previous_session
    ):
        missing_sessions = session.generate_missing_sessions()

    # 3 orbits between the two acquisition starts (01:44:24 -> 06:47:24), so a
    # step of 1h41 between two consecutive sessions
    assert [
        missing.acquisition_start_time.isoformat() for missing in missing_sessions
    ] == [
        "2026-04-02T05:06:24+00:00",
        "2026-04-02T03:25:24+00:00",
    ]

    # and each of them lasts as long as the session that spotted them
    for missing in missing_sessions:
        assert (
            missing.acquisition_stop_time - missing.acquisition_start_time
        ) == datetime.timedelta(minutes=10)


def test_generate_missing_sessions_undated_previous(session, previous_session):
    """Without a date on the previous session, the nominal orbit duration is used"""
    del previous_session.acquisition_start_time
    del previous_session.acquisition_stop_time

    with patch.object(
        S3pSession, "get_previous_orbit_document", return_value=previous_session
    ):
        missing_sessions = session.generate_missing_sessions()

    assert (
        to_datetime(session.acquisition_start_time)
        - missing_sessions[0].acquisition_start_time
    ) == S3pSession.ORBIT_DURATION


def test_generate_missing_sessions_no_gap(session):
    """Consecutive orbits leave nothing to create"""
    previous = S3pSession.from_session_name("S3A_20260402053024052723")

    with patch.object(S3pSession, "get_previous_orbit_document", return_value=previous):
        assert not session.generate_missing_sessions()


def test_generate_missing_sessions_no_previous(session):
    """Nothing to compare with, nothing to create"""
    with patch.object(S3pSession, "get_previous_orbit_document", return_value=None):
        assert not session.generate_missing_sessions()


def test_generate_missing_sessions_is_capped(session):
    """A long gap does not flood the index, the most recent orbits win"""
    previous = S3pSession.from_session_name("S3A_20260401053024050000")
    previous.acquisition_start_time = "2026-03-01T01:44:24.000Z"
    previous.acquisition_stop_time = "2026-03-01T01:54:24.000Z"

    with patch.object(S3pSession, "get_previous_orbit_document", return_value=previous):
        missing_sessions = session.generate_missing_sessions()

    assert len(missing_sessions) == S3pSession.MAX_MISSING_SESSIONS

    # the ones right before the session that spotted them
    assert missing_sessions[0].downlink_orbit == "052723"

    # and the cap does not stretch them over the whole gap: the step stays the
    # one of the 2724 orbits actually missing
    session_start = to_datetime(session.acquisition_start_time)

    step = (session_start - to_datetime(previous.acquisition_start_time)) / 2724

    assert session_start - missing_sessions[0].acquisition_start_time == step

    assert missing_sessions[-1].acquisition_start_time == (
        session_start - S3pSession.MAX_MISSING_SESSIONS * step
    )


def test_generate_missing_sessions_inconsistent_orbit(session, previous_session):
    """An unparseable downlink orbit is reported, not raised"""
    previous_session.downlink_orbit = "5272?"

    with patch.object(
        S3pSession, "get_previous_orbit_document", return_value=previous_session
    ):
        assert not session.generate_missing_sessions()


@pytest.fixture
def engine():
    """The consolidation engine, with no configuration index to reach"""
    S3pSessionConsolidatorEngine.MODEL_MODULE = model

    return S3pSessionConsolidatorEngine(raw_data_type="S3pMetricsThinLayer")


def test_engine_creates_the_missing_sessions(engine, session, previous_session):
    """The consolidation indexes the sessions of the missing downlink orbits"""
    engine.local_session_cache[SESSION_NAME] = session

    with patch.object(
        S3pSession, "get_previous_orbit_document", return_value=previous_session
    ):
        actions = list(engine.missing_sessions_actions())

    assert [action["_id"] for action in actions] == [
        "S3A_MISSING_______052723",
        "S3A_MISSING_______052722",
    ]

    assert all(action["_source"]["is_missing"] is True for action in actions)


def test_engine_skips_the_sessions_without_the_trigger(engine, session):
    """A session without the trigger product type does not look back"""
    session.l0pp_granules = [
        granule("SR_0_SRA__G", "2026-04-02T06:00:00.000Z", "2026-04-02T06:44:00.000Z"),
    ]

    engine.local_session_cache[SESSION_NAME] = session

    with patch.object(S3pSession, "get_previous_orbit_document") as mock_previous:
        assert not list(engine.missing_sessions_actions())

    mock_previous.assert_not_called()


def test_engine_skips_the_orbits_of_the_batch(engine, session, previous_session):
    """A session of this very batch is not indexed yet but is not missing"""
    real_session = S3pSession.from_session_name("S3A_20260402060024052723")

    engine.local_session_cache[SESSION_NAME] = session
    engine.local_session_cache[real_session.meta.id] = real_session

    with patch.object(
        S3pSession, "get_previous_orbit_document", return_value=previous_session
    ):
        actions = list(engine.missing_sessions_actions())

    assert [action["_id"] for action in actions] == ["S3A_MISSING_______052722"]


def test_engine_drops_a_superseded_missing_session(engine):
    """The session of an orbit that turned out to have a real one is deleted"""
    real_session = S3pSession.from_session_name("S3A_20260402060024052723")

    missing_session = S3pSession.from_missing_orbit("S3A", "052723")
    missing_session.meta.index = "s3p-session"

    with patch.object(S3pSession, "get_by_id", return_value=missing_session):
        engine.collect_superseded_missing_session(real_session)

    actions = list(engine.missing_sessions_actions())

    assert actions == [
        {
            "_id": "S3A_MISSING_______052723",
            "_index": "s3p-session",
            "_op_type": "delete",
        }
    ]


def test_engine_keeps_going_without_a_superseded_session(engine):
    """Nothing to drop when the orbit was never reported as missing"""
    real_session = S3pSession.from_session_name("S3A_20260402060024052723")

    with patch.object(S3pSession, "get_by_id", return_value=None):
        engine.collect_superseded_missing_session(real_session)

    assert not engine.superseded_missing_sessions
