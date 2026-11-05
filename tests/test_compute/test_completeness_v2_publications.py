"""The S1 / S2 completeness V2 computes on publications.

CdsCompleteness overrides CdsDatatake.find_brother_products_scan to read the
publications of one service instead of the products. The S1 / S2 compute it shares
with the datatakes calls it with ``include_deleted`` and reads the deletion trace
of each hit, so both must exist for the publications too.
"""

import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import opensearchpy

from maas_cds.engines.compute.compute_completeness_v2 import ComputeCompletenessEngineV2
from maas_cds.model.cds_completeness import CdsCompletenessS1, CdsCompletenessS2
from maas_cds.model.publication import CdsPublication

BASE = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc)

KEY = {
    "datatake_id": "1",
    "satellite_unit": "S1A",
    "service_type": "DD",
    "service_id": "DAS",
}

S2_KEY = KEY | {"satellite_unit": "S2A"}


def _publication(name, product_type, start_offset, end_offset, **fields):
    key = S2_KEY if product_type.startswith("MSI") else KEY
    publication = CdsPublication(
        name=name,
        product_type=product_type,
        sensing_start_date=BASE + timedelta(seconds=start_offset),
        sensing_end_date=BASE + timedelta(seconds=end_offset),
        **(key | fields),
    )
    publication.sensing_duration = (end_offset - start_offset) * 1_000_000
    return publication


def _filters(search):
    return search.to_dict()["query"]["bool"]["filter"]


def _matches(publication, clause):
    """Evaluate the query clauses used by the completeness, as OpenSearch would"""
    ((kind, body),) = clause.items()

    if kind == "term":
        ((field, value),) = body.items()
        return getattr(publication, field, None) == value
    if kind == "terms":
        ((field, values),) = body.items()
        return getattr(publication, field, None) in values
    if kind == "exists":
        return bool(getattr(publication, body["field"], None))
    if kind == "bool":
        return all(
            _matches(publication, sub_clause) for sub_clause in body.get("filter", [])
        ) and not any(
            _matches(publication, sub_clause) for sub_clause in body.get("must_not", [])
        )

    raise NotImplementedError(clause)


def _fake_scan(publications):
    """Stand-in for Search.scan, on a list of publications"""

    def scan(search):
        query = search.to_dict()["query"]
        return iter(
            publication for publication in publications if _matches(publication, query)
        )

    return scan


def test_publication_deletion_trace():
    """A publication is deleted from its own service only"""

    publication = CdsPublication(service_type="DD")
    assert publication.deletion_trace() == (False, None)
    assert publication.deletion_trace_by_interface() == {
        "DD": (False, None),
        "LTA": (False, None),
    }

    publication = CdsPublication(
        service_type="DD", deletion_issue="GSANOM-1", deletion_date=BASE
    )
    assert publication.deletion_trace() == (True, "GSANOM-1")
    assert publication.deletion_trace_by_interface() == {
        "DD": (True, "GSANOM-1"),
        "LTA": (False, None),
    }

    # marked before the deletion issue was recorded: only the date
    publication = CdsPublication(service_type="LTA", deletion_date=BASE)
    assert publication.deletion_trace() == (True, None)
    assert publication.deletion_trace_by_interface() == {
        "DD": (False, None),
        "LTA": (True, None),
    }

    # not a deletion interface
    publication = CdsPublication(service_type="PRIP", deletion_issue="GSANOM-1")
    assert publication.deletion_trace() == (True, "GSANOM-1")
    assert publication.deletion_trace_by_interface() == {
        "DD": (False, None),
        "LTA": (False, None),
    }


@patch.object(opensearchpy.Search, "scan", autospec=True, return_value=iter([]))
def test_find_brother_products_scan_query(mock_scan):
    """Same contract as CdsDatatake: include_deleted keeps the deleted
    publications, the indices (product partitions) are ignored"""

    completeness = CdsCompletenessS1(**KEY)

    list(completeness.find_brother_products_scan("IW_RAW__0S"))
    list(
        completeness.find_brother_products_scan(
            "IW_RAW__0S", ["cds-product-2026-11"], include_deleted=True
        )
    )

    live, with_deleted = (call.args[0] for call in mock_scan.call_args_list)

    # the custom model, whose hits carry the deletion trace
    assert live._doc_type == [CdsPublication]
    assert live._index == ["cds-publication"]
    assert with_deleted._index == ["cds-publication"]

    deletion_exclusion = [
        {"bool": {"must_not": [{"exists": {"field": "deletion_issue"}}]}},
        {"bool": {"must_not": [{"exists": {"field": "deletion_date"}}]}},
    ]
    key_terms = [
        {"term": {"datatake_id": "1"}},
        {"term": {"satellite_unit": "S1A"}},
        {"term": {"product_type": "IW_RAW__0S"}},
        {"term": {"service_type": "DD"}},
        {"term": {"service_id": "DAS"}},
    ]
    assert _filters(live) == key_terms + deletion_exclusion
    assert _filters(with_deleted) == key_terms

    deleted = _publication("B", "IW_RAW__0S", 0, 100, deletion_date=BASE)
    assert not _matches(deleted, live.to_dict()["query"])
    assert _matches(deleted, with_deleted.to_dict()["query"])


