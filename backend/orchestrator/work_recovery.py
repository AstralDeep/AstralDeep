"""Discovers expired admissions and preserves work without a supported execution boundary.
Explicit metadata-only mode supports coordinator callers; durable execution and
current-authority checks remain with the assignment runner and supported workers.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from orchestrator.work_admission import (
    AdmissionResult,
    OperationOwner,
    OperationRecord,
    OperationRequest,
    OperationState,
    OwnerScope,
    RefusedAdmission,
    RetryableOperationCursor,
    WorkAdmissionCoordinator,
)

RECOVERY_IDEMPOTENCY_NAMESPACE = "work.executor_recovery.v1"
DEFAULT_UNCERTAIN_PHASES = frozenset({"awaiting_tool"})
_EXPIRED_TERMINAL_CODE = "execution_lease_expired"


class RecoveryDisposition(str, Enum):
    RESCHEDULED = "rescheduled"
    REPLAYED = "replayed"
    PRESERVED_CANCELLED = "preserved_cancelled"
    PRESERVED_STOPPED = "preserved_stopped"
    PRESERVED_UNCERTAIN = "preserved_uncertain"
    PRESERVED_EXHAUSTED = "preserved_exhausted"
    UNAVAILABLE = "unavailable"
    RESCHEDULE_REFUSED = "reschedule_refused"


class ExecutorRecoveryMode(str, Enum):
    PRESERVE_UNSUPPORTED = "preserve_unsupported"
    ADMISSION_METADATA_ONLY = "admission_metadata_only"


class StopEpochReader(Protocol):
    def load(self, owner_id: str) -> dict | None: ...


StopGate = Callable[[OperationRecord], bool]


@dataclass(frozen=True)
class RecoveredOperation:
    operation_id: uuid.UUID
    disposition: RecoveryDisposition
    recovery_operation_id: uuid.UUID | None = None
    refusal_code: str | None = None
    retryable_refusal: bool = False
    retry_after_ms: int | None = None


@dataclass(frozen=True)
class RecoveryReport:
    expired: int
    recovered: tuple[RecoveredOperation, ...]

    def count(self, disposition: RecoveryDisposition) -> int:
        return sum(
            1 for entry in self.recovered if entry.disposition is disposition
        )


class ExecutorRecoveryService:
    Mode = ExecutorRecoveryMode

    def __init__(
        self,
        coordinator: WorkAdmissionCoordinator,
        *,
        stop_gate: StopGate,
        uncertain_phases: frozenset[str] = DEFAULT_UNCERTAIN_PHASES,
        max_recovery_depth: int = 1,
        idempotency_namespace: str = RECOVERY_IDEMPOTENCY_NAMESPACE,
        page_size: int = 100,
        mode: ExecutorRecoveryMode = ExecutorRecoveryMode.PRESERVE_UNSUPPORTED,
    ) -> None:
        if type(mode) is not ExecutorRecoveryMode:
            raise ValueError("executor recovery mode must be explicit and typed")
        if max_recovery_depth < 0:
            raise ValueError("max_recovery_depth cannot be negative")
        if not (1 <= len(idempotency_namespace) <= 128):
            raise ValueError("idempotency_namespace must be 1..128 characters")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 1000:
            raise ValueError("recovery page_size must be between 1 and 1000")
        self._coordinator = coordinator
        self._stop_gate = stop_gate
        self._uncertain_phases = frozenset(uncertain_phases)
        self._max_recovery_depth = max_recovery_depth
        self._idempotency_namespace = idempotency_namespace
        self._page_size = page_size
        self._after: RetryableOperationCursor | None = None
        self._mode = mode

    def recover_expired_executors(self) -> RecoveryReport:
        expired = self._coordinator.expire_execution_leases()
        candidates = self._coordinator.list_retryable_for_administration(
            terminal_code=_EXPIRED_TERMINAL_CODE, after=self._after, limit=self._page_size,
            handled_idempotency_namespace=self._handled_namespace(),
        )
        if not candidates and self._after is not None:
            candidates = self._coordinator.list_retryable_for_administration(
                terminal_code=_EXPIRED_TERMINAL_CODE, limit=self._page_size,
                handled_idempotency_namespace=self._handled_namespace(),
            )
        recovered = tuple(self._recover_one(record) for record in candidates)
        self._after = (
            RetryableOperationCursor(candidates[-1].accepted_at, candidates[-1].operation_id)
            if len(candidates) == self._page_size else None
        )
        return RecoveryReport(expired=len(expired), recovered=recovered)

    def _handled_namespace(self):
        return (self._idempotency_namespace
                if self._mode is ExecutorRecoveryMode.ADMISSION_METADATA_ONLY else None)

    def reschedule_interrupted(self, record: OperationRecord) -> RecoveredOperation:
        return self._recover_one(record)

    def _recover_one(self, record: OperationRecord) -> RecoveredOperation:
        current = self._coordinator.repository.get_operation_for_administration(
            record.operation_id
        )
        if (
            current is None
            or current.state is not OperationState.RETRYABLE
            or current.terminal_code != _EXPIRED_TERMINAL_CODE
            or current.purge_after is None
            or current.purge_after <= self._coordinator.current_time()
        ):
            return RecoveredOperation(
                operation_id=record.operation_id,
                disposition=RecoveryDisposition.UNAVAILABLE,
            )
        if current.cancel_requested_at is not None:
            return RecoveredOperation(
                operation_id=current.operation_id,
                disposition=RecoveryDisposition.PRESERVED_CANCELLED,
            )
        if self._stop_gate(current):
            return RecoveredOperation(
                operation_id=current.operation_id,
                disposition=RecoveryDisposition.PRESERVED_STOPPED,
            )
        if current.phase_code in self._uncertain_phases:
            return RecoveredOperation(
                operation_id=current.operation_id,
                disposition=RecoveryDisposition.PRESERVED_UNCERTAIN,
            )
        if self._expired_ancestor_depth(current) >= self._max_recovery_depth:
            return RecoveredOperation(
                operation_id=current.operation_id,
                disposition=RecoveryDisposition.PRESERVED_EXHAUSTED,
            )
        if self._mode is ExecutorRecoveryMode.PRESERVE_UNSUPPORTED:
            return RecoveredOperation(
                operation_id=current.operation_id,
                disposition=RecoveryDisposition.UNAVAILABLE,
                refusal_code="executor_recovery_unavailable",
            )
        return self._reschedule(current)

    def _expired_ancestor_depth(self, record: OperationRecord) -> int:
        depth = 0
        current = record
        while (
            depth < self._max_recovery_depth
            and current.terminal_code == _EXPIRED_TERMINAL_CODE
        ):
            if current.parent_operation_id is None:
                return self._max_recovery_depth if current.idempotency_namespace == self._idempotency_namespace else depth
            parent = self._coordinator.repository.get_operation_for_administration(
                current.parent_operation_id
            )
            if parent is None:
                return self._max_recovery_depth
            depth += 1
            current = parent
        return depth

    def _reschedule(self, record: OperationRecord) -> RecoveredOperation:
        identity_material = (
            f"{record.operation_kind}:{record.accepted_at.isoformat()}:"
            f"{record.operation_id}"
        )
        digest = record.normalized_input_digest or hashlib.sha256(
            identity_material.encode("utf-8")
        ).hexdigest()
        owner = OperationOwner(
            owner_scope=record.owner_scope, owner_user_id=record.owner_user_id,
            connection_scope_id=record.connection_scope_id,
        )
        existing = self._coordinator.repository.get_idempotent_operation(
            owner=owner, idempotency_namespace=self._idempotency_namespace,
            idempotency_key=str(record.operation_id),
        )
        if existing is not None:
            if not self._matches_recovery(record, existing, digest):
                return self._identity_conflict(record)
            return RecoveredOperation(
                operation_id=record.operation_id, disposition=RecoveryDisposition.REPLAYED,
                recovery_operation_id=existing.operation_id,
            )
        request = OperationRequest(
            operation_kind=record.operation_kind,
            admission_class=record.admission_class,
            owner=owner,
            submission_id=uuid.uuid4(),
            idempotency_namespace=self._idempotency_namespace,
            idempotency_key=str(record.operation_id),
            normalized_input_digest=digest,
            chat_id=record.chat_id,
            parent_operation_id=record.operation_id,
            connection_generation=record.connection_generation,
            request_generation=record.request_generation,
        )
        admitted = self._coordinator.submit(request)
        if not isinstance(admitted, RefusedAdmission):
            child = self._coordinator.repository.get_operation_for_administration(admitted.operation_id)
            if child is None or not self._matches_recovery(record, child, digest):
                return self._identity_conflict(record)
        return self._admitted(record, admitted)

    def _matches_recovery(self, record: OperationRecord, child: OperationRecord, digest: str) -> bool:
        return (
            child.parent_operation_id == record.operation_id
            and child.owner_scope is record.owner_scope
            and child.owner_user_id == record.owner_user_id
            and child.connection_scope_id == record.connection_scope_id
            and child.operation_kind == record.operation_kind
            and child.admission_class is record.admission_class
            and child.chat_id == record.chat_id
            and child.connection_generation == record.connection_generation
            and child.request_generation == record.request_generation
            and child.idempotency_namespace == self._idempotency_namespace
            and child.idempotency_key == str(record.operation_id)
            and child.normalized_input_digest == digest
        )

    @staticmethod
    def _identity_conflict(record: OperationRecord) -> RecoveredOperation:
        return RecoveredOperation(
            operation_id=record.operation_id, disposition=RecoveryDisposition.RESCHEDULE_REFUSED,
            refusal_code="idempotency_conflict",
        )

    @staticmethod
    def _admitted(
        record: OperationRecord, admitted: AdmissionResult
    ) -> RecoveredOperation:
        if isinstance(admitted, RefusedAdmission):
            return RecoveredOperation(
                operation_id=record.operation_id,
                disposition=RecoveryDisposition.RESCHEDULE_REFUSED,
                refusal_code=admitted.code,
                retryable_refusal=admitted.retryable,
                retry_after_ms=admitted.retry_after_ms,
            )
        replayed = admitted.state in {
            OperationState.COMPLETED,
            OperationState.FAILED,
            OperationState.CANCELLED,
            OperationState.RETRYABLE,
        }
        return RecoveredOperation(
            operation_id=record.operation_id,
            disposition=(
                RecoveryDisposition.REPLAYED
                if replayed
                else RecoveryDisposition.RESCHEDULED
            ),
            recovery_operation_id=admitted.operation_id,
        )


def owner_stop_gate(store: StopEpochReader) -> StopGate:
    def gate(record: OperationRecord) -> bool:
        if record.owner_scope not in {OwnerScope.USER, OwnerScope.SCHEDULE}:
            return False
        if not record.owner_user_id:
            return True
        try:
            current = store.load(record.owner_user_id)
        except Exception:
            return True
        return bool(current and current.get("engaged"))

    return gate


def recovery_interval_seconds() -> int:
    raw = os.getenv("WORK_RECOVERY_INTERVAL_SECONDS", "15")
    try:
        interval = int(raw)
    except ValueError:
        raise ValueError("WORK_RECOVERY_INTERVAL_SECONDS must be an integer") from None
    if interval <= 0:
        raise ValueError("WORK_RECOVERY_INTERVAL_SECONDS must be positive")
    return interval
