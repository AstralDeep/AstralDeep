"""Retry-safe persistence for maintenance units, their input membership, and claim-lease
fencing. Used by orchestrator/knowledge_synthesis.py to coordinate retryable
background maintenance work.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _non_negative_int,
    _positive_int,
    _required_id,
    _row_value,
    _single_returned,
)

_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class MaintenanceState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MaintenanceInputRecord:
    unit_id: str
    input_kind: str
    input_id: str
    input_digest: str | None
    state: str = "pending"
    operation_id: str | None = None
    operation_execution_generation: int | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MaintenanceUnitRecord:
    unit_id: str
    unit_kind: str
    owner_id: str | None = field(default=None, repr=False)
    scope_key: str = ""
    idempotency_key: str = field(default="", repr=False)
    state: MaintenanceState = MaintenanceState.PENDING
    lease_token: str | None = field(default=None, repr=False)
    claim_generation: int = 0
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    max_attempts: int = 5
    operation_id: str | None = None
    operation_execution_generation: int | None = None
    output_generation: str | None = None
    output_relative_path: str | None = field(default=None, repr=False)
    output_digest: str | None = None
    last_error_code: str | None = None
    state_revision: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    terminal_at: datetime | None = None
    next_attempt_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MaintenanceClaim:
    unit: MaintenanceUnitRecord
    inputs: tuple[MaintenanceInputRecord, ...]


class MaintenanceRepository:
    _UNIT_FIELDS = (
        "unit_id, unit_kind, owner_user_id, scope_key, idempotency_key, state, "
        "lease_token, claim_generation, claimed_by, lease_expires_at, attempt_count, "
        "max_attempts, operation_id, operation_execution_generation, output_generation, "
        "output_relative_path, output_digest, last_error_code, state_revision, created_at, "
        "updated_at, terminal_at, next_attempt_at"
    )
    _INPUT_FIELDS = (
        "unit_id, input_kind, input_id, input_digest, state, operation_id, "
        "operation_execution_generation, completed_at"
    )

    def create_unit(
        self,
        transaction: Transaction,
        unit: MaintenanceUnitRecord,
        *,
        inputs: Sequence[MaintenanceInputRecord],
    ) -> MaintenanceUnitRecord:
        candidate = _validated_unit(unit, initial=True)
        members = tuple(
            _validated_input(item, expected_unit_id=candidate.unit_id)
            for item in inputs
        )
        if not members:
            raise RepositoryValidationError("maintenance unit requires at least one input")
        identities = {(item.input_kind, item.input_id) for item in members}
        if len(identities) != len(members):
            raise RepositoryValidationError("maintenance unit inputs must be unique")
        result = transaction.execute(
            f"""
            INSERT INTO maintenance_unit (
                unit_id, unit_kind, owner_user_id, scope_key, idempotency_key,
                state, claim_generation, attempt_count, max_attempts,
                output_generation, state_revision
            ) VALUES (%s, %s, %s, %s, %s, 'pending', 0, 0, %s, %s, 0)
            ON CONFLICT (unit_kind, idempotency_key) DO NOTHING
            RETURNING {self._UNIT_FIELDS}
            """,
            (
                candidate.unit_id,
                candidate.unit_kind,
                candidate.owner_id,
                candidate.scope_key,
                candidate.idempotency_key,
                candidate.max_attempts,
                candidate.output_generation,
            ),
        )
        inserted = bool(getattr(result, "returned_records", ()))
        stable = (
            _unit(_single_returned(result, "maintenance.create_unit"))
            if inserted
            else self._get_by_idempotency_for_administration(
                transaction,
                unit_kind=candidate.unit_kind,
                idempotency_key=candidate.idempotency_key,
            )
        )
        if stable is None:
            raise RepositoryConflictError("maintenance idempotency identity raced")
        if (
            stable.owner_id != candidate.owner_id
            or stable.scope_key != candidate.scope_key
            or stable.max_attempts != candidate.max_attempts
        ):
            raise RepositoryConflictError("maintenance replay changed immutable semantics")
        for member in members:
            transaction.execute(
                """
                INSERT INTO maintenance_unit_input (
                    unit_id, input_kind, input_id, input_digest, state
                ) VALUES (%s, %s, %s, %s, 'pending')
                ON CONFLICT (unit_id, input_kind, input_id) DO NOTHING
                """,
                (
                    stable.unit_id,
                    member.input_kind,
                    member.input_id,
                    member.input_digest,
                ),
            )
        persisted = self.list_inputs_for_administration(transaction, unit_id=stable.unit_id)
        expected = {
            (member.input_kind, member.input_id, member.input_digest) for member in members
        }
        observed = {
            (member.input_kind, member.input_id, member.input_digest) for member in persisted
        }
        if observed != expected:
            raise RepositoryConflictError("maintenance replay changed input membership")
        return stable

    def get_for_owner(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        unit_id: str,
    ) -> MaintenanceUnitRecord | None:
        owner = _required_id(owner_id, "owner_id")
        unit = _uuid4(unit_id, "unit_id")
        row = query.fetch_one(
            f"SELECT {self._UNIT_FIELDS} FROM maintenance_unit "
            "WHERE unit_id = %s AND owner_user_id = %s",
            (unit, owner),
        )
        return None if row is None else _owned_unit(row, owner)

    def get_for_administration(
        self,
        query: QueryExecutor,
        *,
        unit_id: str,
    ) -> MaintenanceUnitRecord | None:
        unit = _uuid4(unit_id, "unit_id")
        row = query.fetch_one(
            f"SELECT {self._UNIT_FIELDS} FROM maintenance_unit WHERE unit_id = %s",
            (unit,),
        )
        return None if row is None else _unit(row)

    def list_inputs_for_administration(
        self,
        query: QueryExecutor,
        *,
        unit_id: str,
    ) -> tuple[MaintenanceInputRecord, ...]:
        unit = _uuid4(unit_id, "unit_id")
        rows = query.fetch_all(
            f"SELECT {self._INPUT_FIELDS} FROM maintenance_unit_input "
            "WHERE unit_id = %s ORDER BY input_kind, input_id",
            (unit,),
        )
        try:
            return tuple(
                _validated_input(_input(row), expected_unit_id=unit) for row in rows
            )
        except RepositoryValidationError as exc:
            raise RepositoryDataError("persisted maintenance input is invalid") from exc

    def has_pending_for_administration(
        self,
        query: QueryExecutor,
        *,
        unit_kinds: Sequence[str],
    ) -> bool:
        kinds = _codes(unit_kinds, "unit_kinds")
        row = query.fetch_one(
            """
            SELECT EXISTS (
                SELECT 1 FROM maintenance_unit
                 WHERE unit_kind = ANY(%s)
                   AND state IN ('pending','claimed','running','failed_retryable')
            ) AS pending
            """,
            (list(kinds),),
        )
        return bool(row and _row_value(row, "pending"))

    def recover_expired_for_administration(
        self,
        transaction: Transaction,
        *,
        observed_at: datetime,
        limit: int = 1000,
    ) -> tuple[MaintenanceUnitRecord, ...]:
        observed = _aware(observed_at, "observed_at")
        maximum = _bounded_limit(limit, maximum=2000)
        result = transaction.execute(
            f"""
            WITH expired AS (
                SELECT unit_id
                FROM maintenance_unit
                WHERE state IN ('claimed', 'running')
                  AND lease_expires_at <= %s
                ORDER BY lease_expires_at, unit_id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE maintenance_unit AS unit
               SET state = CASE WHEN unit.attempt_count >= unit.max_attempts
                                THEN 'failed_terminal' ELSE 'failed_retryable' END,
                   lease_token = NULL, claimed_by = NULL, lease_expires_at = NULL,
                   last_error_code = 'lease_expired',
                   next_attempt_at = CASE
                       WHEN unit.attempt_count >= unit.max_attempts THEN NULL ELSE %s END,
                   terminal_at = CASE
                       WHEN unit.attempt_count >= unit.max_attempts THEN %s ELSE NULL END,
                   state_revision = unit.state_revision + 1,
                   updated_at = %s
              FROM expired
             WHERE unit.unit_id = expired.unit_id
            RETURNING unit.{self._UNIT_FIELDS.replace(', ', ', unit.')}
            """,
            (observed, maximum, observed, observed, observed),
        )
        records = tuple(_unit(row) for row in getattr(result, "returned_records", ()))
        return tuple(sorted(records, key=lambda record: record.unit_id))

    def claim_next_for_administration(
        self,
        transaction: Transaction,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        unit_kinds: Sequence[str],
        eligible_unit_ids: Sequence[str] | None = None,
    ) -> MaintenanceClaim | None:
        worker = _bounded_text(worker_id, "worker_id", maximum=128)
        observed = _aware(now, "now")
        expiry = _aware(lease_expires_at, "lease_expires_at")
        if expiry <= observed:
            raise RepositoryValidationError("maintenance lease must expire after now")
        kinds = _codes(unit_kinds, "unit_kinds")
        eligible = (
            None
            if eligible_unit_ids is None
            else tuple(_uuid4(value, "eligible_unit_id") for value in eligible_unit_ids)
        )
        if eligible == ():
            return None
        token = str(uuid.uuid4())
        result = transaction.execute(
            f"""
            WITH candidate AS (
                SELECT unit_id FROM maintenance_unit
                 WHERE unit_kind = ANY(%s)
                   AND state IN ('pending','failed_retryable')
                   AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
                   AND (%s::uuid[] IS NULL OR unit_id = ANY(%s::uuid[]))
                 ORDER BY created_at, unit_id
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
            )
            UPDATE maintenance_unit AS unit
               SET state = 'claimed', lease_token = %s, claimed_by = %s,
                   lease_expires_at = %s,
                   claim_generation = unit.claim_generation + 1,
                   attempt_count = unit.attempt_count + 1,
                   state_revision = unit.state_revision + 1,
                   updated_at = %s, next_attempt_at = NULL,
                   last_error_code = NULL
              FROM candidate
             WHERE unit.unit_id = candidate.unit_id
               AND unit.attempt_count < unit.max_attempts
            RETURNING unit.{self._UNIT_FIELDS.replace(', ', ', unit.')}
            """,
            (
                list(kinds),
                observed,
                None if eligible is None else list(eligible),
                None if eligible is None else list(eligible),
                token,
                worker,
                expiry,
                observed,
            ),
        )
        if not getattr(result, "returned_records", ()):
            return None
        unit = _unit(_single_returned(result, "maintenance.claim_next"))
        inputs = self.list_inputs_for_administration(transaction, unit_id=unit.unit_id)
        if not inputs:
            raise RepositoryDataError("claimed maintenance unit has no inputs")
        return MaintenanceClaim(unit=unit, inputs=inputs)

    def bind_operation_for_administration(
        self,
        transaction: Transaction,
        *,
        unit_id: str,
        lease_token: str,
        claim_generation: int,
        expected_state_revision: int,
        operation_id: str,
        operation_execution_generation: int,
        observed_at: datetime,
    ) -> MaintenanceUnitRecord:
        return self._claim_update(
            transaction,
            unit_id=unit_id,
            lease_token=lease_token,
            claim_generation=claim_generation,
            expected_state_revision=expected_state_revision,
            expected_state=MaintenanceState.CLAIMED,
            assignment=(
                "state = 'running', operation_id = %s, "
                "operation_execution_generation = %s, updated_at = %s"
            ),
            assignment_parameters=(
                _uuid(operation_id, "operation_id"),
                _positive_int(
                    operation_execution_generation,
                    "operation_execution_generation",
                ),
                _aware(observed_at, "observed_at"),
            ),
            operation="maintenance.bind_operation",
        )

    def renew_claim_for_administration(
        self,
        transaction: Transaction,
        *,
        unit_id: str,
        lease_token: str,
        claim_generation: int,
        expected_state_revision: int,
        lease_expires_at: datetime,
        observed_at: datetime,
    ) -> MaintenanceUnitRecord:
        expiry = _aware(lease_expires_at, "lease_expires_at")
        observed = _aware(observed_at, "observed_at")
        if expiry <= observed:
            raise RepositoryValidationError("maintenance lease must expire after observation")
        return self._claim_update(
            transaction,
            unit_id=unit_id,
            lease_token=lease_token,
            claim_generation=claim_generation,
            expected_state_revision=expected_state_revision,
            expected_state=None,
            assignment="lease_expires_at = %s, updated_at = %s",
            assignment_parameters=(expiry, observed),
            operation="maintenance.renew_claim",
        )

    def complete_input_for_administration(
        self,
        transaction: Transaction,
        *,
        unit_id: str,
        input_kind: str,
        input_id: str,
        lease_token: str,
        claim_generation: int,
        operation_id: str,
        operation_execution_generation: int,
        completed_at: datetime,
    ) -> MaintenanceInputRecord:
        unit = _uuid4(unit_id, "unit_id")
        kind = _code(input_kind, "input_kind")
        identity = _bounded_text(input_id, "input_id", maximum=256)
        lease = _uuid4(lease_token, "lease_token")
        generation = _positive_int(claim_generation, "claim_generation")
        operation = _uuid(operation_id, "operation_id")
        execution = _positive_int(
            operation_execution_generation, "operation_execution_generation"
        )
        observed = _aware(completed_at, "completed_at")
        result = transaction.execute(
            f"""
            UPDATE maintenance_unit_input AS input
               SET state = 'completed', operation_id = %s,
                   operation_execution_generation = %s, completed_at = %s
              FROM maintenance_unit AS unit
             WHERE input.unit_id = unit.unit_id
               AND input.unit_id = %s AND input.input_kind = %s AND input.input_id = %s
               AND input.state = 'pending' AND unit.state = 'running'
               AND unit.lease_token = %s AND unit.claim_generation = %s
               AND unit.operation_id = %s
               AND unit.operation_execution_generation = %s
            RETURNING input.{self._INPUT_FIELDS.replace(', ', ', input.')}
            """,
            (
                operation,
                execution,
                observed,
                unit,
                kind,
                identity,
                lease,
                generation,
                operation,
                execution,
            ),
        )
        if not getattr(result, "returned_records", ()):
            raise RepositoryConflictError("maintenance input claim fence is stale")
        return _validated_input(
            _input(_single_returned(result, "maintenance.complete_input")),
            expected_unit_id=unit,
        )

    def complete_for_administration(
        self,
        transaction: Transaction,
        *,
        unit_id: str,
        lease_token: str,
        claim_generation: int,
        expected_state_revision: int,
        output_generation: str,
        output_relative_path: str,
        output_digest: str,
        completed_at: datetime,
    ) -> MaintenanceUnitRecord:
        path = _relative_path(output_relative_path)
        digest = _digest(output_digest, "output_digest")
        output = _uuid4(output_generation, "output_generation")
        completed = _aware(completed_at, "completed_at")
        unit = _uuid4(unit_id, "unit_id")
        lease = _uuid4(lease_token, "lease_token")
        generation = _positive_int(claim_generation, "claim_generation")
        revision = _non_negative_revision(expected_state_revision)
        result = transaction.execute(
            f"""
            UPDATE maintenance_unit AS unit
               SET state = 'succeeded', output_relative_path = %s,
                   output_digest = %s, terminal_at = %s, updated_at = %s,
                   lease_token = NULL, claimed_by = NULL, lease_expires_at = NULL,
                   state_revision = unit.state_revision + 1
             WHERE unit.unit_id = %s AND unit.state = 'running'
               AND unit.lease_token = %s AND unit.claim_generation = %s
               AND unit.state_revision = %s AND unit.output_generation = %s
               AND NOT EXISTS (
                   SELECT 1 FROM maintenance_unit_input AS input
                    WHERE input.unit_id = unit.unit_id AND input.state <> 'completed'
               )
            RETURNING unit.{self._UNIT_FIELDS.replace(', ', ', unit.')}
            """,
            (path, digest, completed, completed, unit, lease, generation, revision, output),
        )
        if not getattr(result, "returned_records", ()):
            self._raise_claim_miss(transaction, unit_id=unit)
        return _unit(_single_returned(result, "maintenance.complete"))

    def fail_for_administration(
        self,
        transaction: Transaction,
        *,
        unit_id: str,
        lease_token: str,
        claim_generation: int,
        expected_state_revision: int,
        error_code: str,
        observed_at: datetime,
        next_attempt_at: datetime | None,
    ) -> MaintenanceUnitRecord:
        code = _code(error_code, "error_code")
        observed = _aware(observed_at, "observed_at")
        retry_at = None if next_attempt_at is None else _aware(next_attempt_at, "next_attempt_at")
        if retry_at is not None and retry_at <= observed:
            raise RepositoryValidationError("next_attempt_at must follow observed_at")
        return self._claim_update(
            transaction,
            unit_id=unit_id,
            lease_token=lease_token,
            claim_generation=claim_generation,
            expected_state_revision=expected_state_revision,
            expected_state=None,
            assignment=(
                "state = CASE WHEN attempt_count >= max_attempts THEN 'failed_terminal' "
                "ELSE 'failed_retryable' END, last_error_code = %s, "
                "next_attempt_at = CASE WHEN attempt_count >= max_attempts THEN NULL ELSE %s END, "
                "terminal_at = CASE WHEN attempt_count >= max_attempts THEN %s ELSE NULL END, "
                "lease_token = NULL, claimed_by = NULL, lease_expires_at = NULL, "
                "updated_at = %s"
            ),
            assignment_parameters=(code, retry_at, observed, observed),
            operation="maintenance.fail",
        )

    def _claim_update(
        self,
        transaction: Transaction,
        *,
        unit_id: str,
        lease_token: str,
        claim_generation: int,
        expected_state_revision: int,
        expected_state: MaintenanceState | None,
        assignment: str,
        assignment_parameters: tuple[object, ...],
        operation: str,
    ) -> MaintenanceUnitRecord:
        unit = _uuid4(unit_id, "unit_id")
        lease = _uuid4(lease_token, "lease_token")
        generation = _positive_int(claim_generation, "claim_generation")
        revision = _non_negative_revision(expected_state_revision)
        states = (
            (expected_state.value,)
            if expected_state is not None
            else (MaintenanceState.CLAIMED.value, MaintenanceState.RUNNING.value)
        )
        result = transaction.execute(
            f"""
            UPDATE maintenance_unit
               SET {assignment}, state_revision = state_revision + 1
             WHERE unit_id = %s AND lease_token = %s AND claim_generation = %s
               AND state_revision = %s AND state = ANY(%s)
            RETURNING {self._UNIT_FIELDS}
            """,
            (*assignment_parameters, unit, lease, generation, revision, list(states)),
        )
        if not getattr(result, "returned_records", ()):
            self._raise_claim_miss(transaction, unit_id=unit)
        return _unit(_single_returned(result, operation))

    def _raise_claim_miss(self, query: QueryExecutor, *, unit_id: str) -> None:
        existing = self.get_for_administration(query, unit_id=unit_id)
        if existing is None:
            raise RepositoryNotFoundError("maintenance unit was not found")
        raise RepositoryConflictError("maintenance claim compare-and-set fence is stale")

    def _get_by_idempotency_for_administration(
        self,
        query: QueryExecutor,
        *,
        unit_kind: str,
        idempotency_key: str,
    ) -> MaintenanceUnitRecord | None:
        row = query.fetch_one(
            f"SELECT {self._UNIT_FIELDS} FROM maintenance_unit "
            "WHERE unit_kind = %s AND idempotency_key = %s",
            (unit_kind, idempotency_key),
        )
        return None if row is None else _unit(row)


def _uuid(value: object, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise RepositoryValidationError(f"{field} must be a UUID") from exc


def _uuid4(value: object, field: str) -> str:
    parsed = _uuid(value, field)
    if uuid.UUID(parsed).version != 4:
        raise RepositoryValidationError(f"{field} must be a UUID4")
    return parsed


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RepositoryValidationError(f"{field} must be timezone-aware")
    return value


def _code(value: object, field: str) -> str:
    code = str(value)
    if _CODE.fullmatch(code) is None:
        raise RepositoryValidationError(f"{field} is not a supported code")
    return code


def _codes(values: Sequence[str], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise RepositoryValidationError(f"{field} must be a sequence")
    result = tuple(_code(value, field) for value in values)
    if not result or len(result) > 64 or len(set(result)) != len(result):
        raise RepositoryValidationError(f"{field} must contain 1-64 unique codes")
    return result


def _digest(value: object, field: str) -> str:
    digest = str(value)
    if _DIGEST.fullmatch(digest) is None:
        raise RepositoryValidationError(f"{field} must be a lowercase SHA-256 digest")
    return digest


def _relative_path(value: object) -> str:
    path = PurePosixPath(_bounded_text(value, "output_relative_path", maximum=4096))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RepositoryValidationError("output_relative_path must be a safe relative path")
    return path.as_posix()


def _non_negative_revision(value: object) -> int:
    if isinstance(value, bool):
        raise RepositoryValidationError("expected_state_revision must be non-negative")
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        raise RepositoryValidationError("expected_state_revision must be non-negative") from exc
    if revision < 0:
        raise RepositoryValidationError("expected_state_revision must be non-negative")
    return revision


def _state(value: object) -> MaintenanceState:
    try:
        return MaintenanceState(value)
    except (TypeError, ValueError) as exc:
        raise RepositoryValidationError("maintenance state is unsupported") from exc


def _validated_unit(
    record: MaintenanceUnitRecord,
    *,
    initial: bool = False,
) -> MaintenanceUnitRecord:
    if not isinstance(record, MaintenanceUnitRecord):
        raise RepositoryValidationError("record must be a MaintenanceUnitRecord")
    unit = _uuid4(record.unit_id, "unit_id")
    kind = _code(record.unit_kind, "unit_kind")
    owner = None if record.owner_id is None else _required_id(record.owner_id, "owner_id")
    scope = _bounded_text(record.scope_key, "scope_key", maximum=256)
    idempotency = _bounded_text(record.idempotency_key, "idempotency_key", maximum=256)
    state = _state(record.state)
    max_attempts = _positive_int(record.max_attempts, "max_attempts")
    if max_attempts > 100:
        raise RepositoryValidationError("max_attempts exceeds the supported bound")
    output_generation = (
        None
        if record.output_generation is None
        else _uuid4(record.output_generation, "output_generation")
    )
    if initial and (
        state is not MaintenanceState.PENDING
        or record.claim_generation != 0
        or record.attempt_count != 0
        or record.state_revision != 0
        or record.lease_token is not None
        or output_generation is None
    ):
        raise RepositoryValidationError("initial maintenance unit state is invalid")
    claim_generation = _non_negative_int(record.claim_generation, "claim_generation")
    attempt_count = _non_negative_int(record.attempt_count, "attempt_count")
    state_revision = _non_negative_int(record.state_revision, "state_revision")
    if attempt_count > max_attempts:
        raise RepositoryValidationError("attempt_count cannot exceed max_attempts")
    lease_token = (
        None
        if record.lease_token is None
        else _uuid4(record.lease_token, "lease_token")
    )
    claimed_by = (
        None
        if record.claimed_by is None
        else _bounded_text(record.claimed_by, "claimed_by", maximum=128)
    )
    lease_expires_at = (
        None
        if record.lease_expires_at is None
        else _aware(record.lease_expires_at, "lease_expires_at")
    )
    leased = state in {MaintenanceState.CLAIMED, MaintenanceState.RUNNING}
    lease_fields = (lease_token, claimed_by, lease_expires_at)
    if (leased and not all(value is not None for value in lease_fields)) or (
        not leased and any(value is not None for value in lease_fields)
    ):
        raise RepositoryValidationError("maintenance lease fields are inconsistent")
    operation_id = (
        None
        if record.operation_id is None
        else _uuid(record.operation_id, "operation_id")
    )
    operation_generation = (
        None
        if record.operation_execution_generation is None
        else _positive_int(
            record.operation_execution_generation,
            "operation_execution_generation",
        )
    )
    if (operation_id is None) != (operation_generation is None):
        raise RepositoryValidationError("maintenance operation fence is incomplete")
    path = (
        None
        if record.output_relative_path is None
        else _relative_path(record.output_relative_path)
    )
    digest = (
        None
        if record.output_digest is None
        else _digest(record.output_digest, "output_digest")
    )
    terminal_at = (
        None
        if record.terminal_at is None
        else _aware(record.terminal_at, "terminal_at")
    )
    terminal = state in {
        MaintenanceState.SUCCEEDED,
        MaintenanceState.FAILED_TERMINAL,
        MaintenanceState.CANCELLED,
    }
    if terminal != (terminal_at is not None):
        raise RepositoryValidationError("maintenance terminal timestamp is inconsistent")
    if state is MaintenanceState.SUCCEEDED and (
        output_generation is None or path is None or digest is None
    ):
        raise RepositoryValidationError("successful maintenance output is incomplete")
    created_at = None if record.created_at is None else _aware(record.created_at, "created_at")
    updated_at = None if record.updated_at is None else _aware(record.updated_at, "updated_at")
    next_attempt_at = (
        None
        if record.next_attempt_at is None
        else _aware(record.next_attempt_at, "next_attempt_at")
    )
    return MaintenanceUnitRecord(
        unit_id=unit,
        unit_kind=kind,
        owner_id=owner,
        scope_key=scope,
        idempotency_key=idempotency,
        state=state,
        lease_token=lease_token,
        claim_generation=claim_generation,
        claimed_by=claimed_by,
        lease_expires_at=lease_expires_at,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        operation_id=operation_id,
        operation_execution_generation=operation_generation,
        output_generation=output_generation,
        output_relative_path=path,
        output_digest=digest,
        last_error_code=record.last_error_code,
        state_revision=state_revision,
        created_at=created_at,
        updated_at=updated_at,
        terminal_at=terminal_at,
        next_attempt_at=next_attempt_at,
    )


def _unit(row: Mapping[str, Any]) -> MaintenanceUnitRecord:
    try:
        return _validated_unit(
            MaintenanceUnitRecord(
                unit_id=str(_row_value(row, "unit_id")),
                unit_kind=str(_row_value(row, "unit_kind")),
                owner_id=None if row.get("owner_user_id") is None else str(row["owner_user_id"]),
                scope_key=str(_row_value(row, "scope_key")),
                idempotency_key=str(_row_value(row, "idempotency_key")),
                state=_state(_row_value(row, "state")),
                lease_token=None if row.get("lease_token") is None else str(row["lease_token"]),
                claim_generation=int(_row_value(row, "claim_generation")),
                claimed_by=None if row.get("claimed_by") is None else str(row["claimed_by"]),
                lease_expires_at=row.get("lease_expires_at"),
                attempt_count=int(_row_value(row, "attempt_count")),
                max_attempts=int(_row_value(row, "max_attempts")),
                operation_id=None if row.get("operation_id") is None else str(row["operation_id"]),
                operation_execution_generation=row.get("operation_execution_generation"),
                output_generation=(
                    None
                    if row.get("output_generation") is None
                    else str(row["output_generation"])
                ),
                output_relative_path=row.get("output_relative_path"),
                output_digest=row.get("output_digest"),
                last_error_code=row.get("last_error_code"),
                state_revision=int(_row_value(row, "state_revision")),
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
                terminal_at=row.get("terminal_at"),
                next_attempt_at=row.get("next_attempt_at"),
            )
        )
    except (RepositoryValidationError, TypeError, ValueError) as exc:
        raise RepositoryDataError("persisted maintenance unit is invalid") from exc


def _owned_unit(row: Mapping[str, Any], owner_id: str) -> MaintenanceUnitRecord:
    record = _unit(row)
    if record.owner_id != owner_id:
        raise RepositoryDataError("maintenance query returned another owner's unit")
    return record


def _validated_input(
    record: MaintenanceInputRecord,
    *,
    expected_unit_id: str,
) -> MaintenanceInputRecord:
    if not isinstance(record, MaintenanceInputRecord):
        raise RepositoryValidationError("input must be a MaintenanceInputRecord")
    unit = _uuid4(record.unit_id, "unit_id")
    if unit != expected_unit_id:
        raise RepositoryValidationError("maintenance input belongs to another unit")
    kind = _code(record.input_kind, "input_kind")
    identity = _bounded_text(record.input_id, "input_id", maximum=256)
    digest = None if record.input_digest is None else _digest(record.input_digest, "input_digest")
    if record.state not in {"pending", "completed"}:
        raise RepositoryValidationError("maintenance input state is unsupported")
    complete = record.state == "completed"
    completed_at = (
        None
        if record.completed_at is None
        else _aware(record.completed_at, "completed_at")
    )
    operation_id = (
        None
        if record.operation_id is None
        else _uuid(record.operation_id, "operation_id")
    )
    operation_generation = (
        None
        if record.operation_execution_generation is None
        else _positive_int(
            record.operation_execution_generation,
            "operation_execution_generation",
        )
    )
    if complete != (completed_at is not None):
        raise RepositoryValidationError("maintenance input completion is inconsistent")
    if complete != (operation_id is not None and operation_generation is not None):
        raise RepositoryValidationError("maintenance input operation fence is inconsistent")
    return MaintenanceInputRecord(
        unit_id=unit,
        input_kind=kind,
        input_id=identity,
        input_digest=digest,
        state=record.state,
        operation_id=operation_id,
        operation_execution_generation=operation_generation,
        completed_at=completed_at,
    )


def _input(row: Mapping[str, Any]) -> MaintenanceInputRecord:
    try:
        return MaintenanceInputRecord(
            unit_id=str(_row_value(row, "unit_id")),
            input_kind=str(_row_value(row, "input_kind")),
            input_id=str(_row_value(row, "input_id")),
            input_digest=None if row.get("input_digest") is None else str(row["input_digest"]),
            state=str(_row_value(row, "state")),
            operation_id=None if row.get("operation_id") is None else str(row["operation_id"]),
            operation_execution_generation=row.get("operation_execution_generation"),
            completed_at=row.get("completed_at"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RepositoryDataError("persisted maintenance input is invalid") from exc


__all__ = (
    "MaintenanceClaim",
    "MaintenanceInputRecord",
    "MaintenanceRepository",
    "MaintenanceState",
    "MaintenanceUnitRecord",
)
