import json, pathlib
import pytest

TEMPLATES = pathlib.Path(__file__).parents[2] / "resources" / "templates"
# BackupReport (maas-collector 3.14.0) and the raw document's own fields
REPORT_FIELDS = {"ingestionTime", "reportName", "filename", "source_filename", "source_path", "size_bytes",
                 "compressed", "backup_type", "destination", "destination_path", "interface_name", "model_name",
                 "start_time", "end_time", "duration", "attempts", "status", "error_type", "details",
                 "origin", "backup_name", "operation_id", "product_id", "product_name", "written_md5", "catalogue_checksum"}
NEW_KEYWORDS = {"origin", "backup_name", "operation_id", "product_id", "product_name", "written_md5", "catalogue_checksum"}

def load(name): return json.loads((TEMPLATES / f"{name}_template.json").read_text())

def test_backup_template_declares_every_report_field():                 # Review Focus 5
    assert REPORT_FIELDS <= set(load("raw-data-backup")["mappings"]["properties"])

def test_backup_template_names_and_partition():
    template = load("raw-data-backup")
    assert (template["index_patterns"], list(template["aliases"])) == (["raw-data-backup-*"], ["raw-data-backup"])
    assert template["mappings"]["_meta"] == {"partition_field": "ingestionTime", "partition_format": "%Y"}

def test_new_report_fields_are_keywords_and_status_too():
    properties = load("raw-data-backup")["mappings"]["properties"]
    assert {name: properties[name]["type"] for name in NEW_KEYWORDS | {"status"}} == dict.fromkeys(NEW_KEYWORDS | {"status"}, "keyword")

def test_cdse_template_carries_the_checksum_the_archive_reads():
    template = load("raw-data-dm-cdse-product")
    properties = template["mappings"]["properties"]
    assert template["index_patterns"] == ["raw-data-dm-cdse-product-*"]
    assert {n: properties[n]["type"] for n in ("product_id", "product_name", "origin_checksum_md5")} == dict.fromkeys(("product_id", "product_name", "origin_checksum_md5"), "keyword")
    assert properties["content_length"]["type"] == "long" and properties["publication_date"]["type"] == "date"

def test_neither_template_uses_the_fastapigen_api_key():                  # that key belongs to the retired repository
    assert all("api" not in load(n)["mappings"].get("_meta", {}) for n in ("raw-data-backup", "raw-data-dm-cdse-product"))

def test_models_are_generated():
    from maas_cds.model import Backup, DmCdseProduct
    assert (Backup.Index.name, DmCdseProduct.Index.name) == ("raw-data-backup", "raw-data-dm-cdse-product")
    assert {"origin", "backup_name", "written_md5"} <= set(Backup._INITIAL_FIELDS)
