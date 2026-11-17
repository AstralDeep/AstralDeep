"""Durable PostgreSQL mechanics for operation admission, lifecycle, and execution-fence
state; each method takes a caller-owned Transaction and never commits or rolls back.
Used by orchestrator/work_admission.py and astralplane.api.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from astralplane.contracts import Transaction
from astralplane.database.dialect import dialect_of
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.harness_bindings import (
    HarnessProfileIdentity,
    OperationHarnessBinding,
    operation_harness_binding_from_row,
)
from astralplane.repositories.harness_capture import (
    HarnessCapture,
    HarnessSelectionRecord,
    harness_capture_from_value,
)
from astralplane.repositories.harness_preferences import (
    assert_harness_admission,
    lock_harness_owner,
)

_OPERATION_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AdmissionClass(StrEnum):
    GLOBAL = "global"
    INTERACTIVE = "interactive"
    VOICE_INTERACTIVE = "voice_interactive"
    MCP = "mcp"
    BACKGROUND = "background"
    SCHEDULED = "scheduled"
    MAINTENANCE = "maintenance"
    SYSTEM = "system"


class OwnerScope(StrEnum):
    CONNECTION = "connection"
    USER = "user"
    SCHEDULE = "schedule"
    MAINTENANCE = "maintenance"
    SYSTEM = "system"


class OperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYABLE = "retryable"


_TERMINAL_STATES = frozenset(
    {
        OperationState.COMPLETED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.RETRYABLE,
    }
)
VOICE_INTERACTIVE_PER_USER_ACTIVE_LIMIT = 2
_VOICE_CAPACITY_RETRY_AFTER_MS = 1_000


_RETRY_AUTHORITY = object()


class _HarnessRetryProof:
    __slots__ = ("authority", "capture", "parent", "request", "transaction")

    def __init__(self, authority, transaction, request, parent, capture):
        if authority is not _RETRY_AUTHORITY:
            raise RepositoryValidationError("server retry proof is unavailable")
        self.authority, self.transaction = authority, transaction
        self.request, self.parent, self.capture = request, parent, capture


class WorkAdmissionNotFoundError(RepositoryNotFoundError):
    default_code = "work_admission_not_found"


class StaleWorkExecutionFenceError(RepositoryConflictError):
    default_code = "work_admission_stale_execution_fence"


class WorkAdmissionConfigurationError(RepositoryValidationError):
    default_code = "work_admission_configuration_invalid"


class WorkAdmissionIntegrityError(RepositoryDataError):
    default_code = "work_admission_integrity_error"


@dataclass(frozen=True, slots=True)
class AdmissionClassConfig:
    class_name: AdmissionClass
    parent_class_name: AdmissionClass | None
    active_limit: int
    queue_limit: int
    max_wait_ms: int | None
    config_revision: str

    def __post_init__(self) -> None:
        if self.active_limit <= 0:
            raise WorkAdmissionConfigurationError("active_limit must be positive")
        if self.queue_limit < 0:
            raise WorkAdmissionConfigurationError("queue_limit cannot be negative")
        if self.queue_limit > 0 and (self.max_wait_ms is None or self.max_wait_ms <= 0):
            raise WorkAdmissionConfigurationError(
                "a finite positive max_wait_ms is required for a non-empty queue"
            )
        if self.max_wait_ms is not None and self.max_wait_ms < 0:
            raise WorkAdmissionConfigurationError("max_wait_ms cannot be negative")
        if self.class_name is AdmissionClass.VOICE_INTERACTIVE and (
            self.queue_limit != 0 or self.max_wait_ms not in {None, 0}
        ):
            raise WorkAdmissionConfigurationError(
                "voice_interactive must not queue or wait for capacity"
            )
        if not (1 <= len(self.config_revision) <= 128):
            raise WorkAdmissionConfigurationError(
                "config_revision must be 1..128 characters"
            )
        if self.parent_class_name is self.class_name:
            raise WorkAdmissionConfigurationError(
                "an admission class cannot parent itself"
            )


@dataclass(frozen=True, slots=True)
class OperationOwner:
    owner_scope: OwnerScope
    owner_user_id: str | None
    connection_scope_id: uuid.UUID | None

    def __post_init__(self) -> None:
        if self.owner_scope in {OwnerScope.USER, OwnerScope.SCHEDULE}:
            if not self.owner_user_id:
                raise RepositoryValidationError(
                    "user and schedule ownership require owner_user_id"
                )
        elif self.owner_user_id is not None:
            raise RepositoryValidationError(
                "owner_user_id is invalid for this owner scope"
            )
        if self.owner_scope is OwnerScope.CONNECTION:
            if not isinstance(self.connection_scope_id, uuid.UUID):
                raise RepositoryValidationError(
                    "connection ownership requires a UUID connection_scope_id"
                )
        elif self.connection_scope_id is not None and not isinstance(
            self.connection_scope_id, uuid.UUID
        ):
            raise RepositoryValidationError(
                "connection_scope_id must be a UUID when supplied"
            )


@dataclass(frozen=True, slots=True)
class OperationRequest:
    operation_kind: str
    admission_class: AdmissionClass
    owner: OperationOwner
    submission_id: uuid.UUID
    idempotency_namespace: str | None
    idempotency_key: str | None
    normalized_input_digest: str | None
    chat_id: str | None
    parent_operation_id: uuid.UUID | None
    connection_generation: uuid.UUID | None
    request_generation: uuid.UUID | None
    harness_selection: HarnessSelectionRecord | None = None
    harness_capture: HarnessCapture | None = None
    harness_assignment_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not _OPERATION_KIND_RE.fullmatch(self.operation_kind):
            raise RepositoryValidationError(
                "operation_kind must be bounded snake_case"
            )
        if self.admission_class is AdmissionClass.GLOBAL:
            raise RepositoryValidationError(
                "global is a parent capacity class, not a work class"
            )
        if (
            self.admission_class is AdmissionClass.VOICE_INTERACTIVE
            and self.owner.owner_scope is not OwnerScope.USER
        ):
            raise RepositoryValidationError(
                "voice_interactive requires authenticated user ownership"
            )
        if self.harness_assignment_id is not None:
            _require_uuid(self.harness_assignment_id, "harness_assignment_id")
        if self.harness_capture is not None:
            if type(self.harness_capture) is not HarnessCapture:
                raise RepositoryValidationError("harness capture must be typed")
            if self.owner.owner_scope not in {OwnerScope.USER, OwnerScope.CONNECTION}:
                raise RepositoryValidationError("harness capture requires an owner")
            if (self.owner.owner_scope is OwnerScope.USER
                    and self.harness_capture.owner_id != self.owner.owner_user_id):
                raise RepositoryValidationError("harness capture owner mismatch")
        if (self.harness_selection is not None
                and type(self.harness_selection) is not HarnessSelectionRecord):
            raise RepositoryValidationError("harness selection must be typed")
        _require_uuid(self.submission_id, "submission_id")
        for name, value in (
            ("parent_operation_id", self.parent_operation_id),
            ("connection_generation", self.connection_generation),
            ("request_generation", self.request_generation),
        ):
            if value is not None:
                _require_uuid(value, name)
        identity = (
            self.idempotency_namespace,
            self.idempotency_key,
            self.normalized_input_digest,
        )
        if any(value is not None for value in identity):
            if any(value is None for value in identity):
                raise RepositoryValidationError(
                    "idempotency namespace, key, and digest are all-or-none"
                )
            if not (1 <= len(self.idempotency_namespace or "") <= 128):
                raise RepositoryValidationError(
                    "idempotency_namespace must be 1..128 characters"
                )
            if not (1 <= len(self.idempotency_key or "") <= 256):
                raise RepositoryValidationError(
                    "idempotency_key must be 1..256 characters"
                )
            if not _SHA256_RE.fullmatch(self.normalized_input_digest or ""):
                raise RepositoryValidationError(
                    "normalized_input_digest must be lowercase SHA-256"
                )


@dataclass(frozen=True, slots=True)
class ExecutionFence:
    operation_id: uuid.UUID
    execution_generation: int
    execution_lease_token: uuid.UUID

    def __post_init__(self) -> None:
        _require_uuid(self.operation_id, "operation_id")
        if self.execution_generation <= 0:
            raise RepositoryValidationError(
                "execution_generation must be positive"
            )
        _require_uuid(self.execution_lease_token, "execution_lease_token")


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: uuid.UUID
    operation_kind: str
    admission_class: AdmissionClass
    owner_scope: OwnerScope
    owner_user_id: str | None
    connection_scope_id: uuid.UUID | None
    idempotency_namespace: str | None
    idempotency_key: str | None
    normalized_input_digest: str | None
    chat_id: str | None
    parent_operation_id: uuid.UUID | None
    connection_generation: uuid.UUID | None
    request_generation: uuid.UUID | None
    state: OperationState
    phase_code: str | None
    terminal_code: str | None
    safe_summary: str | None
    retry_after_ms: int | None
    execution_generation: int
    execution_lease_token: uuid.UUID | None
    state_revision: int
    accepted_at: datetime
    updated_at: datetime
    queue_deadline_at: datetime | None
    started_at: datetime | None
    terminal_at: datetime | None
    cancel_requested_at: datetime | None
    purge_after: datetime | None


@dataclass(frozen=True, slots=True)
class RetryableOperationCursor:
    accepted_at: datetime
    operation_id: uuid.UUID

    def __post_init__(self) -> None:
        _validated_datetime(self.accepted_at, "accepted_at")
        _require_uuid(self.operation_id, "operation_id")


@dataclass(frozen=True, slots=True)
class SafeOperationProjection:
    operation_id: uuid.UUID
    operation_kind: str
    admission_class: AdmissionClass
    owner_scope: OwnerScope
    chat_id: str | None
    parent_operation_id: uuid.UUID | None
    connection_generation: uuid.UUID | None
    request_generation: uuid.UUID | None
    state: OperationState
    phase_code: str | None
    terminal_code: str | None
    safe_summary: str | None
    retry_after_ms: int | None
    state_revision: int
    accepted_at: datetime
    queue_deadline_at: datetime | None
    started_at: datetime | None
    terminal_at: datetime | None
    updated_at: datetime
    purge_after: datetime | None


@dataclass(frozen=True, slots=True)
class AcceptedAdmission:
    accepted: bool
    operation_id: uuid.UUID
    state: OperationState
    state_revision: int
    queue_position: int | None
    queue_deadline_at: datetime | None
    created: bool = False


@dataclass(frozen=True, slots=True)
class RefusedAdmission:
    accepted: bool
    code: str
    retryable: bool
    retry_after_ms: int | None


AdmissionResult = AcceptedAdmission | RefusedAdmission


@dataclass(frozen=True, slots=True)
class AcceptedSubmission:
    accepted: bool
    operation: SafeOperationProjection


SubmissionResult = AcceptedSubmission | RefusedAdmission


@dataclass(frozen=True, slots=True)
class OperationClaim:
    operation: OperationRecord
    fence: ExecutionFence


@dataclass(frozen=True, slots=True)
class AdmissionClassStatus:
    class_name: AdmissionClass
    parent_class_name: AdmissionClass | None
    active_limit: int
    queue_limit: int
    max_wait_ms: int | None
    active_count: int
    queued_count: int
    oldest_queued_at: datetime | None
    oldest_running_at: datetime | None


@dataclass(frozen=True, slots=True)
class PurgeResult:
    operations: int
    submissions: int


@dataclass(frozen=True, slots=True)
class SlotLeaseRenewal:
    operation_id: uuid.UUID
    execution_generation: int
    lease_expires_at: datetime


class _StatementSession:
    def __init__(self, transaction: Transaction) -> None:
        if not isinstance(transaction, Transaction):
            raise RepositoryValidationError(
                "work-admission operations require a Plane Transaction"
            )
        self._transaction = transaction
        self._rows: tuple[Mapping[str, Any], ...] = ()
        self._offset = 0
        self.rowcount = -1

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> None:
        result = self._transaction.execute(statement, parameters)
        self._rows = tuple(result.returned_records)
        self._offset = 0
        self.rowcount = result.rowcount

    def fetchone(self) -> Mapping[str, Any] | None:
        if self._offset >= len(self._rows):
            return None
        row = self._rows[self._offset]
        self._offset += 1
        return row

    def fetchall(self) -> tuple[Mapping[str, Any], ...]:
        rows = self._rows[self._offset :]
        self._offset = len(self._rows)
        return rows


def _normalize_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WorkAdmissionIntegrityError(
            "coordination timestamps must be timezone-aware"
        )
    return value.astimezone(UTC)


def _validated_datetime(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RepositoryValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _validated_duration(value: timedelta, name: str) -> timedelta:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise RepositoryValidationError(f"{name} must be a positive duration")
    return value


def _validated_class(value: AdmissionClass) -> AdmissionClass:
    if not isinstance(value, AdmissionClass):
        raise RepositoryValidationError("class_name must be an AdmissionClass")
    return value


def _validated_owner(value: OperationOwner) -> OperationOwner:
    if not isinstance(value, OperationOwner):
        raise RepositoryValidationError("owner must be an OperationOwner")
    return value


def _validated_fence(value: ExecutionFence) -> ExecutionFence:
    if not isinstance(value, ExecutionFence):
        raise RepositoryValidationError("fence must be an ExecutionFence")
    return value


def _validated_request(value: OperationRequest) -> OperationRequest:
    if not isinstance(value, OperationRequest):
        raise RepositoryValidationError("request must be an OperationRequest")
    return value


def _validated_code(value: str, name: str) -> str:
    if not isinstance(value, str) or _SAFE_CODE_RE.fullmatch(value) is None:
        raise RepositoryValidationError(f"{name} must be bounded snake_case")
    return value


def _validated_summary(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value) > 512):
        raise RepositoryValidationError("safe_summary must not exceed 512 characters")
    return value


def _validated_terminal_payload(
    state: OperationState,
    *,
    terminal_code: str | None,
    safe_summary: str | None,
    retry_after_ms: int | None,
) -> tuple[str | None, str | None, int | None]:
    if not isinstance(state, OperationState) or state not in _TERMINAL_STATES:
        raise RepositoryValidationError("state must be a terminal OperationState")
    if state is not OperationState.COMPLETED and terminal_code is None:
        raise RepositoryValidationError("non-completed terminal state requires terminal_code")
    code = None if terminal_code is None else _validated_code(terminal_code, "terminal_code")
    summary = _validated_summary(safe_summary)
    if retry_after_ms is not None and (
        state is not OperationState.RETRYABLE
        or isinstance(retry_after_ms, bool)
        or not isinstance(retry_after_ms, int)
        or retry_after_ms < 0
    ):
        raise RepositoryValidationError(
            "retry_after_ms is valid only for retryable state and must be nonnegative"
        )
    return code, summary, retry_after_ms


def _validated_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000:
        raise RepositoryValidationError("limit must be between 1 and 10000")
    return value


def _require_uuid(value: object, name: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise RepositoryValidationError(f"{name} must be a UUID")
    return value


def _owner_partition(owner: OperationOwner) -> tuple[str, str]:
    if owner.owner_scope is OwnerScope.CONNECTION:
        return owner.owner_scope.value, str(owner.connection_scope_id)
    if owner.owner_scope in {OwnerScope.USER, OwnerScope.SCHEDULE}:
        return owner.owner_scope.value, owner.owner_user_id or ""
    return owner.owner_scope.value, ""


def _validate_admission_graph(configs: Sequence[AdmissionClassConfig]) -> None:
    if not configs:
        raise WorkAdmissionConfigurationError(
            "at least one admission class is required"
        )
    if any(not isinstance(config, AdmissionClassConfig) for config in configs):
        raise WorkAdmissionConfigurationError(
            "admission classes must contain AdmissionClassConfig records"
        )
    by_name = {config.class_name: config for config in configs}
    if len(by_name) != len(configs):
        raise WorkAdmissionConfigurationError(
            "admission class names must be unique"
        )
    voice_config = by_name.get(AdmissionClass.VOICE_INTERACTIVE)
    if (
        voice_config is not None
        and voice_config.parent_class_name is not AdmissionClass.INTERACTIVE
    ):
        raise WorkAdmissionConfigurationError(
            "voice_interactive must be a child of interactive"
        )
    for config in configs:
        if (
            config.parent_class_name is not None
            and config.parent_class_name not in by_name
        ):
            raise WorkAdmissionConfigurationError(
                f"missing parent admission class {config.parent_class_name.value}"
            )
        seen: set[AdmissionClass] = set()
        current: AdmissionClass | None = config.class_name
        while current is not None:
            if current in seen:
                raise WorkAdmissionConfigurationError(
                    "admission class graph contains a cycle"
                )
            seen.add(current)
            current = by_name[current].parent_class_name


def _admission_configs_from_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[AdmissionClassConfig, ...]:
    try:
        configs = tuple(
            AdmissionClassConfig(
                class_name=AdmissionClass(str(row["class_name"])),
                parent_class_name=(
                    AdmissionClass(str(row["parent_class_name"]))
                    if row["parent_class_name"] is not None
                    else None
                ),
                active_limit=int(row["active_limit"]),
                queue_limit=int(row["queue_limit"]),
                max_wait_ms=(
                    int(row["max_wait_ms"])
                    if int(row["max_wait_ms"]) > 0
                    else None
                ),
                config_revision=str(row["config_revision"]),
            )
            for row in rows
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkAdmissionIntegrityError(
            "persisted admission configuration is invalid"
        ) from exc
    configured = {config.class_name for config in configs}
    required = set(AdmissionClass)
    if configured != required:
        missing = sorted(member.value for member in required - configured)
        unexpected = sorted(member.value for member in configured - required)
        raise WorkAdmissionConfigurationError(
            "production admission config must contain every class "
            f"(missing={missing}, unexpected={unexpected})"
        )
    _validate_admission_graph(configs)
    return configs


def _safe_projection(record: OperationRecord) -> SafeOperationProjection:
    return SafeOperationProjection(
        operation_id=record.operation_id,
        operation_kind=record.operation_kind,
        admission_class=record.admission_class,
        owner_scope=record.owner_scope,
        chat_id=record.chat_id,
        parent_operation_id=record.parent_operation_id,
        connection_generation=record.connection_generation,
        request_generation=record.request_generation,
        state=record.state,
        phase_code=record.phase_code,
        terminal_code=record.terminal_code,
        safe_summary=record.safe_summary,
        retry_after_ms=record.retry_after_ms,
        state_revision=record.state_revision,
        accepted_at=record.accepted_at,
        queue_deadline_at=record.queue_deadline_at,
        started_at=record.started_at,
        terminal_at=record.terminal_at,
        updated_at=record.updated_at,
        purge_after=record.purge_after,
    )


class WorkAdmissionRepository:
    def __init__(self) -> None:
        self._configuration_lock = threading.RLock()
        self._configs: dict[AdmissionClass, AdmissionClassConfig] = {}

    def load_existing_configs(
        self, transaction: Transaction
    ) -> tuple[AdmissionClassConfig, ...]:
        cursor = _StatementSession(transaction)
        cursor.execute(
            """
            SELECT class_name, parent_class_name, active_limit,
                   queue_limit, max_wait_ms, config_revision
            FROM operation_admission_class
            ORDER BY CASE WHEN parent_class_name IS NULL THEN 0 ELSE 1 END,
                     class_name
            FOR SHARE
            """
        )
        return _admission_configs_from_rows(cursor.fetchall())

    def bind_configs(
        self, admission_classes: Sequence[AdmissionClassConfig]
    ) -> None:
        configs = tuple(admission_classes)
        _validate_admission_graph(configs)
        with self._configuration_lock:
            self._configs = {config.class_name: config for config in configs}

    @staticmethod
    def _current_time(cursor: _StatementSession, now: datetime | None) -> datetime:
        if now is not None:
            return _validated_datetime(now, "now")
        cursor.execute("SELECT CURRENT_TIMESTAMP AS current_time")
        row = cursor.fetchone()
        if row is None:
            raise WorkAdmissionIntegrityError(
                "PostgreSQL did not return its transaction timestamp"
            )
        return _normalize_datetime(row["current_time"])

    @staticmethod
    def _advisory_identity(*parts: object) -> int:
        digest = hashlib.sha256()
        for part in parts:
            encoded = str(part).encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        return int.from_bytes(digest.digest()[:8], "big", signed=True)

    @classmethod
    def _lock_request_identities(
        cls, cursor: _StatementSession, request: OperationRequest
    ) -> None:
        scope, partition = _owner_partition(request.owner)
        lock_ids = {
            cls._advisory_identity(
                "operation-submission", scope, partition, request.submission_id
            )
        }
        if request.idempotency_namespace is not None:
            lock_ids.add(
                cls._advisory_identity(
                    "operation-idempotency",
                    scope,
                    partition,
                    request.idempotency_namespace,
                    request.idempotency_key,
                )
            )
        for lock_id in sorted(lock_ids):
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))

    @staticmethod
    def _owner_clause(
        owner: OperationOwner, *, alias: str = ""
    ) -> tuple[str, tuple[object, ...]]:
        prefix = f"{alias}." if alias else ""
        if owner.owner_scope is OwnerScope.CONNECTION:
            return (
                f"{prefix}owner_scope = %s AND {prefix}connection_scope_id = %s",
                (owner.owner_scope.value, str(owner.connection_scope_id)),
            )
        if owner.owner_scope in {OwnerScope.USER, OwnerScope.SCHEDULE}:
            return (
                f"{prefix}owner_scope = %s AND {prefix}owner_user_id = %s",
                (owner.owner_scope.value, owner.owner_user_id),
            )
        return f"{prefix}owner_scope = %s", (owner.owner_scope.value,)

    @staticmethod
    def _uuid(value: object) -> uuid.UUID | None:
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

    @classmethod
    def _operation_from_row(cls, row: Mapping[str, Any]) -> OperationRecord:
        try:
            operation_id = cls._uuid(row["operation_id"])
            if operation_id is None:
                raise ValueError("operation_id is null")
            return OperationRecord(
                operation_id=operation_id,
                operation_kind=str(row["operation_kind"]),
                admission_class=AdmissionClass(row["admission_class"]),
                owner_scope=OwnerScope(row["owner_scope"]),
                owner_user_id=row["owner_user_id"],
                connection_scope_id=cls._uuid(row["connection_scope_id"]),
                idempotency_namespace=row["idempotency_namespace"],
                idempotency_key=row["idempotency_key"],
                normalized_input_digest=(
                    str(row["normalized_input_digest"])
                    if row["normalized_input_digest"] is not None
                    else None
                ),
                chat_id=row["chat_id"],
                parent_operation_id=cls._uuid(row["parent_operation_id"]),
                connection_generation=cls._uuid(row["connection_generation"]),
                request_generation=cls._uuid(row["request_generation"]),
                state=OperationState(row["state"]),
                phase_code=row["phase_code"],
                terminal_code=row["terminal_code"],
                safe_summary=row["safe_summary"],
                retry_after_ms=row["retry_after_ms"],
                execution_generation=int(row["execution_generation"]),
                execution_lease_token=cls._uuid(row["execution_lease_token"]),
                state_revision=int(row["state_revision"]),
                accepted_at=_normalize_datetime(row["accepted_at"]),
                updated_at=_normalize_datetime(row["updated_at"]),
                queue_deadline_at=(
                    _normalize_datetime(row["queue_deadline_at"])
                    if row["queue_deadline_at"] is not None
                    else None
                ),
                started_at=(
                    _normalize_datetime(row["started_at"])
                    if row["started_at"] is not None
                    else None
                ),
                terminal_at=(
                    _normalize_datetime(row["terminal_at"])
                    if row["terminal_at"] is not None
                    else None
                ),
                cancel_requested_at=(
                    _normalize_datetime(row["cancel_requested_at"])
                    if row["cancel_requested_at"] is not None
                    else None
                ),
                purge_after=(
                    _normalize_datetime(row["purge_after"])
                    if row["purge_after"] is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkAdmissionIntegrityError(
                "persisted operation record is invalid"
            ) from exc

    @staticmethod
    def _configuration_order(
        configs: Sequence[AdmissionClassConfig],
    ) -> tuple[AdmissionClassConfig, ...]:
        by_name = {config.class_name: config for config in configs}
        ordered: list[AdmissionClassConfig] = []

        def visit(config: AdmissionClassConfig) -> None:
            parent = config.parent_class_name
            if parent is not None and by_name[parent] not in ordered:
                visit(by_name[parent])
            if config not in ordered:
                ordered.append(config)

        for config in configs:
            visit(config)
        return tuple(ordered)

    def configure(
        self,
        transaction: Transaction,
        admission_classes: Sequence[AdmissionClassConfig],
    ) -> None:
        configs = tuple(admission_classes)
        _validate_admission_graph(configs)
        ordered = self._configuration_order(configs)
        cursor = _StatementSession(transaction)
        for config in ordered:
            cursor.execute(
                """
                    INSERT INTO operation_admission_class (
                        class_name, parent_class_name, active_limit, queue_limit,
                        max_wait_ms, config_revision, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (class_name) DO UPDATE SET
                        parent_class_name = EXCLUDED.parent_class_name,
                        active_limit = EXCLUDED.active_limit,
                        queue_limit = EXCLUDED.queue_limit,
                        max_wait_ms = EXCLUDED.max_wait_ms,
                        config_revision = EXCLUDED.config_revision,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                (
                    config.class_name.value,
                    (
                        config.parent_class_name.value
                        if config.parent_class_name is not None
                        else None
                    ),
                    config.active_limit,
                    config.queue_limit,
                    config.max_wait_ms or 0,
                    config.config_revision,
                ),
            )
            if dialect_of(transaction).is_sqlite:
                cursor.execute(
                    "WITH RECURSIVE slots(value) AS ("
                    "SELECT 1 WHERE 1<=%s UNION ALL SELECT value+1 FROM slots WHERE value<%s) "
                    "INSERT INTO operation_admission_slot(class_name,slot_number) "
                    "SELECT %s,value FROM slots WHERE true "
                    "ON CONFLICT(class_name,slot_number) DO NOTHING",
                    (config.active_limit, config.active_limit, config.class_name.value),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO operation_admission_slot (class_name, slot_number)
                    SELECT %s, generate_series(1, %s)
                    ON CONFLICT (class_name, slot_number) DO NOTHING
                    """,
                    (config.class_name.value, config.active_limit),
                )
            cursor.execute(
                """
                    DELETE FROM operation_admission_slot
                    WHERE class_name = %s AND slot_number > %s
                      AND operation_id IS NULL
                    """,
                (config.class_name.value, config.active_limit),
            )

    def _chain(self, class_name: AdmissionClass) -> tuple[AdmissionClass, ...]:
        with self._configuration_lock:
            if class_name not in self._configs:
                raise WorkAdmissionConfigurationError(
                    f"unknown admission class {class_name.value}"
                )
            chain: list[AdmissionClass] = []
            current: AdmissionClass | None = class_name
            while current is not None:
                chain.append(current)
                current = self._configs[current].parent_class_name
        return tuple(reversed(chain))

    def _lock_class_chain(
        self, cursor: _StatementSession, class_name: AdmissionClass
    ) -> None:
        for member in self._chain(class_name):
            cursor.execute(
                """
                SELECT class_name FROM operation_admission_class
                WHERE class_name = %s FOR UPDATE
                """,
                (member.value,),
            )
            if cursor.fetchone() is None:
                raise WorkAdmissionConfigurationError(
                    f"unknown admission class {member.value}"
                )

    def _submission_row(
        self,
        cursor: _StatementSession,
        owner: OperationOwner,
        submission_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> Mapping[str, Any] | None:
        owner_sql, owner_params = self._owner_clause(owner)
        cursor.execute(
            f"""
            SELECT * FROM operation_submission_result
            WHERE submission_id = %s AND {owner_sql}
            {"FOR UPDATE" if lock else ""}
            """,
            (str(submission_id), *owner_params),
        )
        return cursor.fetchone()

    @staticmethod
    def _refusal_from_row(row: Mapping[str, Any]) -> RefusedAdmission:
        return RefusedAdmission(
            accepted=False,
            code=str(row["refusal_code"]),
            retryable=bool(row["retryable"]),
            retry_after_ms=row["retry_after_ms"],
        )

    def _operation_row(
        self,
        cursor: _StatementSession,
        operation_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> Mapping[str, Any] | None:
        cursor.execute(
            f"SELECT * FROM operation_record WHERE operation_id = %s "
            f"{'FOR UPDATE' if lock else ''}",
            (str(operation_id),),
        )
        return cursor.fetchone()

    def _queue_position(
        self, cursor: _StatementSession, operation: OperationRecord
    ) -> int | None:
        if operation.state is not OperationState.QUEUED:
            return None
        cursor.execute(
            """
            SELECT COUNT(*) AS queue_position
            FROM operation_record
            WHERE admission_class = %s AND state = 'queued'
              AND (
                  accepted_at < %s
                  OR (accepted_at = %s AND operation_id <= %s)
              )
            """,
            (
                operation.admission_class.value,
                operation.accepted_at,
                operation.accepted_at,
                str(operation.operation_id),
            ),
        )
        row = cursor.fetchone()
        return int(row["queue_position"]) if row is not None else None

    def _accepted(
        self, cursor: _StatementSession, operation: OperationRecord, *, created: bool = False
    ) -> AcceptedAdmission:
        return AcceptedAdmission(
            accepted=True,
            operation_id=operation.operation_id,
            state=operation.state,
            state_revision=operation.state_revision,
            queue_position=self._queue_position(cursor, operation),
            queue_deadline_at=operation.queue_deadline_at,
            created=created,
        )

    @staticmethod
    def _insert_submission(
        cursor: _StatementSession,
        request: OperationRequest,
        *,
        current_time: datetime,
        retention: timedelta,
        operation_id: uuid.UUID | None = None,
        refusal_code: str | None = None,
        retryable: bool = False,
        retry_after_ms: int | None = None,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO operation_submission_result (
                submission_result_id, submission_id, owner_scope, owner_user_id,
                connection_scope_id, accepted, operation_id, refusal_code,
                retryable, retry_after_ms, observed_at, purge_after
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                str(request.submission_id),
                request.owner.owner_scope.value,
                request.owner.owner_user_id,
                (
                    str(request.owner.connection_scope_id)
                    if request.owner.connection_scope_id is not None
                    else None
                ),
                operation_id is not None,
                str(operation_id) if operation_id is not None else None,
                refusal_code,
                retryable,
                retry_after_ms,
                current_time,
                current_time + retention,
            ),
        )

    def _existing_idempotent_operation(
        self, cursor: _StatementSession, request: OperationRequest
    ) -> Mapping[str, Any] | None:
        if request.idempotency_namespace is None:
            return None
        owner_sql, owner_params = self._owner_clause(request.owner)
        cursor.execute(
            f"""
            SELECT * FROM operation_record
            WHERE {owner_sql}
              AND idempotency_namespace = %s AND idempotency_key = %s
            FOR UPDATE
            """,
            (
                *owner_params,
                request.idempotency_namespace,
                request.idempotency_key,
            ),
        )
        return cursor.fetchone()

    def _select_free_slots(
        self, cursor: _StatementSession, class_name: AdmissionClass
    ) -> list[tuple[AdmissionClass, int]] | None:
        with self._configuration_lock:
            configs = dict(self._configs)
        selected: list[tuple[AdmissionClass, int]] = []
        for member in self._chain(class_name):
            cursor.execute(
                """
                SELECT slot_number FROM operation_admission_slot
                WHERE class_name = %s AND slot_number <= %s
                  AND operation_id IS NULL
                ORDER BY slot_number
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (member.value, configs[member].active_limit),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            selected.append((member, int(row["slot_number"])))
        return selected

    @staticmethod
    def _occupy_slots(
        cursor: _StatementSession,
        selected: Sequence[tuple[AdmissionClass, int]],
        *,
        operation_id: uuid.UUID,
        lease_token: uuid.UUID | None,
        lease_expires_at: datetime,
    ) -> None:
        for member, slot_number in selected:
            cursor.execute(
                """
                UPDATE operation_admission_slot
                SET operation_id = %s,
                    lease_token = %s,
                    claim_generation = claim_generation + 1,
                    lease_expires_at = %s
                WHERE class_name = %s AND slot_number = %s
                  AND operation_id IS NULL
                """,
                (
                    str(operation_id),
                    str(lease_token or uuid.uuid4()),
                    lease_expires_at,
                    member.value,
                    slot_number,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkAdmissionIntegrityError(
                    "admission slot claim lost atomicity"
                )

    def _lock_admission_request(self, transaction, cursor, request, retry=None):
        harness_owner = None
        if request.harness_selection is not None:
            harness_owner = request.harness_selection.owner_id
        elif request.harness_capture is not None:
            harness_owner = request.harness_capture.owner_id
        elif retry is not None:
            harness_owner = request.owner.owner_user_id
        if harness_owner is not None:
            lock_harness_owner(transaction, harness_owner)
        self._lock_request_identities(cursor, request)
        return harness_owner

    def has_admission_replay(self, transaction, request):
        request = _validated_request(request)
        cursor = _StatementSession(transaction)
        self._lock_admission_request(transaction, cursor, request)
        existing = self._submission_row(cursor, request.owner, request.submission_id, lock=True)
        if existing is not None:
            return True
        return self._existing_idempotent_operation(cursor, request) is not None

    def submit(
        self, transaction: Transaction, request: OperationRequest, *, now: datetime | None,
        retention: timedelta, slot_lease: timedelta,
    ) -> AdmissionResult:
        return self._submit(transaction, request, now=now, retention=retention,
                            slot_lease=slot_lease)

    def submit_retry(
        self, transaction: Transaction, request: OperationRequest, *,
        expected_parent: OperationRecord, expected_capture: HarnessCapture | None,
        now: datetime | None, retention: timedelta, slot_lease: timedelta,
    ) -> AdmissionResult:
        if type(expected_parent) is not OperationRecord:
            raise RepositoryValidationError("retry parent must be a detached operation record")
        if expected_capture is not None and type(expected_capture) is not HarnessCapture:
            raise RepositoryValidationError("retry capture must be typed")
        proof = _HarnessRetryProof(_RETRY_AUTHORITY, transaction, request,
                                   expected_parent, expected_capture)
        return self._submit(transaction, request, now=now, retention=retention,
                            slot_lease=slot_lease, _retry=proof)

    def _retry_capture(self, transaction, request, proof, current_time):
        if (type(proof) is not _HarnessRetryProof or proof.authority is not _RETRY_AUTHORITY
                or proof.transaction is not transaction or proof.request is not request
                or request.owner.owner_scope is not OwnerScope.USER
                or request.harness_selection is not None or request.harness_capture is not None
                or request.harness_assignment_id is not None):
            raise RepositoryConflictError("server retry identity changed")
        expected = proof.parent
        current = self.get_operation_record(transaction, request.owner,
                                            expected.operation_id, for_update=True)
        if (current is None or current != expected or current.state is not OperationState.RETRYABLE
                or current.purge_after is None or current.purge_after <= current_time
                or current.operation_id != request.parent_operation_id
                or current.operation_kind != request.operation_kind
                or current.admission_class is not request.admission_class
                or current.idempotency_namespace is None
                or current.idempotency_namespace != request.idempotency_namespace
                or current.normalized_input_digest is None
                or current.normalized_input_digest != request.normalized_input_digest
                or request.idempotency_key != "retry:" + str(current.operation_id)):
            raise RepositoryConflictError("server retry parent changed")
        captured = self.get_harness_capture(transaction, request.owner, current.operation_id)
        if captured != proof.capture:
            raise RepositoryConflictError("server retry capture changed")
        return captured

    def _submit(
        self,
        transaction: Transaction,
        request: OperationRequest,
        *,
        now: datetime | None,
        retention: timedelta,
        slot_lease: timedelta,
        _retry=None,
    ) -> AdmissionResult:
        request = _validated_request(request)
        retention = _validated_duration(retention, "retention")
        slot_lease = _validated_duration(slot_lease, "slot_lease")
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        harness_owner = self._lock_admission_request(transaction, cursor, request, _retry)
        existing_submission = self._submission_row(
            cursor, request.owner, request.submission_id, lock=True
        )
        if existing_submission is not None:
            if not existing_submission["accepted"]:
                return self._refusal_from_row(existing_submission)
            operation_id = self._uuid(existing_submission["operation_id"])
            if operation_id is None:
                raise WorkAdmissionNotFoundError("operation submission not found")
            operation_row = self._operation_row(cursor, operation_id)
            if operation_row is None:
                raise WorkAdmissionNotFoundError("operation submission not found")
            return self._accepted(cursor, self._operation_from_row(operation_row))

        idempotent_row = self._existing_idempotent_operation(cursor, request)
        if idempotent_row is not None:
            operation = self._operation_from_row(idempotent_row)
            if (
                operation.operation_kind != request.operation_kind
                or operation.admission_class is not request.admission_class
                or operation.normalized_input_digest != request.normalized_input_digest
            ):
                self._insert_submission(
                    cursor,
                    request,
                    current_time=current_time,
                    retention=retention,
                    refusal_code="idempotency_conflict",
                )
                return RefusedAdmission(False, "idempotency_conflict", False, None)
            self._insert_submission(
                cursor,
                request,
                current_time=current_time,
                retention=retention,
                operation_id=operation.operation_id,
            )
            return self._accepted(cursor, operation)

        capture = request.harness_capture
        if _retry is not None:
            capture = self._retry_capture(transaction, request, _retry, current_time)
        elif harness_owner is not None:
            if (request.owner.owner_scope is OwnerScope.USER
                    and request.owner.owner_user_id != harness_owner):
                raise RepositoryValidationError("harness admission owner mismatch")
            assert_harness_admission(transaction, owner_id=harness_owner,
                selection=request.harness_selection, capture=request.harness_capture,
                assignment_id=(None if request.harness_assignment_id is None
                               else str(request.harness_assignment_id)))
        elif request.harness_assignment_id is not None:
            raise RepositoryValidationError("inherited harness capture is missing")

        with self._configuration_lock:
            config = self._configs.get(request.admission_class)
        if config is None:
            raise WorkAdmissionConfigurationError(
                f"unknown admission class {request.admission_class.value}"
            )
        self._lock_class_chain(cursor, request.admission_class)
        self._expire_queued_locked(cursor, current_time, retention)
        self._expire_execution_leases_locked(cursor, current_time, retention)
        if request.admission_class is AdmissionClass.VOICE_INTERACTIVE:
            cursor.execute(
                """
                SELECT COUNT(*) AS running_count
                FROM operation_record
                WHERE admission_class = %s
                  AND owner_scope = %s
                  AND owner_user_id = %s
                  AND state = 'running'
                """,
                (
                    AdmissionClass.VOICE_INTERACTIVE.value,
                    OwnerScope.USER.value,
                    request.owner.owner_user_id,
                ),
            )
            running_row = cursor.fetchone()
            running_for_user = (
                int(running_row["running_count"]) if running_row else 0
            )
            if running_for_user >= VOICE_INTERACTIVE_PER_USER_ACTIVE_LIMIT:
                self._insert_submission(
                    cursor,
                    request,
                    current_time=current_time,
                    retention=retention,
                    refusal_code="capacity_exceeded",
                    retryable=True,
                    retry_after_ms=_VOICE_CAPACITY_RETRY_AFTER_MS,
                )
                return RefusedAdmission(
                    False,
                    "capacity_exceeded",
                    True,
                    _VOICE_CAPACITY_RETRY_AFTER_MS,
                )
        selected_slots = self._select_free_slots(cursor, request.admission_class)
        cursor.execute(
            """
            SELECT COUNT(*) AS queued_count FROM operation_record
            WHERE admission_class = %s AND state = 'queued'
            """,
            (request.admission_class.value,),
        )
        queue_row = cursor.fetchone()
        queued_count = int(queue_row["queued_count"]) if queue_row else 0
        if selected_slots is None and queued_count >= config.queue_limit:
            retry_after_ms = max(1, min(config.max_wait_ms or 1_000, 60_000))
            self._insert_submission(
                cursor,
                request,
                current_time=current_time,
                retention=retention,
                refusal_code="capacity_exceeded",
                retryable=True,
                retry_after_ms=retry_after_ms,
            )
            return RefusedAdmission(False, "capacity_exceeded", True, retry_after_ms)

        if selected_slots is None and (
            config.max_wait_ms is None or config.max_wait_ms <= 0
        ):
            raise WorkAdmissionConfigurationError(
                f"work class {config.class_name.value} requires finite queue wait"
            )
        operation_id = uuid.uuid4()
        execution_token = uuid.uuid4() if selected_slots is not None else None
        state = (
            OperationState.RUNNING
            if selected_slots is not None
            else OperationState.QUEUED
        )
        queue_deadline = (
            None
            if selected_slots is not None
            else current_time + timedelta(milliseconds=config.max_wait_ms or 0)
        )
        cursor.execute(
            """
            INSERT INTO operation_record (
                operation_id, operation_kind, admission_class, owner_scope,
                owner_user_id, connection_scope_id, idempotency_namespace,
                idempotency_key, normalized_input_digest, chat_id,
                parent_operation_id, connection_generation, request_generation,
                state, execution_generation, execution_lease_token,
                state_revision, accepted_at, updated_at, queue_deadline_at,
                started_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING *
            """,
            (
                str(operation_id),
                request.operation_kind,
                request.admission_class.value,
                request.owner.owner_scope.value,
                request.owner.owner_user_id,
                (
                    str(request.owner.connection_scope_id)
                    if request.owner.connection_scope_id is not None
                    else None
                ),
                request.idempotency_namespace,
                request.idempotency_key,
                request.normalized_input_digest,
                request.chat_id,
                (
                    str(request.parent_operation_id)
                    if request.parent_operation_id is not None
                    else None
                ),
                (
                    str(request.connection_generation)
                    if request.connection_generation is not None
                    else None
                ),
                (
                    str(request.request_generation)
                    if request.request_generation is not None
                    else None
                ),
                state.value,
                1 if selected_slots is not None else 0,
                str(execution_token) if execution_token is not None else None,
                1 if selected_slots is not None else 0,
                current_time,
                current_time,
                queue_deadline,
                current_time if selected_slots is not None else None,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise WorkAdmissionIntegrityError(
                "accepted operation insert returned no record"
            )
        if capture is not None:
            transaction.execute(
                "INSERT INTO operation_harness_capture(operation_id,owner_id,capture,created_at) "
                "VALUES(%s,%s,%s::jsonb,%s)",
                (str(operation_id), harness_owner,
                 json.dumps(capture.to_dict(), ensure_ascii=True,
                            separators=(",", ":")),
                 current_time))
        if selected_slots is not None:
            if execution_token is None:  # pragma: no cover
                raise WorkAdmissionIntegrityError(
                    "preselected execution is missing its token"
                )
            self._occupy_slots(
                cursor,
                selected_slots,
                operation_id=operation_id,
                lease_token=execution_token,
                lease_expires_at=current_time + slot_lease,
            )
        self._insert_submission(
            cursor,
            request,
            current_time=current_time,
            retention=retention,
            operation_id=operation_id,
        )
        return self._accepted(cursor, self._operation_from_row(row), created=True)

    @staticmethod
    def _expire_queued_locked(
        cursor: _StatementSession, current_time: datetime, retention: timedelta
    ) -> tuple[OperationRecord, ...]:
        cursor.execute(
            """
            UPDATE operation_record
            SET state = 'retryable',
                terminal_code = 'queue_wait_expired',
                safe_summary = 'Queue wait expired',
                retry_after_ms = 1000,
                state_revision = state_revision + 1,
                updated_at = %s,
                terminal_at = %s,
                purge_after = %s
            WHERE state = 'queued' AND queue_deadline_at <= %s
            RETURNING *
            """,
            (current_time, current_time, current_time + retention, current_time),
        )
        return tuple(
            WorkAdmissionRepository._operation_from_row(row)
            for row in cursor.fetchall()
        )

    def _claim_preselected(
        self,
        cursor: _StatementSession,
        operation: OperationRecord,
        *,
        current_time: datetime,
        slot_lease: timedelta,
    ) -> OperationClaim:
        marker_token = operation.execution_lease_token
        if marker_token is None:
            raise WorkAdmissionIntegrityError(
                "preselected execution is missing its token"
            )
        cursor.execute(
            """
            UPDATE operation_admission_slot
            SET lease_token = %s,
                claim_generation = claim_generation + 1,
                lease_expires_at = %s
            WHERE operation_id = %s AND lease_token = %s
            """,
            (
                str(uuid.uuid4()),
                current_time + slot_lease,
                str(operation.operation_id),
                str(marker_token),
            ),
        )
        if cursor.rowcount != len(self._chain(operation.admission_class)):
            raise WorkAdmissionIntegrityError(
                "preselected handoff marker is incomplete"
            )
        return OperationClaim(
            operation=operation,
            fence=ExecutionFence(
                operation_id=operation.operation_id,
                execution_generation=operation.execution_generation,
                execution_lease_token=marker_token,
            ),
        )

    def _claim_queued(
        self,
        cursor: _StatementSession,
        *,
        class_name: AdmissionClass,
        operation_id: uuid.UUID,
        current_time: datetime,
        slot_lease: timedelta,
    ) -> OperationClaim | None:
        claimed_slots = self._select_free_slots(cursor, class_name)
        if claimed_slots is None:
            return None
        self._occupy_slots(
            cursor,
            claimed_slots,
            operation_id=operation_id,
            lease_token=None,
            lease_expires_at=current_time + slot_lease,
        )
        execution_token = uuid.uuid4()
        cursor.execute(
            """
            UPDATE operation_record
            SET state = 'running',
                execution_generation = execution_generation + 1,
                execution_lease_token = %s,
                state_revision = state_revision + 1,
                updated_at = %s,
                started_at = COALESCE(started_at, %s)
            WHERE operation_id = %s AND state = 'queued'
            RETURNING *
            """,
            (
                str(execution_token),
                current_time,
                current_time,
                str(operation_id),
            ),
        )
        running_row = cursor.fetchone()
        if running_row is None:
            raise WorkAdmissionIntegrityError("operation selection lost atomicity")
        operation = self._operation_from_row(running_row)
        return OperationClaim(
            operation=operation,
            fence=ExecutionFence(
                operation_id=operation.operation_id,
                execution_generation=operation.execution_generation,
                execution_lease_token=execution_token,
            ),
        )

    def peek_next(
        self, transaction: Transaction, class_name: AdmissionClass
    ) -> OperationRecord | None:
        class_name = _validated_class(class_name)
        cursor = _StatementSession(transaction)
        cursor.execute(
            """
            SELECT operation.*
            FROM operation_record AS operation
            JOIN operation_admission_slot AS marker
              ON marker.operation_id = operation.operation_id
             AND marker.class_name = operation.admission_class
             AND marker.lease_token = operation.execution_lease_token
            WHERE operation.admission_class = %s
              AND operation.state = 'running'
              AND operation.cancel_requested_at IS NULL
            ORDER BY operation.accepted_at, operation.operation_id
            LIMIT 1
            """,
            (class_name.value,),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                """
                SELECT * FROM operation_record
                WHERE admission_class = %s AND state = 'queued'
                ORDER BY accepted_at, operation_id
                LIMIT 1
                """,
                (class_name.value,),
            )
            row = cursor.fetchone()
        return None if row is None else self._operation_from_row(row)

    def claim_next(
        self,
        transaction: Transaction,
        class_name: AdmissionClass,
        *,
        now: datetime | None,
        slot_lease: timedelta,
        retention: timedelta,
    ) -> OperationClaim | None:
        class_name = _validated_class(class_name)
        slot_lease = _validated_duration(slot_lease, "slot_lease")
        retention = _validated_duration(retention, "retention")
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        self._lock_class_chain(cursor, class_name)
        self._expire_execution_leases_locked(cursor, current_time, retention)
        self._expire_queued_locked(cursor, current_time, retention)
        cursor.execute(
            """
            SELECT operation.*
            FROM operation_record AS operation
            JOIN operation_admission_slot AS marker
              ON marker.operation_id = operation.operation_id
             AND marker.class_name = operation.admission_class
             AND marker.lease_token = operation.execution_lease_token
            WHERE operation.admission_class = %s
              AND operation.state = 'running'
              AND operation.cancel_requested_at IS NULL
            ORDER BY operation.accepted_at, operation.operation_id
            FOR UPDATE OF operation, marker SKIP LOCKED
            LIMIT 1
            """,
            (class_name.value,),
        )
        preselected_row = cursor.fetchone()
        if preselected_row is not None:
            return self._claim_preselected(
                cursor,
                self._operation_from_row(preselected_row),
                current_time=current_time,
                slot_lease=slot_lease,
            )
        cursor.execute(
            """
            SELECT * FROM operation_record
            WHERE admission_class = %s AND state = 'queued'
            ORDER BY accepted_at, operation_id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            (class_name.value,),
        )
        candidate = cursor.fetchone()
        if candidate is None:
            return None
        operation_id = self._uuid(candidate["operation_id"])
        if operation_id is None:
            raise WorkAdmissionIntegrityError("queued operation has no identity")
        return self._claim_queued(
            cursor,
            class_name=class_name,
            operation_id=operation_id,
            current_time=current_time,
            slot_lease=slot_lease,
        )

    def claim_operation(
        self,
        transaction: Transaction,
        class_name: AdmissionClass,
        operation_id: uuid.UUID,
        *,
        now: datetime | None,
        slot_lease: timedelta,
        retention: timedelta,
    ) -> OperationClaim | None:
        class_name = _validated_class(class_name)
        operation_id = _require_uuid(operation_id, "operation_id")
        slot_lease = _validated_duration(slot_lease, "slot_lease")
        retention = _validated_duration(retention, "retention")
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        self._lock_class_chain(cursor, class_name)
        self._expire_execution_leases_locked(cursor, current_time, retention)
        self._expire_queued_locked(cursor, current_time, retention)
        cursor.execute(
            """
            SELECT operation.*
            FROM operation_record AS operation
            JOIN operation_admission_slot AS marker
              ON marker.operation_id = operation.operation_id
             AND marker.class_name = operation.admission_class
             AND marker.lease_token = operation.execution_lease_token
            WHERE operation.operation_id = %s
              AND operation.admission_class = %s
              AND operation.state = 'running'
              AND operation.cancel_requested_at IS NULL
            FOR UPDATE OF operation, marker
            """,
            (str(operation_id), class_name.value),
        )
        preselected_row = cursor.fetchone()
        if preselected_row is not None:
            return self._claim_preselected(
                cursor,
                self._operation_from_row(preselected_row),
                current_time=current_time,
                slot_lease=slot_lease,
            )
        cursor.execute(
            """
            SELECT * FROM operation_record
            WHERE operation_id = %s AND admission_class = %s
            FOR UPDATE
            """,
            (str(operation_id), class_name.value),
        )
        candidate = cursor.fetchone()
        if candidate is None or candidate["state"] != OperationState.QUEUED.value:
            return None
        cursor.execute(
            """
            SELECT operation_id FROM operation_record
            WHERE admission_class = %s AND state = 'queued'
            ORDER BY accepted_at, operation_id
            LIMIT 1
            """,
            (class_name.value,),
        )
        head = cursor.fetchone()
        if head is None or str(head["operation_id"]) != str(operation_id):
            return None
        return self._claim_queued(
            cursor,
            class_name=class_name,
            operation_id=operation_id,
            current_time=current_time,
            slot_lease=slot_lease,
        )

    def inspect_admission_class(
        self,
        transaction: Transaction,
        class_name: AdmissionClass,
        *,
        now: datetime | None,
    ) -> AdmissionClassStatus:
        class_name = _validated_class(class_name)
        cursor = _StatementSession(transaction)
        self._current_time(cursor, now)
        cursor.execute(
            """
            SELECT class_name, parent_class_name, active_limit, queue_limit,
                   max_wait_ms
            FROM operation_admission_class WHERE class_name = %s
            """,
            (class_name.value,),
        )
        config = cursor.fetchone()
        if config is None:
            raise WorkAdmissionConfigurationError(
                f"unknown admission class {class_name.value}"
            )
        cursor.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE s.operation_id IS NOT NULL) AS active_count,
                MIN(o.started_at) AS oldest_running_at
            FROM operation_admission_slot AS s
            LEFT JOIN operation_record AS o ON o.operation_id = s.operation_id
            WHERE s.class_name = %s
            """,
            (class_name.value,),
        )
        active = cursor.fetchone()
        cursor.execute(
            """
            SELECT COUNT(*) AS queued_count, MIN(accepted_at) AS oldest_queued_at
            FROM operation_record
            WHERE admission_class = %s AND state = 'queued'
            """,
            (class_name.value,),
        )
        queued = cursor.fetchone()
        max_wait = int(config["max_wait_ms"])
        return AdmissionClassStatus(
            class_name=AdmissionClass(config["class_name"]),
            parent_class_name=(
                AdmissionClass(config["parent_class_name"])
                if config["parent_class_name"] is not None
                else None
            ),
            active_limit=int(config["active_limit"]),
            queue_limit=int(config["queue_limit"]),
            max_wait_ms=max_wait or None,
            active_count=int(active["active_count"]) if active else 0,
            queued_count=int(queued["queued_count"]) if queued else 0,
            oldest_queued_at=(
                _normalize_datetime(queued["oldest_queued_at"])
                if queued and queued["oldest_queued_at"] is not None
                else None
            ),
            oldest_running_at=(
                _normalize_datetime(active["oldest_running_at"])
                if active and active["oldest_running_at"] is not None
                else None
            ),
        )

    def query_operation(
        self,
        transaction: Transaction,
        owner: OperationOwner,
        operation_id: uuid.UUID,
    ) -> SafeOperationProjection:
        owner = _validated_owner(owner)
        operation_id = _require_uuid(operation_id, "operation_id")
        owner_sql, owner_params = self._owner_clause(owner)
        cursor = _StatementSession(transaction)
        cursor.execute(
            f"""
            SELECT * FROM operation_record
            WHERE operation_id = %s AND {owner_sql}
            """,
            (str(operation_id), *owner_params),
        )
        row = cursor.fetchone()
        if row is None:
            raise WorkAdmissionNotFoundError("operation not found")
        return _safe_projection(self._operation_from_row(row))

    def get_operation_record(self, transaction, owner, operation_id, *, for_update=False):
        owner = _validated_owner(owner)
        operation_id = _require_uuid(operation_id, "operation_id")
        if type(for_update) is not bool:
            raise RepositoryValidationError("for_update must be boolean")
        clause, parameters = self._owner_clause(owner)
        cursor = _StatementSession(transaction)
        cursor.execute("SELECT * FROM operation_record WHERE operation_id=%s AND " + clause
            + (" FOR UPDATE" if for_update else ""), (str(operation_id), *parameters))
        row = cursor.fetchone()
        return None if row is None else self._operation_from_row(row)

    def get_operation_for_administration(
        self,
        transaction: Transaction,
        *,
        operation_id: uuid.UUID,
        for_update: bool = False,
    ) -> OperationRecord | None:
        operation_id = _require_uuid(operation_id, "operation_id")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        cursor = _StatementSession(transaction)
        cursor.execute(
            "SELECT * FROM operation_record WHERE operation_id = %s"
            + (" FOR UPDATE" if for_update else ""),
            (str(operation_id),),
        )
        row = cursor.fetchone()
        return None if row is None else self._operation_from_row(row)

    def get_idempotent_operation(
        self,
        transaction: Transaction,
        *,
        owner: OperationOwner,
        idempotency_namespace: str,
        idempotency_key: str,
    ) -> OperationRecord | None:
        owner = _validated_owner(owner)
        if not isinstance(idempotency_namespace, str) or not 1 <= len(idempotency_namespace) <= 128:
            raise RepositoryValidationError("idempotency namespace must be 1..128 characters")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
            raise RepositoryValidationError("idempotency key must be 1..256 characters")
        owner_sql, owner_parameters = self._owner_clause(owner)
        cursor = _StatementSession(transaction)
        cursor.execute(
            f"""
            SELECT * FROM operation_record WHERE {owner_sql}
              AND idempotency_namespace = %s AND idempotency_key = %s
            LIMIT 1
            """,
            (*owner_parameters, idempotency_namespace, idempotency_key),
        )
        row = cursor.fetchone()
        return None if row is None else self._operation_from_row(row)

    def reconcile_submission(
        self,
        transaction: Transaction,
        owner: OperationOwner,
        submission_id: uuid.UUID,
    ) -> SubmissionResult:
        owner = _validated_owner(owner)
        submission_id = _require_uuid(submission_id, "submission_id")
        cursor = _StatementSession(transaction)
        row = self._submission_row(cursor, owner, submission_id)
        if row is None:
            raise WorkAdmissionNotFoundError("operation submission not found")
        if not row["accepted"]:
            return self._refusal_from_row(row)
        operation_id = self._uuid(row["operation_id"])
        if operation_id is None:
            raise WorkAdmissionNotFoundError("operation submission not found")
        operation = self._operation_row(cursor, operation_id)
        if operation is None:
            raise WorkAdmissionNotFoundError("operation submission not found")
        return AcceptedSubmission(
            accepted=True,
            operation=_safe_projection(self._operation_from_row(operation)),
        )

    @staticmethod
    def _release_slots(cursor: _StatementSession, operation_id: uuid.UUID) -> None:
        cursor.execute(
            """
            UPDATE operation_admission_slot
            SET operation_id = NULL,
                lease_token = NULL,
                claim_generation = claim_generation + 1,
                lease_expires_at = NULL
            WHERE operation_id = %s
            """,
            (str(operation_id),),
        )
        cursor.execute(
            """
            DELETE FROM operation_admission_slot AS slot
            USING operation_admission_class AS config
            WHERE slot.class_name = config.class_name
              AND slot.slot_number > config.active_limit
              AND slot.operation_id IS NULL
            """
        )

    def cancel(
        self,
        transaction: Transaction,
        owner: OperationOwner,
        operation_id: uuid.UUID,
        terminal_code: str,
        *,
        now: datetime | None,
        retention: timedelta,
        request_running: bool = True,
    ) -> OperationRecord:
        owner = _validated_owner(owner)
        operation_id = _require_uuid(operation_id, "operation_id")
        terminal_code = _validated_code(terminal_code, "terminal_code")
        retention = _validated_duration(retention, "retention")
        if not isinstance(request_running, bool):
            raise RepositoryValidationError("request_running must be boolean")
        owner_sql, owner_params = self._owner_clause(owner)
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        cursor.execute(
            f"SELECT admission_class FROM operation_record "
            f"WHERE operation_id = %s AND {owner_sql}",
            (str(operation_id), *owner_params),
        )
        identity = cursor.fetchone()
        if identity is None:
            raise WorkAdmissionNotFoundError("operation not found")
        self._lock_class_chain(
            cursor, AdmissionClass(str(identity["admission_class"]))
        )
        cursor.execute(
            f"""
            SELECT * FROM operation_record
            WHERE operation_id = %s AND {owner_sql}
            FOR UPDATE
            """,
            (str(operation_id), *owner_params),
        )
        row = cursor.fetchone()
        if row is None:
            raise WorkAdmissionNotFoundError("operation not found")
        operation = self._operation_from_row(row)
        if (
            operation.state in _TERMINAL_STATES
            or operation.cancel_requested_at is not None
        ):
            return operation
        preselected = False
        if operation.state is OperationState.RUNNING:
            cursor.execute(
                """
                SELECT COUNT(*) AS slot_count,
                       BOOL_AND(lease_token = %s) AS marker_matches
                FROM operation_admission_slot
                WHERE operation_id = %s
                """,
                (
                    str(operation.execution_lease_token),
                    str(operation.operation_id),
                ),
            )
            marker = cursor.fetchone()
            preselected = bool(
                marker
                and int(marker["slot_count"])
                == len(self._chain(operation.admission_class))
                and marker["marker_matches"]
            )
        if operation.state is OperationState.QUEUED or preselected:
            cursor.execute(
                """
                UPDATE operation_record
                SET state = 'cancelled', terminal_code = %s,
                    safe_summary = 'Cancelled',
                    state_revision = state_revision + 1,
                    updated_at = %s, cancel_requested_at = %s,
                    terminal_at = %s, purge_after = %s,
                    execution_lease_token = NULL
                WHERE operation_id = %s
                  AND state IN ('queued', 'running')
                RETURNING *
                """,
                (
                    terminal_code,
                    current_time,
                    current_time,
                    current_time,
                    current_time + retention,
                    str(operation_id),
                ),
            )
            release_slots = True
        else:
            if not request_running:
                return operation
            cursor.execute(
                """
                UPDATE operation_record
                SET state_revision = state_revision + 1,
                    updated_at = %s, cancel_requested_at = %s
                WHERE operation_id = %s AND state = 'running'
                RETURNING *
                """,
                (current_time, current_time, str(operation_id)),
            )
            release_slots = False
        updated = cursor.fetchone()
        if updated is None:
            raise WorkAdmissionIntegrityError(
                "operation cancellation lost atomicity"
            )
        if release_slots:
            self._release_slots(cursor, operation_id)
        return self._operation_from_row(updated)

    def terminalize_unselected(
        self,
        transaction: Transaction,
        operation_id: uuid.UUID,
        *,
        terminal_code: str,
        safe_summary: str | None,
        retry_after_ms: int | None,
        now: datetime | None,
        retention: timedelta,
    ) -> OperationRecord | None:
        operation_id = _require_uuid(operation_id, "operation_id")
        terminal_code, safe_summary, retry_after_ms = _validated_terminal_payload(
            OperationState.RETRYABLE,
            terminal_code=terminal_code,
            safe_summary=safe_summary,
            retry_after_ms=retry_after_ms,
        )
        retention = _validated_duration(retention, "retention")
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        cursor.execute(
            "SELECT admission_class FROM operation_record WHERE operation_id = %s",
            (str(operation_id),),
        )
        identity = cursor.fetchone()
        if identity is None:
            return None
        admission_class = AdmissionClass(identity["admission_class"])
        self._lock_class_chain(cursor, admission_class)
        row = self._operation_row(cursor, operation_id, lock=True)
        if row is None:
            return None
        operation = self._operation_from_row(row)
        if operation.state in _TERMINAL_STATES:
            return operation
        if operation.state is OperationState.RUNNING:
            cursor.execute(
                """
                SELECT class_name, lease_token
                FROM operation_admission_slot
                WHERE operation_id = %s
                ORDER BY class_name, slot_number
                FOR UPDATE
                """,
                (str(operation_id),),
            )
            slots = cursor.fetchall()
            expected_classes = {
                member.value for member in self._chain(admission_class)
            }
            preselected = (
                operation.cancel_requested_at is None
                and operation.execution_lease_token is not None
                and len(slots) == len(expected_classes)
                and {str(slot["class_name"]) for slot in slots} == expected_classes
                and all(
                    self._uuid(slot["lease_token"])
                    == operation.execution_lease_token
                    for slot in slots
                )
            )
            if not preselected:
                return None
        elif operation.state is not OperationState.QUEUED:
            return None
        execution_token = (
            str(operation.execution_lease_token)
            if operation.execution_lease_token is not None
            else None
        )
        cursor.execute(
            """
            UPDATE operation_record
            SET state = 'retryable', terminal_code = %s,
                safe_summary = %s, retry_after_ms = %s,
                execution_lease_token = NULL,
                state_revision = state_revision + 1,
                updated_at = %s, terminal_at = %s, purge_after = %s
            WHERE operation_id = %s AND state = %s
              AND execution_generation = %s
              AND execution_lease_token IS NOT DISTINCT FROM %s
            RETURNING *
            """,
            (
                terminal_code,
                safe_summary,
                retry_after_ms,
                current_time,
                current_time,
                current_time + retention,
                str(operation_id),
                operation.state.value,
                operation.execution_generation,
                execution_token,
            ),
        )
        terminal = cursor.fetchone()
        if terminal is None:
            raise WorkAdmissionIntegrityError(
                "unselected terminalization lost atomicity"
            )
        self._release_slots(cursor, operation_id)
        return self._operation_from_row(terminal)

    @staticmethod
    def _fence_matches(operation: OperationRecord, fence: ExecutionFence) -> bool:
        return (
            operation.state is OperationState.RUNNING
            and operation.execution_generation == fence.execution_generation
            and operation.execution_lease_token == fence.execution_lease_token
        )

    def terminalize(
        self,
        transaction: Transaction,
        fence: ExecutionFence,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str | None,
        retry_after_ms: int | None,
        now: datetime | None,
        retention: timedelta,
    ) -> OperationRecord:
        fence = _validated_fence(fence)
        terminal_code, safe_summary, retry_after_ms = _validated_terminal_payload(
            state,
            terminal_code=terminal_code,
            safe_summary=safe_summary,
            retry_after_ms=retry_after_ms,
        )
        retention = _validated_duration(retention, "retention")
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        row = self._operation_row(cursor, fence.operation_id, lock=True)
        if row is None:
            raise StaleWorkExecutionFenceError("execution fence is stale")
        operation = self._operation_from_row(row)
        if operation.state in _TERMINAL_STATES:
            return operation
        if not self._fence_matches(operation, fence):
            raise StaleWorkExecutionFenceError("execution fence is stale")
        cursor.execute(
            """
            UPDATE operation_record
            SET state = %s, terminal_code = %s, safe_summary = %s,
                retry_after_ms = %s, execution_lease_token = NULL,
                state_revision = state_revision + 1,
                updated_at = %s, terminal_at = %s, purge_after = %s
            WHERE operation_id = %s AND state = 'running'
              AND execution_generation = %s
              AND execution_lease_token = %s
            RETURNING *
            """,
            (
                state.value,
                terminal_code,
                safe_summary,
                retry_after_ms,
                current_time,
                current_time,
                current_time + retention,
                str(fence.operation_id),
                fence.execution_generation,
                str(fence.execution_lease_token),
            ),
        )
        terminal = cursor.fetchone()
        if terminal is None:
            raise StaleWorkExecutionFenceError("execution fence is stale")
        self._release_slots(cursor, fence.operation_id)
        return self._operation_from_row(terminal)

    def expire_queued(
        self,
        transaction: Transaction,
        *,
        now: datetime | None,
        retention: timedelta,
    ) -> tuple[OperationRecord, ...]:
        retention = _validated_duration(retention, "retention")
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        return self._expire_queued_locked(cursor, current_time, retention)

    def _assert_current_execution_session(
        self, cursor: _StatementSession, fence: ExecutionFence
    ) -> OperationRecord:
        cursor.execute(
            """
            SELECT * FROM operation_record
            WHERE operation_id = %s
            FOR UPDATE
            """,
            (str(fence.operation_id),),
        )
        row = cursor.fetchone()
        if row is None:
            raise StaleWorkExecutionFenceError("execution fence is stale")
        operation = self._operation_from_row(row)
        if not self._fence_matches(operation, fence):
            raise StaleWorkExecutionFenceError("execution fence is stale")
        return operation

    def assert_current_execution(
        self, transaction: Transaction, fence: ExecutionFence
    ) -> OperationRecord:
        fence = _validated_fence(fence)
        return self._assert_current_execution_session(
            _StatementSession(transaction), fence
        )

    # Caller must already hold owner/session and occurrence locks
    def assert_current_execution_lease(
        self, transaction: Transaction, fence: ExecutionFence
    ) -> OperationRecord:
        if (type(fence) is not ExecutionFence
                or type(fence.execution_generation) is not int
                or not 1 <= fence.execution_generation <= 2**63 - 1):
            raise RepositoryValidationError("execution fence is invalid")
        fence = ExecutionFence(
            fence.operation_id, fence.execution_generation, fence.execution_lease_token
        )
        cursor = _StatementSession(transaction)
        operation = self._assert_current_execution_session(cursor, fence)
        if operation.cancel_requested_at is not None:
            raise StaleWorkExecutionFenceError("execution capacity lease is unavailable")
        expected = self._chain(operation.admission_class)
        cursor.execute(
            """
            SELECT class_name, slot_number, operation_id, lease_token,
                   claim_generation, lease_expires_at
            FROM operation_admission_slot WHERE operation_id = %s
            ORDER BY class_name, slot_number FOR UPDATE
            """,
            (str(fence.operation_id),),
        )
        slots = cursor.fetchall()
        cursor.execute("SELECT clock_timestamp() AS current_time")
        clock = cursor.fetchone()
        if clock is None:
            raise WorkAdmissionIntegrityError("execution clock is unavailable")
        now = _normalize_datetime(clock["current_time"])
        try:
            complete = (
                expected == self._chain(operation.admission_class)
                and len(slots) == len(expected)
                and {row["class_name"] for row in slots} == {c.value for c in expected}
                and len({self._uuid(row["lease_token"]) for row in slots}) == 1
                and all(
                    self._uuid(row["operation_id"]) == fence.operation_id
                    and self._uuid(row["lease_token"]) is not None
                    and type(row["claim_generation"]) is int
                    and row["claim_generation"] > 0
                    and _normalize_datetime(row["lease_expires_at"]) > now
                    for row in slots
                )
            )
        except (KeyError, TypeError, ValueError, WorkAdmissionIntegrityError):
            complete = False
        if not complete:
            raise StaleWorkExecutionFenceError("execution capacity lease is unavailable")
        return operation

    def reselect_execution(
        self,
        transaction: Transaction,
        fence: ExecutionFence,
        *,
        now: datetime | None,
        slot_lease: timedelta,
    ) -> ExecutionFence:
        fence = _validated_fence(fence)
        slot_lease = _validated_duration(slot_lease, "slot_lease")
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        operation = self._assert_current_execution_session(cursor, fence)
        execution_token = uuid.uuid4()
        cursor.execute(
            """
            UPDATE operation_record
            SET execution_generation = execution_generation + 1,
                execution_lease_token = %s,
                state_revision = state_revision + 1,
                updated_at = %s
            WHERE operation_id = %s AND state = 'running'
              AND execution_generation = %s
              AND execution_lease_token = %s
            RETURNING execution_generation
            """,
            (
                str(execution_token),
                current_time,
                str(fence.operation_id),
                fence.execution_generation,
                str(fence.execution_lease_token),
            ),
        )
        selected = cursor.fetchone()
        if selected is None:
            raise StaleWorkExecutionFenceError("execution fence is stale")
        slot_token = uuid.uuid4()
        cursor.execute(
            """
            UPDATE operation_admission_slot
            SET lease_token = %s,
                claim_generation = claim_generation + 1,
                lease_expires_at = %s
            WHERE operation_id = %s
            """,
            (
                str(slot_token),
                current_time + slot_lease,
                str(fence.operation_id),
            ),
        )
        expected_slots = len(self._chain(operation.admission_class))
        if cursor.rowcount != expected_slots:
            raise StaleWorkExecutionFenceError(
                "execution capacity lease is missing"
            )
        return ExecutionFence(
            operation_id=fence.operation_id,
            execution_generation=int(selected["execution_generation"]),
            execution_lease_token=execution_token,
        )

    def update_phase(
        self,
        transaction: Transaction,
        fence: ExecutionFence,
        phase_code: str,
        *,
        now: datetime | None,
    ) -> OperationRecord:
        fence = _validated_fence(fence)
        phase_code = _validated_code(phase_code, "phase_code")
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        operation = self._assert_current_execution_session(cursor, fence)
        if operation.phase_code == phase_code:
            return operation
        cursor.execute(
            """
            UPDATE operation_record
            SET phase_code = %s, state_revision = state_revision + 1,
                updated_at = %s
            WHERE operation_id = %s AND state = 'running'
              AND execution_generation = %s
              AND execution_lease_token = %s
            RETURNING *
            """,
            (
                phase_code,
                current_time,
                str(fence.operation_id),
                fence.execution_generation,
                str(fence.execution_lease_token),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise StaleWorkExecutionFenceError("execution fence is stale")
        return self._operation_from_row(row)

    def bind_chat(
        self,
        transaction: Transaction,
        fence: ExecutionFence,
        chat_id: str,
        *,
        now: datetime | None,
    ) -> OperationRecord:
        fence = _validated_fence(fence)
        if not isinstance(chat_id, str) or not chat_id or len(chat_id) > 512:
            raise RepositoryValidationError(
                "chat_id must be a non-empty string of at most 512 characters"
            )
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        operation = self._assert_current_execution_session(cursor, fence)
        if operation.chat_id is not None:
            if str(operation.chat_id) == str(chat_id):
                return operation
            raise RepositoryValidationError(
                "operation is bound to a different conversation"
            )
        cursor.execute(
            """
            UPDATE operation_record
            SET chat_id = %s, state_revision = state_revision + 1,
                updated_at = %s
            WHERE operation_id = %s AND state = 'running'
              AND execution_generation = %s
              AND execution_lease_token = %s
              AND chat_id IS NULL
            RETURNING *
            """,
            (
                str(chat_id),
                current_time,
                str(fence.operation_id),
                fence.execution_generation,
                str(fence.execution_lease_token),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise StaleWorkExecutionFenceError("execution fence is stale")
        return self._operation_from_row(row)

    def bind_request_generation(
        self,
        transaction: Transaction,
        *,
        fence: ExecutionFence,
        request_generation: uuid.UUID,
    ) -> OperationRecord:
        fence = _validated_fence(fence)
        request_generation = _require_uuid(request_generation, "request_generation")
        cursor = _StatementSession(transaction)
        operation = self._assert_current_execution_session(cursor, fence)
        if operation.request_generation is not None:
            if operation.request_generation == request_generation:
                return operation
            raise RepositoryConflictError(
                "operation is bound to a different request generation"
            )
        cursor.execute(
            """
            UPDATE operation_record
            SET request_generation = %s,
                state_revision = state_revision + 1,
                updated_at = clock_timestamp()
            WHERE operation_id = %s AND state = 'running'
              AND execution_generation = %s
              AND execution_lease_token = %s
              AND request_generation IS NULL
            RETURNING *
            """,
            (
                str(request_generation),
                str(fence.operation_id),
                fence.execution_generation,
                str(fence.execution_lease_token),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise StaleWorkExecutionFenceError("execution fence is stale")
        return self._operation_from_row(row)

    def get_harness_capture(self, transaction, owner, operation_id):
        owner = _validated_owner(owner)
        operation_id = _require_uuid(operation_id, "operation_id")
        owner_sql, parameters = self._owner_clause(owner, alias="operation")
        cursor = _StatementSession(transaction)
        cursor.execute(
            "SELECT capture.capture,capture.owner_id FROM operation_harness_capture AS capture "
            "JOIN operation_record AS operation ON operation.operation_id=capture.operation_id "
            f"WHERE operation.operation_id=%s AND {owner_sql}", (str(operation_id), *parameters))
        row = cursor.fetchone()
        if row is None:
            return None
        captured = harness_capture_from_value(row["capture"])
        if (captured.owner_id != row["owner_id"] or (
                owner.owner_scope is OwnerScope.USER and captured.owner_id != owner.owner_user_id)):
            raise RepositoryDataError("operation harness capture owner is invalid")
        return captured

    def bind_harness_profile(
        self,
        transaction: Transaction,
        owner: OperationOwner,
        fence: ExecutionFence,
        identity: HarnessProfileIdentity,
        *,
        now: datetime | None = None,
    ) -> OperationHarnessBinding:
        owner = _validated_owner(owner)
        fence = _validated_fence(fence)
        if not isinstance(identity, HarnessProfileIdentity):
            raise RepositoryValidationError(
                "identity must be a HarnessProfileIdentity"
            )
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        owner_sql, owner_parameters = self._owner_clause(owner, alias="operation")
        cursor.execute(
            f"""
            SELECT operation.* FROM operation_record AS operation
            WHERE operation.operation_id = %s AND {owner_sql}
            FOR UPDATE
            """,
            (str(fence.operation_id), *owner_parameters),
        )
        row = cursor.fetchone()
        if row is None:
            raise WorkAdmissionNotFoundError("operation not found")
        operation = self._operation_from_row(row)
        if not self._fence_matches(operation, fence):
            raise StaleWorkExecutionFenceError("execution fence is stale")
        capture = self.get_harness_capture(transaction, owner, fence.operation_id)
        if capture is not None and capture.identity != identity:
            raise RepositoryConflictError(
                "operation capture has a different harness profile identity")
        cursor.execute(
            f"""
            SELECT binding.* FROM operation_harness_binding AS binding
            JOIN operation_record AS operation
              ON operation.operation_id = binding.operation_id
            WHERE binding.operation_id = %s AND {owner_sql}
            """,
            (str(fence.operation_id), *owner_parameters),
        )
        existing_row = cursor.fetchone()
        if existing_row is not None:
            existing = operation_harness_binding_from_row(existing_row)
            if existing.identity == identity:
                return existing
            raise RepositoryConflictError(
                "operation is bound to a different harness profile identity"
            )
        cursor.execute(
            f"""
            INSERT INTO operation_harness_binding (
                operation_id, profile, profile_version, artifact_digest,
                execution_generation, bound_at
            )
            SELECT operation.operation_id, %s, %s, %s,
                   operation.execution_generation, %s
            FROM operation_record AS operation
            WHERE operation.operation_id = %s AND {owner_sql}
              AND operation.state = 'running'
              AND operation.execution_generation = %s
              AND operation.execution_lease_token = %s
            ON CONFLICT (operation_id) DO NOTHING
            RETURNING operation_id, profile, profile_version, artifact_digest,
                      execution_generation, bound_at
            """,
            (
                identity.profile,
                identity.version,
                identity.artifact_digest,
                current_time,
                str(fence.operation_id),
                *owner_parameters,
                fence.execution_generation,
                str(fence.execution_lease_token),
            ),
        )
        inserted = cursor.fetchone()
        if inserted is not None:
            return operation_harness_binding_from_row(inserted)
        cursor.execute(
            f"""
            SELECT binding.* FROM operation_harness_binding AS binding
            JOIN operation_record AS operation
              ON operation.operation_id = binding.operation_id
            WHERE binding.operation_id = %s AND {owner_sql}
            """,
            (str(fence.operation_id), *owner_parameters),
        )
        raced_row = cursor.fetchone()
        if raced_row is None:
            raise WorkAdmissionIntegrityError(
                "harness binding insert lost atomicity"
            )
        raced = operation_harness_binding_from_row(raced_row)
        if raced.identity == identity:
            return raced
        raise RepositoryConflictError(
            "operation is bound to a different harness profile identity"
        )

    def get_harness_profile_binding(
        self,
        transaction: Transaction,
        owner: OperationOwner,
        operation_id: uuid.UUID,
    ) -> OperationHarnessBinding | None:
        owner = _validated_owner(owner)
        operation_id = _require_uuid(operation_id, "operation_id")
        owner_sql, owner_parameters = self._owner_clause(owner, alias="operation")
        cursor = _StatementSession(transaction)
        cursor.execute(
            f"""
            SELECT binding.operation_id, binding.profile, binding.profile_version,
                   binding.artifact_digest, binding.execution_generation,
                   binding.bound_at
            FROM operation_harness_binding AS binding
            JOIN operation_record AS operation
              ON operation.operation_id = binding.operation_id
            WHERE binding.operation_id = %s AND {owner_sql}
            LIMIT 1
            """,
            (str(operation_id), *owner_parameters),
        )
        row = cursor.fetchone()
        return None if row is None else operation_harness_binding_from_row(row)

    def renew_execution_lease(
        self,
        transaction: Transaction,
        fence: ExecutionFence,
        *,
        now: datetime | None,
        slot_lease: timedelta,
    ) -> SlotLeaseRenewal:
        fence = _validated_fence(fence)
        slot_lease = _validated_duration(slot_lease, "slot_lease")
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        operation = self._assert_current_execution_session(cursor, fence)
        lease_expires_at = current_time + slot_lease
        slot_token = uuid.uuid4()
        cursor.execute(
            """
            UPDATE operation_admission_slot
            SET lease_token = %s,
                claim_generation = claim_generation + 1,
                lease_expires_at = %s
            WHERE operation_id = %s
            """,
            (str(slot_token), lease_expires_at, str(fence.operation_id)),
        )
        if cursor.rowcount != len(self._chain(operation.admission_class)):
            raise StaleWorkExecutionFenceError(
                "execution capacity lease is missing"
            )
        return SlotLeaseRenewal(
            operation_id=fence.operation_id,
            execution_generation=fence.execution_generation,
            lease_expires_at=lease_expires_at,
        )

    def expire_execution_leases(
        self,
        transaction: Transaction,
        *,
        now: datetime | None,
        retention: timedelta,
    ) -> tuple[OperationRecord, ...]:
        retention = _validated_duration(retention, "retention")
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        return self._expire_execution_leases_locked(cursor, current_time, retention)

    def list_retryable_for_administration(
        self,
        transaction: Transaction,
        *,
        terminal_code: str,
        now: datetime | None = None,
        after: RetryableOperationCursor | None = None,
        limit: int = 100,
        handled_idempotency_namespace: str | None = None,
    ) -> tuple[OperationRecord, ...]:
        terminal_code = _validated_code(terminal_code, "terminal_code")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise RepositoryValidationError("inventory limit must be between 1 and 1000")
        if after is not None and not isinstance(after, RetryableOperationCursor):
            raise RepositoryValidationError("after must be a retryable operation cursor")
        if handled_idempotency_namespace is not None and (
            not isinstance(handled_idempotency_namespace, str)
            or not 1 <= len(handled_idempotency_namespace) <= 128
        ):
            raise RepositoryValidationError(
                "handled idempotency namespace must be 1..128 characters"
            )
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        cursor.execute(
            """
            SELECT operation.* FROM operation_record AS operation
            WHERE operation.state = 'retryable' AND operation.terminal_code = %s
              AND operation.purge_after > %s
              AND (%s::timestamptz IS NULL
                   OR (operation.accepted_at, operation.operation_id) > (%s, %s::uuid))
              AND (%s::text IS NULL OR NOT EXISTS (
                  SELECT 1 FROM operation_record AS child
                  WHERE child.parent_operation_id = operation.operation_id
                    AND child.operation_kind = operation.operation_kind
                    AND child.admission_class = operation.admission_class
                    AND child.chat_id IS NOT DISTINCT FROM operation.chat_id
                    AND child.connection_generation
                        IS NOT DISTINCT FROM operation.connection_generation
                    AND child.request_generation
                        IS NOT DISTINCT FROM operation.request_generation
                    AND operation.normalized_input_digest IS NOT NULL
                    AND child.normalized_input_digest = operation.normalized_input_digest
                    AND child.owner_scope = operation.owner_scope
                    AND child.owner_user_id IS NOT DISTINCT FROM operation.owner_user_id
                    AND child.connection_scope_id IS NOT DISTINCT FROM operation.connection_scope_id
                    AND (CASE
                        WHEN child.owner_scope = 'connection' THEN child.connection_scope_id::text
                        WHEN child.owner_scope IN ('user', 'schedule') THEN child.owner_user_id
                        ELSE '' END) = (CASE
                        WHEN operation.owner_scope = 'connection'
                            THEN operation.connection_scope_id::text
                        WHEN operation.owner_scope IN ('user', 'schedule')
                            THEN operation.owner_user_id
                        ELSE '' END)
                    AND child.idempotency_namespace = %s
                    AND child.idempotency_key = operation.operation_id::text
              ))
            ORDER BY operation.accepted_at, operation.operation_id
            LIMIT %s
            """,
            (
                terminal_code, current_time,
                None if after is None else after.accepted_at,
                None if after is None else after.accepted_at,
                None if after is None else str(after.operation_id),
                handled_idempotency_namespace, handled_idempotency_namespace, limit,
            ),
        )
        return tuple(self._operation_from_row(row) for row in cursor.fetchall())

    def oldest_purge_eligible_due_at(
        self,
        transaction: Transaction,
        *,
        now: datetime | None,
    ) -> datetime | None:
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        cursor.execute(
            """
            WITH eligible AS (
                SELECT submission.purge_after AS due_at
                FROM operation_submission_result AS submission
                LEFT JOIN operation_record AS operation
                  ON operation.operation_id = submission.operation_id
                WHERE submission.purge_after < %s
                  AND (
                      NOT submission.accepted
                      OR operation.operation_id IS NULL
                      OR (
                          operation.state IN (
                              'completed', 'failed', 'cancelled', 'retryable'
                          )
                          AND operation.purge_after < %s
                      )
                  )
                UNION ALL
                SELECT operation.purge_after AS due_at
                FROM operation_record AS operation
                WHERE operation.state IN (
                    'completed', 'failed', 'cancelled', 'retryable'
                )
                  AND operation.purge_after < %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM operation_submission_result AS submission
                      WHERE submission.accepted
                        AND submission.operation_id = operation.operation_id
                  )
            )
            SELECT MIN(due_at) AS due_at FROM eligible
            """,
            (current_time, current_time, current_time),
        )
        row = cursor.fetchone()
        if row is None or row.get("due_at") is None:
            return None
        try:
            return _normalize_datetime(row["due_at"])
        except WorkAdmissionIntegrityError as exc:
            raise WorkAdmissionIntegrityError(
                "persisted purge eligibility timestamp is invalid"
            ) from exc

    def _expire_execution_leases_locked(
        self,
        cursor: _StatementSession,
        current_time: datetime,
        retention: timedelta,
    ) -> tuple[OperationRecord, ...]:
        cursor.execute(
            """
            SELECT class_name, slot_number, operation_id
            FROM operation_admission_slot
            WHERE operation_id IS NOT NULL AND lease_expires_at <= %s
            ORDER BY lease_expires_at, class_name, slot_number
            """,
            (current_time,),
        )
        operation_ids = sorted(
            {
                operation_id
                for row in cursor.fetchall()
                if (operation_id := self._uuid(row["operation_id"])) is not None
            },
            key=lambda value: value.int,
        )
        terminal_records: list[OperationRecord] = []
        for operation_id in operation_ids:
            row = self._operation_row(cursor, operation_id, lock=True)
            if row is None:
                self._release_slots(cursor, operation_id)
                continue
            operation = self._operation_from_row(row)
            if operation.state is not OperationState.RUNNING:
                self._release_slots(cursor, operation_id)
                continue
            cursor.execute(
                """
                SELECT 1 FROM operation_admission_slot
                WHERE operation_id = %s
                ORDER BY class_name, slot_number
                FOR UPDATE
                """,
                (str(operation_id),),
            )
            cursor.fetchall()
            cursor.execute(
                """
                SELECT 1 FROM operation_admission_slot
                WHERE operation_id = %s AND lease_expires_at <= %s
                LIMIT 1
                """,
                (str(operation_id), current_time),
            )
            if cursor.fetchone() is None:
                continue
            cursor.execute(
                """
                UPDATE operation_record
                SET state = 'retryable',
                    terminal_code = 'execution_lease_expired',
                    safe_summary = 'Execution lease expired',
                    retry_after_ms = 1000,
                    execution_lease_token = NULL,
                    state_revision = state_revision + 1,
                    updated_at = %s, terminal_at = %s, purge_after = %s
                WHERE operation_id = %s AND state = 'running'
                RETURNING *
                """,
                (
                    current_time,
                    current_time,
                    current_time + retention,
                    str(operation_id),
                ),
            )
            terminal = cursor.fetchone()
            if terminal is None:
                raise WorkAdmissionIntegrityError(
                    "execution lease recovery lost atomicity"
                )
            self._release_slots(cursor, operation_id)
            terminal_records.append(self._operation_from_row(terminal))
        return tuple(terminal_records)

    def purge_expired(
        self,
        transaction: Transaction,
        *,
        now: datetime | None,
        limit: int,
        fence: ExecutionFence | None = None,
    ) -> PurgeResult:
        limit = _validated_limit(limit)
        if fence is not None:
            fence = _validated_fence(fence)
        cursor = _StatementSession(transaction)
        current_time = self._current_time(cursor, now)
        if fence is not None:
            self._assert_current_execution_session(cursor, fence)
        cursor.execute(
            """
            WITH candidates AS (
                SELECT submission.submission_result_id
                FROM operation_submission_result AS submission
                LEFT JOIN operation_record AS operation
                  ON operation.operation_id = submission.operation_id
                WHERE submission.purge_after < %s
                  AND (
                      NOT submission.accepted
                      OR operation.operation_id IS NULL
                      OR (
                          operation.state IN (
                              'completed', 'failed', 'cancelled', 'retryable'
                          )
                          AND operation.purge_after < %s
                      )
                  )
                ORDER BY submission.purge_after, submission.submission_result_id
                FOR UPDATE OF submission SKIP LOCKED
                LIMIT %s
            )
            DELETE FROM operation_submission_result AS submission
            USING candidates
            WHERE submission.submission_result_id = candidates.submission_result_id
            RETURNING submission.submission_result_id
            """,
            (current_time, current_time, limit),
        )
        submissions = len(cursor.fetchall())
        cursor.execute(
            """
            SELECT operation_id
            FROM operation_record
            WHERE state IN ('completed', 'failed', 'cancelled', 'retryable')
              AND purge_after < %s
              AND NOT EXISTS (
                  SELECT 1 FROM operation_submission_result AS submission
                  WHERE submission.accepted
                    AND submission.operation_id = operation_record.operation_id
              )
            ORDER BY purge_after, operation_id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (current_time, limit),
        )
        candidate_ids = [str(row["operation_id"]) for row in cursor.fetchall()]
        if not candidate_ids:
            return PurgeResult(operations=0, submissions=submissions)
        cursor.execute(
            """
            DELETE FROM operation_record AS operation
            WHERE operation.operation_id = ANY(%s::uuid[])
              AND NOT EXISTS (
                  SELECT 1 FROM operation_submission_result AS submission
                  WHERE submission.accepted
                    AND submission.operation_id = operation.operation_id
              )
            RETURNING operation.operation_id
            """,
            (candidate_ids,),
        )
        operations = len(cursor.fetchall())
        return PurgeResult(operations=operations, submissions=submissions)


__all__ = (
    "AcceptedAdmission",
    "AcceptedSubmission",
    "AdmissionClass",
    "AdmissionClassConfig",
    "AdmissionClassStatus",
    "AdmissionResult",
    "ExecutionFence",
    "HarnessProfileIdentity",
    "OperationClaim",
    "OperationHarnessBinding",
    "OperationOwner",
    "OperationRecord",
    "OperationRequest",
    "OperationState",
    "OwnerScope",
    "PurgeResult",
    "RefusedAdmission",
    "SafeOperationProjection",
    "SlotLeaseRenewal",
    "StaleWorkExecutionFenceError",
    "SubmissionResult",
    "WorkAdmissionConfigurationError",
    "WorkAdmissionIntegrityError",
    "WorkAdmissionNotFoundError",
    "WorkAdmissionRepository",
)
