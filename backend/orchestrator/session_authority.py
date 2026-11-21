"""Typed prerequisites that bind an assignment operation to its exact authority record.
The Work services and persistent-agent services use them to refuse work whose operation record changed or expired; they grant nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

from astralplane.repositories.assignment_models import AssignmentOperationRead, AssignmentRecord



class SessionAuthorityUnavailable(Exception):
    pass


def _unavailable() -> None:
    raise SessionAuthorityUnavailable("session_authority_unavailable")


def _uuid4(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except ValueError:
        return False


def _operation_reference(value, owner_id, assignment_id):
    if not isinstance(value, AssignmentOperationRead) or value.continuation_supported is not True:
        _unavailable()
    record = value.assignment
    if (not isinstance(record, AssignmentRecord) or record.owner_id != owner_id
            or record.assignment_id != assignment_id
            or record.execution_profile != "one_shot" or not isinstance(record.operation, Mapping)):
        _unavailable()
    operation = record.operation
    authority = operation.get("authority")
    if (type(operation.get("version")) is not int or operation["version"] != 2
            or not isinstance(authority, Mapping) or authority.get("owner_id") != owner_id
            or authority.get("origin") != "interactive"
            or authority.get("reference_kind") != "session_incarnation"
            or not _uuid4(authority.get("reference_id"))):
        _unavailable()
    expiry = datetime.fromisoformat(authority["expires_at"])
    deadline = datetime.fromisoformat(operation["deadline_at"])
    if (expiry.utcoffset() is None or deadline.utcoffset() is None
            or min(expiry, deadline) <= datetime.now(timezone.utc)):
        _unavailable()
    return record, authority["reference_id"], expiry, deadline


def _operation_context(value, owner_id, assignment_id):
    selected = _operation_reference(value, owner_id, assignment_id)
    if selected[0].lifecycle != "active":
        _unavailable()
    return selected


def _same_operation(current, original):
    return replace(current, updated_at=original.updated_at) == original

