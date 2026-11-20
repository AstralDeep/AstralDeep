"""Qualifies one explicit PostgreSQL database and every paired blob root as a complete
restored unit, then delegates commit-safe session retirement through recovery.py.
It never chooses a backup, elects a writer, opens admission, or mutates blob content.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from astralplane.blob_store import (
    ExplicitRootStreamingBlobStore,
    validate_blob_owner_id,
    validate_blob_storage_key,
)
from astralplane.compatibility import BLOB_LAYOUT_VERSION
from astralplane.database.migrations import CURRENT_DATA_PLANE_REVISION, MIGRATION_REGISTRY
from astralplane.endpoint_readiness import EndpointFence, inspect_endpoint_readiness
from astralplane.errors import PlaneError
from astralplane.recovery import retire_restored_sessions

JOINT_RESTORE_CONTRACT_VERSION: Final = "astralplane.joint-restore/v1"
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_ROOT_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LOCK_NAME: Final = re.compile(r"^[0-9a-f]{64}\.lock$")
_STAGE_MARKER_NAME: Final = re.compile(r"^\.astralplane-stage-[0-9a-f]{64}\.lock$")
_EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()
_CONTROL_DIRECTORY: Final = ".astralplane-owner-locks"
_MAX_KEY_COMPONENTS: Final = 32
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400
_READ_CHUNK_BYTES: Final = 1024 * 1024


class JointRestoreError(PlaneError):
    default_code = "joint_restore_unavailable"


@dataclass(frozen=True, slots=True)
class RestoreBlobEntry:
    root: str
    storage_key: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.root, str) or _ROOT_NAME.fullmatch(self.root) is None:
            raise ValueError("restore blob root must be a bounded identifier")
        _validate_restore_storage_key(self.storage_key)
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("restore blob size must be a non-negative integer")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("restore blob digest must be a lowercase SHA-256")
        if _is_stage_marker(self.storage_key) and (
            self.size_bytes != 0 or self.sha256 != _EMPTY_SHA256
        ):
            raise ValueError("restore stage markers must be empty")


@dataclass(frozen=True, slots=True)
class JointRestoreEvidence:
    contract_version: str
    schema_revision: str
    migration_digest: str
    database_identity: str
    schema_identity: str
    blob_roots: tuple[str, ...]
    blob_identity: str
    blob_inventory_digest: str
    blob_count: int
    blob_size_bytes: int
    fence_digest: str
    digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "blob_count": self.blob_count,
            "blob_identity": self.blob_identity,
            "blob_inventory_digest": self.blob_inventory_digest,
            "blob_roots": list(self.blob_roots),
            "blob_size_bytes": self.blob_size_bytes,
            "contract_version": self.contract_version,
            "database_identity": self.database_identity,
            "digest": self.digest,
            "fence_digest": self.fence_digest,
            "migration_digest": self.migration_digest,
            "schema_identity": self.schema_identity,
            "schema_revision": self.schema_revision,
            "state": "qualified",
        }


@dataclass(frozen=True, slots=True)
class JointRestoreCompletion:
    evidence: JointRestoreEvidence
    retired_sessions: int

    def to_dict(self) -> dict[str, object]:
        return {
            **self.evidence.to_dict(),
            "retired_sessions": self.retired_sessions,
            "state": "recovered",
        }


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _identifier(value: object) -> bool:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= 63
    except UnicodeEncodeError:
        return False


def _refuse(code: str, reason: str) -> None:
    raise JointRestoreError(
        "joint restore is not qualified",
        code=code,
        metadata={"reason": reason},
    )


def _normalized_roots(
    value: object,
) -> dict[str, str | os.PathLike[str]]:
    if not isinstance(value, Mapping) or not value:
        _refuse("joint_restore_input_incompatible", "blob_roots_invalid")
    normalized: dict[str, str | os.PathLike[str]] = {}
    for name, path in value.items():
        if not isinstance(name, str) or _ROOT_NAME.fullmatch(name) is None:
            _refuse("joint_restore_input_incompatible", "blob_roots_invalid")
        if not isinstance(path, (str, os.PathLike)):
            _refuse("joint_restore_input_incompatible", "blob_roots_invalid")
        try:
            selected = os.fspath(path)
        except TypeError:
            _refuse("joint_restore_input_incompatible", "blob_roots_invalid")
        if not isinstance(selected, str) or not selected:
            _refuse("joint_restore_input_incompatible", "blob_roots_invalid")
        normalized[name] = path
    return normalized


def _normalized_entries(
    value: object,
    roots: Mapping[str, object],
) -> tuple[RestoreBlobEntry, ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        _refuse("joint_restore_input_incompatible", "blob_inventory_invalid")
    entries: list[RestoreBlobEntry] = []
    seen: set[tuple[str, str]] = set()
    folded: set[tuple[str, str]] = set()
    try:
        for entry in value:
            if not isinstance(entry, RestoreBlobEntry):
                _refuse("joint_restore_input_incompatible", "blob_inventory_invalid")
            if entry.root not in roots:
                _refuse("joint_restore_input_incompatible", "blob_inventory_root_unknown")
            key = (entry.root, entry.storage_key)
            folded_key = (entry.root, entry.storage_key.casefold())
            if key in seen or folded_key in folded:
                _refuse("joint_restore_input_incompatible", "blob_inventory_duplicate")
            seen.add(key)
            folded.add(folded_key)
            entries.append(entry)
    except (TypeError, ValueError):
        _refuse("joint_restore_input_incompatible", "blob_inventory_invalid")
    return tuple(sorted(entries, key=lambda item: (item.root, item.storage_key)))


def _inventory_digest(
    roots: Iterable[str],
    entries: Iterable[RestoreBlobEntry],
) -> str:
    return _digest(
        {
            "contract_version": JOINT_RESTORE_CONTRACT_VERSION,
            "entries": [
                {
                    "root": entry.root,
                    "sha256": entry.sha256,
                    "size_bytes": entry.size_bytes,
                    "storage_key": entry.storage_key,
                }
                for entry in entries
            ],
            "roots": sorted(roots),
        }
    )


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _is_stage_marker(storage_key: str) -> bool:
    if not isinstance(storage_key, str):
        return False
    components = storage_key.split("/")
    if len(components) <= 1 or _STAGE_MARKER_NAME.fullmatch(components[-1]) is None:
        return False
    try:
        validate_blob_owner_id(components[0])
    except ValueError:
        return False
    candidate = f"{'/'.join(components[:-1])}/{'x' * len(components[-1])}"
    try:
        return validate_blob_storage_key(candidate) == candidate
    except ValueError:
        return False


def _validate_restore_storage_key(storage_key: str) -> str:
    try:
        return validate_blob_storage_key(storage_key)
    except ValueError:
        if _is_stage_marker(storage_key):
            return storage_key
        raise


def _verify_control_directory(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except OSError:
        _refuse("joint_restore_inventory_mismatch", "blob_control_unavailable")
    if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        _refuse("joint_restore_inventory_mismatch", "blob_control_unsafe")
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                entry_metadata = entry.stat(follow_symlinks=False)
                if (
                    _is_reparse(entry_metadata)
                    or not stat.S_ISREG(entry_metadata.st_mode)
                    or _LOCK_NAME.fullmatch(entry.name) is None
                ):
                    _refuse("joint_restore_inventory_mismatch", "blob_control_unsafe")
    except OSError:
        _refuse("joint_restore_inventory_mismatch", "blob_control_unavailable")


def _file_evidence(path: Path, *, require_single_link: bool = False) -> tuple[int, str]:
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            _is_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or (require_single_link and metadata.st_nlink != 1)
        ):
            _refuse("joint_restore_inventory_mismatch", "blob_object_unsafe")
        hasher = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                hasher.update(chunk)
                size += len(chunk)
            final = os.fstat(stream.fileno())
        if final.st_size != size:
            _refuse("joint_restore_inventory_mismatch", "blob_content_changed")
        return size, hasher.hexdigest()
    except OSError:
        _refuse("joint_restore_inventory_mismatch", "blob_unavailable")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return 0, ""


def _walk_root(
    directory: Path,
    components: tuple[str, ...],
    observed: list[RestoreBlobEntry],
    *,
    root_name: str,
) -> None:
    if len(components) > _MAX_KEY_COMPONENTS:
        _refuse("joint_restore_inventory_mismatch", "blob_membership_mismatch")
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not components and entry.name == _CONTROL_DIRECTORY:
                    _verify_control_directory(Path(entry.path))
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if _is_reparse(metadata):
                    _refuse("joint_restore_inventory_mismatch", "blob_link_unsafe")
                if stat.S_ISDIR(metadata.st_mode):
                    _walk_root(
                        Path(entry.path),
                        (*components, entry.name),
                        observed,
                        root_name=root_name,
                    )
                elif stat.S_ISREG(metadata.st_mode):
                    storage_key = "/".join((*components, entry.name))
                    try:
                        _validate_restore_storage_key(storage_key)
                    except ValueError:
                        _refuse("joint_restore_inventory_mismatch", "blob_key_unsafe")
                    stage_marker = _is_stage_marker(storage_key)
                    if stage_marker and metadata.st_nlink != 1:
                        _refuse("joint_restore_inventory_mismatch", "blob_object_unsafe")
                    size, digest = _file_evidence(
                        Path(entry.path), require_single_link=stage_marker
                    )
                    if stage_marker and (size != 0 or digest != _EMPTY_SHA256):
                        _refuse("joint_restore_inventory_mismatch", "blob_content_changed")
                    observed.append(
                        RestoreBlobEntry(
                            root=root_name,
                            storage_key=storage_key,
                            size_bytes=size,
                            sha256=digest,
                        )
                    )
                else:
                    _refuse("joint_restore_inventory_mismatch", "blob_object_unsafe")
    except OSError:
        _refuse("joint_restore_inventory_mismatch", "blob_root_unavailable")


def _scan_root(
    name: str,
    path: str | os.PathLike[str],
    expected: Mapping[str, RestoreBlobEntry],
) -> tuple[str, int, int]:
    store: ExplicitRootStreamingBlobStore | None = None
    try:
        store = ExplicitRootStreamingBlobStore(path, create_root=False)
        identity = store.root_identity_digest
        supplied = Path(path)
        metadata = os.lstat(supplied)
        if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            _refuse("joint_restore_inventory_mismatch", "blob_root_unsafe")
        root = supplied.resolve(strict=True)
        observed: list[RestoreBlobEntry] = []
        _walk_root(root, (), observed, root_name=name)
        if store.root_identity_digest != identity:
            _refuse("joint_restore_inventory_mismatch", "blob_root_changed")
    except JointRestoreError:
        raise
    except Exception:
        _refuse("joint_restore_inventory_mismatch", "blob_root_unavailable")
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                _refuse("joint_restore_inventory_mismatch", "blob_root_cleanup_failed")
    actual = {entry.storage_key: entry for entry in observed}
    if set(actual) != set(expected):
        _refuse("joint_restore_inventory_mismatch", "blob_membership_mismatch")
    for key, entry in actual.items():
        wanted = expected[key]
        if entry.size_bytes != wanted.size_bytes:
            _refuse("joint_restore_inventory_mismatch", "blob_size_mismatch")
        if entry.sha256 != wanted.sha256:
            _refuse("joint_restore_inventory_mismatch", "blob_digest_mismatch")
    return identity, len(actual), sum(entry.size_bytes for entry in actual.values())


def _blob_evidence(
    roots: Mapping[str, str | os.PathLike[str]],
    entries: tuple[RestoreBlobEntry, ...],
) -> tuple[str, int, int]:
    expected_by_root: dict[str, dict[str, RestoreBlobEntry]] = {
        name: {} for name in roots
    }
    for entry in entries:
        expected_by_root[entry.root][entry.storage_key] = entry
    identities: list[tuple[str, str]] = []
    count = 0
    size = 0
    for name in sorted(roots):
        identity, root_count, root_size = _scan_root(
            name,
            roots[name],
            expected_by_root[name],
        )
        identities.append((name, identity))
        count += root_count
        size += root_size
    observed = tuple(identity for _, identity in identities)
    if len(set(observed)) != len(observed):
        _refuse("joint_restore_inventory_mismatch", "blob_root_duplicate")
    return _digest(identities), count, size


def _evidence(
    *,
    fence: EndpointFence,
    inventory_digest: str,
    blob_identity: str,
    blob_count: int,
    blob_size_bytes: int,
) -> JointRestoreEvidence:
    digest = _digest(
        {
            "blob_count": blob_count,
            "blob_identity": blob_identity,
            "blob_inventory_digest": inventory_digest,
            "blob_size_bytes": blob_size_bytes,
            "contract_version": JOINT_RESTORE_CONTRACT_VERSION,
            "fence_digest": fence.digest,
        }
    )
    return JointRestoreEvidence(
        contract_version=JOINT_RESTORE_CONTRACT_VERSION,
        schema_revision=fence.schema_revision,
        migration_digest=fence.migration_digest,
        database_identity=fence.database_identity,
        schema_identity=fence.schema_identity,
        blob_roots=fence.blob_roots,
        blob_identity=blob_identity,
        blob_inventory_digest=inventory_digest,
        blob_count=blob_count,
        blob_size_bytes=blob_size_bytes,
        fence_digest=fence.digest,
        digest=digest,
    )


def qualify_joint_restore(
    *,
    database_url: str,
    blob_roots: Mapping[str, str | os.PathLike[str]],
    blob_inventory: Iterable[RestoreBlobEntry],
    expected_database: str,
    expected_schema: str,
    expected_schema_revision: str,
    expected_migration_digest: str,
    expected_blob_inventory_digest: str | None = None,
    expected_blob_roots: Iterable[str] | None = None,
    expected_blob_layout_version: str = BLOB_LAYOUT_VERSION,
    connect_timeout_seconds: int = 5,
    acquire_timeout_seconds: float = 1.0,
) -> JointRestoreEvidence:
    try:
        if not isinstance(database_url, str) or not database_url.strip():
            _refuse("joint_restore_input_incompatible", "database_url_invalid")
        if not _identifier(expected_database):
            _refuse("joint_restore_input_incompatible", "expected_database_invalid")
        if (
            not _identifier(expected_schema)
            or expected_schema.startswith("pg_")
            or expected_schema == "information_schema"
        ):
            _refuse("joint_restore_input_incompatible", "expected_schema_invalid")
        if expected_schema_revision != CURRENT_DATA_PLANE_REVISION.schema_revision:
            _refuse("joint_restore_input_incompatible", "schema_revision_not_current")
        if (
            expected_migration_digest != CURRENT_DATA_PLANE_REVISION.migration_digest
            or expected_migration_digest != MIGRATION_REGISTRY.digest
        ):
            _refuse("joint_restore_input_incompatible", "migration_digest_not_current")
        if expected_blob_layout_version != BLOB_LAYOUT_VERSION:
            _refuse("joint_restore_input_incompatible", "blob_layout_not_current")
        if expected_blob_inventory_digest is not None and (
            not isinstance(expected_blob_inventory_digest, str)
            or _SHA256.fullmatch(expected_blob_inventory_digest) is None
        ):
            _refuse("joint_restore_input_incompatible", "blob_inventory_digest_invalid")
        roots = _normalized_roots(blob_roots)
        if expected_blob_roots is not None:
            if isinstance(expected_blob_roots, (str, bytes)) or not isinstance(
                expected_blob_roots, Iterable
            ):
                _refuse("joint_restore_input_incompatible", "expected_blob_roots_invalid")
            expected_roots = tuple(expected_blob_roots)
            if (
                any(
                    not isinstance(name, str) or _ROOT_NAME.fullmatch(name) is None
                    for name in expected_roots
                )
                or len(set(expected_roots)) != len(expected_roots)
            ):
                _refuse("joint_restore_input_incompatible", "expected_blob_roots_invalid")
            if tuple(sorted(expected_roots)) != tuple(sorted(roots)):
                _refuse("joint_restore_inventory_mismatch", "blob_root_set_mismatch")
        entries = _normalized_entries(blob_inventory, roots)
        inventory_digest = _inventory_digest(roots, entries)
        if (
            expected_blob_inventory_digest is not None
            and expected_blob_inventory_digest != inventory_digest
        ):
            _refuse("joint_restore_inventory_mismatch", "blob_inventory_digest_mismatch")
        blob_identity, blob_count, blob_size_bytes = _blob_evidence(roots, entries)
        report = inspect_endpoint_readiness(
            database_url=database_url,
            blob_roots=roots,
            blob_inventory_digest=inventory_digest,
            expected_database=expected_database,
            expected_schema=expected_schema,
            expected_schema_revision=expected_schema_revision,
            expected_migration_digest=expected_migration_digest,
            expected_blob_layout_version=expected_blob_layout_version,
            connect_timeout_seconds=connect_timeout_seconds,
            acquire_timeout_seconds=acquire_timeout_seconds,
        )
        if not report.writable or report.fence is None:
            _refuse(
                "joint_restore_database_unqualified",
                ",".join(report.reasons) or "database_not_writable",
            )
        fence = report.fence
        if (
            fence.blob_identity != blob_identity
            or fence.blob_inventory_digest != inventory_digest
            or fence.blob_roots != tuple(sorted(roots))
        ):
            _refuse("joint_restore_inventory_mismatch", "blob_fence_mismatch")
        return _evidence(
            fence=fence,
            inventory_digest=inventory_digest,
            blob_identity=blob_identity,
            blob_count=blob_count,
            blob_size_bytes=blob_size_bytes,
        )
    except JointRestoreError:
        raise
    except Exception:
        raise JointRestoreError("joint restore is not qualified") from None


def recover_joint_restore(
    *,
    database_url: str,
    blob_roots: Mapping[str, str | os.PathLike[str]],
    blob_inventory: Iterable[RestoreBlobEntry],
    expected_database: str,
    expected_schema: str,
    expected_schema_revision: str,
    expected_migration_digest: str,
    expected_blob_inventory_digest: str | None = None,
    expected_blob_roots: Iterable[str] | None = None,
    expected_blob_layout_version: str = BLOB_LAYOUT_VERSION,
    connect_timeout_seconds: int = 5,
    acquire_timeout_seconds: float = 1.0,
) -> JointRestoreCompletion:
    evidence = qualify_joint_restore(
        database_url=database_url,
        blob_roots=blob_roots,
        blob_inventory=blob_inventory,
        expected_database=expected_database,
        expected_schema=expected_schema,
        expected_schema_revision=expected_schema_revision,
        expected_migration_digest=expected_migration_digest,
        expected_blob_inventory_digest=expected_blob_inventory_digest,
        expected_blob_roots=expected_blob_roots,
        expected_blob_layout_version=expected_blob_layout_version,
        connect_timeout_seconds=connect_timeout_seconds,
        acquire_timeout_seconds=acquire_timeout_seconds,
    )
    retirement = retire_restored_sessions(
        database_url=database_url,
        expected_database=expected_database,
        expected_schema=expected_schema,
        expected_schema_revision=expected_schema_revision,
        expected_migration_digest=expected_migration_digest,
    )
    return JointRestoreCompletion(
        evidence=evidence,
        retired_sessions=retirement.retired_sessions,
    )


__all__ = (
    "JOINT_RESTORE_CONTRACT_VERSION",
    "JointRestoreCompletion",
    "JointRestoreError",
    "JointRestoreEvidence",
    "RestoreBlobEntry",
    "qualify_joint_restore",
    "recover_joint_restore",
)
