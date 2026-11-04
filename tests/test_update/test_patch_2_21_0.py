"""The 2.21.0 migration: the S2C documents follow the shifted mission planning

Built on the S2C orbit 11224 (2026-10-29), as test_datatake_attachment_s2: the
second L0 datastrip and its publications belong to 11224-5, not 11224-4.
"""

import argparse
from unittest.mock import MagicMock, patch

import pytest
from maas_model import datestr_to_utc_datetime

from maas_cds.model import (
    CdsDatatakeS2,
    CdsProduct,
    CdsProductS2,
    CdsPublication,
)
from maas_cds.update.patch_2_21_0 import PatchV2_21_0

GROUP_DS_2 = "GS2C_20261029T212931_011224_N05.13"


class FakeSearch:
    """A search returning the given documents, only filtered by datatake_id"""

    def __init__(self, documents):
        self.documents = documents
        self.search_params = {}

    def filter(self, *args, **kwargs):
        if args and args[0] == "terms" and "datatake_id" in kwargs:
            return FakeSearch(
                [
                    document
                    for document in self.documents
                    if document.datatake_id in kwargs["datatake_id"]
                ]
            )
        return self

    def exclude(self, *args, **kwargs):
        return self

    def params(self, **kwargs):
        self.search_params.update(kwargs)
        return self

    def source(self, *args, **kwargs):
        return self

    def count(self):
        return len(self.documents)

    def scan(self):
        return iter(self.documents)


def build_datatake(datatake_id, start, stop, **kwargs):
    datatake = CdsDatatakeS2(
        key=f"S2C-{datatake_id}",
        datatake_id=datatake_id,
        satellite_unit="S2C",
        mission="S2",
        instrument_mode="NOBS",
        absolute_orbit="11224",
        timeliness="NOMINAL",
        observation_time_start=datestr_to_utc_datetime(start),
        observation_time_stop=datestr_to_utc_datetime(stop),
        **kwargs,
    )
    datatake.meta.id = datatake.key
    datatake.meta.index = "cds-datatake-s1-s2"
    return datatake


@pytest.fixture
def datatake_11224_4():
    return build_datatake(
        "11224-4",
        "2026-10-29T21:28:54.543Z",
        "2026-10-29T21:29:23.407Z",
        product_group_ids=["GS2C_20261029T212831_011224_N05.13", GROUP_DS_2],
    )


@pytest.fixture
def datatake_11224_5():
    return build_datatake(
        "11224-5", "2026-10-29T21:30:01.337Z", "2026-10-29T21:30:19.377Z"
    )


def build_document(model, product_type, datatake_id, start, end, index, **kwargs):
    document = model(
        name=f"S2C_OPER_{product_type}_2CPS_20261029T223319_S{start[:19]}_N05.13",
        product_type=product_type,
        mission="S2",
        satellite_unit="S2C",
        datatake_id=datatake_id,
        sensing_start_date=datestr_to_utc_datetime(start),
        sensing_end_date=datestr_to_utc_datetime(end),
        **kwargs,
    )
    # the partition date
    setattr(
        document,
        "publication_date" if model is CdsPublication else "prip_publication_date",
        datestr_to_utc_datetime("2026-10-29T22:41:20.000Z"),
    )
    document.meta.id = f"id-{product_type}-{start}"
    document.meta.index = index
    # as returned by a search with seq_no_primary_term
    document.meta.seq_no = 12
    document.meta.primary_term = 3
    return document


def migrator(dry_run=False):
    patch_ = PatchV2_21_0(argparse.Namespace(dry_run=dry_run, es_timeout=30))
    patch_.es_conn = MagicMock()
    return patch_


def test_datastrips_move_to_their_shifted_datatake(datatake_11224_5):
    datastrip = build_document(
        CdsProductS2,
        "MSI_L0__DS",
        "11224-4",
        "2026-10-29T21:29:34.000Z",
        "2026-10-29T21:29:48.000Z",
        "cds-product-2026-10",
        product_group_id=GROUP_DS_2,
    )

    patch_ = migrator()

    with (
        patch.object(CdsProductS2, "search", return_value=FakeSearch([datastrip])),
        patch.object(
            CdsProductS2, "search_datastrip_datatakes", return_value=[datatake_11224_5]
        ),
    ):
        actions = list(patch_.datastrip_iterator())

    assert [(a["_id"], a["_source"]["datatake_id"]) for a in actions] == [
        ("id-MSI_L0__DS-2026-10-29T21:29:34.000Z", "11224-5")
    ]
    assert patch_.datatakes_to_compute == {"11224-4", "11224-5"}


