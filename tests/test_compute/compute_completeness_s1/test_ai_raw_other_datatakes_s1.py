"""An AIS datatake counts the AI_RAW attached to its neighbours (prod cases)

Values from the prod cds-datatake on 2026-11-25: 19 of the 21 S1 AIS datatakes
below 50 % from 2026-11-11 to 2026-11-24 were covered by an AI_RAW attached to
the datatake next to them.
"""

from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock, patch

from maas_cds.engines.compute.compute_completeness import ComputeCompletenessEngine

from maas_cds.lib.periodutils import (
    Period,
    compute_uncovered_sensing,
    merge_periods,
)
from maas_cds.model import CdsDatatakeS1, CdsProductS1
from maas_cds.model import datatake as datatake_module
from maas_cds.model import datatake_s1

# the class the S1 datatake searches the neighbours with
CdsProduct = datatake_s1.CdsProductWithDeletion

SERVICE = ["PRIP_S1C_Werum"]


def _date(value):
    return datetime.strptime(value, "%Y%m%dT%H%M%S.%f").replace(tzinfo=UTC)


def _ais(datatake_id, start, stop, timeliness, satellite_unit="S1C"):
    datatake = CdsDatatakeS1(
        key=f"{satellite_unit}-{datatake_id}",
        datatake_id=datatake_id,
        satellite_unit=satellite_unit,
        mission="S1",
        instrument_mode="AIS",
        timeliness=timeliness,
        observation_time_start=_date(start),
        observation_time_stop=_date(stop),
    )
    datatake.meta.id = datatake.key
    return datatake


def _ai_raw(start, end, datatake_id, timeliness, satellite_unit="S1C", **fields):
    sensing_start, sensing_end = _date(f"{start}.0"), _date(f"{end}.0")
    return CdsProductS1(
        name=f"{satellite_unit}_AI_RAW__0____{start}_{end}_000000________0000.SAFE.zip",
        product_type="AI_RAW__0_",
        mission="S1",
        satellite_unit=satellite_unit,
        datatake_id=datatake_id,
        timeliness=timeliness,
        sensing_start_date=sensing_start,
        sensing_end_date=sensing_end,
        sensing_duration=int((sensing_end - sensing_start).total_seconds() * 1e6),
        prip_id="prip-id",
        prip_service=SERVICE[0],
        **fields,
    )


class FakeSearch:
    """A search returning the given documents, recording its filters"""

    def __init__(self, documents):
        self.documents = documents
        self.filters = []
        self.excludes = []

    def filter(self, *args, **kwargs):
        self.filters.append((args, kwargs))
        return self

    def exclude(self, *args, **kwargs):
        self.excludes.append((args, kwargs))
        return self

    def params(self, **kwargs):
        return self

    def scan(self):
        return iter(self.documents)


def _ai_raw_value(datatake, own, others):
    """compute_local_value of AI_RAW__0_, the brothers and the neighbours given"""
    search = FakeSearch(others)

    with (
        patch.object(CdsDatatakeS1, "find_brother_products_scan", return_value=own),
        patch.object(
            CdsDatatakeS1, "get_service_for_completeness", return_value=SERVICE
        ),
        patch.object(CdsProduct, "search", return_value=search),
    ):
        related = []
        value = datatake.compute_local_value("AI_RAW__0_", related)

    return value, related, search


def _percentage(value, datatake):
    expected = (
        datatake.observation_time_stop - datatake.observation_time_start
    ).total_seconds() * 1e6
    return min(value, expected) / expected * 100


# S1C orbit 10307 / 10308, 2026-11-12, NRT-PT: 1.44 % in prod
S1C_10308001 = ("10308001", "20261112T191322.301", "20261112T191742.175", "NRT-PT")
# the AI_RAW of 10307004 runs to 19:17:41, 10308001 has 3 s of its own
S1C_10307004_AI_RAW = ("20261112T190258", "20261112T191741", "10307004", "NRT-PT")
S1C_10308001_AI_RAW = ("20261112T191741", "20261112T191744", "10308001", "NRT-PT")


