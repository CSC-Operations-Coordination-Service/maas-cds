"""The 2.22.0 migration: the AIS datatakes back, the AI_RAW attached to them

Built on the S1D orbit 3688 (2026-07-15): the report of 2026-07-15 deleted the
AIS datatakes 3688000..3688003 of the previous one, the AI_RAW sensed there had
no datatake.
"""

import argparse
from unittest.mock import MagicMock, patch

import pytest
from maas_model import datestr_to_utc_datetime

from maas_cds.model import (
    CdsAiProductionCompleteness,
    CdsDatatakeS1,
    CdsProductS1,
    MpProduct,
)
from maas_cds.update.patch_2_22_0 import PatchV2_22_0

PREVIOUS_REPORT = "S1D_MP_ACQ__L0__20260714T173851_20260726T194506.csv"
REPORT = "S1D_MP_ACQ__L0__20260715T175452_20260727T193402.csv"


class FakeSearch:
    """A search returning the given documents"""

    def __init__(self, documents):
        self.documents = documents

    def filter(self, *args, **kwargs):
        return self

    def params(self, **kwargs):
        return self

    def source(self, *args, **kwargs):
        return self

    def count(self):
        return len(self.documents)

    def scan(self):
        return iter(self.documents)


def build_datatake(datatake_id, start, stop, timeliness="NTC"):
    datatake = CdsDatatakeS1(
        key=f"S1D-{datatake_id}",
        datatake_id=datatake_id,
        satellite_unit="S1D",
        mission="S1",
        instrument_mode="AIS",
        timeliness=timeliness,
        observation_time_start=datestr_to_utc_datetime(start),
        observation_time_stop=datestr_to_utc_datetime(stop),
    )
    datatake.meta.id = datatake.key
    return datatake


def build_mp_product(datatake_id, start):
    return MpProduct(
        satellite_id="S1D",
        datatake_id=datatake_id,
        instrument_mode="AIS",
        timeliness="NTC",
        reportName=PREVIOUS_REPORT,
        observation_time_start=datestr_to_utc_datetime(start),
    )


def build_ai_raw(start, end, datatake_id="______", timeliness="_"):
    name = (
        f"S1D_AI_RAW__0____{start[:4]}{start[5:7]}{start[8:13]}{start[14:16]}"
        f"{start[17:19]}_00000000T000000_003688________0000.SAFE.zip"
    )
    product = CdsProductS1(
        name=name,
        product_type="AI_RAW__0_",
        mission="S1",
        satellite_unit="S1D",
        datatake_id=datatake_id,
        timeliness=timeliness,
        sensing_start_date=datestr_to_utc_datetime(start),
        sensing_end_date=datestr_to_utc_datetime(end),
    )
    product.meta.id = f"id-{name}"
    product.meta.index = "cds-product-2026-07"
    # as returned by a search with seq_no_primary_term
    product.meta.seq_no = 12
    product.meta.primary_term = 3
    return product


# AIS datatakes of the S1D orbits 3687..3689 on 2026-07-15
DATATAKE_3687001 = ("3687001", "2026-07-15T17:50:35.059Z", "2026-07-15T18:32:51.684Z")
DATATAKE_3688002 = ("3688002", "2026-07-15T19:02:03.192Z", "2026-07-15T19:19:30.307Z")
DATATAKE_3689000 = ("3689000", "2026-07-15T21:07:39.834Z", "2026-07-15T22:08:54.697Z")


def migrator(dry_run=False):
    patch_ = PatchV2_22_0(argparse.Namespace(dry_run=dry_run, es_timeout=30))
    patch_.engine = MagicMock()
    return patch_


@pytest.fixture
def ais_migrator():
    patch_ = migrator()
    patch_.get_ais_satellite_units = MagicMock(return_value=["S1D"])
    patch_.get_report_names = MagicMock(return_value=[PREVIOUS_REPORT, REPORT])
    patch_.get_first_datatake_start = MagicMock(
        return_value=datestr_to_utc_datetime("2025-11-14T00:00:00.000Z")
    )
    patch_.engine.get_ais_to_recreate.return_value = [
        build_mp_product("3688002", "2026-07-15T19:02:03.192Z"),
        build_mp_product("3688002", "2026-07-15T19:02:03.192Z"),
        # test mission planning before the unit flew
        build_mp_product("3513000", "2025-07-03T18:32:54.934Z"),
    ]
    patch_.engine.consolidate_CdsDatatake_from_MpProduct.side_effect = (
        lambda mp_product: build_datatake(mp_product.datatake_id, *DATATAKE_3688002[1:])
    )
    return patch_


def test_the_ais_datatakes_deleted_by_a_report_are_recreated(ais_migrator):
    actions = list(ais_migrator.ais_datatake_iterator())

    assert [action["_id"] for action in actions] == ["S1D-3688002"]
    ais_migrator.engine.get_ais_to_recreate.assert_called_once_with(
        "S1D", REPORT, PREVIOUS_REPORT
    )
    assert ais_migrator.datatakes_to_compute == {"S1D-3688002"}


