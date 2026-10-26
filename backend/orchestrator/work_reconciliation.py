"""Fence-bound ledger and owner late-result reconciliation for ambiguous cross-node
effect attempts, layered on work_admission.py's coordinator and work_recovery.py's
preservation dispositions; durable records live in the owner-scoped credential store
and targets are re-verified against their current authority at report time.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from astralplane.repositories import RepositoryConflictError
from audit.repository import AuditRepository
from audit.schemas import AuditEventCreate, AuditEventDTO
from pydantic import BaseModel, Field, ValidationError, field_validator

from orchestrator import mesh_enrollment as me
from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from
from orchestrator.work_admission import (
    ExecutionFence,
    OperationRecord,
    OperationState,
    OwnerScope,
    WorkAdmissionCoordinator,
)

EFFECT_ATTEMPT_NAMESPACE = "work.effect-attempts.v1"
EFFECT_DISPATCHED_PHASE = "effect_dispatched"
RECONCILED_APPLIED_TERMINAL_CODE = "reconciled_effect_applied"

_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TARGET_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_TARGET_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")

_ATTEMPT_RECORD_VERSION = 1
_OWNER_SCAN_LIMIT = 128
_MAX_UNRESOLVED_ATTEMPTS = 8
_PRUNE_SCAN_LIMIT = 64


class AttemptOutcome(StrEnum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNCERTAIN = "uncertain"


_OUTCOME_BY_REPORT = {
    "confirmed_applied": AttemptOutcome.APPLIED,
    "confirmed_not_applied": AttemptOutcome.NOT_APPLIED,
    "uncertain": AttemptOutcome.UNCERTAIN,
}
_REPORT_BY_OUTCOME = {
    AttemptOutcome.APPLIED: "confirmed_applied",
    AttemptOutcome.NOT_APPLIED: "confirmed_not_applied",
    AttemptOutcome.UNCERTAIN: "uncertain",
}
_ATTEMPT_STATES = frozenset(
    {"bound", AttemptOutcome.APPLIED.value, AttemptOutcome.NOT_APPLIED.value,
     AttemptOutcome.UNCERTAIN.value}
)


class ReconciliationRefused(RuntimeError):
    def __init__(self, code: str, *, status: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class EffectAttempt:
    operation_id: uuid.UUID
    execution_generation: int
    attempt_nonce: str
    argument_digest: str
    target_kind: str
    target_id: str
    recorded_at: int
    state: str
    outcome: AttemptOutcome | None
    outcome_digest: str | None
    settled_at: int | None
    revision: int
    dispatch_id: str | None = None
    authority_digest: str | None = None

    @property
    def settled(self) -> bool:
        return self.state in {AttemptOutcome.APPLIED.value, AttemptOutcome.NOT_APPLIED.value}

    @property
    def report_outcome(self) -> str | None:
        if self.outcome is None:
            return None
        return _REPORT_BY_OUTCOME[self.outcome]


@runtime_checkable
class TargetReportAuthority(Protocol):
    def assert_reportable(self, *, owner_id: str, target_kind: str, target_id: str) -> None: ...


class MeshTargetAuthority:
    def __init__(
        self, *, plane_runtime: Any, plane_repositories: Any, audit_repository: Any
    ) -> None:
        self._plane_runtime = plane_runtime
        self._store = me.MeshEnrollmentStore(
            plane_runtime=plane_runtime,
            plane_repositories=plane_repositories,
            audit_repository=audit_repository,
        )
        self._remote, _ = repository_from(
            "remote",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
        )

    def assert_reportable(self, *, owner_id: str, target_kind: str, target_id: str) -> None:
        if target_kind == "mesh_member":
            try:
                with self._store._transaction() as transaction:
                    self._store._current_member(transaction, owner_id, target_id)
            except me.MeshEnrollmentError as exc:
                raise ReconciliationRefused("reconcile_target_unavailable") from exc
            except Exception as exc:
                raise ReconciliationRefused(
                    "reconcile_authority_unavailable", status=503
                ) from exc
            return
        if target_kind == "remote_machine":
            try:
                with self._plane_runtime.transaction() as transaction:
                    machine = self._remote.get_machine(
                        transaction, owner_id=owner_id, machine_id=target_id
                    )
            except Exception as exc:
                raise ReconciliationRefused(
                    "reconcile_authority_unavailable", status=503
                ) from exc
            if machine is None:
                raise ReconciliationRefused("reconcile_target_unavailable")
            return
        raise ReconciliationRefused("reconcile_target_unavailable")


def _decode_record(ciphertext: str) -> dict[str, Any] | None:
    try:
        record = me._decode_record(ciphertext)
    except me.MeshEnrollmentError:
        return None
    return record if isinstance(record, dict) else None


def _partition_of(record: OperationRecord) -> str:
    if record.owner_scope in {OwnerScope.USER, OwnerScope.SCHEDULE}:
        return record.owner_user_id or ""
    if record.owner_scope is OwnerScope.CONNECTION:
        return "connection:" + str(record.connection_scope_id)
    return record.owner_scope.value


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


class _LedgerStore:
    def __init__(self, source: PlaneRepositoryContext) -> None:
        self._source = source

    @property
    def source(self) -> PlaneRepositoryContext:
        return self._source

    def get(self, transaction, *, partition: str, nonce: str):
        return self._source.repository.get_credential(
            transaction,
            owner_id=partition,
            agent_id=EFFECT_ATTEMPT_NAMESPACE,
            credential_key=nonce,
        )

    def list(self, transaction, *, partition: str, limit: int):
        return self._source.repository.list_credentials(
            transaction,
            owner_id=partition,
            agent_id=EFFECT_ATTEMPT_NAMESPACE,
            limit=limit,
        )

    def put(self, transaction, *, partition: str, nonce: str, record: dict[str, Any]):
        return self._source.repository.upsert_credential(
            transaction,
            owner_id=partition,
            agent_id=EFFECT_ATTEMPT_NAMESPACE,
            credential_key=nonce,
            encrypted_value=me._encode_record(record),
            updated_at=int(record["revision"]),
        )

    def compare_and_set(
        self,
        transaction,
        *,
        partition: str,
        nonce: str,
        expected_revision: int,
        record: dict[str, Any],
    ):
        return self._source.repository.compare_and_set_ciphertext(
            transaction,
            owner_id=partition,
            agent_id=EFFECT_ATTEMPT_NAMESPACE,
            credential_key=nonce,
            expected_updated_at=expected_revision,
            encrypted_value=me._encode_record(record),
            updated_at=int(record["revision"]),
        )

    def delete(self, transaction, *, partition: str, nonce: str) -> bool:
        return self._source.repository.delete_credential(
            transaction,
            owner_id=partition,
            agent_id=EFFECT_ATTEMPT_NAMESPACE,
            credential_key=nonce,
        )


def _validated_identity(
    *, argument_digest: str, target_kind: str, target_id: str
) -> None:
    if _SHA256_RE.fullmatch(argument_digest) is None:
        raise ReconciliationRefused("reconcile_report_invalid")
    if (
        _TARGET_KIND_RE.fullmatch(target_kind) is None
        or _TARGET_ID_RE.fullmatch(target_id) is None
    ):
        raise ReconciliationRefused("reconcile_report_invalid")


class EffectAttemptLedger:
    def __init__(
        self,
        *,
        coordinator: WorkAdmissionCoordinator,
        source: PlaneRepositoryContext,
        audit_repository: AuditRepository | None = None,
    ) -> None:
        if not isinstance(coordinator, WorkAdmissionCoordinator):
            raise ValueError("a WorkAdmissionCoordinator is required")
        if not isinstance(source, PlaneRepositoryContext):
            raise ValueError("a Plane repository context is required")
        if audit_repository is not None and not isinstance(audit_repository, AuditRepository):
            raise ValueError("the audit repository is invalid")
        self._coordinator = coordinator
        self._store = _LedgerStore(source)
        self._audit = audit_repository

    def bind(
        self,
        fence: ExecutionFence,
        *,
        target_kind: str,
        target_id: str,
        argument_digest: str,
        dispatch_id: str | None = None,
        authority_digest: str | None = None,
    ) -> EffectAttempt:
        if not isinstance(fence, ExecutionFence):
            raise ReconciliationRefused("reconcile_fence_invalid")
        _validated_identity(
            argument_digest=argument_digest,
            target_kind=target_kind,
            target_id=target_id,
        )
        with self._coordinator.fenced_transaction(fence) as transaction:
            return self._bind_in_transaction(
                transaction,
                fence,
                argument_digest=argument_digest,
                target_kind=target_kind,
                target_id=target_id,
                dispatch_id=dispatch_id,
                authority_digest=authority_digest,
            )

    def read(
        self, *, partition: str, operation_id: uuid.UUID, nonce: str
    ) -> EffectAttempt | None:
        with self._store.source.transaction() as transaction:
            return self._read_in_transaction(
                transaction,
                partition=partition,
                operation_id=operation_id,
                nonce=nonce,
            )

    def list_for_operation(
        self, *, partition: str, operation_id: uuid.UUID
    ) -> tuple[EffectAttempt, ...]:
        with self._store.source.transaction() as transaction:
            return self._list_in_transaction(
                transaction, partition=partition, operation_id=operation_id
            )

    def settle(
        self,
        *,
        partition: str,
        operation_id: uuid.UUID,
        nonce: str,
        outcome: AttemptOutcome,
        outcome_digest: str | None,
    ) -> EffectAttempt:
        with self._store.source.transaction() as transaction:
            return self._settle_in_transaction(
                transaction,
                partition=partition,
                operation_id=operation_id,
                nonce=nonce,
                outcome=outcome,
                outcome_digest=outcome_digest,
            )


    def _bind_in_transaction(
        self,
        transaction,
        fence: ExecutionFence,
        *,
        argument_digest: str,
        target_kind: str,
        target_id: str,
        dispatch_id: str | None = None,
        authority_digest: str | None = None,
    ) -> EffectAttempt:
        if dispatch_id is not None and (
            not isinstance(dispatch_id, str) or _NONCE_RE.fullmatch(dispatch_id) is None
            or not isinstance(authority_digest, str)
            or _SHA256_RE.fullmatch(authority_digest) is None
        ):
            raise ReconciliationRefused("reconcile_dispatch_invalid")
        record = self._coordinator.repository.get_operation_for_administration(
            fence.operation_id, for_update=True, transaction=transaction
        )
        if record is None:  # pragma: no cover - fenced_transaction guarantees the record
            raise ReconciliationRefused("reconcile_operation_missing")
        partition = _partition_of(record)
        if not partition:  # pragma: no cover - owner scopes always partition
            raise ReconciliationRefused("reconcile_partition_unavailable", status=503)
        nonce = self._attempt_nonce(fence, target_kind, target_id, dispatch_id)
        rows = self._store.list(
            transaction, partition=partition, limit=_OWNER_SCAN_LIMIT + 1
        )
        if len(rows) > _OWNER_SCAN_LIMIT:
            raise ReconciliationRefused("attempt_inventory_exhausted", status=503)
        for row in rows:
            if self._parse_row(row) is None:
                raise ReconciliationRefused("attempt_inventory_invalid", status=503)
        self._prune_missing_operations(
            transaction,
            partition=partition,
            rows=rows,
            keep_operation_id=fence.operation_id,
        )
        unresolved = 0
        existing = None
        for row in rows:
            parsed = self._parse_row(row)
            if parsed is None:
                continue
            if parsed.attempt_nonce == nonce:
                existing = parsed
                continue
            if parsed.operation_id == fence.operation_id and not parsed.settled:
                unresolved += 1
        if existing is not None:
            if (
                existing.execution_generation != fence.execution_generation
                or existing.argument_digest != argument_digest
                or existing.target_kind != target_kind
                or existing.target_id != target_id
            ):
                raise ReconciliationRefused("attempt_nonce_conflict")
            if dispatch_id is not None:
                raise ReconciliationRefused("effect_selection_already_reserved")
            if existing.settled:
                raise ReconciliationRefused("attempt_already_settled")
            return existing
        if unresolved >= _MAX_UNRESOLVED_ATTEMPTS:
            raise ReconciliationRefused("attempt_capacity_exhausted")
        now = _now_ms()
        stored = self._store.put(
            transaction,
            partition=partition,
            nonce=nonce,
            record=self._attempt_record(
                fence=fence,
                nonce=nonce,
                argument_digest=argument_digest,
                target_kind=target_kind,
                target_id=target_id,
                recorded_at=now,
                dispatch_id=dispatch_id,
                authority_digest=authority_digest,
            ),
        )
        self._audit_in_transaction(
            transaction,
            partition,
            "work.effect_attempt.bound",
            {
                "operation_id": str(fence.operation_id),
                "attempt": nonce,
                "generation": fence.execution_generation,
                "target_kind": target_kind,
                "target": hashlib.sha256(target_id.encode("utf-8")).hexdigest()[:16],
            },
        )
        self._coordinator.repository.update_phase(
            fence,
            EFFECT_DISPATCHED_PHASE,
            now=self._coordinator.current_time(),
            transaction=transaction,
        )
        return self._from_row(stored, nonce)

    def _read_in_transaction(
        self, transaction, *, partition: str, operation_id: uuid.UUID, nonce: str
    ) -> EffectAttempt | None:
        if _NONCE_RE.fullmatch(nonce) is None or not isinstance(operation_id, uuid.UUID):
            return None
        row = self._store.get(transaction, partition=partition, nonce=nonce)
        parsed = None if row is None else self._parse_row(row)
        if parsed is None or parsed.operation_id != operation_id:
            return None
        return parsed

    def _list_in_transaction(
        self, transaction, *, partition: str, operation_id: uuid.UUID
    ) -> tuple[EffectAttempt, ...]:
        rows = self._store.list(
            transaction, partition=partition, limit=_OWNER_SCAN_LIMIT + 1
        )
        if len(rows) > _OWNER_SCAN_LIMIT:
            raise ReconciliationRefused("attempt_inventory_exhausted", status=503)
        attempts = []
        for row in rows:
            parsed = self._parse_row(row)
            if parsed is None:
                raise ReconciliationRefused("attempt_inventory_invalid", status=503)
            if parsed.operation_id == operation_id:
                attempts.append(parsed)
        return tuple(attempts)

    def _settle_in_transaction(
        self,
        transaction,
        *,
        partition: str,
        operation_id: uuid.UUID,
        nonce: str,
        outcome: AttemptOutcome,
        outcome_digest: str | None,
    ) -> EffectAttempt:
        if not isinstance(outcome, AttemptOutcome):
            raise ReconciliationRefused("reconcile_outcome_invalid")
        if outcome is AttemptOutcome.APPLIED:
            if outcome_digest is None or _SHA256_RE.fullmatch(outcome_digest) is None:
                raise ReconciliationRefused("reconcile_report_invalid")
        elif outcome_digest is not None:
            raise ReconciliationRefused("reconcile_report_invalid")
        row = self._store.get(transaction, partition=partition, nonce=nonce)
        if row is None:
            raise ReconciliationRefused("reconcile_attempt_unknown")
        parsed = self._parse_row(row)
        if parsed is None or parsed.operation_id != operation_id:
            raise ReconciliationRefused("reconcile_attempt_unknown")
        if parsed.outcome is outcome and parsed.outcome_digest == outcome_digest:
            return parsed
        if parsed.settled:
            raise ReconciliationRefused("reconcile_outcome_conflict")
        now = max(_now_ms(), parsed.revision + 1)
        updated = self._record_of(parsed)
        updated.update(
            state=outcome.value,
            outcome=outcome.value,
            outcome_digest=outcome_digest,
            settled_at=now if outcome is not AttemptOutcome.UNCERTAIN else None,
            revision=now,
        )
        try:
            stored = self._store.compare_and_set(
                transaction,
                partition=partition,
                nonce=nonce,
                expected_revision=parsed.revision,
                record=updated,
            )
        except RepositoryConflictError as exc:
            raise ReconciliationRefused(
                "attempt_settlement_contended", status=503
            ) from exc
        self._audit_in_transaction(
            transaction,
            partition,
            "work.effect_attempt.settled",
            {
                "operation_id": str(operation_id),
                "attempt": nonce,
                "generation": parsed.execution_generation,
                "target_kind": parsed.target_kind,
                "outcome": outcome.value,
                "outcome_digest": outcome_digest,
            },
        )
        return self._from_row(stored, nonce)

    def resolution_probe(self, record: OperationRecord) -> str | None:
        partition = _partition_of(record)
        if not partition:
            return None
        try:
            with self._store.source.transaction() as transaction:
                attempts = self._list_in_transaction(
                    transaction, partition=partition, operation_id=record.operation_id
                )
        except Exception:
            return None
        if not attempts:
            return None
        states = {attempt.state for attempt in attempts}
        if states == {AttemptOutcome.NOT_APPLIED.value}:
            return "cleared"
        if states <= {AttemptOutcome.APPLIED.value, AttemptOutcome.NOT_APPLIED.value}:
            return "applied"
        return None

    def completion_uncertain(self, transaction, fence):
        record = self._coordinator.repository.get_operation_for_administration(
            fence.operation_id, for_update=True, transaction=transaction)
        if record is None:
            raise ReconciliationRefused("reconcile_operation_missing")
        try:
            attempts = self._list_in_transaction(transaction, partition=_partition_of(record),
                                                 operation_id=fence.operation_id)
        except Exception:
            return True
        return any(not attempt.settled for attempt in attempts)

    def preservation_probe(self, record: OperationRecord) -> str | None:
        try:
            attempts = self.list_for_operation(
                partition=_partition_of(record), operation_id=record.operation_id
            )
        except Exception:
            return "uncertain"
        if not attempts:
            return None
        if any(not attempt.settled for attempt in attempts):
            return "uncertain"
        return "applied" if any(
            attempt.outcome is AttemptOutcome.APPLIED for attempt in attempts
        ) else "cleared"

    def _attempt_nonce(
        self, fence: ExecutionFence, target_kind: str, target_id: str, dispatch_id=None
    ) -> str:
        material = "|".join(
            (
                str(fence.operation_id),
                str(fence.execution_generation),
                str(fence.execution_lease_token),
                target_kind,
                target_id,
            )
        )
        if dispatch_id is not None:
            material += "|" + dispatch_id
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def _prune_missing_operations(
        self, transaction, *, partition: str, rows, keep_operation_id
    ) -> None:
        missing: set[str] = set()
        checked = 0
        for row in rows:
            parsed = self._parse_row(row)
            if parsed is None or parsed.operation_id == keep_operation_id:
                continue
            if str(parsed.operation_id) in missing:
                self._store.delete(
                    transaction, partition=partition, nonce=parsed.attempt_nonce
                )
                continue
            if checked >= _PRUNE_SCAN_LIMIT:
                continue
            checked += 1
            current = self._coordinator.repository.get_operation_for_administration(
                parsed.operation_id, transaction=transaction
            )
            if current is None:
                missing.add(str(parsed.operation_id))
                self._store.delete(
                    transaction, partition=partition, nonce=parsed.attempt_nonce
                )

    @staticmethod
    def _attempt_record(
        *,
        fence: ExecutionFence,
        nonce: str,
        argument_digest: str,
        target_kind: str,
        target_id: str,
        recorded_at: int,
        dispatch_id: str | None = None,
        authority_digest: str | None = None,
    ) -> dict[str, Any]:
        return {
            "version": 2 if dispatch_id else _ATTEMPT_RECORD_VERSION,
            "dispatch_id": dispatch_id,
            "authority_digest": authority_digest,
            "operation_id": str(fence.operation_id),
            "execution_generation": fence.execution_generation,
            "attempt_nonce": nonce,
            "argument_digest": argument_digest,
            "target_kind": target_kind,
            "target_id": target_id,
            "recorded_at": recorded_at,
            "state": "bound",
            "outcome": None,
            "outcome_digest": None,
            "settled_at": None,
            "revision": recorded_at,
        }

    def _parse_row(self, row) -> EffectAttempt | None:
        record = _decode_record(row.encrypted_value)
        if record is None or type(record.get("version")) is not int or record.get("version") not in {1, 2}:
            return None
        try:
            attempt = EffectAttempt(
                operation_id=uuid.UUID(str(record["operation_id"])),
                execution_generation=int(record["execution_generation"]),
                attempt_nonce=str(record["attempt_nonce"]),
                argument_digest=str(record["argument_digest"]),
                target_kind=str(record["target_kind"]),
                target_id=str(record["target_id"]),
                recorded_at=int(record["recorded_at"]),
                state=str(record["state"]),
                outcome=(
                    AttemptOutcome(str(record["outcome"]))
                    if record.get("outcome") is not None
                    else None
                ),
                outcome_digest=(
                    str(record["outcome_digest"])
                    if record.get("outcome_digest") is not None
                    else None
                ),
                settled_at=(
                    int(record["settled_at"])
                    if record.get("settled_at") is not None
                    else None
                ),
                revision=int(row.updated_at),
                dispatch_id=record.get("dispatch_id"),
                authority_digest=record.get("authority_digest"),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if (
            attempt.attempt_nonce != row.credential_key
            or any(type(record.get(key)) is not str for key in ("operation_id", "attempt_nonce",
                "argument_digest", "target_kind", "target_id", "state"))
            or (record.get("outcome_digest") is not None and type(record["outcome_digest"]) is not str)
            or attempt.state not in _ATTEMPT_STATES
            or _NONCE_RE.fullmatch(attempt.attempt_nonce) is None
            or _SHA256_RE.fullmatch(attempt.argument_digest) is None
            or _TARGET_KIND_RE.fullmatch(attempt.target_kind) is None
            or _TARGET_ID_RE.fullmatch(attempt.target_id) is None
            or any(type(record.get(key)) is not int or not 0 < record[key] < 2**63
                   for key in ("execution_generation", "recorded_at", "revision"))
            or record["revision"] != attempt.revision
            or attempt.revision < attempt.recorded_at
            or (attempt.state == "bound" and (attempt.outcome is not None
                or attempt.outcome_digest is not None or attempt.settled_at is not None))
            or (attempt.state != "bound" and (attempt.outcome is None
                or attempt.state != attempt.outcome.value))
            or (attempt.outcome is AttemptOutcome.APPLIED and (
                attempt.outcome_digest is None or _SHA256_RE.fullmatch(attempt.outcome_digest) is None))
            or (attempt.outcome is not AttemptOutcome.APPLIED and attempt.outcome_digest is not None)
            or (attempt.settled and (type(record.get("settled_at")) is not int
                or not attempt.recorded_at <= attempt.settled_at <= attempt.revision))
            or (record["version"] == 2 and (not isinstance(attempt.dispatch_id, str)
                or _NONCE_RE.fullmatch(attempt.dispatch_id) is None
                or not isinstance(attempt.authority_digest, str)
                or _SHA256_RE.fullmatch(attempt.authority_digest) is None))
            or (record["version"] == 1 and (attempt.dispatch_id is not None
                or attempt.authority_digest is not None))
        ):
            return None
        return attempt

    def _record_of(self, attempt: EffectAttempt) -> dict[str, Any]:
        return {
            "version": 2 if attempt.dispatch_id else _ATTEMPT_RECORD_VERSION,
            "dispatch_id": attempt.dispatch_id,
            "authority_digest": attempt.authority_digest,
            "operation_id": str(attempt.operation_id),
            "execution_generation": attempt.execution_generation,
            "attempt_nonce": attempt.attempt_nonce,
            "argument_digest": attempt.argument_digest,
            "target_kind": attempt.target_kind,
            "target_id": attempt.target_id,
            "recorded_at": attempt.recorded_at,
            "state": attempt.state,
            "outcome": attempt.outcome.value if attempt.outcome else None,
            "outcome_digest": attempt.outcome_digest,
            "settled_at": attempt.settled_at,
            "revision": attempt.revision,
        }

    def _from_row(self, row, nonce: str) -> EffectAttempt:
        parsed = self._parse_row(row)
        if parsed is None or parsed.attempt_nonce != nonce:
            raise ReconciliationRefused("attempt_record_invalid", status=503)
        return parsed

    def _audit_in_transaction(
        self, transaction, partition: str, action: str, metadata: dict[str, Any]
    ) -> None:
        if self._audit is None:
            return
        event = AuditEventCreate(
            event_id=str(uuid.uuid4()),
            actor_user_id=partition,
            auth_principal="work-reconciliation:ledger",
            event_class="agent_tool_call",
            action_type=action,
            description=action,
            correlation_id=str(metadata.get("operation_id", partition)),
            inputs_meta=metadata,
            outcome="success",
            started_at=datetime.now(UTC),
        )
        try:
            receipt = self._audit.insert_in_transaction(
                event,
                transaction=transaction,
                plane_runtime=self._store.source.plane_runtime,
            )
        except Exception as exc:
            raise ReconciliationRefused(
                "reconcile_audit_unavailable", status=503
            ) from exc
        if not isinstance(receipt, AuditEventDTO):
            raise ReconciliationRefused("reconcile_audit_unavailable", status=503)


class LateResultReport(BaseModel):
    attempt_nonce: str = Field(min_length=32, max_length=32)
    argument_digest: str = Field(min_length=64, max_length=64)
    target_kind: str = Field(min_length=2, max_length=64)
    target_id: str = Field(min_length=1, max_length=128)
    outcome: str
    result_digest: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("attempt_nonce")
    @classmethod
    def _nonce(cls, value: str) -> str:
        if _NONCE_RE.fullmatch(value) is None:
            raise ValueError("a canonical attempt nonce is required")
        return value

    @field_validator("argument_digest", "result_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError("a lowercase sha-256 digest is required")
        return value

    @field_validator("target_kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if _TARGET_KIND_RE.fullmatch(value) is None:
            raise ValueError("a bounded target kind is required")
        return value

    @field_validator("target_id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if _TARGET_ID_RE.fullmatch(value) is None:
            raise ValueError("a bounded target identifier is required")
        return value

    @field_validator("outcome")
    @classmethod
    def _decision(cls, value: str) -> str:
        if value not in _OUTCOME_BY_REPORT:
            raise ValueError("outcome must be a supported reconciliation class")
        return value

    def model_post_init(self, __context: Any) -> None:
        if self.outcome == "confirmed_applied":
            if self.result_digest is None:
                raise ValueError("a confirmed application requires the result digest")
        elif self.result_digest is not None:
            raise ValueError("only a confirmed application carries a result digest")


def _parse_operation_id(value: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise ReconciliationRefused("reconcile_operation_invalid", status=422) from None


class EffectReconciliationService:
    def __init__(
        self,
        *,
        ledger: EffectAttemptLedger,
        target_authority: TargetReportAuthority,
    ) -> None:
        if not isinstance(ledger, EffectAttemptLedger):
            raise ValueError("an EffectAttemptLedger is required")
        if not isinstance(target_authority, TargetReportAuthority):
            raise ValueError("a TargetReportAuthority is required")
        self._ledger = ledger
        self._targets = target_authority

    async def list_current(self, caller, operation_id, *, orchestrator):
        from orchestrator.human_request_authority import CurrentHumanCaller
        from persistent_agents.models import AssignmentError

        if (type(caller) is not CurrentHumanCaller
                or caller.runtime is not self._ledger._store.source.plane_runtime):
            raise ReconciliationRefused("effect_current_authority_unavailable")
        caller.require_session()
        parsed_operation = _parse_operation_id(operation_id)

        def read(transaction, repositories):
            try:
                record = self._ledger._coordinator.repository.get_operation_for_administration(
                    parsed_operation, transaction=transaction)
                if (record is None or record.owner_scope is not OwnerScope.USER
                        or record.owner_user_id != caller.owner_id):
                    raise ReconciliationRefused("work_not_found", status=404)
                attempts = self._ledger._list_in_transaction(transaction,
                    partition=caller.owner_id, operation_id=parsed_operation)
                for attempt in attempts:
                    if attempt.execution_generation != record.execution_generation:
                        raise ReconciliationRefused("reconcile_report_stale")
                    if attempt.dispatch_id is not None:
                        self._assert_current_target(transaction, caller.owner_id, attempt, read_only=True)
                    else:
                        self._targets.assert_reportable(owner_id=caller.owner_id,
                            target_kind=attempt.target_kind, target_id=attempt.target_id)
                return {
                    "operation_id": str(parsed_operation),
                    "execution_generation": record.execution_generation,
                    "operation_state": record.state.value,
                    "attempts": [{
                        "attempt_nonce": attempt.attempt_nonce,
                        "argument_digest": attempt.argument_digest,
                        "target_kind": attempt.target_kind,
                        "target_id": attempt.target_id,
                        "execution_generation": attempt.execution_generation,
                        "state": attempt.state,
                        "outcome": attempt.report_outcome,
                        "outcome_digest": attempt.outcome_digest,
                        "recorded_at": attempt.recorded_at,
                        "settled_at": attempt.settled_at,
                        "revision": attempt.revision,
                    } for attempt in attempts],
                }
            except ReconciliationRefused as exc:
                raise AssignmentError(exc.code, exc.status) from None

        return await caller.transaction(read, expected_orchestrator=orchestrator)

    def report(self, owner_id: object, operation_id: object, body: object) -> dict:
        if not isinstance(owner_id, str) or not owner_id:
            raise ReconciliationRefused("work_authentication_required", status=401)
        parsed_operation = _parse_operation_id(operation_id)
        if type(body) is not LateResultReport:
            try:
                body = LateResultReport.model_validate(body)
            except ValidationError:
                raise ReconciliationRefused("reconcile_report_invalid", status=422) from None
        with self._ledger._store.source.transaction() as transaction:
            return self._report_in_transaction(transaction, owner_id, parsed_operation, body)

    async def report_current(self, caller, operation_id, body, *, orchestrator):
        from orchestrator.remote_effect_authority import lock_owner
        from orchestrator.human_request_authority import CurrentHumanCaller
        if type(caller) is not CurrentHumanCaller:
            raise ReconciliationRefused("effect_current_authority_unavailable")
        caller.require_write()
        if self._ledger._audit is None:
            raise ReconciliationRefused("effect_audit_unavailable")
        if caller.runtime is not self._ledger._store.source.plane_runtime:
            raise ReconciliationRefused("effect_current_authority_unavailable")
        parsed_operation = _parse_operation_id(operation_id)
        if type(body) is not LateResultReport:
            raise ReconciliationRefused("reconcile_report_invalid", status=422)

        def apply(transaction, repositories):
            lock_owner(caller.runtime, transaction, caller.owner_id)
            from persistent_agents.models import AssignmentError
            try:
                return self._report_in_transaction(transaction, caller.owner_id,
                    parsed_operation, body, current_human=True)
            except ReconciliationRefused as exc:
                raise AssignmentError(exc.code, exc.status) from None

        return await caller.transaction(apply, expected_orchestrator=orchestrator)

    def _report_in_transaction(self, transaction, owner_id, parsed_operation, body, *, current_human=False):
        coordinator = self._ledger._coordinator
        record = coordinator.repository.get_operation_for_administration(
            parsed_operation, for_update=True, transaction=transaction
        )
        if (
            record is None
            or record.owner_scope is not OwnerScope.USER
            or record.owner_user_id != owner_id
        ):
            raise ReconciliationRefused("work_not_found", status=404)
        partition = _partition_of(record)
        if not partition:  # pragma: no cover - owner scopes always partition
            raise ReconciliationRefused("reconcile_partition_unavailable", status=503)
        attempt = self._ledger._read_in_transaction(
            transaction,
            partition=partition,
            operation_id=parsed_operation,
            nonce=body.attempt_nonce,
        )
        if attempt is None:
            raise ReconciliationRefused("reconcile_attempt_unknown", status=404)
        if attempt.dispatch_id is not None:
            if not current_human:
                raise ReconciliationRefused("effect_current_authority_unavailable")
            self._assert_current_target(transaction, owner_id, attempt)
        else:
            self._targets.assert_reportable(owner_id=owner_id, target_kind=body.target_kind,
                                            target_id=body.target_id)
        if (
            attempt.argument_digest != body.argument_digest
            or attempt.target_kind != body.target_kind
            or attempt.target_id != body.target_id
        ):
            raise ReconciliationRefused("reconcile_report_stale")
        if record.execution_generation != attempt.execution_generation:
            raise ReconciliationRefused("reconcile_report_stale")
        cancelled_qualified = (record.state is OperationState.CANCELLED
            and attempt.dispatch_id is not None and current_human)
        if cancelled_qualified:
            self._assert_cancelled_report_current(transaction, owner_id, parsed_operation)
        outcome = _OUTCOME_BY_REPORT[body.outcome]
        outcome_digest = (
            body.result_digest if outcome is AttemptOutcome.APPLIED else None
        )
        if (
            record.state is OperationState.RUNNING
            and record.execution_generation == attempt.execution_generation
            and not attempt.settled
        ):
            raise ReconciliationRefused("reconcile_execution_current")
        if (
            record.state
            in {
                OperationState.QUEUED,
                OperationState.COMPLETED,
                OperationState.FAILED,
                OperationState.CANCELLED,
            }
            and not attempt.settled
            and not cancelled_qualified
        ):
            raise ReconciliationRefused("reconcile_outcome_conflict")
        settled = self._ledger._settle_in_transaction(
            transaction,
            partition=partition,
            operation_id=parsed_operation,
            nonce=body.attempt_nonce,
            outcome=outcome,
            outcome_digest=outcome_digest,
        )
        if attempt.dispatch_id is not None:
            self._assert_current_target(transaction, owner_id, attempt)
        if cancelled_qualified:
            self._assert_cancelled_report_current(transaction, owner_id, parsed_operation)
        return {
            "operation_id": str(parsed_operation),
            "attempt_nonce": settled.attempt_nonce,
            "outcome": settled.report_outcome,
            "applied": settled.outcome is AttemptOutcome.APPLIED,
            "operation_state": record.state.value,
        }


    def _assert_cancelled_report_current(self, transaction, owner_id, operation_id):
        from astralplane.repositories.stop_epochs import OwnerStoppedError, StopEpochConflictError
        repository = self._ledger._store.source.plane_runtime.repositories.stop_epochs
        try:
            repository.assert_running(transaction, owner_id=owner_id)
            repository.assert_operation(transaction, owner_id=owner_id, operation_id=operation_id)
        except (OwnerStoppedError, StopEpochConflictError):
            raise ReconciliationRefused("reconcile_execution_stopped") from None

    def _assert_current_target(self, transaction, owner_id, attempt, *, read_only=False):
        from orchestrator.remote_effect_authority import (
            authority_digest, read_authority_digest, RemoteAuthorityUnavailable,
        )
        try:
            digest = read_authority_digest if read_only else authority_digest
            current_digest = digest(self._ledger._store.source.plane_runtime,
                                    transaction, owner_id, attempt.target_id)
        except RemoteAuthorityUnavailable:
            raise ReconciliationRefused("reconcile_target_unavailable") from None
        if attempt.target_kind != "remote_machine" or current_digest != attempt.authority_digest:
            raise ReconciliationRefused("reconcile_target_unavailable")


__all__ = (
    "AttemptOutcome",
    "EffectAttempt",
    "EffectAttemptLedger",
    "EffectReconciliationService",
    "EFFECT_ATTEMPT_NAMESPACE",
    "EFFECT_DISPATCHED_PHASE",
    "LateResultReport",
    "MeshTargetAuthority",
    "RECONCILED_APPLIED_TERMINAL_CODE",
    "ReconciliationRefused",
    "TargetReportAuthority",
)
