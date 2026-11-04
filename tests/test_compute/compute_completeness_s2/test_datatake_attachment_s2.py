"""Attachment of S2 products to their datatake when the mission planning shifts

Built on the S2C orbit 11224 (2026-10-29): the MP datatakes 11224-4 and 11224-5
are ~27s late compared to the real sensing. Both L0 datastrips were attached to
11224-4, while the second one (group GS2C_20261029T212931_011224_N05.13) belongs
to 11224-5, where its granules and tiles were attached.
"""

import datetime
from unittest.mock import MagicMock, patch

import pytest
from maas_model import datestr_to_utc_datetime

from maas_cds.engines.compute.compute_completeness import ComputeCompletenessEngine
from maas_cds.lib.queryutils.find_datatake_from_product_group_id import (
    find_datatake_from_product_group_id,
)
from maas_cds.lib.queryutils.find_datatake_from_sensing import (
    sort_datatakes_by_distance,
)
from maas_cds.engines.reports.base import BaseProductConsolidatorEngine
from maas_cds.model.datatake_s2 import CdsDatatakeS2
from maas_cds.model.product_s2 import CdsProductS2
from maas_cds.model.publication import CdsPublication

GROUP_DS_1 = "GS2C_20261029T212831_011224_N05.13"
GROUP_DS_2 = "GS2C_20261029T212931_011224_N05.13"


def build_datatake(datatake_id, start, stop):
    return CdsDatatakeS2(
        key=f"S2C-{datatake_id}",
        datatake_id=datatake_id,
        satellite_unit="S2C",
        mission="S2",
        instrument_mode="NOBS",
        observation_time_start=datestr_to_utc_datetime(start),
        observation_time_stop=datestr_to_utc_datetime(stop),
    )


@pytest.fixture
def datatake_11224_4():
    return build_datatake(
        "11224-4", "2026-10-29T21:28:54.543Z", "2026-10-29T21:29:23.407Z"
    )


@pytest.fixture
def datatake_11224_5():
    return build_datatake(
        "11224-5", "2026-10-29T21:30:01.337Z", "2026-10-29T21:30:19.377Z"
    )


@pytest.fixture
def datastrip_11224_2():
    """Second L0 datastrip of the orbit, attached to 11224-4 in production"""
    product = CdsProductS2(
        key="30c15014-d7cd-461d-92ab-deec89b84bbf",
        name="S2C_OPER_MSI_L0__DS_2CPS_20261029T223319_S20261029T212934_N05.13.tar",
        product_type="MSI_L0__DS",
        mission="S2",
        satellite_unit="S2C",
        datatake_id="11224-4",
        product_group_id=GROUP_DS_2,
        sensing_start_date=datestr_to_utc_datetime("2026-10-29T21:29:34.000Z"),
        sensing_end_date=datestr_to_utc_datetime("2026-10-29T21:29:48.000Z"),
        prip_id="30c15014-d7cd-461d-92ab-deec89b84bbf",
        prip_publication_date=datestr_to_utc_datetime("2026-10-29T22:41:20.000Z"),
    )
    product.meta.id = product.key
    product.meta.index = "cds-product-2026-10"
    return product


def test_sort_datatakes_by_distance_s2c_shift(datatake_11224_4, datatake_11224_5):
    """The middle of the second datastrip, shifted by 30s, is inside 11224-5"""

    middle = datestr_to_utc_datetime("2026-10-29T21:29:41.000Z").timestamp()

    assert sort_datatakes_by_distance(
        [datatake_11224_4, datatake_11224_5], "S2C", middle
    ) == [datatake_11224_5, datatake_11224_4]

    # Without the shift the middle is closer to the end of 11224-4
    assert sort_datatakes_by_distance(
        [datatake_11224_5, datatake_11224_4], "S2A", middle
    ) == [datatake_11224_4, datatake_11224_5]


@patch("opensearchpy.Search.execute")
def test_product_group_id_held_by_two_s2c_datatakes(
    mock_execute, datatake_11224_4, datatake_11224_5
):
    """A stale product_group_id on 11224-4 must not win over 11224-5"""

    mock_execute.return_value = [datatake_11224_4, datatake_11224_5]

    datatakes = find_datatake_from_product_group_id("S2", "S2C", GROUP_DS_2)

    assert [dt.datatake_id for dt in datatakes] == ["11224-5", "11224-4"]


@patch(
    "maas_cds.lib.queryutils.find_datatake_from_product_group_id.search_datatakes_around_date"
)
@patch("opensearchpy.Search.execute", return_value=[])
def test_product_group_id_held_by_no_s2c_datatake(
    mock_execute, mock_around, datatake_11224_4, datatake_11224_5
):
    """L0 granules come before any datatake holds their group: the group date,
    shifted by 30s, gives the datatake"""

    mock_around.return_value = [datatake_11224_4, datatake_11224_5]

    datatakes = find_datatake_from_product_group_id("S2", "S2C", GROUP_DS_2)

    mock_around.assert_called_once_with(
        "S2", "S2C", "11224", datestr_to_utc_datetime("2026-10-29T21:30:01.000Z")
    )
    assert [dt.datatake_id for dt in datatakes] == ["11224-5", "11224-4"]