def test_dry_run_recreates_in_memory_only(ais_migrator):
    ais_migrator.args.dry_run = True

    assert not list(ais_migrator.ais_datatake_iterator())
    assert list(ais_migrator.recreated["S1D"]) == ["3688002"]


def run_ai_raw(patch_, products, datatakes):
    patch_.load_ais_datatakes = MagicMock(
        return_value={"S1D": [build_datatake(*datatake) for datatake in datatakes]}
    )
    with patch.object(CdsProductS1, "search", return_value=FakeSearch(products)):
        return list(patch_.ai_raw_iterator())


def test_the_ai_raw_without_datatake_is_attached():
    patch_ = migrator()
    product = build_ai_raw("2026-07-15T19:02:07.003Z", "2026-07-15T19:19:33.871Z")

    actions = run_ai_raw(
        patch_, [product], [DATATAKE_3687001, DATATAKE_3688002, DATATAKE_3689000]
    )

    assert len(actions) == 1
    assert actions[0]["_id"] == product.meta.id
    assert actions[0]["if_seq_no"] == 12
    assert {
        field: actions[0]["doc"][field] for field in ("datatake_id", "timeliness")
    } == {"datatake_id": "3688002", "timeliness": "NTC"}
    assert patch_.datatakes_to_compute == {"S1D-3688002"}
    assert patch_.ai_raw_datatakes[product.name] == ("3688002", "NTC")


def test_the_ai_raw_moving_computes_both_datatakes():
    patch_ = migrator()
    # attached to 3687001 while 3688002 was missing
    product = build_ai_raw(
        "2026-07-15T19:02:07.003Z",
        "2026-07-15T19:19:33.871Z",
        datatake_id="3687001",
        timeliness="NTC",
    )

    actions = run_ai_raw(patch_, [product], [DATATAKE_3687001, DATATAKE_3688002])

    assert actions[0]["doc"]["datatake_id"] == "3688002"
    assert "timeliness" not in actions[0]["doc"]
    assert patch_.datatakes_to_compute == {"S1D-3687001", "S1D-3688002"}


def test_the_ai_raw_in_line_or_without_datatake_is_left():
    patch_ = migrator()
    in_line = build_ai_raw(
        "2026-07-15T19:02:07.003Z",
        "2026-07-15T19:19:33.871Z",
        datatake_id="3688002",
        timeliness="NTC",
    )
    # no AIS planned on the S1D orbit 3477 (2026-07-01)
    alone = build_ai_raw("2026-07-01T20:18:56.562Z", "2026-07-01T20:31:21.374Z")

    assert not run_ai_raw(patch_, [in_line, alone], [DATATAKE_3688002])
    assert not patch_.datatakes_to_compute
    assert patch_.ai_raw_datatakes[alone.name] == ("______", "_")


def test_the_ai_completeness_gets_the_datatake_of_its_ai_raw():
    patch_ = migrator()
    patch_.ai_raw_datatakes = {
        "attached.SAFE.zip": ("3688002", "NTC"),
        "alone.SAFE.zip": ("______", "_"),
    }

    documents = []
    for input_name, datatake_id in (
        ("attached.SAFE.zip", "______"),
        ("alone.SAFE.zip", "______"),
        ("unknown.SAFE.zip", "______"),
    ):
        document = CdsAiProductionCompleteness(
            input_name=input_name, datatake_id=datatake_id, timeliness="_"
        )
        document.meta.id = input_name
        document.meta.index = "cds-ai-production-completeness"
        document.meta.seq_no = 4
        document.meta.primary_term = 1
        documents.append(document)

    with patch.object(
        CdsAiProductionCompleteness, "search", return_value=FakeSearch(documents)
    ):
        actions = list(patch_.ai_completeness_iterator())

    assert [action["_id"] for action in actions] == ["attached.SAFE.zip"]
    assert {
        field: actions[0]["doc"][field] for field in ("datatake_id", "timeliness")
    } == {"datatake_id": "3688002", "timeliness": "NTC"}


def test_dry_run_computes_no_datatake():
    patch_ = migrator(dry_run=True)
    patch_.datatakes_to_compute = {"S1D-3688002"}

    with patch.object(CdsDatatakeS1, "search") as search:
        assert not list(patch_.datatake_iterator())

    search.assert_not_called()


def test_the_recreated_datatakes_are_candidates_in_dry_run():
    patch_ = migrator(dry_run=True)
    patch_.recreated = {"S1D": {"3688002": build_datatake(*DATATAKE_3688002)}}

    with patch.object(
        CdsDatatakeS1,
        "search",
        return_value=FakeSearch(
            [build_datatake(*DATATAKE_3689000), build_datatake(*DATATAKE_3687001)]
        ),
    ):
        datatakes = patch_.load_ais_datatakes()

    assert [datatake.datatake_id for datatake in datatakes["S1D"]] == [
        "3687001",
        "3688002",
        "3689000",
    ]