def test_datatakes_take_the_products_of_their_groups(
    datatake_11224_4, datatake_11224_5
):
    granules = [
        # attached to the previous datatake
        build_document(
            CdsProduct,
            "MSI_L0__GR",
            "11224-4",
            "2026-10-29T21:29:36.000Z",
            "2026-10-29T21:29:40.000Z",
            "cds-product-2026-10",
            product_group_id=GROUP_DS_2,
        ),
        # never attached
        build_document(
            CdsProduct,
            "MSI_L0__GR",
            "______",
            "2026-10-29T21:29:40.000Z",
            "2026-10-29T21:29:44.000Z",
            "cds-product-2026-10",
            product_group_id=GROUP_DS_2,
        ),
    ]

    owned_groups = {
        "11224-4": ["GS2C_20261029T212831_011224_N05.13"],
        "11224-5": [GROUP_DS_2],
    }

    def find_related_document_not_attached(datatake):
        return FakeSearch(granules if datatake.datatake_id == "11224-5" else [])

    patch_ = migrator()

    with (
        patch.object(
            CdsDatatakeS2,
            "search",
            return_value=FakeSearch([datatake_11224_4, datatake_11224_5]),
        ),
        patch.object(
            CdsDatatakeS2,
            "search_datastrip_product_group_ids",
            lambda datatake: owned_groups[datatake.datatake_id],
        ),
        patch.object(
            CdsDatatakeS2,
            "find_related_document_not_attached",
            find_related_document_not_attached,
        ),
        patch.object(CdsDatatakeS2, "search_expected_tiles", return_value=[]),
        patch.object(CdsDatatakeS2, "get_downlinks", return_value=[None]),
    ):
        actions = list(patch_.product_group_iterator())

    assert [a["_source"]["datatake_id"] for a in actions] == ["11224-5", "11224-5"]

    # 11224-4 lost a group and a granule, 11224-5 gained both granules
    assert patch_.datatakes_to_compute == {"11224-4", "11224-5"}


def test_only_the_changed_datatakes_are_written(datatake_11224_4, datatake_11224_5):
    other = build_datatake(
        "11224-6", "2026-10-29T21:31:00.000Z", "2026-10-29T21:31:30.000Z"
    )
    computed = []

    def compute_completeness(datatake):
        computed.append(datatake.datatake_id)
        if datatake.datatake_id == "11224-5":
            datatake.final_completeness_status = "Complete"

    search = FakeSearch([datatake_11224_4, datatake_11224_5, other])
    patch_ = migrator()

    with (
        patch.object(CdsDatatakeS2, "search", return_value=search),
        patch.object(CdsDatatakeS2, "load_data_before_compute", return_value=iter([])),
        patch.object(CdsDatatakeS2, "compute_completeness", compute_completeness),
    ):
        actions = list(patch_.datatake_iterator())

    # all the datatakes of the period: a stopped run gained nothing in this one
    assert computed == ["11224-4", "11224-5", "11224-6"]

    assert [a["_id"] for a in actions] == ["S2C-11224-5"]
    assert actions[0]["_source"]["final_completeness_status"] == "Complete"

    # a page is computed before its shard contexts expire
    assert search.search_params["scroll"] == "1h"
    assert search.search_params["size"] == 100


def test_a_failed_action_does_not_stop_the_patch(caplog):
    conflict = {"index": {"_id": "S2C-11224-5", "status": 409}}
    patch_ = migrator()

    with (
        patch(
            "maas_cds.update.patch_2_21_0.parallel_bulk",
            return_value=[(True, {}), (False, conflict)],
        ) as bulk,
        patch("maas_cds.update.patch_2_21_0.db_connections.get_connection"),
        caplog.at_level("ERROR"),
    ):
        patch_.bulk_exec(iter([]))

    assert bulk.call_args.kwargs["raise_on_error"] is False
    assert caplog.messages == [str(conflict)]


