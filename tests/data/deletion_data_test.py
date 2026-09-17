"""Fixtures for the deletion reconciliation tests"""

import datetime

import pytest

from maas_cds import model

__all__ = [
    "make_deletion",
    "make_issue",
    "make_product",
    "make_publication",
    "deletion_issue_werum",
    "zulu",
]


def make_issue(
    key="OMCS-1234",
    interface_type="LTA",
    deletion_interfaces=None,
    attachment_ids=None,
    attachments=None,
):
    """Build a deletion ticket"""
    issue = model.CdsDeletionIssue(
        key=key,
        interface_type=interface_type,
        deletion_interfaces=(
            ["Werum"] if deletion_interfaces is None else deletion_interfaces
        ),
        deletion_date="2024-05-13T00:00:00.000Z",
        deletion_cause="Corrupted products",
    )

    if attachment_ids is not None:
        issue.attachment_ids = attachment_ids

    if attachments is not None:
        issue.attachments = attachments

    issue.meta.id = key

    return issue


def make_deletion(
    product_name,
    attachment_id=None,
    created=None,
    report_name="OMCS-1234_LTA_Werum_DelList.csv",
    jira_issue="OMCS-1234",
    interface_type="LTA",
):
    """Build a consolidated product deletion, as scanned from the index"""
    deletion = model.CdsInterfaceProductDeletion(
        product_name=product_name,
        effective_product_name=product_name,
        jira_issue=jira_issue,
        interface_type=interface_type,
    )

    deletion.reportName = report_name

    if attachment_id is not None:
        deletion.attachment_id = attachment_id

    if created is not None:
        deletion.attachment_created = created

    deletion.meta.id = f"{product_name}-{attachment_id}"
    deletion.meta.index = "cds-interface-product-deletion"

    return deletion


def make_product(name, mission="S1", index="cds-product-2024-05", **kwargs):
    """Build a product as returned by a search: it carries the locking metadata"""
    product = model.CdsProduct(name=name, mission=mission, **kwargs)

    product.meta.id = name
    product.meta.index = index
    product.meta.seq_no = 1
    product.meta.primary_term = 1

    return product


def make_publication(
    name,
    service_type="LTA",
    service_id="Werum",
    mission="S1",
    index="cds-publication-2024-05",
):
    """Build a publication as returned by a search"""
    publication = model.CdsPublication(
        name=name,
        mission=mission,
        service_type=service_type,
        service_id=service_id,
    )

    publication.meta.id = f"{service_id}-{name}"
    publication.meta.index = index
    publication.meta.seq_no = 1
    publication.meta.primary_term = 1

    return publication


def zulu(day, hour=0):
    """Build a creation date in the index deserialized form"""
    return datetime.datetime(2024, 5, day, hour, tzinfo=datetime.timezone.utc)


@pytest.fixture
def deletion_issue_werum():
    """A nominal LTA deletion ticket targeting a single service"""
    return make_issue()
