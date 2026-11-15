"""Machine-readable inspection of AstralPlane's producer schema/revision compatibility,
built from database/migrations.py and database/revision.py for api.py's
inspect_compatibility().
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from astralplane.database.migrations import CURRENT_DATA_PLANE_REVISION, MIGRATION_DIGEST
from astralplane.database.revision import ADVISORY_LOCK_IDS, SCHEMA_REVISION, DataPlaneRevision

CONTRACT_VERSION: Final = "astralplane.contract/v1"
PACKAGE_VERSION: Final = "0.1.12"
MINIMUM_CONSUMER_VERSION: Final = "0.1.0"
BLOB_LAYOUT_VERSION: Final = "astralplane.blob-layout/v1"
RECOVERY_CONTRACT_VERSION: Final = "astralplane.recovery/v1"

_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _version_key(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER.fullmatch(value) if isinstance(value, str) else None
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


class CompatibilityState(StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    state: CompatibilityState
    contract_version: str
    package_version: str
    schema_revision: str
    read_compatible_from: tuple[str, ...]
    migration_digest: str
    minimum_consumer_version: str
    blob_layout_version: str
    recovery_contract_version: str
    advisory_lock_ids: tuple[tuple[int, int], ...]
    reasons: tuple[str, ...]

    @property
    def compatible(self) -> bool:
        return self.state is CompatibilityState.COMPATIBLE

    def to_dict(self) -> dict[str, object]:
        return {
            "advisory_lock_ids": [list(item) for item in self.advisory_lock_ids],
            "blob_layout_version": self.blob_layout_version,
            "compatible": self.compatible,
            "contract_version": self.contract_version,
            "migration_digest": self.migration_digest,
            "minimum_consumer_version": self.minimum_consumer_version,
            "package_version": self.package_version,
            "read_compatible_from": list(self.read_compatible_from),
            "reasons": list(self.reasons),
            "recovery_contract_version": self.recovery_contract_version,
            "schema_revision": self.schema_revision,
            "state": self.state.value,
        }


def inspect_compatibility(
    *,
    expected_contract_version: str,
    observed_schema_revision: str,
    consumer_version: str,
    data_plane_revision: DataPlaneRevision = CURRENT_DATA_PLANE_REVISION,
) -> CompatibilityReport:
    reasons: list[str] = []
    if expected_contract_version != CONTRACT_VERSION:
        reasons.append("contract_version_mismatch")
    try:
        schema_accepted = data_plane_revision.accepts_reader_at(observed_schema_revision)
    except Exception:
        schema_accepted = False
    if not schema_accepted:
        reasons.append("schema_revision_incompatible")
    consumer_key = _version_key(consumer_version)
    minimum_key = _version_key(MINIMUM_CONSUMER_VERSION)
    if consumer_key is None or minimum_key is None or consumer_key < minimum_key:
        reasons.append("consumer_version_too_old")
    return CompatibilityReport(
        state=(CompatibilityState.INCOMPATIBLE if reasons else CompatibilityState.COMPATIBLE),
        contract_version=CONTRACT_VERSION,
        package_version=PACKAGE_VERSION,
        schema_revision=SCHEMA_REVISION,
        read_compatible_from=data_plane_revision.read_compatible_from,
        migration_digest=data_plane_revision.migration_digest,
        minimum_consumer_version=MINIMUM_CONSUMER_VERSION,
        blob_layout_version=BLOB_LAYOUT_VERSION,
        recovery_contract_version=RECOVERY_CONTRACT_VERSION,
        advisory_lock_ids=ADVISORY_LOCK_IDS,
        reasons=tuple(reasons),
    )


__all__ = (
    "ADVISORY_LOCK_IDS",
    "BLOB_LAYOUT_VERSION",
    "CONTRACT_VERSION",
    "MIGRATION_DIGEST",
    "MINIMUM_CONSUMER_VERSION",
    "PACKAGE_VERSION",
    "RECOVERY_CONTRACT_VERSION",
    "SCHEMA_REVISION",
    "CompatibilityReport",
    "CompatibilityState",
    "inspect_compatibility",
)