@patch.object(opensearchpy.Search, "scan", autospec=True)
def test_find_brother_products_scan_cache(mock_scan):
    """During the two completeness passes the publications are fetched once"""

    publications = [_publication("A", "IW_RAW__0S", 0, 100)]
    mock_scan.side_effect = _fake_scan(publications)

    completeness = CdsCompletenessS1(**KEY)
    completeness._brother_products_cache = {}

    first = completeness.find_brother_products_scan("IW_RAW__0S", include_deleted=True)
    second = completeness.find_brother_products_scan("IW_RAW__0S", include_deleted=True)

    assert first == second == publications
    assert mock_scan.call_count == 1


@patch.object(CdsCompletenessS1, "get_expected_value", return_value=300_000_000)
@patch.object(CdsCompletenessS1, "get_all_product_types", return_value=["IW_RAW__0S"])
@patch.object(opensearchpy.Search, "scan", autospec=True)
def test_s1_compute_completeness_with_deleted_publication(mock_scan, *_mocks):
    """A and B overlap by 50%, B is deleted from DD; C is alone.

    The live completeness ignores B, the original one keeps it. The per-service
    completeness does not detect the duplicated publications: no ``duplicateds``.
    """

    mock_scan.side_effect = _fake_scan(
        [
            _publication("A", "IW_RAW__0S", 0, 100),
            _publication(
                "B",
                "IW_RAW__0S",
                50,
                150,
                deletion_issue="GSANOM-1",
                deletion_date=BASE,
            ),
            _publication("C", "IW_RAW__0S", 200, 300),
        ]
    )

    completeness = CdsCompletenessS1(
        **KEY,
        mission="S1",
        observation_time_start=BASE,
        observation_time_stop=BASE + timedelta(seconds=300),
    )

    completeness.compute_completeness()

    # live: A(0-100) + C(200-300)
    assert completeness.IW_RAW__0S_local_value == 200_000_000
    # original: A(0-100) + B(up to 150) + C(200-300)
    assert completeness.original_completeness["IW_RAW__0S_local_value"] == 250_000_000

    # a single query for both passes
    assert mock_scan.call_count == 1

    assert "duplicateds" not in completeness.to_dict()


@patch.object(CdsCompletenessS2, "get_product_partitionning", return_value=None)
@patch.object(opensearchpy.Search, "scan", autospec=True)
def test_s2_compute_local_value_ignores_deleted_publications(mock_scan, _mock_part):
    """DS (sensing), GR and TL (counts) without the deleted publications"""

    mock_scan.side_effect = _fake_scan(
        [
            _publication("DS_A", "MSI_L0__DS", 0, 100),
            _publication("DS_B", "MSI_L0__DS", 100, 200, deletion_date=BASE),
            _publication(
                "GR_1", "MSI_L0__GR", 0, 3, detector_id="01", datastrip_id="DS_A"
            ),
            _publication(
                "GR_2", "MSI_L0__GR", 10, 13, detector_id="01", datastrip_id="DS_A"
            ),
            _publication(
                "GR_3",
                "MSI_L0__GR",
                20,
                23,
                detector_id="01",
                datastrip_id="DS_A",
                deletion_issue="GSANOM-2",
            ),
            _publication("TL_1", "MSI_L1C_TL", 0, 5, tile_number="31TCJ"),
            _publication(
                "TL_2", "MSI_L1C_TL", 0, 5, tile_number="31TDJ", deletion_date=BASE
            ),
        ]
    )

    completeness = CdsCompletenessS2(
        **S2_KEY,
        mission="S2",
        instrument_mode="NOBS",
        observation_time_start=BASE,
        observation_time_stop=BASE + timedelta(seconds=200),
    )

    assert completeness.compute_local_value("MSI_L0__DS") > 100_000_000
    assert completeness.compute_local_value("MSI_L0__DS") < 200_000_000
    assert completeness.compute_local_value("MSI_L0__GR") == 2
    assert completeness.compute_local_value("MSI_L1C_TL") == 1


