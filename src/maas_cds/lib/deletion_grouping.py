"""Attachment grouping helpers for the deletion pipeline.

A deletion ticket carries one or more attached files listing the products to
delete. A file can be superseded: a newer version is attached, or the same file
is re-uploaded with a product line removed. Only the products of the current
attachment of each service must stay deleted.

Every extracted row is stamped with the identity of the JIRA attachment it comes
from (``attachment_id`` / ``attachment_created``), which changes at every upload
even when the file name and the file content are identical. These helpers turn
that stamp into a current / stale verdict. They are pure: no database access, no
engine coupling.
"""

import itertools
import re

__all__ = [
    "DEFAULT_INTERFACE_DICT",
    "AttachmentInfo",
    "build_groups",
    "chunked",
    "extract_service_id",
    "resolve_service_id",
    "strip_issue_prefix",
]


# Mirrors the interface_dict of the CONSOLIDATE_DELETION_TICKET configuration.
# Only used when the engine is configured without one, so that the pipeline
# degrades to a sane mapping instead of silently grouping everything together.
DEFAULT_INTERFACE_DICT = {
    "werum": "Werum",
    "exprivia": "Exprivia",
    "acri": "Acri",
    "cloudferro": "CloudFerro",
    "dlr": "S5P_DLR",
    "cdse": "DAS",
    "dhus": "DHUS",
}

# attachment file names are conventionnaly OMCS-1234_LTA_Werum_DelList_20250528.csv
_TOKEN_SPLIT = re.compile(r"[_\-.\s]+")

# prefix of the group key used when the service cannot be resolved. It embeds the
# report name so that two unresolved attachments never share a group: an
# unresolved attachment can only ever supersede itself.
UNRESOLVED_PREFIX = "__unresolved__"


class AttachmentInfo:
    """Identity of an attachment, as seen from the deletion documents it produced"""

    __slots__ = ("attachment_id", "created", "report_name", "service_id")

    def __init__(self, attachment_id, created=None, report_name="", service_id=None):
        self.attachment_id = attachment_id

        self.created = created

        self.report_name = report_name

        self.service_id = service_id

    def __repr__(self):
        return (
            f"AttachmentInfo({self.attachment_id!r}, created={self.created!r}, "
            f"report_name={self.report_name!r}, service_id={self.service_id!r})"
        )


def chunked(iterable, size):
    """Split an iterable in lists of at most `size` elements

    Args:
        iterable (Iterable): any iterable
        size (int): maximum chunk length

    Yields:
        list: a chunk
    """
    iterator = iter(iterable)

    while chunk := list(itertools.islice(iterator, size)):
        yield chunk


def strip_issue_prefix(report_name: str, issue_key: str) -> str:
    """Remove the issue key prepended to an attachment file name by the collector

    'OMCS-1234_LTA_Werum_DelList.csv' -> 'LTA_Werum_DelList.csv'

    Mirrors JIRAExtendedCollector, which downloads attachments to
    f"{issue.key}_{attachment.filename}" when attachement_prefix is enabled. When
    that option is disabled the report name has no prefix and this is a no-op.

    Args:
        report_name (str): attachment file name, possibly prefixed
        issue_key (str): issue key

    Returns:
        str: the unprefixed file name
    """
    if not report_name:
        return ""

    prefix = f"{issue_key}_"

    return report_name[len(prefix) :] if report_name.startswith(prefix) else report_name