@patch(
    "maas_cds.lib.queryutils.find_datatake_from_product_group_id.search_datatakes_around_date"
)
def test_product_group_id_other_units_are_not_shifted(mock_around):
    mock_around.return_value = []

    assert (
        find_datatake_from_product_group_id(
            "S2", "S2A", "GS2A_20240207T101201_045064_N05.10"
        )
        == []
    )

    mock_around.assert_called_once_with(
        "S2", "S2A", "45064", datestr_to_utc_datetime("2024-02-07T10:12:01.000Z")
    )


def test_datastrip_moves_to_covering_datatake(datastrip_11224_2, datatake_11224_5):
    with patch.object(
        CdsProductS2, "search_datastrip_datatakes", return_value=[datatake_11224_5]
    ):
        datastrip_11224_2.check_datatake_id()

    assert datastrip_11224_2.datatake_id == "11224-5"
    assert datastrip_11224_2.get_compute_key() == ("S2C-11224-5", "MSI_L0__DS")
    assert datastrip_11224_2.get_detached_compute_key() == (
        "S2C-11224-4",
        "MSI_L0__DS",
    )

    # The detached datatake is in memory only, never saved with the product
    assert "detached_datatake_id" not in datastrip_11224_2.to_dict()


@pytest.mark.parametrize(
    "candidates",
    [
        # Still a candidate, even not the first one: never switch back and forth
        ["11224-5", "11224-4"],
        # No datatake matches the sensing anymore: keep what we have
        [],
    ],
)
def test_datastrip_stays_on_its_datatake(
    candidates, datastrip_11224_2, datatake_11224_4, datatake_11224_5
):
    datatakes = {"11224-4": datatake_11224_4, "11224-5": datatake_11224_5}

    with patch.object(
        CdsProductS2,
        "search_datastrip_datatakes",
        return_value=[datatakes[datatake_id] for datatake_id in candidates],
    ):
        datastrip_11224_2.check_datatake_id()

    assert datastrip_11224_2.datatake_id == "11224-4"
    assert datastrip_11224_2.get_detached_compute_key() is None


@pytest.mark.parametrize(
    "product_type, datatake_id",
    [("MSI_L0__GR", "11224-4"), ("MSI_L0__DS", "______"), ("MSI_L0__DS", None)],
)
def test_check_datatake_id_only_for_attached_datastrips(
    product_type, datatake_id, datastrip_11224_2
):
    datastrip_11224_2.product_type = product_type
    datastrip_11224_2.datatake_id = datatake_id

    with patch.object(CdsProductS2, "search_datastrip_datatakes") as mock_search:
        datastrip_11224_2.check_datatake_id()

    mock_search.assert_not_called()
    assert datastrip_11224_2.datatake_id == datatake_id


@patch("maas_cds.engines.compute.compute_completeness.MaasConfigManager")
def test_engine_computes_the_detached_datatake(
    mock_manager, datastrip_11224_2, datatake_11224_5
):
    """Both the new and the previous datatake of a moved datastrip are computed"""

    engine = ComputeCompletenessEngine()
    engine.input_documents = [datastrip_11224_2]

    datatake_docs = {}

    def load_datatake_doc(datatake_id, mission=""):
        datatake_doc = MagicMock()
        datatake_doc.impact_other_calculation.return_value = []
        datatake_docs[datatake_id] = datatake_doc
        engine.local_cache_datatake[datatake_id] = (datatake_doc, {})

    with (
        patch.object(engine, "load_datatake_doc", side_effect=load_datatake_doc),
        patch.object(
            CdsProductS2,
            "search_datastrip_datatakes",
            return_value=[datatake_11224_5],
        ),
    ):
        actions = list(engine.load_compute_keys_from_input_documents())

    # The move of the datastrip is saved
    assert len(actions) == 1
    assert actions[0]["_source"]["datatake_id"] == "11224-5"

    assert engine.tuples_to_compute == [
        ("S2C-11224-5", "MSI_L0__DS"),
        ("S2C-11224-4", "MSI_L0__DS"),
    ]

    # Only the new datatake owns the datastrip product group
    datatake_docs[
        "S2C-11224-5"
    ].retrieve_additional_fields_from_product.assert_called_once_with(datastrip_11224_2)
    datatake_docs[
        "S2C-11224-4"
    ].retrieve_additional_fields_from_product.assert_not_called()


