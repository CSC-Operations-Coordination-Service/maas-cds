"""The 2.19.0 migration: the AI completeness maps and gets its absolute orbit"""

import argparse
import pathlib
from unittest.mock import patch

import pytest

from maas_cds.model import CdsAiProductionCompleteness
from maas_cds.update.patch_2_19_0 import PatchV2_19_0

RESOURCES = str(pathlib.Path(__file__).parents[2] / "resources")

INDEX = "cds-ai-production-completeness"

ABSOLUTE_ORBIT_MAPPING = {"properties": {"absolute_orbit": {"type": "keyword"}}}


class FakeIndices:
    def __init__(self, existing, mapping):
        self.existing = set(existing)
        self.mapping = mapping
        self.templates = {}
        self.mappings = []

    def exists(self, index):
        return index in self.existing

    def put_template(self, name, body, **params):
        self.templates[name] = body

    def put_mapping(self, body, index, **params):
        self.mappings.append((index, body))

    def get_mapping(self, index):
        return {index: {"mappings": self.mapping}}


class FakeConnection:
    """OpenSearch, as far as the patch uses it"""

    def __init__(self, existing=(INDEX,), mapping=None):
        self.indices = FakeIndices(existing, mapping or {})


class FakeSearch:
    """A search returning the given documents, whatever the filters"""

    def __init__(self, documents):
        self.documents = documents

    def filter(self, *args, **kwargs):
        return self

    def exclude(self, *args, **kwargs):
        return self

    def params(self, **kwargs):
        return self

    def scan(self):
        return iter(self.documents)


def completeness(input_name, doc_id):
    document = CdsAiProductionCompleteness(input_name=input_name, completeness=0)
    document.meta.index = INDEX
    document.meta.id = doc_id
    return document


@pytest.fixture
def documents():
    documents = [
        completeness(
            "S1C_AI_RAW__0____20260419T201846_20260419T203117_007290________2DFD.SAFE",
            "with-orbit",
        ),
        # no orbit where the S1 naming puts it
        completeness("S1C_AI_RAW__0____malformed", "without-orbit"),
    ]

    with patch.object(
        CdsAiProductionCompleteness, "search", return_value=FakeSearch(documents)
    ):
        yield documents


def migrator(connection, dry_run=False):
    patch_ = PatchV2_19_0(
        argparse.Namespace(dry_run=dry_run, resources=RESOURCES, es_timeout=30)
    )
    patch_.es_conn = connection
    patch_.migrated = []
    patch_.migrate_index_template = lambda name, partitions, script=None: (
        patch_.migrated.append(name)
    )
    patch_.actions = []
    patch_.bulk_exec = lambda iterator: patch_.actions.extend(iterator)
    return patch_


@pytest.mark.parametrize(
    "mapping",
    [
        {"properties": {"completeness": {"type": "long"}}},
        # already put by hand: putting it again changes nothing
        {"properties": {"absolute_orbit": {"type": "keyword"}}},
    ],
)
def test_the_template_is_put_and_the_existing_index_maps_absolute_orbit(
    documents, mapping
):
    connection = FakeConnection(mapping=mapping)
    patch_ = migrator(connection)

    patch_.run()

    template = connection.indices.templates[f"template_{INDEX}"]
    assert template["mappings"]["properties"]["absolute_orbit"] == {"type": "keyword"}
    # only the new field: the others may be mapped otherwise on an old index
    assert connection.indices.mappings == [(INDEX, ABSOLUTE_ORBIT_MAPPING)]
    assert patch_.migrated == []


def test_an_index_that_mapped_absolute_orbit_otherwise_is_reindexed(documents):
    connection = FakeConnection(
        mapping={"properties": {"absolute_orbit": {"type": "text"}}}
    )
    patch_ = migrator(connection)

    patch_.run()

    assert patch_.migrated == [INDEX]
    assert connection.indices.mappings == []


def test_a_missing_index_only_gets_the_template(documents):
    connection = FakeConnection(existing=())
    patch_ = migrator(connection)

    patch_.run()

    assert f"template_{INDEX}" in connection.indices.templates
    assert (connection.indices.mappings, patch_.migrated) == ([], [])


def test_the_existing_documents_get_the_orbit_of_their_ai_raw(documents):
    patch_ = migrator(FakeConnection())

    patch_.run()

    assert patch_.actions == [
        {
            "_op_type": "update",
            "_index": INDEX,
            "_id": "with-orbit",
            "doc": {"absolute_orbit": "7290"},
        }
    ]


def test_a_dry_run_writes_nothing(documents):
    connection = FakeConnection(
        mapping={"properties": {"absolute_orbit": {"type": "text"}}}
    )
    patch_ = migrator(connection, dry_run=True)

    patch_.run()

    assert (connection.indices.templates, connection.indices.mappings) == ({}, [])
    assert (patch_.migrated, patch_.actions) == ([], [])
