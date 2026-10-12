"""TRACK_DELETION finds a collector configuration by its interface name

The configuration API (ocs-operator-backend) creates maas-config-collector
documents under counter ids: the interface name is a field, not the id.
"""

import pytest

from maas_cds.engines.compute.track_deletion import TrackDeletionEngine
from maas_cds.model.generated import MaasConfigCollector


class FakeSearch:
    """answers the documents it is given, recording the filters applied"""

    def __init__(self):
        self.found = []
        self.filters = []

    def filter(self, query):
        self.filters.append(query.to_dict())
        return self

    def params(self, **params):
        self.params_given = params
        return self

    def execute(self):
        return list(self.found)


@pytest.fixture(name="search")
def search_fixture(monkeypatch):
    fake = FakeSearch()
    monkeypatch.setattr(
        MaasConfigCollector, "search", classmethod(lambda cls, **kwargs: fake)
    )
    return fake


def configuration(document_id, interface_name="AUXIP_Exprivia"):
    return MaasConfigCollector(
        meta={"id": document_id},
        interface_name=interface_name,
        **{"class": "ODataCollectorConfiguration"},
    )


def test_a_collector_configuration_is_found_by_its_interface_name(search):
    search.found = [configuration("17")]

    found = TrackDeletionEngine(credential_dict={}).collector_configuration(
        "AUXIP_Exprivia"
    )

    assert found.meta.id == "17"
    # interface_name.keyword: indices mapped before interface_name was a keyword
    assert search.filters == [
        {
            "bool": {
                "should": [
                    {"term": {"interface_name": "AUXIP_Exprivia"}},
                    {"term": {"interface_name.keyword": "AUXIP_Exprivia"}},
                ],
                "minimum_should_match": 1,
            }
        }
    ]


def test_no_collector_configuration_is_none(search):
    assert TrackDeletionEngine(credential_dict={}).collector_configuration("x") is None


def test_several_collector_configurations_are_ambiguous_so_none(search):
    search.found = [configuration("17"), configuration("18")]

    assert (
        TrackDeletionEngine(credential_dict={}).collector_configuration(
            "AUXIP_Exprivia"
        )
        is None
    )