def test_merge_periods_keeps_a_period_inside_another():
    hour = timedelta(hours=1)
    start = _date("20261112T000000.0")

    assert merge_periods(
        [
            Period(start, start + 4 * hour),
            Period(start + hour, start + 2 * hour),
            Period(start + 3 * hour, start + 6 * hour),
            Period(start + 8 * hour, start + 9 * hour),
        ]
    ) == [Period(start, start + 6 * hour), Period(start + 8 * hour, start + 9 * hour)]


def test_uncovered_sensing_leaves_out_what_is_covered():
    hour = timedelta(hours=1)
    start = _date("20261112T000000.0")

    assert (
        compute_uncovered_sensing(
            [Period(start, start + 4 * hour)],
            [
                Period(start + hour, start + 2 * hour),
                Period(start + 3 * hour, start + 5 * hour),
            ],
        )
        == 2 * 3600 * 1e6
    )


def test_short_datatake_counts_the_ai_raw_of_its_neighbour():
    datatake = _ais(*S1C_10308001)

    value, related, search = _ai_raw_value(
        datatake,
        [_ai_raw(*S1C_10308001_AI_RAW)],
        [_ai_raw(*S1C_10307004_AI_RAW)],
    )

    # 19:13:22.301 -> 19:17:41 from the neighbour, 19:17:41 -> 19:17:44 its own
    assert value == (258.699 + 3) * 1e6
    assert _percentage(value, datatake) == 100
    # the neighbour is no duplicate nor missing period of this datatake
    assert len(related) == 1
    assert (("term",), {"timeliness": "NRT-PT"}) in search.filters
    assert search.excludes == [(("term",), {"datatake_id": "10308001"})]


def test_neighbour_ai_raw_only_adds_what_its_own_leave_uncovered():
    # S1D orbit 5443, 2026-11-13, NTC: 20.09 % in prod. Its own AI_RAW are not in
    # the patch output, one of 419.4 s is assumed from the start
    datatake = _ais(
        "5443000", "20261113T024853.962", "20261113T032342.083", "NTC", "S1D"
    )
    own = _ai_raw("20261113T024854", "20261113T025613", "5443000", "NTC", "S1D")
    neighbour = _ai_raw("20261113T025559", "20261113T035746", "5443001", "NTC", "S1D")

    value, _, _ = _ai_raw_value(datatake, [own], [neighbour])

    # own 02:48:54 -> 02:56:13, then the neighbour to the end of the observation
    assert value == (439 + 1649.083) * 1e6
    # all but the first 38 ms of the observation
    assert round(_percentage(value, datatake), 3) == 99.998


def test_a_real_gap_stays():
    # S1C orbit 10447, 2026-11-22, NRT-PT: 34.58 % in prod, nothing from 08:22:36
    # to 08:27:53. The NRT-PT AI_RAW of 10447000 ends at 08:22:30 and the search
    # leaves it out; the NTC one of 10447002 has another timeliness.
    datatake = _ais("10447001", "20261122T082236.420", "20261122T083036.353", "NRT-PT")
    own = [
        _ai_raw("20261122T082753", "20261122T083035", "10447001", "NRT-PT"),
        _ai_raw("20261122T083035", "20261122T083038", "10447001", "NRT-PT"),
    ]

    value, _, _ = _ai_raw_value(datatake, own, [])

    assert value == 165 * 1e6
    assert round(_percentage(value, datatake), 1) == 34.4


def test_deleted_neighbour_counts_in_the_original_pass_only():
    datatake = _ais(*S1C_10308001)
    deleted = _ai_raw(*S1C_10307004_AI_RAW, nb_dd_deleted=1)

    live, _, _ = _ai_raw_value(datatake, [], [deleted])

    datatake._include_deleted_products = True
    original, _, _ = _ai_raw_value(datatake, [], [deleted])

    assert live == 0
    assert original == 258.699 * 1e6


def test_neighbours_are_searched_as_the_brothers_are():
    # the generated CdsProduct has no deletion trace (dry run on prod, 2026-11-25)
    assert CdsProduct is datatake_module.CdsProduct
    assert hasattr(CdsProduct, "deletion_trace")


