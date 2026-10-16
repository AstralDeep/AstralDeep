"""Recovers fenced work whose executor node failed: expires authoritative execution
leases through the work-admission coordinator, reschedules supported operations as
linked recovery admissions, and preserves cancelled, stopped, or uncertain work until
supported reconciliation. Consumed by the orchestrator's periodic recovery loop.
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
    def __init__(
        self,
        coordinator: WorkAdmissionCoordinator,
        *,
        stop_gate: StopGate,
        uncertain_phases: frozenset[str] = DEFAULT_UNCERTAIN_PHASES,
        max_recovery_depth: int = 1,
        idempotency_namespace: str = RECOVERY_IDEMPOTENCY_NAMESPACE,
    ) -> None:
        if max_recovery_depth < 0:
            raise ValueError("max_recovery_depth cannot be negative")
        if not (1 <= len(idempotency_namespace) <= 128):
            raise ValueError("idempotency_namespace must be 1..128 characters")
        self._coordinator = coordinator
        self._stop_gate = stop_gate
        self._uncertain_phases = frozenset(uncertain_phases)
        self._max_recovery_depth = max_recovery_depth
        self._idempotency_namespace = idempotency_namespace

    def recover_expired_executors(self) -> RecoveryReport:
        expired = self._coordinator.expire_execution_leases()
        recovered = tuple(self._recover_one(record) for record in expired)
        return RecoveryReport(expired=len(expired), recovered=recovered)

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
        return self._reschedule(current)

    def _expired_ancestor_depth(self, record: OperationRecord) -> int:
        depth = 0
        current = record
        while (
            depth < self._max_recovery_depth
            and current.parent_operation_id is not None
            and current.terminal_code == _EXPIRED_TERMINAL_CODE
        ):
            parent = self._coordinator.repository.get_operation_for_administration(
                current.parent_operation_id
            )
            if parent is None:
                break
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
        request = OperationRequest(
            operation_kind=record.operation_kind,
            admission_class=record.admission_class,
            owner=OperationOwner(
                owner_scope=record.owner_scope,
                owner_user_id=record.owner_user_id,
                connection_scope_id=record.connection_scope_id,
            ),
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
        return self._admitted(record, admitted)

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
