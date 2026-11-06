"""Tests for the AIS datatakes recreated after the MP consolidation"""

from datetime import datetime, UTC
from unittest.mock import MagicMock, patch

import pytest

import maas_cds.model as model

from maas_cds.engines.reports.post_consolidate_mp_file import (
    PostConsolidateMpFileEngine,
)

PREVIOUS_REPORT = "S1D_MP_ACQ__L0__20260714T173851_20260726T194506.csv"
CURRENT_REPORT = "S1D_MP_ACQ__L0__20260715T175452_20260727T193402.csv"
NEXT_REPORT = "S1D_MP_ACQ__L0__20260716T172342_20260728T193113.csv"

# S1D_MP_ACQ__L0__20260715T175452 on prod: first row at 17:55:04, first AIS
# row at 21:07:39, so 3688000..3688003 of the previous report were lost
REPORT_START = datetime(2026, 7, 15, 17, 55, 4, tzinfo=UTC)
REPORT_AIS_START = datetime(2026, 7, 15, 21, 7, 39, tzinfo=UTC)
NEXT_REPORT_START = datetime(2026, 7, 16, 17, 26, 53, tzinfo=UTC)


def _mp_product(datatake_id):
    return model.MpProduct(
        satellite_id="S1D",
        datatake_id=datatake_id,
        instrument_mode="AIS",
        reportName=PREVIOUS_REPORT,
    )


@pytest.fixture
def engine():
    with patch(
        "maas_cds.engines.reports.post_consolidate_mp_file.MaasConfigManager"
    ), patch("maas_cds.engines.reports.post_consolidate_mp_file.OcsConfigCompleteness"):
        PostConsolidateMpFileEngine.MODEL_MODULE = model
        engine = PostConsolidateMpFileEngine(
            raw_data_type="MpProduct",
            consolidated_data_type="CdsDatatake",
            raw_data_time_start_field_name="observation_time_start",
        )

    engine.consolidated_data = MagicMock()
    engine.raw_data = MagicMock()
    return engine


def _report_dates(ais_start=REPORT_AIS_START, next_start=None):
    dates = {
        (CURRENT_REPORT, None): REPORT_START,
        (CURRENT_REPORT, "AIS"): ais_start,
        (NEXT_REPORT, None): next_start,
    }
    return lambda report_name, instrument_mode=None: dates[
        (report_name, instrument_mode)
    ]


def _recreate(engine, raw_rows, existing_ids=(), ais_start=REPORT_AIS_START):
    engine.get_report_min_date = MagicMock(side_effect=_report_dates(ais_start))
    engine.get_next_report = MagicMock(return_value=None)

    raw_search = engine.raw_data.search.return_value.filter.return_value
    raw_search.filter.return_value.filter.return_value.params.return_value.scan.return_value = (
        raw_rows
    )

    existing_search = (
        engine.consolidated_data.search.return_value.filter.return_value.filter.return_value
    )
    existing_search.source.return_value.params.return_value.scan.return_value = [
        model.CdsDatatake(datatake_id=datatake_id) for datatake_id in existing_ids
    ]

    return engine.get_ais_to_recreate("S1D", CURRENT_REPORT, PREVIOUS_REPORT)


def test_ais_rows_before_the_first_ais_of_the_report_are_recreated(engine):
    rows = [_mp_product(datatake_id) for datatake_id in ("3688000", "3688003")]

    assert _recreate(engine, rows) == rows

    # between the first row of the report and its first AIS row
    range_filter = (
        engine.raw_data.search.return_value.filter.return_value.filter.return_value.filter
    )
    range_filter.assert_called_once_with(
        "range",
        observation_time_start={"gte": REPORT_START, "lt": REPORT_AIS_START},
    )


def test_ais_datatakes_already_there_are_kept(engine):
    rows = [_mp_product(datatake_id) for datatake_id in ("3688000", "3688003")]

    assert _recreate(engine, rows, existing_ids=["3688000"]) == [rows[1]]


def test_report_without_ais_recreates_nothing(engine):
    assert not _recreate(engine, [_mp_product("3688000")], ais_start=None)
    engine.raw_data.search.assert_not_called()


def test_report_consolidated_again_stops_at_the_next_report(engine):
    engine.get_report_min_date = MagicMock(
        side_effect=_report_dates(
            ais_start=datetime(2026, 7, 17, tzinfo=UTC), next_start=NEXT_REPORT_START
        )
    )
    engine.get_next_report = MagicMock(return_value=NEXT_REPORT)

    engine.get_ais_to_recreate("S1D", CURRENT_REPORT, PREVIOUS_REPORT)

    range_filter = (
        engine.raw_data.search.return_value.filter.return_value.filter.return_value.filter
    )
    range_filter.assert_called_once_with(
        "range",
        observation_time_start={"gte": REPORT_START, "lt": NEXT_REPORT_START},
    )


@pytest.fixture
def report_engine(engine):
    engine.get_nbr_raw_data_for_report = MagicMock(return_value=1914)
    engine.get_previous_report = MagicMock(return_value=PREVIOUS_REPORT)
    engine.get_ais_to_recreate = MagicMock(
        return_value=[_mp_product("3688000"), _mp_product("3688003")]
    )
    engine.consolidate_CdsDatatake_from_MpProduct = MagicMock(
        side_effect=lambda mp_product: MagicMock(
            **{"to_bulk_action.return_value": mp_product.datatake_id}
        )
    )
    return engine


def _run(engine, report_name):
    engine.input_documents = [report_name]
    return list(engine.action_iterator())


def test_s1d_ais_datatakes_deleted_by_the_report_are_recreated(report_engine):
    assert _run(report_engine, CURRENT_REPORT) == ["3688000", "3688003"]

    report_engine.get_ais_to_recreate.assert_called_once_with(
        "S1D", CURRENT_REPORT, PREVIOUS_REPORT
    )


def test_not_recording_rows_are_not_recreated(report_engine):
    report_engine.consolidate_CdsDatatake_from_MpProduct.side_effect = None
    report_engine.consolidate_CdsDatatake_from_MpProduct.return_value = None

    assert not _run(report_engine, CURRENT_REPORT)


def test_first_report_without_previous_one_is_skipped(report_engine):
    report_engine.get_previous_report.return_value = None

    assert not _run(report_engine, CURRENT_REPORT)
    report_engine.get_ais_to_recreate.assert_not_called()


def test_other_missions_are_not_post_consolidated(report_engine):
    assert not _run(
        report_engine, "S2B_MP_ACQ__L0__20260715T175452_20260727T193402.csv"
    )
    report_engine.get_previous_report.assert_not_called()
