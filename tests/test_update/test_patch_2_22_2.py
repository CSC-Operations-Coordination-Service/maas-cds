"""The 2.22.2 migration: the AIS datatakes an AI_RAW of another one covers

Built on the S1C orbits 10307 / 10308 (2026-11-12): the NRT-PT AI_RAW of 10307004
runs to 19:17:41 and covers 10308000 and most of 10308001, at 0 % and 1.44 % in prod.
"""

import argparse
from unittest.mock import patch

from maas_model import datestr_to_utc_datetime

from maas_cds.model import CdsDatatakeS1, CdsProductS1
from maas_cds.update.patch_2_22_2 import PatchV2_22_2


class FakeSearch:
    """A search returning the given documents, recording its filters"""

    def __init__(self, documents):
        self.documents = documents
        self.filters = []

    def filter(self, *args, **kwargs):
        self.filters.append((args, kwargs))
        return self

    def params(self, **kwargs):
        return self

    def source(self, *args, **kwargs):
        return self

    def count(self):
        return len(self.documents)

    def scan(self):
        return iter(self.documents)


def build_datatake(datatake_id, start, stop, timeliness="NRT-PT"):
    datatake = CdsDatatakeS1(
        key=f"S1C-{datatake_id}",
        datatake_id=datatake_id,
        satellite_unit="S1C",
        mission="S1",
        instrument_mode="AIS",
        timeliness=timeliness,
        observation_time_start=datestr_to_utc_datetime(start),
        observation_time_stop=datestr_to_utc_datetime(stop),
    )
    datatake.meta.id = datatake.key
    return datatake


def build_ai_raw(start, end, datatake_id, timeliness="NRT-PT"):
    return CdsProductS1(
        name=f"S1C_AI_RAW__0____{start}.SAFE.zip",
        product_type="AI_RAW__0_",
        mission="S1",
        satellite_unit="S1C",
        datatake_id=datatake_id,
        timeliness=timeliness,
        sensing_start_date=datestr_to_utc_datetime(start),
        sensing_end_date=datestr_to_utc_datetime(end),
        prip_id="prip-id",
    )


DATATAKES = [
    ("10307004", "2026-11-12T19:02:55.343Z", "2026-11-12T19:12:20.093Z"),
    ("10308000", "2026-11-12T19:12:23.343Z", "2026-11-12T19:13:19.051Z"),
    ("10308001", "2026-11-12T19:13:22.301Z", "2026-11-12T19:17:42.175Z"),
    ("10308002", "2026-11-12T19:17:45.000Z", "2026-11-12T19:44:10.000Z", "NTC"),
]


def migrator(dry_run=False):
    return PatchV2_22_2(argparse.Namespace(dry_run=dry_run, es_timeout=30))


def find(products):
    patch_ = migrator()
    patch_.load_ais_datatakes = lambda: {
        "S1C": [build_datatake(*datatake) for datatake in DATATAKES]
    }

    with patch.object(CdsProductS1, "search", return_value=FakeSearch(products)):
        patch_.find_datatakes_to_compute()

    return patch_.datatakes_to_compute


def test_the_datatakes_an_ai_raw_of_another_one_covers_are_found():
    assert find(
        [
            build_ai_raw(
                "2026-11-12T19:02:58.000Z", "2026-11-12T19:17:41.000Z", "10307004"
            ),
            # 3 s on its own datatake
            build_ai_raw(
                "2026-11-12T19:17:41.000Z", "2026-11-12T19:17:44.000Z", "10308001"
            ),
        ]
    ) == {"S1C-10308000", "S1C-10308001"}


def test_an_ai_raw_does_not_cover_a_datatake_of_another_timeliness():
    # the NTC AI_RAW of 10308002 overlaps no NTC neighbour
    assert not find(
        [
            build_ai_raw(
                "2026-11-12T19:17:48.000Z",
                "2026-11-12T19:23:33.000Z",
                "10308002",
                "NTC",
            ),
            build_ai_raw(
                "2026-11-12T19:13:00.000Z",
                "2026-11-12T19:17:00.000Z",
                "10308001",
                "NTC",
            ),
        ]
    )


def test_an_ai_raw_inside_its_datatake_covers_nothing_else():
    assert not find(
        [
            build_ai_raw(
                "2026-11-12T19:03:00.000Z", "2026-11-12T19:12:00.000Z", "10307004"
            )
        ]
    )


def run_datatakes(patch_, datatakes, compute):
    patch_.datatakes_to_compute = {datatake.key for datatake in datatakes}

    with patch.object(
        CdsDatatakeS1, "search", return_value=FakeSearch(datatakes)
    ), patch.object(CdsDatatakeS1, "purge_dynamic_fields"), patch.object(
        CdsDatatakeS1, "load_data_before_compute", return_value=iter(())
    ), patch.object(
        CdsDatatakeS1, "compute_completeness", autospec=True, side_effect=compute
    ) as compute_completeness:
        return list(patch_.datatake_iterator()), compute_completeness


def complete(datatake):
    datatake.AI_RAW__0__local_percentage = 100.0


def test_only_the_changed_datatakes_are_written():
    changed = build_datatake(*DATATAKES[2])
    changed.AI_RAW__0__local_percentage = 1.4387741751772012
    in_line = build_datatake(*DATATAKES[0])
    in_line.AI_RAW__0__local_percentage = 100.0

    actions, _ = run_datatakes(migrator(), [changed, in_line], complete)

    assert [action["_id"] for action in actions] == ["S1C-10308001"]


def test_dry_run_computes_but_writes_nothing():
    datatake = build_datatake(*DATATAKES[1])

    actions, compute = run_datatakes(migrator(dry_run=True), [datatake], complete)

    assert not actions
    compute.assert_called_once()
