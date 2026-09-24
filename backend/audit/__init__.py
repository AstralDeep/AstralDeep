"""HIPAA/NIST-aligned per-user audit log: append-only, hash-chained rows with HMAC
payload digests and no raw PHI. Exposed via audit/repository.py, audit/recorder.py,
and audit/hooks.py; schemas.py defines the row shape.
"""

from .schemas import (
    AuditEventCreate,
    AuditEventDTO,
    ArtifactPointer,
    EVENT_CLASSES,
    OUTCOMES,
)

__all__ = [
    "AuditEventCreate",
    "AuditEventDTO",
    "ArtifactPointer",
    "EVENT_CLASSES",
    "OUTCOMES",
]