@pytest.mark.parametrize(
    "product_type, datatake_id, start, end, expected",
    [
        # the second datastrip, on the previous datatake
        (
            "MSI_L0__DS",
            "11224-4",
            "2026-10-29T21:29:34.000Z",
            "2026-10-29T21:29:48.000Z",
            "11224-5",
        ),
        # a granule in the gap between the MP datatakes, never attached
        (
            "MSI_L0__GR",
            "______",
            "2026-10-29T21:29:36.000Z",
            "2026-10-29T21:29:40.000Z",
            "11224-5",
        ),
        # already on its datatake
        (
            "MSI_L0__GR",
            "11224-4",
            "2026-10-29T21:28:40.000Z",
            "2026-10-29T21:28:44.000Z",
            None,
        ),
        # no datatake around: left as is
        (
            "MSI_L0__GR",
            "______",
            "2026-10-29T22:10:00.000Z",
            "2026-10-29T22:10:04.000Z",
            None,
        ),
    ],
)
def test_publications_move_to_the_nearest_shifted_datatake(
    product_type,
    datatake_id,
    start,
    end,
    expected,
    datatake_11224_4,
    datatake_11224_5,
):
    publication = build_document(
        CdsPublication,
        product_type,
        datatake_id,
        start,
        end,
        "cds-publication-2026-10",
        service_type="DD",
        service_id="DAS",
        timeliness="NOMINAL",
        absolute_orbit="11224",
        instrument_mode="NOBS",
    )
    patch_ = migrator()

    with (
        patch.object(
            CdsDatatakeS2,
            "search",
            return_value=FakeSearch([datatake_11224_4, datatake_11224_5]),
        ),
        patch.object(CdsPublication, "search", return_value=FakeSearch([publication])),
    ):
        actions = list(patch_.publication_iterator())

    if expected is None:
        assert not actions
        assert not patch_.completeness_to_compute
        return

    assert len(actions) == 1
    assert {k: v for k, v in actions[0].items() if k != "doc"} == {
        "_op_type": "update",
        "_index": "cds-publication-2026-10",
        "_id": publication.meta.id,
        "if_seq_no": 12,
        "if_primary_term": 3,
    }
    assert actions[0]["doc"]["datatake_id"] == expected
    assert set(actions[0]["doc"]) == {"datatake_id", "updateTime"}

    assert patch_.completeness_to_compute == {
        ("DD", "DAS"): {datatake_id, expected} - {"______"}
    }


def test_the_per_service_completeness_is_logged(caplog):
    patch_ = migrator()
    patch_.completeness_to_compute = {
        ("DD", "DAS"): {"11224-5", "11224-4"},
        # no per service completeness for this service
        ("LTA", "Acri"): {"11224-5"},
    }
    patch_.es_conn.indices.exists.side_effect = lambda index: index.endswith("-das")

    with caplog.at_level("INFO"):
        patch_.log_completeness_to_compute()

    assert caplog.messages == [
        "[Completeness][cds-completeness-s2-s2c-dd-das] - 2 datatakes to compute"
        " again: 11224-4 11224-5",
        "[Completeness][cds-completeness-s2-s2c-lta-acri] - no per service completeness",
    ]


def test_candidate_datatakes_follow_the_consolidation_window(
    datatake_11224_4, datatake_11224_5
):
    datatakes = [datatake_11224_4, datatake_11224_5]
    starts = [datatake.observation_time_start for datatake in datatakes]

    def candidates(start, end):
        publication = CdsPublication(
            sensing_start_date=datestr_to_utc_datetime(start),
            sensing_end_date=datestr_to_utc_datetime(end),
        )
        return [
            datatake.datatake_id
            for datatake in PatchV2_21_0.search_candidate_datatakes(
                datatakes, starts, publication
            )
        ]

    # 11224-5 starts 13s after the end + 40s tolerance: fetched with the shift
    assert candidates("2026-10-29T21:29:00.000Z", "2026-10-29T21:29:08.000Z") == [
        "11224-4",
        "11224-5",
    ]
    assert candidates("2026-10-29T21:28:00.000Z", "2026-10-29T21:28:05.000Z") == [
        "11224-4"
    ]
    assert candidates("2026-10-29T21:31:00.000Z", "2026-10-29T21:31:05.000Z") == []


def test_a_dry_run_writes_nothing(datatake_11224_4, datatake_11224_5):
    datastrip = build_document(
        CdsProductS2,
        "MSI_L0__DS",
        "11224-4",
        "2026-10-29T21:29:34.000Z",
        "2026-10-29T21:29:48.000Z",
        "cds-product-2026-10",
        product_group_id=GROUP_DS_2,
    )
    publication = build_document(
        CdsPublication,
        "MSI_L0__DS",
        "11224-4",
        "2026-10-29T21:29:34.000Z",
        "2026-10-29T21:29:48.000Z",
        "cds-publication-2026-10",
        service_type="DD",
        service_id="DAS",
    )

    patch_ = migrator(dry_run=True)
    patch_.actions = []
    patch_.bulk_exec = lambda iterator: patch_.actions.extend(iterator)

    with (
        patch.object(CdsProductS2, "search", return_value=FakeSearch([datastrip])),
        patch.object(
            CdsProductS2, "search_datastrip_datatakes", return_value=[datatake_11224_5]
        ),
        patch.object(
            CdsDatatakeS2,
            "search",
            return_value=FakeSearch([datatake_11224_4, datatake_11224_5]),
        ),
        patch.object(
            CdsDatatakeS2, "search_datastrip_product_group_ids", return_value=[]
        ),
        patch.object(CdsPublication, "search", return_value=FakeSearch([publication])),
    ):
        patch_.run()

    assert not patch_.actions

    # what would be computed is still counted
    assert patch_.datatakes_to_compute == {"11224-4", "11224-5"}
    assert patch_.completeness_to_compute == {("DD", "DAS"): {"11224-4", "11224-5"}}