@pytest.mark.parametrize(
    "product_groups, expected",
    [
        # The L0 datastrip is the reference
        (
            {"MSI_L0__DS": {GROUP_DS_1}, "MSI_L1C_DS": {GROUP_DS_1, GROUP_DS_2}},
            [GROUP_DS_1],
        ),
        # Without L0 datastrip the other levels are used
        (
            {"MSI_L1B_DS": {GROUP_DS_2}, "MSI_L1C_DS": {GROUP_DS_1}},
            [GROUP_DS_1, GROUP_DS_2],
        ),
        # No datastrip anymore: the datatake owns no group
        ({}, []),
    ],
)
def test_datatake_owns_the_groups_of_its_datastrips(
    product_groups, expected, datatake_11224_4
):
    datatake_11224_4.product_group_ids = [GROUP_DS_1, GROUP_DS_2]

    with patch.object(
        CdsDatatakeS2, "search_datastrip_product_groups", return_value=product_groups
    ):
        assert datatake_11224_4.search_datastrip_product_group_ids() == expected


def test_rattach_query_covers_all_groups_but_not_datastrips(datatake_11224_5):
    datatake_11224_5.product_group_ids = [GROUP_DS_1, GROUP_DS_2]

    query = datatake_11224_5.find_related_document_not_attached().to_dict()
    filters = query["query"]["bool"]["filter"]

    assert {"terms": {"product_group_id": [GROUP_DS_1, GROUP_DS_2]}} in filters

    excluded = [
        must_not
        for f in filters
        if "bool" in f
        for must_not in f["bool"].get("must_not", [])
    ]
    assert {
        "terms": {
            "product_type": [
                "MSI_L0__DS",
                "MSI_L1A_DS",
                "MSI_L1B_DS",
                "MSI_L1C_DS",
                "MSI_L2A_DS",
            ]
        }
    } in excluded
    assert {"term": {"datatake_id": "11224-5"}} in excluded


def build_publication(product_type, start, end, satellite_unit="S2C"):
    return CdsPublication(
        name=f"{satellite_unit}_OPER_{product_type}_2CPS_20261029T223319.tar",
        product_type=product_type,
        mission="S2",
        satellite_unit=satellite_unit,
        sensing_start_date=datestr_to_utc_datetime(start),
        sensing_end_date=datestr_to_utc_datetime(end),
    )


@pytest.mark.parametrize(
    "product_type, start, end",
    [
        # The second datastrip: nearer to the end of 11224-4 without the shift
        ("MSI_L0__DS", "2026-10-29T21:29:34.000Z", "2026-10-29T21:29:48.000Z"),
        # A granule in the gap between the MP datatakes
        ("MSI_L0__GR", "2026-10-29T21:29:36.000Z", "2026-10-29T21:29:40.000Z"),
    ],
)
def test_consolidation_attaches_s2c_with_the_shift(
    product_type, start, end, datatake_11224_4, datatake_11224_5
):
    """Publications are only attached at consolidation, by their sensing"""

    tolerance = datetime.timedelta(
        seconds=BaseProductConsolidatorEngine.S2_DATATAKE_ATTACHEMENT_DELTA
    )
    publication = build_publication(product_type, start, end)

    nearest_datatake = publication.find_nearest_datatake(
        [datatake_11224_4, datatake_11224_5], tolerance
    )

    assert nearest_datatake.datatake_id == "11224-5"

    # Other units are not shifted
    publication.satellite_unit = "S2A"

    nearest_datatake = publication.find_nearest_datatake(
        [datatake_11224_4, datatake_11224_5], tolerance
    )

    assert nearest_datatake.datatake_id == "11224-4"


def test_consolidation_fetches_the_next_s2c_datatake():
    """The shifted sensing can fall in a datatake starting after it"""

    engine = BaseProductConsolidatorEngine.__new__(BaseProductConsolidatorEngine)
    tolerance = datetime.timedelta(
        seconds=BaseProductConsolidatorEngine.S2_DATATAKE_ATTACHEMENT_DELTA
    )
    period = MagicMock(
        start=datestr_to_utc_datetime("2026-10-29T21:29:34.000Z"),
        end=datestr_to_utc_datetime("2026-10-29T21:29:48.000Z"),
    )

    for satellite, shift in (("S2C", 30), ("S2A", 0)):
        with patch("maas_cds.engines.reports.base.MultiSearch") as mock_ms:
            mock_ms.return_value.add.return_value = mock_ms.return_value
            mock_ms.return_value.execute.return_value = []

            engine._get_datatake_S2_map([period], satellite, tolerance, [None])

        search = mock_ms.return_value.add.call_args.args[0].to_dict()
        start_filter = [
            f["range"]["observation_time_start"]
            for f in search["query"]["bool"]["filter"]
            if "observation_time_start" in f.get("range", {})
        ]
        assert start_filter == [
            {"lte": period.end + 2 * tolerance + datetime.timedelta(seconds=shift)}
        ]