def extract_service_id(report_name: str, interface_dict: dict = None, default=None):
    """Resolve the service an attachment targets, from its file name

    Tokens are matched against the interface_dict keys (the configured aliases,
    ie 'cdse' -> 'DAS') then against its values, so that a file already named
    with the canonical service identifier resolves too.

    Args:
        report_name (str): attachment file name
        interface_dict (dict, optional): alias -> service identifier mapping.
            Defaults to DEFAULT_INTERFACE_DICT.
        default: value returned when no token matches. Defaults to None.

    Returns:
        str: the service identifier, or `default`
    """
    if not report_name:
        return default

    if interface_dict is None:
        interface_dict = DEFAULT_INTERFACE_DICT

    lookup = {key.lower(): value for key, value in interface_dict.items()}

    # a file may be named with the canonical identifier rather than the alias
    lookup.update({value.lower(): value for value in interface_dict.values()})

    stem = report_name.rsplit(".", 1)[0]

    for token in _TOKEN_SPLIT.split(stem):
        if service_id := lookup.get(token.lower()):
            return service_id

    return default


def resolve_service_id(report_name, issue, interface_dict=None, logger=None) -> str:
    """Get the group discriminator of an attachment

    Grouping drives a destructive operation: attachments sharing a group compete,
    and the losers have their deletions rolled back. So the fallbacks degrade
    towards doing nothing rather than towards rolling back the wrong thing, and
    this never returns a value shared by two unrelated attachments.

    Args:
        report_name (str): attachment file name
        issue (CdsDeletionIssue): the deletion ticket
        interface_dict (dict, optional): alias -> service identifier mapping
        logger (Logger, optional): logger for the unresolved case

    Returns:
        str: the group discriminator
    """
    if service_id := extract_service_id(report_name, interface_dict):
        return service_id

    # a ticket declaring a single interface has only one possible target
    interfaces = list(issue.deletion_interfaces or [])

    if len(interfaces) == 1:
        return interfaces[0]

    # give up on cross-attachment supersession: the attachment becomes its own
    # group. Supersession of the same file by a re-upload still works, as it is
    # ranked on attachment_created within the group.
    if logger:
        logger.warning(
            "[%s] cannot resolve a service identifier from %r (interfaces=%s): "
            "attachment isolated in its own group, no supersession applied",
            issue.key,
            report_name,
            interfaces,
        )

    return f"{UNRESOLVED_PREFIX}{report_name}"


def _rank(attachment: AttachmentInfo):
    """Sort key electing the current attachment of a group: most recent first.

    JIRA attachment identifiers are numeric strings that increase monotonically,
    so they are a sound tie breaker for attachments sharing a creation date. They
    are compared numerically, otherwise '9' would outrank '10'.
    """
    attachment_id = attachment.attachment_id or ""

    if attachment_id.isdigit():
        id_rank = (1, int(attachment_id), "")
    else:
        id_rank = (0, 0, attachment_id)

    # creation dates are datetimes: keep them out of the comparison entirely when
    # one is missing rather than mixing types
    created = attachment.created

    created_rank = (1, created) if created is not None else (0, None)

    return (created_rank, id_rank)


def build_groups(attachments, issue):
    """Split the attachments of a ticket into current and stale ones

    An attachment is stale when it has been detached from the ticket, or when
    another attachment of the same (interface_type, service_id) group is more
    recent.

    Args:
        attachments (List[AttachmentInfo]): attachments that produced deletions
        issue (CdsDeletionIssue): the deletion ticket

    Returns:
        tuple: (current attachment identifiers, stale attachment identifiers)
    """
    known_ids = issue.current_attachment_ids

    detached = set()

    if known_ids:
        # an empty attachment_ids means the ticket has not been collected since
        # attachment identities are recorded: there is no ground truth, so the
        # detachment rule stays inert rather than declaring everything stale.
        detached = {
            attachment.attachment_id
            for attachment in attachments
            if attachment.attachment_id not in known_ids
        }

    grouped = {}

    for attachment in attachments:
        if attachment.attachment_id in detached:
            continue

        grouped.setdefault(attachment.service_id, []).append(attachment)

    current = []

    stale = list(detached)

    for group in grouped.values():
        latest = max(group, key=_rank)

        current.append(latest.attachment_id)

        stale.extend(
            attachment.attachment_id for attachment in group if attachment is not latest
        )

    return current, stale