def test_other_product_types_do_not_look_at_the_neighbours():
    datatake = _ais(*S1C_10308001)

    with (
        patch.object(CdsDatatakeS1, "find_brother_products_scan", return_value=[]),
        patch.object(CdsProduct, "search") as search,
    ):
        datatake.compute_local_value("IW_RAW__0S")

    search.assert_not_called()


def test_datatake_without_timeliness_counts_its_own_only():
    datatake = _ais(*S1C_10308001)
    datatake.timeliness = None

    with patch.object(CdsProduct, "search") as search:
        assert datatake.get_ai_raw_of_other_datatakes() == []

    search.assert_not_called()


def test_ai_raw_counts_for_the_neighbours_of_its_timeliness():
    product = _ai_raw(*S1C_10307004_AI_RAW)

    assert product.counts_for_other_ai_datatake(_ais(*S1C_10308001))
    # its own datatake
    assert not product.counts_for_other_ai_datatake(
        _ais("10307004", "20261112T190255.343", "20261112T191220.093", "NRT-PT")
    )
    # 10308002 is NTC
    assert not product.counts_for_other_ai_datatake(
        _ais("10308002", "20261112T191745.000", "20261112T194410.000", "NTC")
    )
    # touching is not overlapping
    assert not product.counts_for_other_ai_datatake(
        _ais("10308003", "20261112T191741.000", "20261112T191800.000", "NRT-PT")
    )


def test_compute_key_gives_the_other_datatakes_it_covers():
    product = _ai_raw("20261112T190258", "20261112T191741", "______", "NRT-PT")
    candidates = [
        _ais("10307004", "20261112T190255.343", "20261112T191220.093", "NRT-PT"),
        _ais("10308000", "20261112T191223.343", "20261112T191319.051", "NRT-PT"),
        _ais(*S1C_10308001),
    ]

    with patch("maas_cds.model.product_s1.CdsDatatakeS1.search") as search:
        search.return_value.filter.return_value.filter.return_value.execute.return_value = (
            candidates
        )
        key = product.get_compute_key()

    assert key == ("S1C-10307004", "AI_RAW__0_")
    assert product.get_overlapped_compute_keys() == [
        ("S1C-10308000", "AI_RAW__0_"),
        ("S1C-10308001", "AI_RAW__0_"),
    ]
    # not stored with the product
    assert "overlapped_datatake_ids" not in product.to_dict()


@patch("maas_cds.engines.compute.compute_completeness.MaasConfigManager")
def test_engine_computes_the_datatakes_the_ai_raw_covers(mock_manager):
    """The datatake of the AI_RAW and the neighbours it covers are computed"""
    engine = ComputeCompletenessEngine()
    engine.input_documents = [
        _ai_raw("20261112T190258", "20261112T191741", "10307004", "NRT-PT")
    ]

    def load_datatake_doc(datatake_id, mission=""):
        datatake_doc = MagicMock()
        datatake_doc.impact_other_calculation.return_value = []
        engine.local_cache_datatake[datatake_id] = (datatake_doc, {})

    candidates = [
        _ais("10307004", "20261112T190255.343", "20261112T191220.093", "NRT-PT"),
        _ais("10308000", "20261112T191223.343", "20261112T191319.051", "NRT-PT"),
        _ais(*S1C_10308001),
    ]

    with (
        patch.object(engine, "load_datatake_doc", side_effect=load_datatake_doc),
        patch("maas_cds.model.product_s1.CdsDatatakeS1.search") as search,
    ):
        search.return_value.filter.return_value.filter.return_value.execute.return_value = (
            candidates
        )
        # the AI_RAW does not move: nothing to save
        assert not list(engine.load_compute_keys_from_input_documents())

    assert engine.tuples_to_compute == [
        ("S1C-10307004", "AI_RAW__0_"),
        ("S1C-10308000", "AI_RAW__0_"),
        ("S1C-10308001", "AI_RAW__0_"),
    ]
