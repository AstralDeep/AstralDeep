"""Tests for src/astralplane/purge.py: owner-isolated tombstone scheduling and
reconciliation, and partial database/blob failure handling across the streaming
executor.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Any

import pytest

from astralplane.blob_store import BlobDeleteResult
from astralplane.errors import PlaneError, SQLContractError
from astralplane.purge import (
    _PURGE_EXECUTOR_AUTHORITY_TOKEN,
    DurablePurgeExecutor,
    PostgresPurgeStore,
    PurgeAttemptState,
    PurgeScheduleResult,
    PurgeStatus,
    PurgeTargetScope,
    PurgeTombstone,
    _PurgeExecutorAuthority,
    _validate_tombstone,
    storage_locator_sha256,
)

NOW = datetime(2026, 8, 13, 22, tzinfo=UTC)
EXECUTOR_AUTHORITY = _PurgeExecutorAuthority(_PURGE_EXECUTOR_AUTHORITY_TOKEN)


@dataclass(frozen=True)
class FakeResult:
    rowcount: int
    status_message: str | None = None
    returned_records: tuple[Mapping[str, Any], ...] = ()


class MemoryPurgeTransaction:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.override_result: FakeResult | None = None

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> FakeResult:
        self.statements.append((statement, parameters))
        if self.override_result is not None:
            result, self.override_result = self.override_result, None
            return result
        if statement.startswith("INSERT INTO astralplane_purge_tombstone"):
            (
                tombstone_id,
                owner_id,
                object_kind,
                object_id,
                storage_key,
                target_scope,
                locator_digest,
                requested_at,
                available_at,
            ) = parameters
            if str(tombstone_id) in self.rows or any(
                row["owner_id"] == owner_id
                and row["object_kind"] == object_kind
                and row["object_id"] == object_id
                for row in self.rows.values()
            ):
                return FakeResult(0)
            self.rows[str(tombstone_id)] = {
                "tombstone_id": tombstone_id,
                "owner_id": owner_id,
                "object_kind": object_kind,
                "object_id": object_id,
                "storage_key": storage_key,
                "target_scope": target_scope,
                "storage_locator_sha256": locator_digest,
                "requested_at": requested_at,
                "status": "pending",
                "attempt_count": 0,
                "version": 0,
                "available_at": available_at,
                "verified_absent_at": None,
                "manual_resolution_evidence_sha256": None,
                "manual_resolved_at": None,
                "last_error_code": None,
                "updated_at": requested_at,
            }
            return FakeResult(1)
        if "SET status = 'purged'" in statement:
            if "manual_resolution_evidence_sha256" in statement:
                (
                    verified_at,
                    evidence_digest,
                    resolved_at,
                    _updated_at,
                    owner_id,
                    tombstone_id,
                    expected_locator_digest,
                    version,
                ) = parameters
                row = self.rows.get(str(tombstone_id))
                if (
                    row is None
                    or row["owner_id"] != owner_id
                    or row["storage_locator_sha256"] != expected_locator_digest
                    or row["version"] != version
                    or row["status"] != "manual_review"
                    or row["target_scope"] != "exact_key"
                ):
                    return FakeResult(0)
                row.update(
                    status="purged",
                    attempt_count=row["attempt_count"] + 1,
                    version=row["version"] + 1,
                    verified_absent_at=verified_at,
                    manual_resolution_evidence_sha256=evidence_digest,
                    manual_resolved_at=resolved_at,
                    last_error_code=None,
                )
                return FakeResult(1)
            (
                verified_at,
                available_at,
                owner_id,
                tombstone_id,
                expected_locator_digest,
                version,
            ) = parameters
            row = self._fenced(owner_id, tombstone_id, version)
            if (
                row is None
                or row["storage_locator_sha256"] != expected_locator_digest
            ):
                return FakeResult(0)
            row.update(
                status="purged",
                attempt_count=row["attempt_count"] + 1,
                version=row["version"] + 1,
                verified_absent_at=verified_at,
                available_at=available_at,
                last_error_code=None,
            )
            return FakeResult(1)
        if "SET status = 'failed'" in statement:
            available_at, error_code, owner_id, tombstone_id, version = parameters
            row = self._fenced(owner_id, tombstone_id, version)
            if row is None:
                return FakeResult(0)
            row.update(
                status="failed",
                attempt_count=row["attempt_count"] + 1,
                version=row["version"] + 1,
                available_at=available_at,
                verified_absent_at=None,
                last_error_code=error_code,
            )
            return FakeResult(1)
        if "SET status = 'manual_review'" in statement:
            error_code, owner_id, tombstone_id, version = parameters
            row = self._fenced(owner_id, tombstone_id, version)
            if row is None:
                return FakeResult(0)
            row.update(
                status="manual_review",
                version=row["version"] + 1,
                last_error_code=error_code,
            )
            return FakeResult(1)
        raise AssertionError(f"unexpected execute: {statement}")

    def fetch_one(
        self, statement: str, parameters: tuple[object, ...] = ()
    ) -> Mapping[str, Any] | None:
        self.statements.append((statement, parameters))
        if "AS has_incomplete" in statement:
            return {
                "has_incomplete": any(
                    row["status"] != "purged" for row in self.rows.values()
                )
            }
        if "AND object_kind = %s" in statement:
            owner_id, object_kind, object_id = parameters
            return next(
                (
                    copy.deepcopy(row)
                    for row in self.rows.values()
                    if row["owner_id"] == owner_id
                    and row["object_kind"] == object_kind
                    and row["object_id"] == object_id
                ),
                None,
            )
        if "target_scope = 'exact_key'" in statement:
            (tombstone_id,) = parameters
            row = self.rows.get(str(tombstone_id))
            return (
                None
                if row is None or row["target_scope"] != "exact_key"
                else copy.deepcopy(row)
            )
        owner_id, tombstone_id = parameters
        row = self.rows.get(str(tombstone_id))
        return None if row is None or row["owner_id"] != owner_id else copy.deepcopy(row)

    def fetch_all(
        self, statement: str, parameters: tuple[object, ...] = ()
    ) -> tuple[Mapping[str, Any], ...]:
        self.statements.append((statement, parameters))
        if "available_at <= %s" in statement:
            observed_at, limit = parameters
            return tuple(
                copy.deepcopy(row)
                for row in sorted(
                    self.rows.values(),
                    key=lambda value: (
                        value["available_at"],
                        value["requested_at"],
                        value["tombstone_id"],
                    ),
                )
                if row["status"] in {"pending", "failed"}
                and row["available_at"] <= observed_at
            )[: int(limit)]
        owner_id = parameters[0]
        return tuple(
            copy.deepcopy(row)
            for row in sorted(
                self.rows.values(), key=lambda value: (value["requested_at"], value["tombstone_id"])
            )
            if row["owner_id"] == owner_id and row["status"] != "purged"
        )

    def _fenced(
        self, owner_id: object, tombstone_id: object, version: object
    ) -> dict[str, Any] | None:
        row = self.rows.get(str(tombstone_id))
        if (
            row is None
            or row["owner_id"] != owner_id
            or row["version"] != version
            or row["status"] not in {"pending", "failed"}
        ):
            return None
        return row


class MemoryPurgeDatabase:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.transaction_count = 0
        self.active_transactions = 0
        self.fail_commit_numbers: set[int] = set()

    @contextmanager
    def transaction(self, **_: object) -> Iterator[MemoryPurgeTransaction]:
        self.transaction_count += 1
        number = self.transaction_count
        working = copy.deepcopy(self.rows)
        self.active_transactions += 1
        try:
            yield MemoryPurgeTransaction(working)
            if number in self.fail_commit_numbers:
                raise RuntimeError("simulated database commit failure")
            self.rows = working
        finally:
            self.active_transactions -= 1


class MemoryBlobStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], bytes] = {}
        self.fail_delete = False
        self.deleted: list[tuple[str, str]] = []

    @property
    def root(self) -> PurePath:
        return PurePath("/configured/runtime/blobs")

    def put(self, *, owner_id: str, key: str, content: bytes) -> str:
        self.values[(owner_id, key)] = content
        return "digest"

    def get(self, *, owner_id: str, key: str) -> bytes:
        return self.values[(owner_id, key)]

    def delete_key(self, *, owner_id: str, key: str) -> bool:
        self.deleted.append((owner_id, key))
        if self.fail_delete:
            raise OSError("host path and credentials must not escape")
        return self.values.pop((owner_id, key), None) is not None

    def delete_prefix(self, *, owner_id: str, prefix: str) -> BlobDeleteResult:
        self.deleted.append((owner_id, prefix))
        if self.fail_delete:
            raise OSError("host path and credentials must not escape")
        matches = tuple(
            key
            for scoped_owner, key in self.values
            if scoped_owner == owner_id and (key == prefix or key.startswith(prefix + "/"))
        )
        for key in matches:
            del self.values[(owner_id, key)]
        return BlobDeleteResult(
            len(matches),
            0,
            self.is_prefix_absent(owner_id=owner_id, prefix=prefix),
        )

    def delete_owner(self, *, owner_id: str) -> BlobDeleteResult:
        self.deleted.append((owner_id, "<owner>"))
        if self.fail_delete:
            raise OSError("host path and credentials must not escape")
        matches = tuple(key for scoped_owner, key in self.values if scoped_owner == owner_id)
        for key in matches:
            del self.values[(owner_id, key)]
        return BlobDeleteResult(len(matches), 0, self.is_owner_absent(owner_id=owner_id))

    def _delete_for_purge(self, authority: Any) -> BlobDeleteResult:
        if authority.target_scope == "attachment_prefix":
            return self.delete_prefix(
                owner_id=authority.owner_id,
                prefix=authority.storage_key,
            )
        return self.delete_owner(owner_id=authority.owner_id)

    def is_absent(self, *, owner_id: str, key: str) -> bool:
        return (owner_id, key) not in self.values

    def is_prefix_absent(self, *, owner_id: str, prefix: str) -> bool:
        return not any(
            scoped_owner == owner_id and (key == prefix or key.startswith(prefix + "/"))
            for scoped_owner, key in self.values
        )

    def is_owner_absent(self, *, owner_id: str) -> bool:
        return not any(scoped_owner == owner_id for scoped_owner, _ in self.values)


class StickyBlobStore(MemoryBlobStore):
    def delete_key(self, *, owner_id: str, key: str) -> bool:
        self.deleted.append((owner_id, key))
        return False

    def delete_prefix(self, *, owner_id: str, prefix: str) -> BlobDeleteResult:
        self.deleted.append((owner_id, prefix))
        return BlobDeleteResult(0, 0, False)


def attachment_row(
    *, owner_id: str, attachment_id: str, deleted_at: int | None = None
) -> dict[str, Any]:
    return {
        "attachment_id": attachment_id,
        "user_id": owner_id,
        "filename": "fixture.bin",
        "content_type": "application/octet-stream",
        "category": "data",
        "extension": "bin",
        "size_bytes": 4,
        "sha256": hashlib.sha256(b"data").hexdigest(),
        "storage_path": f"{owner_id}/{attachment_id}/fixture.bin",
        "created_at": 1,
        "deleted_at": deleted_at,
        "materialization_state": "ready",
        "materialization_lease_id": None,
        "materialization_lease_version": None,
        "materialization_lease_expires_at": None,
    }


class ScheduleTransaction(MemoryPurgeTransaction):
    def __init__(
        self,
        rows: dict[str, dict[str, Any]],
        attachments: dict[str, dict[str, Any]],
        owner_states: dict[str, dict[str, Any]],
    ) -> None:
        super().__init__(rows)
        self.attachments = attachments
        self.owner_states = owner_states

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> FakeResult:
        if self.override_result is not None:
            return super().execute(statement, parameters)
        if statement.startswith("INSERT INTO astralplane_blob_owner_state"):
            self.statements.append((statement, parameters))
            owner_id = str(parameters[0])
            self.owner_states.setdefault(
                owner_id,
                {
                    "state": "active",
                    "version": 0,
                    "retired_at": None,
                },
            )
            return FakeResult(1)
        if statement.startswith("UPDATE astralplane_blob_owner_state"):
            self.statements.append((statement, parameters))
            retired_at, owner_id = parameters
            row = self.owner_states.get(str(owner_id))
            if row is None:
                return FakeResult(0)
            if row["state"] == "active":
                row.update(
                    state="retired",
                    version=int(row["version"]) + 1,
                    retired_at=retired_at,
                )
            return FakeResult(1, returned_records=(copy.deepcopy(row),))
        if (
            statement.startswith("UPDATE user_attachments")
            and "materialization_lease_id = %s" in statement
        ):
            self.statements.append((statement, parameters))
            deleted_at, attachment_id, owner_id, lease_id, expected_version = parameters
            row = self.attachments.get(str(attachment_id))
            if (
                row is None
                or row["user_id"] != owner_id
                or row["materialization_state"] != "pending"
                or row["deleted_at"] is not None
                or row["materialization_lease_id"] != lease_id
                or row["materialization_lease_version"] != expected_version
            ):
                return FakeResult(0)
            row["deleted_at"] = deleted_at
            row["materialization_lease_version"] = int(expected_version) + 1
            return FakeResult(
                1,
                returned_records=(
                    {
                        "attachment_id": attachment_id,
                        "user_id": owner_id,
                        "materialization_lease_id": lease_id,
                        "materialization_lease_version": int(expected_version) + 1,
                        "purge_requested_at": NOW,
                    },
                ),
            )
        if statement.startswith("WITH candidates AS"):
            self.statements.append((statement, parameters))
            (limit,) = parameters
            candidates = sorted(
                (
                    row
                    for row in self.attachments.values()
                    if row["materialization_state"] == "pending"
                    and row["deleted_at"] is None
                    and row["materialization_lease_expires_at"] <= NOW
                ),
                key=lambda row: (
                    row["materialization_lease_expires_at"],
                    row["attachment_id"],
                ),
            )[: int(limit)]
            records: list[Mapping[str, Any]] = []
            for row in candidates:
                row["deleted_at"] = 1
                row["materialization_lease_version"] += 1
                records.append(
                    {
                        "attachment_id": row["attachment_id"],
                        "user_id": row["user_id"],
                        "materialization_lease_id": row["materialization_lease_id"],
                        "materialization_lease_version": row[
                            "materialization_lease_version"
                        ],
                        "purge_requested_at": NOW,
                    }
                )
            return FakeResult(len(records), returned_records=tuple(records))
        if "UPDATE user_attachments" not in statement:
            return super().execute(statement, parameters)
        self.statements.append((statement, parameters))
        if "attachment_id = %s" in statement:
            deleted_at, attachment_id, owner_id = parameters
            row = self.attachments.get(str(attachment_id))
            if (
                row is None
                or row["user_id"] != owner_id
                or row["deleted_at"] is not None
            ):
                return FakeResult(0)
            row["deleted_at"] = deleted_at
            return FakeResult(1, returned_records=(copy.deepcopy(row),))
        deleted_at, owner_id = parameters
        changed = 0
        for row in self.attachments.values():
            if row["user_id"] == owner_id and row["deleted_at"] is None:
                row["deleted_at"] = deleted_at
                changed += 1
        return FakeResult(changed)

    def fetch_one(
        self, statement: str, parameters: tuple[object, ...] = ()
    ) -> Mapping[str, Any] | None:
        if "AS has_expired_pending" in statement:
            self.statements.append((statement, parameters))
            return {
                "has_expired_pending": any(
                    row["materialization_state"] == "pending"
                    and row["deleted_at"] is None
                    and row["materialization_lease_expires_at"] <= NOW
                    for row in self.attachments.values()
                )
            }
        if "AS has_incomplete" in statement:
            return super().fetch_one(statement, parameters)
        if "FROM user_attachments" not in statement:
            return super().fetch_one(statement, parameters)
        self.statements.append((statement, parameters))
        attachment_id, owner_id = parameters
        row = self.attachments.get(str(attachment_id))
        return (
            None
            if row is None or row["user_id"] != owner_id
            else copy.deepcopy(row)
        )


class ScheduleDatabase(MemoryPurgeDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.attachments: dict[str, dict[str, Any]] = {}
        self.owner_states: dict[str, dict[str, Any]] = {}

    @contextmanager
    def transaction(self, **_: object) -> Iterator[ScheduleTransaction]:
        self.transaction_count += 1
        number = self.transaction_count
        working_rows = copy.deepcopy(self.rows)
        working_attachments = copy.deepcopy(self.attachments)
        working_owner_states = copy.deepcopy(self.owner_states)
        self.active_transactions += 1
        try:
            yield ScheduleTransaction(
                working_rows,
                working_attachments,
                working_owner_states,
            )
            if number in self.fail_commit_numbers:
                raise RuntimeError("simulated database commit failure")
            self.rows = working_rows
            self.attachments = working_attachments
            self.owner_states = working_owner_states
        finally:
            self.active_transactions -= 1


def tombstone(
    identifier: str = "purge-1",
    *,
    owner_id: str = "owner-1",
    key: str | None = None,
    available_at: datetime | None = None,
) -> PurgeTombstone:
    object_id = f"object-{identifier}"
    storage_key = object_id if key is None else key
    digest = hashlib.sha256(
        f"{PurgeTargetScope.ATTACHMENT_PREFIX.value}\0{owner_id}\0{object_id}".encode()
    ).hexdigest()
    return PurgeTombstone(
        tombstone_id=f"purge-attachment_prefix-{digest}",
        owner_id=owner_id,
        object_kind="attachment",
        object_id=object_id,
        storage_key=storage_key,
        storage_locator_sha256=storage_locator_sha256(
            owner_id=owner_id,
            key=storage_key,
        ),
        requested_at=NOW,
        target_scope=PurgeTargetScope.ATTACHMENT_PREFIX,
        available_at=available_at,
    )


def schedule(
    database: MemoryPurgeDatabase,
    store: PostgresPurgeStore,
    item: PurgeTombstone | None = None,
) -> PurgeTombstone:
    del store
    candidate = item or tombstone()
    database.rows[candidate.tombstone_id] = {
        "tombstone_id": candidate.tombstone_id,
        "owner_id": candidate.owner_id,
        "object_kind": candidate.object_kind,
        "object_id": candidate.object_id,
        "storage_key": candidate.storage_key,
        "target_scope": candidate.target_scope.value,
        "storage_locator_sha256": candidate.storage_locator_sha256,
        "requested_at": candidate.requested_at,
        "status": candidate.status.value,
        "attempt_count": candidate.attempt_count,
        "version": candidate.version,
        "available_at": candidate.available_at or candidate.requested_at,
        "verified_absent_at": candidate.verified_absent_at,
        "manual_resolution_evidence_sha256": (
            candidate.manual_resolution_evidence_sha256
        ),
        "manual_resolved_at": candidate.manual_resolved_at,
        "last_error_code": candidate.last_error_code,
        "updated_at": candidate.requested_at,
    }
    return candidate


def legacy_exact_tombstone(*, owner_id: str = "owner-1") -> PurgeTombstone:
    storage_key = "legacy/object.bin"
    return PurgeTombstone(
        tombstone_id="legacy-purge-1",
        owner_id=owner_id,
        object_kind="artifact",
        object_id="legacy-object-1",
        storage_key=storage_key,
        storage_locator_sha256=storage_locator_sha256(
            owner_id=owner_id,
            key=storage_key,
        ),
        requested_at=NOW,
        target_scope=PurgeTargetScope.EXACT_KEY,
        status=PurgeStatus.MANUAL_REVIEW,
        available_at=NOW,
        last_error_code="legacy_scope_unqualified",
    )


def test_generic_tombstone_enqueue_is_absent_in_favor_of_typed_schedules() -> None:
    assert not hasattr(PostgresPurgeStore, "enqueue")


def test_operator_resolution_attests_legacy_exact_and_persists_evidence() -> None:
    database, store, blobs = MemoryPurgeDatabase(), PostgresPurgeStore(), MemoryBlobStore()
    legacy = schedule(database, store, legacy_exact_tombstone())
    executor = DurablePurgeExecutor(database=database, store=store, blobs=blobs)
    evidence = hashlib.sha256(b"quiesced legacy publisher procedure").hexdigest()

    result = executor.resolve_legacy_exact_for_administration(
        tombstone_id=legacy.tombstone_id,
        expected_owner_id=legacy.owner_id,
        expected_storage_locator_sha256=legacy.storage_locator_sha256,
        observed_at=NOW + timedelta(seconds=1),
        resolution_evidence_sha256=evidence,
    )

    assert result.state is PurgeAttemptState.PURGED
    persisted = database.rows[legacy.tombstone_id]
    assert persisted["status"] == "purged"
    assert persisted["manual_resolution_evidence_sha256"] == evidence
    assert persisted["manual_resolved_at"] == NOW + timedelta(seconds=1)
    assert executor.has_incomplete_for_administration() is False
    replay = executor.resolve_legacy_exact_for_administration(
        tombstone_id=legacy.tombstone_id,
        expected_owner_id=legacy.owner_id,
        expected_storage_locator_sha256=legacy.storage_locator_sha256,
        observed_at=NOW + timedelta(seconds=2),
        resolution_evidence_sha256=evidence,
    )
    assert replay.state is PurgeAttemptState.ALREADY_PURGED
    with pytest.raises(PlaneError) as changed_evidence:
        executor.resolve_legacy_exact_for_administration(
            tombstone_id=legacy.tombstone_id,
            expected_owner_id=legacy.owner_id,
            expected_storage_locator_sha256=legacy.storage_locator_sha256,
            observed_at=NOW + timedelta(seconds=3),
            resolution_evidence_sha256=hashlib.sha256(b"different evidence").hexdigest(),
        )
    assert changed_evidence.value.code == "purge_resolution_evidence_conflict"


def test_operator_resolution_commit_failure_remains_retryable() -> None:
    database, store, blobs = MemoryPurgeDatabase(), PostgresPurgeStore(), MemoryBlobStore()
    legacy = schedule(database, store, legacy_exact_tombstone())
    executor = DurablePurgeExecutor(database=database, store=store, blobs=blobs)
    evidence = hashlib.sha256(b"operator evidence").hexdigest()
    database.fail_commit_numbers.add(2)

    with pytest.raises(RuntimeError, match="commit failure"):
        executor.resolve_legacy_exact_for_administration(
            tombstone_id=legacy.tombstone_id,
            expected_owner_id=legacy.owner_id,
            expected_storage_locator_sha256=legacy.storage_locator_sha256,
            observed_at=NOW,
            resolution_evidence_sha256=evidence,
        )

    assert database.rows[legacy.tombstone_id]["status"] == "manual_review"
    result = executor.resolve_legacy_exact_for_administration(
        tombstone_id=legacy.tombstone_id,
        expected_owner_id=legacy.owner_id,
        expected_storage_locator_sha256=legacy.storage_locator_sha256,
        observed_at=NOW + timedelta(seconds=1),
        resolution_evidence_sha256=evidence,
    )
    assert result.state is PurgeAttemptState.PURGED


def test_concurrent_operator_resolution_accepts_only_the_durable_evidence_winner() -> None:
    evidence = hashlib.sha256(b"operator evidence").hexdigest()

    class ConcurrentLegacyWinnerStore(PostgresPurgeStore):
        def __init__(self, database: MemoryPurgeDatabase, winner_evidence: str) -> None:
            self.database = database
            self.winner_evidence = winner_evidence

        def _mark_legacy_exact_resolved_for_administration(self, transaction, **kwargs):
            del transaction
            row = self.database.rows[str(kwargs["tombstone_id"])]
            row.update(
                status="purged",
                attempt_count=row["attempt_count"] + 1,
                version=row["version"] + 1,
                verified_absent_at=kwargs["verified_absent_at"],
                manual_resolution_evidence_sha256=self.winner_evidence,
                manual_resolved_at=kwargs["verified_absent_at"],
                last_error_code=None,
            )
            raise PlaneError("concurrent winner", code="purge_fence_conflict")

    database = MemoryPurgeDatabase()
    legacy = schedule(database, PostgresPurgeStore(), legacy_exact_tombstone())
    replay = DurablePurgeExecutor(
        database=database,
        store=ConcurrentLegacyWinnerStore(database, evidence),
        blobs=MemoryBlobStore(),
    ).resolve_legacy_exact_for_administration(
        tombstone_id=legacy.tombstone_id,
        expected_owner_id=legacy.owner_id,
        expected_storage_locator_sha256=legacy.storage_locator_sha256,
        observed_at=NOW,
        resolution_evidence_sha256=evidence,
    )
    assert replay.state is PurgeAttemptState.ALREADY_PURGED

    database = MemoryPurgeDatabase()
    legacy = schedule(database, PostgresPurgeStore(), legacy_exact_tombstone())
    with pytest.raises(PlaneError) as conflict:
        DurablePurgeExecutor(
            database=database,
            store=ConcurrentLegacyWinnerStore(
                database,
                hashlib.sha256(b"different operator evidence").hexdigest(),
            ),
            blobs=MemoryBlobStore(),
        ).resolve_legacy_exact_for_administration(
            tombstone_id=legacy.tombstone_id,
            expected_owner_id=legacy.owner_id,
            expected_storage_locator_sha256=legacy.storage_locator_sha256,
            observed_at=NOW,
            resolution_evidence_sha256=evidence,
        )
    assert conflict.value.code == "purge_resolution_evidence_conflict"


def test_operator_resolution_conflict_fails_if_the_tombstone_disappears() -> None:
    database = MemoryPurgeDatabase()
    legacy = schedule(database, PostgresPurgeStore(), legacy_exact_tombstone())

    class DisappearingLegacyStore(PostgresPurgeStore):
        def _mark_legacy_exact_resolved_for_administration(self, transaction, **kwargs):
            del transaction
            database.rows.pop(str(kwargs["tombstone_id"]))
            raise PlaneError("concurrent delete", code="purge_fence_conflict")

    with pytest.raises(PlaneError) as raised:
        DurablePurgeExecutor(
            database=database,
            store=DisappearingLegacyStore(),
            blobs=MemoryBlobStore(),
        ).resolve_legacy_exact_for_administration(
            tombstone_id=legacy.tombstone_id,
            expected_owner_id=legacy.owner_id,
            expected_storage_locator_sha256=legacy.storage_locator_sha256,
            observed_at=NOW,
            resolution_evidence_sha256=hashlib.sha256(b"operator evidence").hexdigest(),
        )
    assert raised.value.code == "purge_integrity_failure"


def test_unqualified_exact_work_is_failed_closed_and_concurrent_state_is_reconciled() -> None:
    manual = legacy_exact_tombstone()
    unqualified = PurgeTombstone(
        tombstone_id=manual.tombstone_id,
        owner_id=manual.owner_id,
        object_kind=manual.object_kind,
        object_id=manual.object_id,
        storage_key=manual.storage_key,
        storage_locator_sha256=manual.storage_locator_sha256,
        requested_at=manual.requested_at,
        target_scope=PurgeTargetScope.EXACT_KEY,
        status=PurgeStatus.PENDING,
        available_at=NOW,
    )

    class UnqualifiedExactStore(PostgresPurgeStore):
        def __init__(self, current: PurgeTombstone, *, conflict: bool = False) -> None:
            self.current = current
            self.conflict = conflict
            self.load_count = 0

        def load(self, transaction, **kwargs):
            del transaction, kwargs
            self.load_count += 1
            return unqualified if self.load_count == 1 else self.current

        def _mark_manual_review_for_executor(self, transaction, **kwargs):
            del transaction, kwargs
            if self.conflict:
                raise PlaneError("concurrent winner", code="purge_fence_conflict")
            return FakeResult(1)

    direct = DurablePurgeExecutor(
        database=MemoryPurgeDatabase(),
        store=UnqualifiedExactStore(manual),
        blobs=MemoryBlobStore(),
    ).execute(
        owner_id=manual.owner_id,
        tombstone_id=manual.tombstone_id,
        now=NOW,
        retry_at=NOW + timedelta(minutes=1),
    )
    assert direct.state is PurgeAttemptState.FAILED
    assert direct.error_code == "publication_fence_required"

    concurrent = DurablePurgeExecutor(
        database=MemoryPurgeDatabase(),
        store=UnqualifiedExactStore(manual, conflict=True),
        blobs=MemoryBlobStore(),
    ).execute(
        owner_id=manual.owner_id,
        tombstone_id=manual.tombstone_id,
        now=NOW,
        retry_at=NOW + timedelta(minutes=1),
    )
    assert concurrent.state is PurgeAttemptState.FAILED
    assert concurrent.error_code == "legacy_scope_unqualified"


def test_attachment_schedule_atomically_soft_deletes_and_replays_first_intent() -> None:
    database, store = ScheduleDatabase(), PostgresPurgeStore()
    database.attachments["attachment-1"] = attachment_row(
        owner_id="owner-1", attachment_id="attachment-1"
    )

    with database.transaction() as transaction:
        scheduled = store.schedule_attachment_prefix(
            transaction,
            owner_id="owner-1",
            attachment_id="attachment-1",
            requested_at=NOW,
            deleted_at=100,
        )

    assert scheduled == PurgeScheduleResult(
        tombstone=scheduled.tombstone,
        tombstone_created=True,
        metadata_rows_soft_deleted=1,
    )
    assert scheduled.tombstone.target_scope is PurgeTargetScope.ATTACHMENT_PREFIX
    assert scheduled.tombstone.storage_key == "attachment-1"
    assert database.attachments["attachment-1"]["deleted_at"] == 100

    with database.transaction() as transaction:
        replay = store.schedule_attachment_prefix(
            transaction,
            owner_id="owner-1",
            attachment_id="attachment-1",
            requested_at=NOW + timedelta(hours=1),
            deleted_at=200,
        )

    assert replay.tombstone == scheduled.tombstone
    assert replay.tombstone_created is False
    assert replay.metadata_rows_soft_deleted == 0
    assert database.attachments["attachment-1"]["deleted_at"] == 100


def test_attachment_schedule_is_owner_scoped_and_rolls_back_metadata_with_intent() -> None:
    database, store = ScheduleDatabase(), PostgresPurgeStore()
    database.attachments["attachment-1"] = attachment_row(
        owner_id="owner-1", attachment_id="attachment-1"
    )

    with database.transaction() as transaction, pytest.raises(PlaneError) as raised:
        store.schedule_attachment_prefix(
            transaction,
            owner_id="owner-2",
            attachment_id="attachment-1",
            requested_at=NOW,
            deleted_at=100,
        )
    assert raised.value.code == "purge_object_not_found"

    with (
        pytest.raises(RuntimeError, match="caller rollback"),
        database.transaction() as transaction,
    ):
        store.schedule_attachment_prefix(
            transaction,
            owner_id="owner-1",
            attachment_id="attachment-1",
            requested_at=NOW,
            deleted_at=100,
        )
        raise RuntimeError("caller rollback")

    assert database.rows == {}
    assert database.attachments["attachment-1"]["deleted_at"] is None


def test_pending_abandon_schedules_cleanup_and_exact_replay() -> None:
    database, store = ScheduleDatabase(), PostgresPurgeStore()
    database.attachments["attachment-1"] = attachment_row(
        owner_id="owner-1", attachment_id="attachment-1"
    ) | {
        "materialization_state": "pending",
        "materialization_lease_id": "lease-1",
        "materialization_lease_version": 4,
        "materialization_lease_expires_at": NOW + timedelta(minutes=5),
    }

    with database.transaction() as transaction:
        scheduled = store.abandon_pending_materialization(
            transaction,
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=4,
            deleted_at=100,
        )

    assert scheduled.tombstone_created is True
    assert scheduled.metadata_rows_soft_deleted == 1
    assert scheduled.tombstone.target_scope is PurgeTargetScope.ATTACHMENT_PREFIX
    assert database.attachments["attachment-1"]["deleted_at"] == 100
    assert database.attachments["attachment-1"]["materialization_lease_version"] == 5

    with database.transaction() as transaction:
        replay = store.abandon_pending_materialization(
            transaction,
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=4,
            deleted_at=200,
        )

    assert replay.tombstone == scheduled.tombstone
    assert replay.tombstone_created is False
    assert replay.metadata_rows_soft_deleted == 0
    assert database.attachments["attachment-1"]["deleted_at"] == 100


def test_pending_abandon_rejects_stale_fences_and_invalid_database_evidence() -> None:
    database, store = ScheduleDatabase(), PostgresPurgeStore()
    database.attachments["attachment-1"] = attachment_row(
        owner_id="owner-1", attachment_id="attachment-1"
    ) | {
        "materialization_state": "pending",
        "materialization_lease_id": "lease-1",
        "materialization_lease_version": 4,
        "materialization_lease_expires_at": NOW + timedelta(minutes=5),
    }

    with database.transaction() as transaction, pytest.raises(PlaneError) as stale:
        store.abandon_pending_materialization(
            transaction,
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=3,
            deleted_at=100,
        )
    assert stale.value.code == "purge_materialization_fence_conflict"

    with pytest.raises(SQLContractError):
        store.abandon_pending_materialization(
            ScheduleTransaction({}, {}, {}),
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=True,
            deleted_at=100,
        )

    transaction = ScheduleTransaction({}, {}, {})
    transaction.override_result = FakeResult(
        1,
        returned_records=(
            {
                "attachment_id": "attachment-1",
                "user_id": "owner-1",
                "materialization_lease_id": "lease-1",
                "materialization_lease_version": 1,
            },
        ),
    )
    with pytest.raises(PlaneError) as malformed:
        store.abandon_pending_materialization(
            transaction,
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
            deleted_at=100,
        )
    assert malformed.value.code == "purge_record_invalid"


def test_expired_pending_recovery_is_bounded_deterministic_and_observable() -> None:
    database, store = ScheduleDatabase(), PostgresPurgeStore()
    database.attachments = {
        "attachment-2": attachment_row(
            owner_id="owner-2", attachment_id="attachment-2"
        )
        | {
            "materialization_state": "pending",
            "materialization_lease_id": "lease-2",
            "materialization_lease_version": 0,
            "materialization_lease_expires_at": NOW - timedelta(seconds=1),
        },
        "attachment-1": attachment_row(
            owner_id="owner-1", attachment_id="attachment-1"
        )
        | {
            "materialization_state": "pending",
            "materialization_lease_id": "lease-1",
            "materialization_lease_version": 2,
            "materialization_lease_expires_at": NOW - timedelta(minutes=1),
        },
        "live": attachment_row(owner_id="owner-3", attachment_id="live")
        | {
            "materialization_state": "pending",
            "materialization_lease_id": "lease-live",
            "materialization_lease_version": 0,
            "materialization_lease_expires_at": NOW + timedelta(minutes=1),
        },
    }

    with database.transaction() as transaction:
        assert store.has_expired_pending_materializations_for_administration(transaction)
        first = store.schedule_expired_pending_materializations_for_administration(
            transaction, limit=1
        )
    assert tuple(item.tombstone.object_id for item in first) == ("attachment-1",)
    assert first[0].metadata_rows_soft_deleted == 1

    with database.transaction() as transaction:
        second = store.schedule_expired_pending_materializations_for_administration(
            transaction, limit=10
        )
        assert not store.has_expired_pending_materializations_for_administration(
            transaction
        )
    assert tuple(item.tombstone.object_id for item in second) == ("attachment-2",)
    assert database.attachments["live"]["deleted_at"] is None


@pytest.mark.parametrize(
    "result, limit",
    [
        (FakeResult(-1), 1),
        (FakeResult(2), 1),
        (FakeResult(1), 1),
    ],
)
def test_expired_pending_recovery_rejects_invalid_database_evidence(
    result: FakeResult,
    limit: int,
) -> None:
    transaction = ScheduleTransaction({}, {}, {})
    transaction.override_result = result
    with pytest.raises(PlaneError) as raised:
        PostgresPurgeStore().schedule_expired_pending_materializations_for_administration(
            transaction,
            limit=limit,
        )
    assert raised.value.code == "purge_write_invalid"


@pytest.mark.parametrize("record", [None, {}, {"has_expired_pending": 1}])
def test_expired_pending_probe_rejects_invalid_database_evidence(
    record: Mapping[str, Any] | None,
) -> None:
    class Query:
        def fetch_one(self, *_: object) -> Mapping[str, Any] | None:
            return record

    with pytest.raises(PlaneError) as raised:
        PostgresPurgeStore().has_expired_pending_materializations_for_administration(
            Query()  # type: ignore[arg-type]
        )
    assert raised.value.code == "purge_record_invalid"


def test_owner_schedule_is_orphan_safe_bounded_and_replayable() -> None:
    database, store = ScheduleDatabase(), PostgresPurgeStore()
    database.attachments = {
        "attachment-1": attachment_row(owner_id="owner-1", attachment_id="attachment-1"),
        "attachment-2": attachment_row(owner_id="owner-1", attachment_id="attachment-2"),
        "foreign": attachment_row(owner_id="owner-2", attachment_id="foreign"),
    }

    with database.transaction() as transaction:
        scheduled = store.schedule_owner_namespace(
            transaction,
            owner_id="owner-1",
            requested_at=NOW,
            deleted_at=100,
        )
    assert scheduled.tombstone.target_scope is PurgeTargetScope.OWNER_NAMESPACE
    assert scheduled.tombstone_created is True
    assert scheduled.metadata_rows_soft_deleted == 2
    assert database.attachments["foreign"]["deleted_at"] is None

    with database.transaction() as transaction:
        replay = store.schedule_owner_namespace(
            transaction,
            owner_id="owner-1",
            requested_at=NOW + timedelta(minutes=1),
            deleted_at=200,
        )
        orphan = store.schedule_owner_namespace(
            transaction,
            owner_id="owner-with-orphan-blobs",
            requested_at=NOW,
            deleted_at=100,
        )
    assert replay.tombstone == scheduled.tombstone
    assert replay.tombstone_created is False
    assert replay.metadata_rows_soft_deleted == 0
    assert orphan.tombstone.target_scope is PurgeTargetScope.OWNER_NAMESPACE
    assert orphan.metadata_rows_soft_deleted == 0


def test_conflicting_tombstone_identity_and_stale_fences_fail_closed() -> None:
    database, store = MemoryPurgeDatabase(), PostgresPurgeStore()
    scheduled = schedule(database, store)
    with database.transaction() as transaction:
        with pytest.raises(PlaneError) as raised:
            store._mark_purged_for_executor(
                transaction,
                authority=EXECUTOR_AUTHORITY,
                owner_id="owner-2",
                tombstone_id=scheduled.tombstone_id,
                expected_storage_locator_sha256=(
                    scheduled.storage_locator_sha256
                ),
                expected_version=0,
                verified_absent_at=NOW,
            )
        assert raised.value.code == "purge_fence_conflict"


def test_ready_discovery_is_bounded_cross_owner_and_excludes_delayed_or_terminal() -> None:
    database, store = MemoryPurgeDatabase(), PostgresPurgeStore()
    ready_one = schedule(database, store, tombstone("ready-1", owner_id="owner-1"))
    ready_two = schedule(database, store, tombstone("ready-2", owner_id="owner-2"))
    delayed = schedule(
        database,
        store,
        tombstone(
            "delayed",
            owner_id="owner-3",
            available_at=NOW + timedelta(minutes=1),
        ),
    )
    manual = schedule(database, store, tombstone("manual", owner_id="owner-4"))
    database.rows[ready_two.tombstone_id].update(
        status="failed", last_error_code="retryable"
    )
    database.rows[manual.tombstone_id].update(
        status="manual_review", last_error_code="operator_required"
    )

    with database.transaction() as transaction:
        first = store.list_ready_for_administration(
            transaction,
            observed_at=NOW,
            limit=1,
        )
        all_ready = store.list_ready_for_administration(
            transaction,
            observed_at=NOW,
            limit=10,
        )
        assert store.has_incomplete_for_administration(transaction) is True

    assert tuple(item.tombstone_id for item in first) == (ready_one.tombstone_id,)
    assert tuple(item.owner_id for item in all_ready) == ("owner-1", "owner-2")
    assert delayed.tombstone_id not in {item.tombstone_id for item in all_ready}
    with pytest.raises(SQLContractError):
        store.list_ready_for_administration(
            MemoryPurgeTransaction({}), observed_at=NOW, limit=0
        )

    for row in database.rows.values():
        row.update(status="purged", verified_absent_at=NOW, last_error_code=None)
    with database.transaction() as transaction:
        assert store.has_incomplete_for_administration(transaction) is False


def test_every_tombstone_transition_updates_its_freshness_marker() -> None:
    store = PostgresPurgeStore()
    database = MemoryPurgeDatabase()
    identifiers: dict[str, str] = {}
    for identifier in ("purged", "failed", "review"):
        identifiers[identifier] = schedule(
            database,
            store,
            tombstone(identifier),
        ).tombstone_id
    transaction = MemoryPurgeTransaction(database.rows)

    store._mark_purged_for_executor(
        transaction,
        authority=EXECUTOR_AUTHORITY,
        owner_id="owner-1",
        tombstone_id=identifiers["purged"],
        expected_storage_locator_sha256=(
            database.rows[identifiers["purged"]]["storage_locator_sha256"]
        ),
        expected_version=0,
        verified_absent_at=NOW,
    )
    store._mark_failed_for_executor(
        transaction,
        authority=EXECUTOR_AUTHORITY,
        owner_id="owner-1",
        tombstone_id=identifiers["failed"],
        expected_version=0,
        available_at=NOW + timedelta(minutes=1),
        error_code="retryable",
    )
    store._mark_manual_review_for_executor(
        transaction,
        authority=EXECUTOR_AUTHORITY,
        owner_id="owner-1",
        tombstone_id=identifiers["review"],
        expected_version=0,
        error_code="operator_required",
    )

    transitions = [
        statement
        for statement, _ in transaction.statements
        if statement.startswith("UPDATE astralplane_purge_tombstone")
    ]
    assert len(transitions) == 3
    assert all("updated_at = clock_timestamp()" in statement for statement in transitions)


def test_database_success_then_blob_failure_remains_visible_and_retryable() -> None:
    database, store, blobs = MemoryPurgeDatabase(), PostgresPurgeStore(), MemoryBlobStore()
    scheduled = schedule(database, store)
    blob_key = f"{scheduled.object_id}/file.bin"
    blobs.put(owner_id="owner-1", key=blob_key, content=b"sensitive")
    blobs.fail_delete = True
    executor = DurablePurgeExecutor(database=database, store=store, blobs=blobs)

    result = executor.execute(
        owner_id="owner-1",
        tombstone_id=scheduled.tombstone_id,
        now=NOW,
        retry_at=NOW + timedelta(minutes=5),
    )
    assert result.state is PurgeAttemptState.FAILED
    assert database.rows[scheduled.tombstone_id]["status"] == "failed"
    assert database.rows[scheduled.tombstone_id]["last_error_code"] == "blob_delete_failed"
    assert "host path" not in repr(database.rows)
    assert blobs.get(owner_id="owner-1", key=blob_key) == b"sensitive"

    blobs.fail_delete = False
    recovered = executor.execute(
        owner_id="owner-1",
        tombstone_id=scheduled.tombstone_id,
        now=NOW + timedelta(minutes=5),
        retry_at=NOW + timedelta(minutes=10),
    )
    assert recovered.state is PurgeAttemptState.PURGED
    assert database.rows[scheduled.tombstone_id]["status"] == "purged"
    assert (
        database.rows[scheduled.tombstone_id]["verified_absent_at"]
        == NOW + timedelta(minutes=5)
    )


def test_blob_success_then_database_failure_never_reports_completion_and_recovers() -> None:
    database, store, blobs = MemoryPurgeDatabase(), PostgresPurgeStore(), MemoryBlobStore()
    scheduled = schedule(database, store)
    blob_key = f"{scheduled.object_id}/file.bin"
    blobs.put(owner_id="owner-1", key=blob_key, content=b"sensitive")
    executor = DurablePurgeExecutor(database=database, store=store, blobs=blobs)
    database.fail_commit_numbers.add(2)

    with pytest.raises(RuntimeError, match="commit failure"):
        executor.execute(
            owner_id="owner-1",
            tombstone_id=scheduled.tombstone_id,
            now=NOW,
            retry_at=NOW + timedelta(minutes=5),
        )
    assert blobs.is_absent(owner_id="owner-1", key=blob_key)
    assert database.rows[scheduled.tombstone_id]["status"] == "pending"
    assert database.rows[scheduled.tombstone_id]["verified_absent_at"] is None

    result = executor.execute(
        owner_id="owner-1",
        tombstone_id=scheduled.tombstone_id,
        now=NOW + timedelta(seconds=1),
        retry_at=NOW + timedelta(minutes=5),
    )
    assert result.state is PurgeAttemptState.PURGED
    assert database.rows[scheduled.tombstone_id]["status"] == "purged"
    assert blobs.deleted.count(("owner-1", scheduled.object_id)) == 2


def test_streaming_executor_deletes_attachment_prefix_without_open_database_transaction() -> None:
    database, store = ScheduleDatabase(), PostgresPurgeStore()
    database.attachments["attachment-1"] = attachment_row(
        owner_id="owner-1", attachment_id="attachment-1"
    )

    class ObservingBlobs(MemoryBlobStore):
        def delete_prefix(self, *, owner_id: str, prefix: str) -> BlobDeleteResult:
            assert database.active_transactions == 0
            return super().delete_prefix(owner_id=owner_id, prefix=prefix)

    blobs = ObservingBlobs()
    with database.transaction() as transaction:
        scheduled = store.schedule_attachment_prefix(
            transaction,
            owner_id="owner-1",
            attachment_id="attachment-1",
            requested_at=NOW,
            deleted_at=100,
        )
    blobs.put(owner_id="owner-1", key="attachment-1/a.bin", content=b"a")
    blobs.put(owner_id="owner-1", key="attachment-1/nested/b.bin", content=b"b")
    blobs.put(owner_id="owner-1", key="attachment-2/keep.bin", content=b"keep")

    result = DurablePurgeExecutor(database=database, store=store, blobs=blobs).execute(
        owner_id="owner-1",
        tombstone_id=scheduled.tombstone.tombstone_id,
        now=NOW,
        retry_at=NOW + timedelta(minutes=1),
    )

    assert result.state is PurgeAttemptState.PURGED
    assert blobs.is_prefix_absent(owner_id="owner-1", prefix="attachment-1")
    assert not blobs.is_prefix_absent(owner_id="owner-1", prefix="attachment-2")


def test_streaming_executor_deletes_whole_reserved_verification_owner_and_replays() -> None:
    database, store, blobs = ScheduleDatabase(), PostgresPurgeStore(), MemoryBlobStore()
    owner_id = "__verif__run_everyday_primary"
    with database.transaction() as transaction:
        scheduled = store.schedule_owner_namespace(
            transaction,
            owner_id=owner_id,
            requested_at=NOW,
            deleted_at=100,
        )
    blobs.put(owner_id=owner_id, key="attachment-1/a.bin", content=b"a")
    blobs.put(owner_id=owner_id, key="attachment-2/b.bin", content=b"b")
    executor = DurablePurgeExecutor(database=database, store=store, blobs=blobs)

    first = executor.execute(
        owner_id=owner_id,
        tombstone_id=scheduled.tombstone.tombstone_id,
        now=NOW,
        retry_at=NOW + timedelta(minutes=1),
    )
    replay = executor.execute(
        owner_id=owner_id,
        tombstone_id=scheduled.tombstone.tombstone_id,
        now=NOW + timedelta(seconds=1),
        retry_at=NOW + timedelta(minutes=1),
    )

    assert first.state is PurgeAttemptState.PURGED
    assert replay.state is PurgeAttemptState.ALREADY_PURGED
    assert blobs.is_owner_absent(owner_id=owner_id)


def test_executor_bounded_discovery_and_reconciliation_recover_pending_work() -> None:
    database, store, blobs = ScheduleDatabase(), PostgresPurgeStore(), MemoryBlobStore()
    database.attachments = {
        "attachment-1": attachment_row(owner_id="owner-1", attachment_id="attachment-1"),
        "attachment-2": attachment_row(owner_id="owner-2", attachment_id="attachment-2"),
    }
    with database.transaction() as transaction:
        store.schedule_attachment_prefix(
            transaction,
            owner_id="owner-1",
            attachment_id="attachment-1",
            requested_at=NOW,
            deleted_at=100,
        )
        store.schedule_attachment_prefix(
            transaction,
            owner_id="owner-2",
            attachment_id="attachment-2",
            requested_at=NOW,
            deleted_at=100,
        )
    blobs.put(owner_id="owner-1", key="attachment-1/a.bin", content=b"a")
    blobs.put(owner_id="owner-2", key="attachment-2/b.bin", content=b"b")
    executor = DurablePurgeExecutor(database=database, store=store, blobs=blobs)

    discovered = executor.discover_ready_for_administration(observed_at=NOW, limit=1)
    results = executor.reconcile_ready_for_administration(
        observed_at=NOW,
        retry_at=NOW + timedelta(minutes=1),
        limit=10,
    )

    assert len(discovered) == 1
    assert tuple(result.state for result in results) == (
        PurgeAttemptState.PURGED,
        PurgeAttemptState.PURGED,
    )
    assert executor.has_incomplete_for_administration() is False
    assert database.active_transactions == 0


def test_concurrent_executor_winner_is_reconciled_as_idempotent_replay() -> None:
    database, blobs = ScheduleDatabase(), MemoryBlobStore()
    database.attachments["attachment-1"] = attachment_row(
        owner_id="owner-1", attachment_id="attachment-1"
    )

    class ConcurrentWinnerStore(PostgresPurgeStore):
        def _mark_purged_for_executor(self, transaction, **kwargs):
            row = database.rows[str(kwargs["tombstone_id"])]
            row.update(
                status="purged",
                attempt_count=row["attempt_count"] + 1,
                version=row["version"] + 1,
                verified_absent_at=kwargs["verified_absent_at"],
                available_at=kwargs["verified_absent_at"],
                last_error_code=None,
            )
            raise PlaneError("concurrent winner", code="purge_fence_conflict")

    store = ConcurrentWinnerStore()
    with database.transaction() as transaction:
        scheduled = store.schedule_attachment_prefix(
            transaction,
            owner_id="owner-1",
            attachment_id="attachment-1",
            requested_at=NOW,
            deleted_at=100,
        )
    blobs.put(owner_id="owner-1", key="attachment-1/a.bin", content=b"a")

    result = DurablePurgeExecutor(database=database, store=store, blobs=blobs).execute(
        owner_id="owner-1",
        tombstone_id=scheduled.tombstone.tombstone_id,
        now=NOW,
        retry_at=NOW + timedelta(minutes=1),
    )

    assert result.state is PurgeAttemptState.ALREADY_PURGED
    assert result.attempt == 1
    assert blobs.is_prefix_absent(owner_id="owner-1", prefix="attachment-1")


def test_wrong_owner_cannot_read_delete_or_transition_another_owners_blob() -> None:
    database, store, blobs = MemoryPurgeDatabase(), PostgresPurgeStore(), MemoryBlobStore()
    scheduled = schedule(database, store)
    blob_key = f"{scheduled.object_id}/file.bin"
    blobs.put(owner_id="owner-1", key=blob_key, content=b"sensitive")
    executor = DurablePurgeExecutor(database=database, store=store, blobs=blobs)

    with pytest.raises(PlaneError) as raised:
        executor.execute(
            owner_id="owner-2",
            tombstone_id=scheduled.tombstone_id,
            now=NOW,
            retry_at=NOW + timedelta(minutes=1),
        )
    assert raised.value.code == "purge_not_found"
    assert blobs.deleted == []
    assert blobs.get(owner_id="owner-1", key=blob_key) == b"sensitive"


def test_manual_review_retry_delay_and_completed_integrity_are_explicit() -> None:
    database, store, blobs = MemoryPurgeDatabase(), PostgresPurgeStore(), MemoryBlobStore()
    scheduled = schedule(
        database,
        store,
        tombstone(available_at=NOW + timedelta(minutes=1)),
    )
    executor = DurablePurgeExecutor(database=database, store=store, blobs=blobs)
    with pytest.raises(PlaneError) as raised:
        executor.execute(
            owner_id="owner-1",
            tombstone_id=scheduled.tombstone_id,
            now=NOW,
            retry_at=NOW + timedelta(minutes=2),
        )
    assert raised.value.code == "purge_retry_not_ready"

    with database.transaction() as transaction:
        store._mark_manual_review_for_executor(
            transaction,
            authority=EXECUTOR_AUTHORITY,
            owner_id="owner-1",
            tombstone_id=scheduled.tombstone_id,
            expected_version=0,
            error_code="operator_required",
        )
    with pytest.raises(PlaneError) as raised:
        executor.execute(
            owner_id="owner-1",
            tombstone_id=scheduled.tombstone_id,
            now=NOW + timedelta(minutes=1),
            retry_at=NOW + timedelta(minutes=2),
        )
    assert raised.value.code == "purge_manual_review"

    database.rows[scheduled.tombstone_id].update(
        status="purged",
        verified_absent_at=NOW,
        available_at=NOW,
    )
    assert (
        executor.execute(
            owner_id="owner-1",
            tombstone_id=scheduled.tombstone_id,
            now=NOW + timedelta(minutes=1),
            retry_at=NOW + timedelta(minutes=2),
        ).state
        is PurgeAttemptState.ALREADY_PURGED
    )
    blob_key = f"{scheduled.object_id}/file.bin"
    blobs.put(owner_id="owner-1", key=blob_key, content=b"returned")
    with pytest.raises(PlaneError) as raised:
        executor.execute(
            owner_id="owner-1",
            tombstone_id=scheduled.tombstone_id,
            now=NOW + timedelta(minutes=1),
            retry_at=NOW + timedelta(minutes=2),
        )
    assert raised.value.code == "purge_integrity_failure"


@pytest.mark.parametrize(
    "mutation",
    [
        {"object_kind": "unsupported"},
        {"target_scope": "unsupported"},
        {"storage_key": "different-prefix"},
        {"target_scope": PurgeTargetScope.OWNER_NAMESPACE},
        {"storage_locator_sha256": "0" * 64},
        {"status": PurgeStatus.PURGED},
        {"verified_absent_at": NOW},
        {
            "target_scope": PurgeTargetScope.EXACT_KEY,
            "status": PurgeStatus.PENDING,
        },
        {
            "target_scope": PurgeTargetScope.EXACT_KEY,
            "status": PurgeStatus.MANUAL_REVIEW,
            "storage_locator_sha256": "not-a-digest",
        },
        {
            "target_scope": PurgeTargetScope.EXACT_KEY,
            "status": PurgeStatus.MANUAL_REVIEW,
            "storage_key": "",
        },
        {
            "target_scope": PurgeTargetScope.EXACT_KEY,
            "status": PurgeStatus.PURGED,
            "verified_absent_at": NOW,
        },
        {
            "target_scope": PurgeTargetScope.EXACT_KEY,
            "status": PurgeStatus.MANUAL_REVIEW,
            "manual_resolution_evidence_sha256": "b" * 64,
        },
        {
            "manual_resolution_evidence_sha256": "b" * 64,
            "manual_resolved_at": NOW,
        },
        {"manual_resolution_evidence_sha256": "not-a-digest"},
        {"attempt_count": -1},
        {"attempt_count": "one"},
        {"version": -1},
        {"version": "one"},
        {"last_error_code": "raw exception details"},
    ],
)
def test_invalid_tombstones_are_rejected(mutation: dict[str, object]) -> None:
    original = tombstone()
    values: dict[str, object] = {
        "tombstone_id": original.tombstone_id,
        "owner_id": original.owner_id,
        "object_kind": original.object_kind,
        "object_id": original.object_id,
        "storage_key": original.storage_key,
        "storage_locator_sha256": original.storage_locator_sha256,
        "requested_at": original.requested_at,
        "target_scope": original.target_scope,
        "status": original.status,
        "attempt_count": original.attempt_count,
        "version": original.version,
        "available_at": original.available_at,
        "verified_absent_at": original.verified_absent_at,
        "manual_resolution_evidence_sha256": (
            original.manual_resolution_evidence_sha256
        ),
        "manual_resolved_at": original.manual_resolved_at,
        "last_error_code": original.last_error_code,
    }
    values.update(mutation)
    with pytest.raises(SQLContractError):
        _validate_tombstone(PurgeTombstone(**values))  # type: ignore[arg-type]


def test_operator_resolved_exact_tombstone_requires_and_preserves_evidence() -> None:
    evidence = hashlib.sha256(b"operator procedure").hexdigest()
    original = legacy_exact_tombstone()
    resolved = PurgeTombstone(
        tombstone_id=original.tombstone_id,
        owner_id=original.owner_id,
        object_kind=original.object_kind,
        object_id=original.object_id,
        storage_key=original.storage_key,
        storage_locator_sha256=original.storage_locator_sha256,
        requested_at=original.requested_at,
        target_scope=PurgeTargetScope.EXACT_KEY,
        status=PurgeStatus.PURGED,
        attempt_count=1,
        version=1,
        available_at=NOW,
        verified_absent_at=NOW,
        manual_resolution_evidence_sha256=evidence,
        manual_resolved_at=NOW,
    )

    assert _validate_tombstone(resolved) == resolved


def test_store_rejects_invalid_transition_parameters_and_row_counts() -> None:
    store = PostgresPurgeStore()
    transaction = MemoryPurgeTransaction({})
    with pytest.raises(SQLContractError):
        store._mark_failed_for_executor(
            transaction,
            authority=EXECUTOR_AUTHORITY,
            owner_id="owner-1",
            tombstone_id="purge-1",
            expected_version=True,
            available_at=NOW,
            error_code="failure",
        )
    with pytest.raises(SQLContractError):
        store._mark_manual_review_for_executor(
            transaction,
            authority=EXECUTOR_AUTHORITY,
            owner_id="owner-1",
            tombstone_id="purge-1",
            expected_version=0,
            error_code="unsafe details",
        )
    with pytest.raises(SQLContractError):
        store._mark_failed_for_executor(
            transaction,
            authority=EXECUTOR_AUTHORITY,
            owner_id="owner-1",
            tombstone_id="purge-1",
            expected_version=0,
            available_at=NOW,
            error_code="unsafe details",
        )


def test_purge_authority_and_input_boundaries_fail_closed_before_database_io() -> None:
    with pytest.raises(SQLContractError, match="not constructible"):
        _PurgeExecutorAuthority(object())

    store = PostgresPurgeStore()
    transaction = MemoryPurgeTransaction({})
    with pytest.raises(SQLContractError, match="requires executor authority"):
        store._mark_failed_for_executor(
            transaction,
            authority=object(),  # type: ignore[arg-type]
            owner_id="owner-1",
            tombstone_id="purge-1",
            expected_version=0,
            available_at=NOW,
            error_code="failure",
        )
    with pytest.raises(SQLContractError, match="deleted_at"):
        store.schedule_attachment_prefix(
            transaction,
            owner_id="owner-1",
            attachment_id="attachment-1",
            requested_at=NOW,
            deleted_at=True,
        )
    with pytest.raises(SQLContractError, match="lowercase SHA-256"):
        DurablePurgeExecutor(
            database=MemoryPurgeDatabase(),
            store=store,
            blobs=MemoryBlobStore(),
        ).resolve_legacy_exact_for_administration(
            tombstone_id="legacy-1",
            expected_owner_id="owner-1",
            expected_storage_locator_sha256="A" * 64,
            observed_at=NOW,
            resolution_evidence_sha256="b" * 64,
        )
    with pytest.raises(SQLContractError, match="expected_version"):
        store._mark_legacy_exact_resolved_for_administration(
            transaction,
            authority=EXECUTOR_AUTHORITY,
            owner_id="owner-1",
            tombstone_id="legacy-1",
            expected_storage_locator_sha256="a" * 64,
            expected_version=-1,
            verified_absent_at=NOW,
            resolution_evidence_sha256="b" * 64,
        )


@pytest.mark.parametrize("insert_rowcount", [2, 0])
def test_typed_schedule_rejects_invalid_or_squatted_enqueue_evidence(
    insert_rowcount: int,
) -> None:
    database, store = ScheduleDatabase(), PostgresPurgeStore()
    database.attachments["attachment-1"] = attachment_row(
        owner_id="owner-1", attachment_id="attachment-1"
    )
    transaction = ScheduleTransaction(
        database.rows,
        database.attachments,
        database.owner_states,
    )
    transaction.override_result = FakeResult(insert_rowcount)

    with pytest.raises(PlaneError) as raised:
        store.schedule_attachment_prefix(
            transaction,
            owner_id="owner-1",
            attachment_id="attachment-1",
            requested_at=NOW,
            deleted_at=100,
        )
    assert raised.value.code == (
        "purge_write_invalid" if insert_rowcount == 2 else "purge_idempotency_conflict"
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("object_kind", 7),
        ("attempt_count", "one"),
        ("requested_at", "yesterday"),
        ("status", "unknown"),
    ],
)
def test_store_rejects_corrupt_database_records(field: str, value: object) -> None:
    database, store = MemoryPurgeDatabase(), PostgresPurgeStore()
    scheduled = schedule(database, store)
    database.rows[scheduled.tombstone_id][field] = value
    with database.transaction() as transaction, pytest.raises(PlaneError) as raised:
        store.load(
            transaction,
            owner_id="owner-1",
            tombstone_id=scheduled.tombstone_id,
        )
    assert raised.value.code == "purge_record_invalid"


def test_non_tombstone_and_invalid_owner_are_rejected() -> None:
    with pytest.raises(SQLContractError):
        _validate_tombstone(object())  # type: ignore[arg-type]
    with pytest.raises(SQLContractError):
        storage_locator_sha256(owner_id="bad owner", key="file.bin")


def test_blob_still_present_is_recorded_as_failed_and_nonverifiable_store_is_rejected() -> None:
    database, store, blobs = MemoryPurgeDatabase(), PostgresPurgeStore(), StickyBlobStore()
    scheduled = schedule(database, store)
    blobs.put(
        owner_id="owner-1",
        key=f"{scheduled.object_id}/file.bin",
        content=b"sensitive",
    )
    result = DurablePurgeExecutor(database=database, store=store, blobs=blobs).execute(
        owner_id="owner-1",
        tombstone_id=scheduled.tombstone_id,
        now=NOW,
        retry_at=NOW + timedelta(minutes=1),
    )
    assert result.state is PurgeAttemptState.FAILED
    assert database.rows[scheduled.tombstone_id]["status"] == "failed"

    class DeleteOnly:
        root = PurePath("/blobs")

        def put(self, **_: object) -> str:
            return hashlib.sha256(b"").hexdigest()

        def get(self, **_: object) -> bytes:
            return b""

        def delete(self, **_: object) -> None:
            return None

    with pytest.raises(SQLContractError, match="absence verification"):
        DurablePurgeExecutor(
            database=database,
            store=store,
            blobs=DeleteOnly(),  # type: ignore[arg-type]
        )
