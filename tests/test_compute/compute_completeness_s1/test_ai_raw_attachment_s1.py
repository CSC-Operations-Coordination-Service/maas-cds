"""Attachment of the AI_RAW products to their AIS datatake (prod cases)"""

from datetime import datetime, UTC
from unittest.mock import patch

from opensearchpy import Q

from maas_cds import model


def _date(value):
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)


def _ai_raw(satellite_unit, start, end, timeliness="_", datatake_id="______"):
    return model.CdsProductS1(
        name=f"{satellite_unit}_AI_RAW__0____{start}_{end}_000000________0000.SAFE",
        product_type="AI_RAW__0_",
        mission="S1",
        satellite_unit=satellite_unit,
        sensing_start_date=_date(start),
        sensing_end_date=_date(end),
        timeliness=timeliness,
        datatake_id=datatake_id,
    )


def _ais(satellite_unit, datatake_id, start, stop, timeliness):
    datatake = model.CdsDatatakeS1(
        datatake_id=datatake_id,
        satellite_unit=satellite_unit,
        instrument_mode="AIS",
        observation_time_start=_date(start),
        observation_time_stop=_date(stop),
        timeliness=timeliness,
    )
    datatake.meta.id = f"{satellite_unit}-{datatake_id}"
    return datatake


# S1D orbit 3249 / 3250 on 2026-06-15
S1D_3249001 = _ais("S1D", "3249001", "20260615T171000", "20260615T174541", "NRT-PT")
S1D_3250000 = _ais("S1D", "3250000", "20260615T174545", "20260615T175400", "NRT-PT")
S1D_3250001 = _ais("S1D", "3250001", "20260615T175400", "20260615T181000", "NTC")


def _candidates(product, datatakes):
    return [
        datatake for datatake in datatakes if product.is_ai_datatake_candidate(datatake)
    ]


def test_ai_raw_goes_to_the_datatake_it_lies_in():
    # was attached to 3249001, ended 1 s before, as 3250000 was missing
    product = _ai_raw("S1D", "20260615T174542", "20260615T174944", "NRT-PT")

    candidates = _candidates(product, [S1D_3249001, S1D_3250000, S1D_3250001])

    assert candidates == [S1D_3249001, S1D_3250000]
    assert product.get_nearest_datatake(candidates) is S1D_3250000


def test_short_ai_raw_after_its_datatake_keeps_its_timeliness():
    # 1 s of NRT-PT packet store just after the end of the NRT-PT datatake,
    # inside the next NTC one
    product = _ai_raw("S1D", "20260615T174542", "20260615T174543", "NRT-PT")

    candidates = _candidates(product, [S1D_3249001, S1D_3250001])

    assert product.get_nearest_datatake(candidates) is S1D_3249001


def test_ai_raw_spanning_two_datatakes_goes_to_the_larger_overlap():
    # S1D 2026-07-16, the PRIP gives no packet store: no timeliness
    first = _ais("S1D", "3691000", "20260716T000726", "20260716T002413", "NTC")
    second = _ais("S1D", "3691001", "20260716T002419", "20260716T004322", "NTC")
    product = _ai_raw("S1D", "20260716T000730", "20260716T004325")

    candidates = _candidates(product, [first, second])

    assert candidates == [first, second]
    assert product.get_nearest_datatake(candidates) is second


def test_ai_raw_far_from_any_datatake_has_no_candidate():
    product = _ai_raw("S1D", "20260615T190000", "20260615T191000")

    assert not _candidates(product, [S1D_3249001, S1D_3250000, S1D_3250001])


def _compute_key(product, datatakes):
    with patch("maas_cds.model.product_s1.CdsDatatakeS1.search") as search:
        search.return_value.filter.return_value.filter.return_value.execute.return_value = (
            datatakes
        )
        key = product.get_compute_key()

    return key, search.return_value.filter.call_args


def test_compute_key_searches_the_widened_sensing():
    product = _ai_raw("S1D", "20260615T174542", "20260615T174944", "NRT-PT")

    key, search_filter = _compute_key(product, [S1D_3249001, S1D_3250000])

    assert key == ("S1D-3250000", "AI_RAW__0_")
    assert product.datatake_id == "3250000"
    assert product.timeliness == "NRT-PT"
    assert search_filter.args[0] == Q(
        "bool",
        filter=[
            Q("term", satellite_unit="S1D"),
            Q("range", observation_time_start={"lte": _date("20260615T175944")}),
            Q("range", observation_time_stop={"gte": _date("20260615T173542")}),
            Q("term", timeliness="NRT-PT"),
        ],
    )


def test_ai_raw_moving_to_another_datatake_detaches_the_previous_one():
    product = _ai_raw(
        "S1D", "20260615T174542", "20260615T174944", "NRT-PT", datatake_id="3249001"
    )

    _compute_key(product, [S1D_3249001, S1D_3250000])

    assert product.datatake_id == "3250000"
    assert product.get_detached_compute_key() == ("S1D-3249001", "AI_RAW__0_")
    # not stored with the product
    assert "detached_datatake_id" not in product.to_dict()


def test_ai_raw_staying_on_its_datatake_detaches_nothing():
    product = _ai_raw(
        "S1D", "20260615T174542", "20260615T174944", "NRT-PT", datatake_id="3250000"
    )

    _compute_key(product, [S1D_3249001, S1D_3250000])

    assert product.get_detached_compute_key() is None
