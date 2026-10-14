"""The 2.16.0 migration: OCS-specific configurations move to ocs-config-*"""

import argparse
import pathlib

import pytest

from maas_cds.update.patch_2_16_0 import PatchV2_16_0

RESOURCES = str(pathlib.Path(__file__).parents[2] / "resources")

MOVES = {
    "maas-config-dataflow": "ocs-config-dataflow",
    "maas-config-completeness": "ocs-config-completeness",
    "maas-config-completeness-s3": "ocs-config-completeness-s3",
    "maas-config-completeness-s5": "ocs-config-completeness-s5",
    "maas-config-mission": "ocs-config-mission",
    "maas-config-satellite": "ocs-config-satellite",
    "maas-config-service": "ocs-config-service",
}


class FakeIndices:
    def __init__(self, existing, collector_mapping):
        self.existing = set(existing)
        self.collector_mapping = collector_mapping
        self.templates = {}
        self.created = []

    def exists(self, index):
        return index in self.existing

    def put_template(self, name, body, **params):
        self.templates[name] = body

    def create(self, index, **params):
        self.created.append(index)
        self.existing.add(index)

    def get_alias(self, pattern):
        return {}

    def put_mapping(self, body, index, **params):
        pass

    def get_mapping(self, index):
        return {index: {"mappings": self.collector_mapping}}


class FakeConnection:
    """OpenSearch, as far as the patch uses it"""

    def __init__(self, existing=(), collector_mapping=None):
        self.indices = FakeIndices(existing, collector_mapping or {})
        self.reindexed = []

    def reindex(self, body, **params):
        self.reindexed.append(body)
        return {"total": 2, "created": 2, "version_conflicts": 0}


def patch(connection, dry_run=False):
    migrator = PatchV2_16_0(
        argparse.Namespace(dry_run=dry_run, resources=RESOURCES, es_timeout=30)
    )
    migrator.es_conn = connection
    migrator.migrated = []
    migrator.migrate_index_template = lambda name, partitions, script=None: (
        migrator.migrated.append(name)
    )
    return migrator


def test_each_ocs_configuration_moves_to_its_new_index_ids_kept():
    connection = FakeConnection(
        existing=["maas-config-dataflow", "maas-config-satellite"]
    )

    patch(connection).run()

    assert sorted(connection.indices.templates) == sorted(
        [f"template_{new}" for new in MOVES.values()]
        + ["template_maas-config-collector"]
    )
    assert sorted(connection.indices.created) == sorted(MOVES.values())
    # create: a rerun leaves alone what the backend already wrote there
    assert connection.reindexed == [
        {
            "conflicts": "proceed",
            "source": {"index": "maas-config-dataflow"},
            "dest": {"index": "ocs-config-dataflow", "op_type": "create"},
        },
        {
            "conflicts": "proceed",
            "source": {"index": "maas-config-satellite"},
            "dest": {"index": "ocs-config-satellite", "op_type": "create"},
        },
    ]


def test_the_old_indices_stay():
    connection = FakeConnection(existing=["maas-config-dataflow"])

    patch(connection).run()

    assert "maas-config-dataflow" in connection.indices.existing


def test_a_dry_run_writes_nothing():
    connection = FakeConnection(
        existing=["maas-config-dataflow", "maas-config-collector"],
        collector_mapping={"properties": {"interface_name": {"type": "text"}}},
    )
    migrator = patch(connection, dry_run=True)

    migrator.run()

    assert (connection.indices.templates, connection.indices.created) == ({}, [])
    assert (connection.reindexed, migrator.migrated) == ([], [])


@pytest.mark.parametrize(
    "existing, mapping, migrated",
    [
        # dynamically mapped as text: reindexed with the new template
        (
            ["maas-config-collector"],
            {"properties": {"interface_name": {"type": "text"}}},
            ["maas-config-collector"],
        ),
        (
            ["maas-config-collector"],
            {"properties": {"interface_name": {"type": "keyword"}}},
            [],
        ),
        ([], {}, []),
    ],
)
def test_the_collector_index_is_remapped_unless_interface_name_is_a_keyword(
    existing, mapping, migrated
):
    connection = FakeConnection(existing=existing, collector_mapping=mapping)
    migrator = patch(connection)

    migrator.run()

    assert migrator.migrated == migrated
