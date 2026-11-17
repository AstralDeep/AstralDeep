"""Declared AstralPlane schema lineage: DataPlaneRevision names one immutable target
revision and its readable predecessor range, consulted by compatibility.py and
database/migrations.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from astralplane.errors import SchemaRevisionError

_REVISION_PATTERN = re.compile(r"^[0-9]{3}\.[0-9]{3}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

SCHEMA_PREDECESSOR_REVISION: Final = "089.021"
SCHEMA_REVISION: Final = "089.022"
READ_COMPATIBLE_FROM: Final = "066.001"
ADVISORY_LOCK_IDS: Final = ((1095980114, 60001), (1095980114, 60002))


def validate_revision(value: str, *, field: str = "revision") -> str:
    if not isinstance(value, str) or _REVISION_PATTERN.fullmatch(value) is None:
        raise SchemaRevisionError(f"{field} must be a canonical numeric revision")
    return value


@dataclass(frozen=True, slots=True)
class DataPlaneRevision:
    schema_revision: str
    read_compatible_from: tuple[str, ...]
    migration_digest: str
    advisory_lock_ids: tuple[tuple[int, int], ...] = ADVISORY_LOCK_IDS
    accepted_predecessor_digests: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        validate_revision(self.schema_revision, field="schema revision")
        if not isinstance(self.read_compatible_from, tuple) or not self.read_compatible_from:
            raise SchemaRevisionError("read-compatible revision set must not be empty")
        normalized_sources = tuple(
            validate_revision(value, field="read-compatible revision")
            for value in self.read_compatible_from
        )
        if len(set(normalized_sources)) != len(normalized_sources):
            raise SchemaRevisionError("read-compatible revisions must be unique")
        if _SHA256_PATTERN.fullmatch(self.migration_digest) is None:
            raise SchemaRevisionError("migration digest must be lowercase SHA-256")
        if not isinstance(self.advisory_lock_ids, tuple) or not self.advisory_lock_ids:
            raise SchemaRevisionError("at least one advisory lock identity is required")
        for lock_id in self.advisory_lock_ids:
            if (
                not isinstance(lock_id, tuple)
                or len(lock_id) != 2
                or not all(isinstance(part, int) for part in lock_id)
            ):
                raise SchemaRevisionError("advisory lock identities must be integer pairs")
        if not isinstance(self.accepted_predecessor_digests, tuple):
            raise SchemaRevisionError("accepted predecessor digests must be an immutable tuple")
        predecessor_revisions: set[str] = set()
        for predecessor in self.accepted_predecessor_digests:
            if not isinstance(predecessor, tuple) or len(predecessor) != 2:
                raise SchemaRevisionError(
                    "accepted predecessor digest entries must be revision/digest pairs"
                )
            source_revision = validate_revision(
                predecessor[0], field="accepted predecessor revision"
            )
            source_digest = predecessor[1]
            if source_revision not in normalized_sources:
                raise SchemaRevisionError(
                    "accepted predecessor digest revision must be read-compatible"
                )
            if source_revision in predecessor_revisions:
                raise SchemaRevisionError("accepted predecessor digest revisions must be unique")
            if (
                not isinstance(source_digest, str)
                or _SHA256_PATTERN.fullmatch(source_digest) is None
            ):
                raise SchemaRevisionError("accepted predecessor digest must be lowercase SHA-256")
            predecessor_revisions.add(source_revision)

    @property
    def migration_lock(self) -> tuple[int, int]:
        return self.advisory_lock_ids[0]

    def accepts_reader_at(self, revision: str) -> bool:
        observed = validate_revision(revision, field="observed revision")
        return observed == self.schema_revision or observed in self.read_compatible_from

    def predecessor_digest_for(self, revision: str | None) -> str | None:
        if revision is None:
            return None
        observed = validate_revision(revision, field="predecessor revision")
        return next(
            (
                digest
                for predecessor, digest in self.accepted_predecessor_digests
                if predecessor == observed
            ),
            None,
        )


__all__ = (
    "ADVISORY_LOCK_IDS",
    "READ_COMPATIBLE_FROM",
    "SCHEMA_PREDECESSOR_REVISION",
    "SCHEMA_REVISION",
    "DataPlaneRevision",
    "validate_revision",
)
