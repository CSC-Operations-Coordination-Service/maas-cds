"""The 2.18.0 migration: the abnormal S3 datatakes are tagged"""

import argparse
import datetime
from unittest.mock import patch

import pytest
from maas_model import datestr_to_utc_datetime
from maas_model.date_utils import datetime_to_zulu

from maas_cds.lib.orbit_id_strategy import S3DatatakeIdStrategy
from maas_cds.model import (
    CdsCompletenessSplittedS3,
    CdsProduct,
    CdsPublication,
    CdsS3Completeness,
)
from maas_cds.update.patch_2_18_0 import PatchV2_18_0

REFERENCE_START = datestr_to_utc_datetime("2026-03-01T00:00:00.000Z")

REFERENCES = {"S3C": ("S3C-003-111", REFERENCE_START)}

NOMINAL_ORBIT = S3DatatakeIdStrategy.NOMINAL_CYCLE_DURATION / 385

COMMISSIONING = REFERENCE_START - datetime.timedelta(days=45)


class FakeSearch:
    """A search returning the given documents, whatever the filters"""

    def __init__(self, documents):
        self.documents = documents

    def filter(self, *args, **kwargs):
        return self

    def params(self, **kwargs):
        return self

    def count(self):
        return len(self.documents)

    def scan(self):
        return iter(self.documents)


def product(model, datatake_id, start, index):
    cycle, orbit = datatake_id.split("-")[1:3]
    document = model(
        name=(
            f"S3C_TM_0_NAT____{start:%Y%m%dT%H%M%S}_{start:%Y%m%dT%H%M%S}"
            f"_{start:%Y%m%dT%H%M%S}_5974_{cycle}_{orbit}______LN3_O_AL_002.SEN3.zip"
        ),
        datatake_id=datatake_id,
        satellite_unit="S3C",
    )
    document.meta.index = index
    document.meta.id = f"id-{datatake_id}"
    return document


def completeness(model, datatake_id, key_suffix, start, index):
    key = datatake_id + key_suffix
    document = model(
        datatake_id=datatake_id,
        key=key,
        satellite_unit="S3C",
        observation_time_start=start,
    )
    document.meta.index = index
    document.meta.id = key
    return document


@pytest.fixture
def searches():
    documents = {
        CdsProduct: [
            product(CdsProduct, "S3C-004-001", COMMISSIONING, "cds-product-2026-01"),
            # nominal
            product(
                CdsProduct,
                "S3C-003-112",
                REFERENCE_START + NOMINAL_ORBIT,
                "cds-product-2026-03",
            ),
        ],
        CdsPublication: [
            product(
                CdsPublication,
                "S3C-009-189",
                REFERENCE_START - NOMINAL_ORBIT,
                "cds-publication-2026-02",
            ),
        ],
        CdsS3Completeness: [
            # commissioning orbit
            completeness(
                CdsS3Completeness,
                "S3C-004-001",
                "#TM_0_NAT___#AL",
                COMMISSIONING,
                "cds-s3-completeness",
            ),
            # generated as missing from the broken numbering (009-189 -> 003-111)
            completeness(
                CdsS3Completeness,
                "S3C-005-300",
                "#TM_0_NAT___#AL",
                REFERENCE_START - 1000 * NOMINAL_ORBIT,
                "cds-s3-completeness",
            ),
            # generated as missing then received: dated by its nominal products
            completeness(
                CdsS3Completeness,
                "S3C-003-113",
                "#TM_0_NAT___#AL",
                REFERENCE_START + 2 * NOMINAL_ORBIT,
                "cds-s3-completeness",
            ),
        ],
        CdsCompletenessSplittedS3: [
            completeness(
                CdsCompletenessSplittedS3,
                "S3C-009-189",
                "-AL-TM_0_NAT___",
                REFERENCE_START - NOMINAL_ORBIT,
                "cds-completeness-splitted-s3-s3c",
            ),
        ],
    }

    patches = [
        patch.object(model, "search", return_value=FakeSearch(docs))
        for model, docs in documents.items()
    ]
    patches.append(
        patch.dict(S3DatatakeIdStrategy.ORBIT_REFERENCES, REFERENCES, clear=True)
    )

    for started in patches:
        started.start()

    yield

    for started in patches:
        started.stop()


def migrator(dry_run=False):
    patch_ = PatchV2_18_0(argparse.Namespace(dry_run=dry_run, es_timeout=30))
    patch_.actions = []
    patch_.bulk_exec = lambda iterator: patch_.actions.extend(iterator)
    return patch_


def test_the_abnormal_datatakes_are_tagged(searches):
    patch_ = migrator()

    patch_.run()

    assert patch_.actions == [
        {
            "_op_type": "update",
            "_index": "cds-product-2026-01",
            "_id": "id-S3C-004-001",
            "doc": {"datatake_id": "S3C-004-001-COM"},
        },
        {
            "_op_type": "update",
            "_index": "cds-publication-2026-02",
            "_id": "id-S3C-009-189",
            "doc": {"datatake_id": "S3C-009-189-COM"},
        },
        {
            "_op_type": "index",
            "_index": "cds-s3-completeness",
            "_id": "S3C-004-001-COM#TM_0_NAT___#AL",
            "_source": {
                "datatake_id": "S3C-004-001-COM",
                "key": "S3C-004-001-COM#TM_0_NAT___#AL",
                "satellite_unit": "S3C",
                "observation_time_start": datetime_to_zulu(COMMISSIONING),
            },
        },
        {
            "_op_type": "delete",
            "_index": "cds-s3-completeness",
            "_id": "S3C-004-001#TM_0_NAT___#AL",
        },
        {
            "_op_type": "delete",
            "_index": "cds-s3-completeness",
            "_id": "S3C-005-300#TM_0_NAT___#AL",
        },
        {
            "_op_type": "index",
            "_index": "cds-completeness-splitted-s3-s3c",
            "_id": "S3C-009-189-COM-AL-TM_0_NAT___",
            "_source": {
                "datatake_id": "S3C-009-189-COM",
                "key": "S3C-009-189-COM-AL-TM_0_NAT___",
                "satellite_unit": "S3C",
                "observation_time_start": datetime_to_zulu(
                    REFERENCE_START - NOMINAL_ORBIT
                ),
            },
        },
        {
            "_op_type": "delete",
            "_index": "cds-completeness-splitted-s3-s3c",
            "_id": "S3C-009-189-AL-TM_0_NAT___",
        },
    ]


def test_a_dry_run_writes_nothing(searches):
    patch_ = migrator(dry_run=True)

    patch_.run()

    assert not patch_.actions


def test_nothing_without_reference():
    patch_ = migrator()

    with patch.dict(S3DatatakeIdStrategy.ORBIT_REFERENCES, {}, clear=True):
        patch_.run()

    assert not patch_.actions
