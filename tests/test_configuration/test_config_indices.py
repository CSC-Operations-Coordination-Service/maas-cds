"""Where each configuration lives: ocs-config-* for the OCS-specific ones,
maas-config-* for the generic MAAS ones"""

import pytest

from maas_cds import model
from maas_cds.model.generated import MaasConfigCollector

OCS_CONFIGURATIONS = {
    "OcsConfigDataflow": "ocs-config-dataflow",
    "OcsConfigCompleteness": "ocs-config-completeness",
    "OcsConfigCompletenessS3": "ocs-config-completeness-s3",
    "OcsConfigCompletenessS5": "ocs-config-completeness-s5",
    "OcsConfigMission": "ocs-config-mission",
    "OcsConfigSatellite": "ocs-config-satellite",
    "OcsConfigService": "ocs-config-service",
}


@pytest.mark.parametrize("class_name, index", sorted(OCS_CONFIGURATIONS.items()))
def test_ocs_specific_configuration_reads_its_ocs_config_index(class_name, index):
    config_class = getattr(model, class_name)

    assert config_class.Index.name == index
    assert config_class._matches({"_index": index})
    assert not config_class._matches({"_index": index.replace("ocs-", "maas-", 1)})


def test_the_collector_configuration_stays_a_generic_maas_one():
    assert MaasConfigCollector.Index.name == "maas-config-collector"
