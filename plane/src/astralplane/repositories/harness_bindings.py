"""Typed immutable harness profile identities and operation binding records shared by
work_admission.py's owner- and execution-fence-checked binding methods. Profile selection,
provider routing, and artifact availability policy remain with the embedding host.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from astralplane.repositories import RepositoryDataError, RepositoryValidationError

_PROFILE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class HarnessProfileIdentity:
    profile: str
    version: str
    artifact_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.profile, str) or _PROFILE_RE.fullmatch(self.profile) is None:
            raise RepositoryValidationError(
                "harness profile must be bounded lowercase snake_case"
            )
        if (
            not isinstance(self.version, str)
            or len(self.version) > 64
            or _VERSION_RE.fullmatch(self.version) is None
        ):
            raise RepositoryValidationError(
                "harness profile version must be a bounded numeric semantic version"
            )
        if (
            not isinstance(self.artifact_digest, str)
            or _DIGEST_RE.fullmatch(self.artifact_digest) is None
        ):
            raise RepositoryValidationError(
                "harness artifact digest must be a lowercase SHA-256 digest"
            )

    @property
    def profile_id(self) -> str:
        return self.profile

    @property
    def profile_version(self) -> str:
        return self.version


@dataclass(frozen=True, slots=True)
class OperationHarnessBinding:
    operation_id: uuid.UUID
    identity: HarnessProfileIdentity
    execution_generation: int
    bound_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, uuid.UUID):
            raise RepositoryValidationError("operation_id must be a UUID")
        if not isinstance(self.identity, HarnessProfileIdentity):
            raise RepositoryValidationError(
                "identity must be a HarnessProfileIdentity"
            )
        if (
            isinstance(self.execution_generation, bool)
            or not isinstance(self.execution_generation, int)
            or self.execution_generation <= 0
        ):
            raise RepositoryValidationError(
                "execution_generation must be a positive integer"
            )
        if (
            not isinstance(self.bound_at, datetime)
            or self.bound_at.tzinfo is None
            or self.bound_at.utcoffset() is None
        ):
            raise RepositoryValidationError("bound_at must be timezone-aware")

    @property
    def profile(self) -> str:
        return self.identity.profile

    @property
    def version(self) -> str:
        return self.identity.version

    @property
    def artifact_digest(self) -> str:
        return self.identity.artifact_digest


def operation_harness_binding_from_row(row: Mapping[str, Any]) -> OperationHarnessBinding:
    try:
        operation_id = row["operation_id"]
        if not isinstance(operation_id, uuid.UUID):
            operation_id = uuid.UUID(str(operation_id))
        bound_at = row["bound_at"]
        if not isinstance(bound_at, datetime):
            raise ValueError("bound_at is not a timestamp")
        if bound_at.tzinfo is None or bound_at.utcoffset() is None:
            raise ValueError("bound_at is not timezone-aware")
        return OperationHarnessBinding(
            operation_id=operation_id,
            identity=HarnessProfileIdentity(
                profile=str(row["profile"]),
                version=str(row["profile_version"]),
                artifact_digest=str(row["artifact_digest"]),
            ),
            execution_generation=int(row["execution_generation"]),
            bound_at=bound_at.astimezone(UTC),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RepositoryDataError(
            "persisted operation harness binding is invalid"
        ) from exc


__all__ = (
    "HarnessProfileIdentity",
    "OperationHarnessBinding",
    "operation_harness_binding_from_row",
)