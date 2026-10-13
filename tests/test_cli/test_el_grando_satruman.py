"""el_grando_satruman reads its OpenSearch and AMQP connections from OpenBao / Vault"""

import os

import pytest

from maas_model.testing.fakevault import FakeVault

from maas_cds.cli import el_grando_satruman
from maas_cds.cli.el_grando_satruman import Satruman, satruman_main

OPENSEARCH = {
    "url": "https://opensearch.example:9200",
    "username": "maas",
    "password": "s3cr3t-opensearch",
}

AMQP = {
    "url": "amqp://rabbit.example:5672//",
    "username": "maas",
    "password": "s3cr3t-amqp",
}

ES_URL = "https://maas:s3cr3t-opensearch@opensearch.example:9200"

AMQP_URL = "amqp://maas:s3cr3t-amqp@rabbit.example:5672//"

CONNECTION_ENV = (
    "ES_URL",
    "ES_USERNAME",
    "ES_PASSWORD",
    "ES_VAULT_PATH",
    "AMQP_URL",
    "AMQP_USERNAME",
    "AMQP_PASSWORD",
    "AMQP_VAULT_PATH",
    "VAULT_ADDR",
    "VAULT_KV_MOUNT",
    "VAULT_AUTH_METHOD",
    "VAULT_ROLE_ID",
    "VAULT_SECRET_ID_FILE",
    "VAULT_TOKEN_FILE",
    "VAULT_TOKEN",
)


@pytest.fixture(name="fake_vault")
def fake_vault_fixture(monkeypatch):
    """a running in-process OpenBao / Vault"""
    # keep the requests to the fake vault off a proxy set for the CI runner
    for variable in ("NO_PROXY", "no_proxy"):
        hosts = [os.environ.get(variable), "127.0.0.1", "localhost"]
        monkeypatch.setenv(variable, ",".join(filter(None, hosts)))

    vault = FakeVault()
    vault.start()
    yield vault
    vault.stop()


@pytest.fixture(name="vault_env")
def vault_env_fixture(fake_vault, monkeypatch, tmp_path):
    """environment of a satruman job reading its connections from the fake vault"""
    for name in CONNECTION_ENV:
        monkeypatch.delenv(name, raising=False)

    secret_id_file = tmp_path / "secret_id"
    secret_id_file.write_text(fake_vault.secret_id + "\n", encoding="UTF-8")

    monkeypatch.setenv("VAULT_ADDR", fake_vault.url)
    monkeypatch.setenv("VAULT_ROLE_ID", fake_vault.role_id)
    monkeypatch.setenv("VAULT_SECRET_ID_FILE", str(secret_id_file))
    monkeypatch.setenv("ES_VAULT_PATH", "shared/opensearch/maas")
    monkeypatch.setenv("AMQP_VAULT_PATH", "shared/amqp/maas")

    fake_vault.put("secret", "shared/opensearch/maas", OPENSEARCH)
    fake_vault.put("secret", "shared/amqp/maas", AMQP)

    return monkeypatch


def test_satruman_connects_with_the_vault_connections(vault_env, tmp_path):
    engine_config = tmp_path / "engine-conf.json"
    engine_config.write_text("{}", encoding="UTF-8")

    # record where satruman connects instead of connecting or querying
    opensearch_hosts = []
    vault_env.setattr(
        el_grando_satruman.es_connections,
        "create_connection",
        lambda **kwargs: opensearch_hosts.extend(kwargs["hosts"]),
    )
    wizards = []
    vault_env.setattr(Satruman, "run", lambda self: wizards.append(self))

    satruman_main(
        [
            "--config",
            str(engine_config),
            "--document-class",
            "CdsProduct",
            "--routing-key",
            "compute.cds-product",
        ]
    )

    assert opensearch_hosts == [ES_URL]
    # the message emitter opens its broker connection on the first publish
    assert wizards[0].messenger.url == AMQP_URL