@patch("maas_cds.engines.compute.compute_completeness_v2.MaasConfigManager")
@patch("maas_cds.engines.compute.compute_completeness.MaasConfigManager")
@patch.object(CdsCompletenessS1, "get_expected_value", return_value=100_000_000)
@patch.object(CdsCompletenessS1, "get_all_product_types", return_value=["IW_RAW__0S"])
@patch.object(CdsCompletenessS2, "search_expected_tiles", return_value=set())
@patch.object(CdsCompletenessS2, "get_all_product_types", return_value=["MSI_L0__DS"])
@patch.object(opensearchpy.Search, "scan", autospec=True)
def test_engine_v2_from_completeness_document(mock_scan, *_mocks):
    """new.cds-completeness-s1 / -s2: the engine computes and updates the
    document instead of failing on the S1 / S2 compute.

    For S2 the publication of the datatake product group published without a
    datatake is attached first, then counted.
    """

    group = "GS2A_20261101T000000_000001_N05.11"
    unattached = _publication(
        "S2_DS_2",
        "MSI_L0__DS",
        50,
        100,
        datatake_id="______",
        mission="S2",
        product_group_id=group,
        publication_date=BASE + timedelta(hours=1),
    )

    mock_scan.side_effect = _fake_scan(
        [
            _publication("S1_A", "IW_RAW__0S", 0, 100),
            _publication("S2_DS_1", "MSI_L0__DS", 0, 50),
            unattached,
        ]
    )

    period = {
        "observation_time_start": BASE,
        "observation_time_stop": BASE + timedelta(seconds=100),
    }
    documents = {
        "CdsCompletenessS1": CdsCompletenessS1(
            meta={"id": "S1A-1", "index": "cds-completeness-s1-s1a-dd-das"},
            **KEY,
            mission="S1",
            **period,
        ),
        "CdsCompletenessS2": CdsCompletenessS2(
            meta={"id": "S2A-1", "index": "cds-completeness-s2-s2a-dd-das"},
            **S2_KEY,
            mission="S2",
            instrument_mode="NOBS",
            observation_duration=100_000_000,
            number_of_scenes=5,
            product_group_ids=[group],
            **period,
        ),
    }

    actions = {}
    for document_class, document in documents.items():
        engine = ComputeCompletenessEngineV2()
        engine.payload = SimpleNamespace(document_class=document_class)
        engine.input_documents = [document]

        actions[document_class] = list(engine.action_iterator())

        # the completeness document is updated last
        assert actions[document_class][-1]["_id"] == document.meta.id
        assert actions[document_class][-1]["_index"] == document.meta.index

    assert len(actions["CdsCompletenessS1"]) == 1
    assert documents["CdsCompletenessS1"].IW_RAW__0S_local_value == 100_000_000
    assert documents["CdsCompletenessS1"].original_completeness

    attach, _ = actions["CdsCompletenessS2"]
    assert attach["_index"] == "cds-publication-2026-11"
    assert attach["_source"]["datatake_id"] == "1"
    # S2_DS_1 (0-50) + the attached S2_DS_2 (50-100)
    assert documents["CdsCompletenessS2"].MSI_L0__DS_local_value >= 100_000_000


@patch("maas_cds.engines.compute.compute_completeness_v2.MaasConfigManager")
@patch("maas_cds.engines.compute.compute_completeness.MaasConfigManager")
@patch.object(CdsCompletenessS1, "get_expected_value", return_value=100_000_000)
@patch.object(CdsCompletenessS1, "get_all_product_types", return_value=["IW_RAW__0S"])
@patch.object(CdsCompletenessS2, "search_expected_tiles", return_value=set())
@patch.object(CdsCompletenessS2, "get_all_product_types", return_value=["MSI_L0__DS"])
@patch.object(opensearchpy.Search, "scan", autospec=True)
def test_engine_v2_from_publication(mock_scan, *_mocks):
    """new.cds-publication-s1 / -s2: the completeness of the publication key is
    computed and the completeness document updated"""

    period = {
        "observation_time_start": BASE,
        "observation_time_stop": BASE + timedelta(seconds=100),
        "observation_duration": 100_000_000,
    }
    completeness_documents = {
        "S1": CdsCompletenessS1(
            meta={"id": "S1A-1", "index": "cds-completeness-s1-s1a-dd-das"},
            **KEY,
            mission="S1",
            **period,
        ),
        "S2": CdsCompletenessS2(
            meta={"id": "S2A-1", "index": "cds-completeness-s2-s2a-dd-das"},
            **S2_KEY,
            mission="S2",
            instrument_mode="NOBS",
            number_of_scenes=5,
            **period,
        ),
    }
    publications = {
        "S1": _publication("S1_A", "IW_RAW__0S", 0, 100, mission="S1"),
        "S2": _publication("S2_DS", "MSI_L0__DS", 0, 100, mission="S2"),
    }
    mock_scan.side_effect = _fake_scan(list(publications.values()))

    for mission, publication in publications.items():
        completeness = completeness_documents[mission]

        engine = ComputeCompletenessEngineV2()
        engine.payload = SimpleNamespace(document_class="CdsPublication")
        engine.input_documents = [publication]

        with patch.object(
            type(completeness), "get_by_id", return_value=completeness
        ) as mock_get_by_id:
            (action,) = engine.action_iterator()

        mock_get_by_id.assert_called_once_with(
            f"{completeness.satellite_unit}-1",
            [publication.completeness_document_index],
        )
        assert action["_id"] == completeness.meta.id
        assert action["_index"] == completeness.meta.index

    assert completeness_documents["S1"].IW_RAW__0S_local_value == 100_000_000
    assert completeness_documents["S2"].MSI_L0__DS_local_value > 0
