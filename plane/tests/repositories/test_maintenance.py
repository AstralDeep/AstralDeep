"""Tests for astralplane.repositories.maintenance: unit/input membership creation and
replay, skip-locked claim recovery, lease bind/renew/complete CAS, and owner-scoped
reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.maintenance import (
    MaintenanceInputRecord,
    MaintenanceRepository,
    MaintenanceState,
    MaintenanceUnitRecord,
)
from tests.repositories._support import Result, ScriptedTransaction

UNIT = "11111111-1111-4111-8111-111111111111"
UNIT_2 = "55555555-5555-4555-8555-555555555555"
OUTPUT = "22222222-2222-4222-8222-222222222222"
LEASE = "33333333-3333-4333-8333-333333333333"
OPERATION = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
DIGEST = "a" * 64


def _unit_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "unit_id": UNIT,
        "unit_kind": "agent_synthesis",
        "owner_user_id": "owner-1",
        "scope_key": "agent-1",
        "idempotency_key": "stable-key",
        "state": "pending",
        "lease_token": None,
        "claim_generation": 0,
        "claimed_by": None,
        "lease_expires_at": None,
        "attempt_count": 0,
        "max_attempts": 5,
        "operation_id": None,
        "operation_execution_generation": None,
        "output_generation": OUTPUT,
        "output_relative_path": None,
        "output_digest": None,
        "last_error_code": None,
        "state_revision": 0,
        "created_at": NOW,
        "updated_at": NOW,
        "terminal_at": None,
        "next_attempt_at": None,
    }
    row.update(overrides)
    return row


def _input_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "unit_id": UNIT,
        "input_kind": "interaction",
        "input_id": "7",
        "input_digest": DIGEST,
        "state": "pending",
        "operation_id": None,
        "operation_execution_generation": None,
        "completed_at": None,
    }
    row.update(overrides)
    return row


def _unit(**overrides: object) -> MaintenanceUnitRecord:
    values: dict[str, object] = {
        "unit_id": UNIT,
        "unit_kind": "agent_synthesis",
        "owner_id": "owner-1",
        "scope_key": "agent-1",
        "idempotency_key": "stable-key",
        "max_attempts": 5,
        "output_generation": OUTPUT,
    }
    values.update(overrides)
    return MaintenanceUnitRecord(**values)  # type: ignore[arg-type]


def _input(**overrides: object) -> MaintenanceInputRecord:
    values: dict[str, object] = {
        "unit_id": UNIT,
        "input_kind": "interaction",
        "input_id": "7",
        "input_digest": DIGEST,
    }
    values.update(overrides)
    return MaintenanceInputRecord(**values)  # type: ignore[arg-type]


def _claimed_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "state": "claimed",
        "lease_token": LEASE,
        "claim_generation": 1,
        "claimed_by": "worker-1",
        "lease_expires_at": NOW + timedelta(minutes=5),
        "attempt_count": 1,
        "state_revision": 1,
        "updated_at": NOW,
    }
    values.update(overrides)
    return _unit_row(**values)


def _running_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "state": "running",
        "lease_token": LEASE,
        "claim_generation": 1,
        "claimed_by": "worker-1",
        "lease_expires_at": NOW + timedelta(minutes=5),
        "attempt_count": 1,
        "state_revision": 2,
        "operation_id": OPERATION,
        "operation_execution_generation": 3,
        "updated_at": NOW,
    }
    values.update(overrides)
    return _unit_row(**values)


def test_create_unit_persists_membership_before_returning() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_unit_row(),))],
        all_rows=[(_input_row(),)],
    )
    record = MaintenanceRepository().create_unit(
        transaction, _unit(), inputs=(_input(),)  # type: ignore[arg-type]
    )
    assert record.unit_id == UNIT
    assert [call[0] for call in transaction.calls] == ["execute", "execute", "all"]
    assert "ON CONFLICT (unit_kind, idempotency_key) DO NOTHING" in transaction.calls[0][1]


def test_create_unit_replay_reuses_stable_identity_and_rejects_changed_inputs() -> None:
    replay = ScriptedTransaction(
        execute=[Result(rowcount=0)],
        one=[_unit_row()],
        all_rows=[(_input_row(),)],
    )
    assert MaintenanceRepository().create_unit(
        replay, _unit(), inputs=(_input(),)  # type: ignore[arg-type]
    ).unit_id == UNIT

    changed = ScriptedTransaction(
        execute=[Result(rowcount=0)],
        one=[_unit_row()],
        all_rows=[(_input_row(input_digest="b" * 64),)],
    )
    with pytest.raises(RepositoryConflictError, match="membership"):
        MaintenanceRepository().create_unit(
            changed, _unit(), inputs=(_input(),)  # type: ignore[arg-type]
        )


def test_create_unit_rejects_missing_duplicate_or_changed_semantics() -> None:
    repository = MaintenanceRepository()
    with pytest.raises(RepositoryValidationError, match="at least one"):
        repository.create_unit(ScriptedTransaction(), _unit(), inputs=())  # type: ignore[arg-type]
    with pytest.raises(RepositoryValidationError, match="unique"):
        repository.create_unit(
            ScriptedTransaction(), _unit(), inputs=(_input(), _input())  # type: ignore[arg-type]
        )
    changed = ScriptedTransaction(execute=[Result(rowcount=0)], one=[_unit_row(scope_key="other")])
    with pytest.raises(RepositoryConflictError, match="semantics"):
        repository.create_unit(changed, _unit(), inputs=(_input(),))  # type: ignore[arg-type]


def test_owner_administrative_reads_and_input_listing_are_separate() -> None:
    transaction = ScriptedTransaction(
        one=[_unit_row(), _unit_row()],
        all_rows=[(_input_row(),)],
    )
    repository = MaintenanceRepository()
    assert repository.get_for_owner(
        transaction, owner_id="owner-1", unit_id=UNIT  # type: ignore[arg-type]
    )
    assert repository.get_for_administration(transaction, unit_id=UNIT)  # type: ignore[arg-type]
    assert repository.list_inputs_for_administration(transaction, unit_id=UNIT)  # type: ignore[arg-type]
    assert transaction.calls[0][2] == (UNIT, "owner-1")
    assert transaction.calls[1][2] == (UNIT,)


def test_owner_read_rejects_foreign_driver_row_and_pending_query_is_bounded() -> None:
    repository = MaintenanceRepository()
    with pytest.raises(RepositoryDataError, match="another owner's"):
        repository.get_for_owner(
            ScriptedTransaction(one=[_unit_row(owner_user_id="owner-2")]),  # type: ignore[arg-type]
            owner_id="owner-1",
            unit_id=UNIT,
        )
    query = ScriptedTransaction(one=[{"pending": True}])
    assert repository.has_pending_for_administration(
        query, unit_kinds=("agent_synthesis", "agent_capability")  # type: ignore[arg-type]
    )
    assert query.calls[0][2] == (["agent_synthesis", "agent_capability"],)


def test_expired_claim_recovery_is_bounded_locked_and_attempt_aware() -> None:
    retryable = _unit_row(
        state="failed_retryable",
        lease_token=None,
        claimed_by=None,
        lease_expires_at=None,
        attempt_count=1,
        state_revision=3,
        last_error_code="lease_expired",
        next_attempt_at=NOW,
        updated_at=NOW,
    )
    terminal = _unit_row(
        unit_id=UNIT_2,
        state="failed_terminal",
        lease_token=None,
        claimed_by=None,
        lease_expires_at=None,
        attempt_count=5,
        state_revision=7,
        last_error_code="lease_expired",
        terminal_at=NOW,
        updated_at=NOW,
    )
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(terminal, retryable))]
    )

    recovered = MaintenanceRepository().recover_expired_for_administration(
        transaction, observed_at=NOW, limit=2  # type: ignore[arg-type]
    )

    assert tuple(record.unit_id for record in recovered) == (UNIT, UNIT_2)
    assert recovered[0].state is MaintenanceState.FAILED_RETRYABLE
    assert recovered[1].state is MaintenanceState.FAILED_TERMINAL
    assert recovered[1].terminal_at == NOW
    sql = transaction.fetch_sql()
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "attempt_count >= unit.max_attempts" in sql
    assert transaction.calls[0][2] == (NOW, 2, NOW, NOW, NOW)


def test_expired_claim_recovery_validates_time_and_limit() -> None:
    repository = MaintenanceRepository()
    with pytest.raises(RepositoryValidationError, match="timezone"):
        repository.recover_expired_for_administration(
            ScriptedTransaction(), observed_at=NOW.replace(tzinfo=None)  # type: ignore[arg-type]
        )
    with pytest.raises(RepositoryValidationError, match="limit"):
        repository.recover_expired_for_administration(
            ScriptedTransaction(), observed_at=NOW, limit=2001  # type: ignore[arg-type]
        )


def test_claim_next_uses_skip_locked_and_returns_exact_membership() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_claimed_row(),))],
        all_rows=[(_input_row(),)],
    )
    claim = MaintenanceRepository().claim_next_for_administration(
        transaction,  # type: ignore[arg-type]
        worker_id="worker-1",
        now=NOW,
        lease_expires_at=NOW + timedelta(minutes=5),
        unit_kinds=("agent_synthesis",),
        eligible_unit_ids=(UNIT,),
    )
    assert claim is not None and claim.unit.state is MaintenanceState.CLAIMED
    assert claim.unit.claim_generation == 1 and len(claim.inputs) == 1
    assert "FOR UPDATE SKIP LOCKED" in transaction.calls[0][1]


def test_claim_none_and_empty_eligibility_are_explicit() -> None:
    repository = MaintenanceRepository()
    assert repository.claim_next_for_administration(
        ScriptedTransaction(),  # type: ignore[arg-type]
        worker_id="worker",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=1),
        unit_kinds=("agent_synthesis",),
        eligible_unit_ids=(),
    ) is None
    assert repository.claim_next_for_administration(
        ScriptedTransaction(execute=[Result(rowcount=0)]),  # type: ignore[arg-type]
        worker_id="worker",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=1),
        unit_kinds=("agent_synthesis",),
    ) is None


def test_claim_rejects_empty_inputs_from_persisted_unit() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_claimed_row(),))], all_rows=[()]
    )
    with pytest.raises(RepositoryDataError, match="no inputs"):
        MaintenanceRepository().claim_next_for_administration(
            transaction,  # type: ignore[arg-type]
            worker_id="worker",
            now=NOW,
            lease_expires_at=NOW + timedelta(seconds=1),
            unit_kinds=("agent_synthesis",),
        )


def test_bind_and_renew_are_exact_lease_generation_revision_cas() -> None:
    repository = MaintenanceRepository()
    bind = ScriptedTransaction(execute=[Result(returned_records=(_running_row(),))])
    running = repository.bind_operation_for_administration(
        bind,  # type: ignore[arg-type]
        unit_id=UNIT,
        lease_token=LEASE,
        claim_generation=1,
        expected_state_revision=1,
        operation_id=OPERATION,
        operation_execution_generation=3,
        observed_at=NOW,
    )
    assert running.state is MaintenanceState.RUNNING
    assert "state_revision = %s" in bind.calls[0][1]

    renewed_row = _running_row(state_revision=3, lease_expires_at=NOW + timedelta(minutes=6))
    renew = ScriptedTransaction(execute=[Result(returned_records=(renewed_row,))])
    assert repository.renew_claim_for_administration(
        renew,  # type: ignore[arg-type]
        unit_id=UNIT,
        lease_token=LEASE,
        claim_generation=1,
        expected_state_revision=2,
        lease_expires_at=NOW + timedelta(minutes=6),
        observed_at=NOW + timedelta(seconds=1),
    ).state_revision == 3


def test_complete_input_binds_same_operation_and_generation() -> None:
    completed = _input_row(
        state="completed",
        operation_id=OPERATION,
        operation_execution_generation=3,
        completed_at=NOW,
    )
    transaction = ScriptedTransaction(execute=[Result(returned_records=(completed,))])
    record = MaintenanceRepository().complete_input_for_administration(
        transaction,  # type: ignore[arg-type]
        unit_id=UNIT,
        input_kind="interaction",
        input_id="7",
        lease_token=LEASE,
        claim_generation=1,
        operation_id=OPERATION,
        operation_execution_generation=3,
        completed_at=NOW,
    )
    assert record.state == "completed"
    assert "unit.operation_id = %s" in transaction.calls[0][1]


def test_complete_unit_requires_all_inputs_and_output_generation_fence() -> None:
    succeeded = _running_row(
        state="succeeded",
        lease_token=None,
        claimed_by=None,
        lease_expires_at=None,
        output_relative_path="agents/agent-1.md",
        output_digest=DIGEST,
        terminal_at=NOW,
        state_revision=3,
        updated_at=NOW,
    )
    transaction = ScriptedTransaction(execute=[Result(returned_records=(succeeded,))])
    record = MaintenanceRepository().complete_for_administration(
        transaction,  # type: ignore[arg-type]
        unit_id=UNIT,
        lease_token=LEASE,
        claim_generation=1,
        expected_state_revision=2,
        output_generation=OUTPUT,
        output_relative_path="agents/agent-1.md",
        output_digest=DIGEST,
        completed_at=NOW,
    )
    assert record.state is MaintenanceState.SUCCEEDED
    assert "NOT EXISTS" in transaction.calls[0][1]


@pytest.mark.parametrize("attempt_count", [1, 5])
def test_failure_releases_lease_and_returns_retryable_or_terminal(attempt_count: int) -> None:
    terminal = attempt_count == 5
    failed = _unit_row(
        state="failed_terminal" if terminal else "failed_retryable",
        attempt_count=attempt_count,
        claim_generation=1,
        state_revision=3,
        last_error_code="model_unavailable",
        terminal_at=NOW if terminal else None,
        next_attempt_at=None if terminal else NOW + timedelta(minutes=1),
        updated_at=NOW,
    )
    transaction = ScriptedTransaction(execute=[Result(returned_records=(failed,))])
    record = MaintenanceRepository().fail_for_administration(
        transaction,  # type: ignore[arg-type]
        unit_id=UNIT,
        lease_token=LEASE,
        claim_generation=1,
        expected_state_revision=2,
        error_code="model_unavailable",
        observed_at=NOW,
        next_attempt_at=NOW + timedelta(minutes=1),
    )
    assert record.state is (
        MaintenanceState.FAILED_TERMINAL
        if terminal
        else MaintenanceState.FAILED_RETRYABLE
    )


@pytest.mark.parametrize("existing", [_running_row(), None])
def test_claim_update_miss_distinguishes_stale_from_missing(existing: object) -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)], one=[existing])  # type: ignore[list-item]
    error = RepositoryConflictError if existing else RepositoryNotFoundError
    with pytest.raises(error):
        MaintenanceRepository().bind_operation_for_administration(
            transaction,  # type: ignore[arg-type]
            unit_id=UNIT,
            lease_token=LEASE,
            claim_generation=1,
            expected_state_revision=1,
            operation_id=OPERATION,
            operation_execution_generation=3,
            observed_at=NOW,
        )


@pytest.mark.parametrize(
    "unit",
    [
        _unit(unit_id="not-a-uuid"),
        _unit(state="running"),
        _unit(max_attempts=0),
        _unit(output_generation=None),
    ],
)
def test_initial_unit_validation_fails_closed(unit: MaintenanceUnitRecord) -> None:
    with pytest.raises(RepositoryValidationError):
        MaintenanceRepository().create_unit(
            ScriptedTransaction(), unit, inputs=(_input(),)  # type: ignore[arg-type]
        )


def test_claim_and_output_validation_are_bounded() -> None:
    repository = MaintenanceRepository()
    with pytest.raises(RepositoryValidationError, match="expire"):
        repository.claim_next_for_administration(
            ScriptedTransaction(),  # type: ignore[arg-type]
            worker_id="worker",
            now=NOW,
            lease_expires_at=NOW,
            unit_kinds=("agent_synthesis",),
        )
    with pytest.raises(RepositoryValidationError, match="relative"):
        repository.complete_for_administration(
            ScriptedTransaction(),  # type: ignore[arg-type]
            unit_id=UNIT,
            lease_token=LEASE,
            claim_generation=1,
            expected_state_revision=2,
            output_generation=OUTPUT,
            output_relative_path="../escape.md",
            output_digest=DIGEST,
            completed_at=NOW,
        )


def test_invalid_persisted_unit_and_input_are_data_or_validation_errors() -> None:
    repository = MaintenanceRepository()
    with pytest.raises(RepositoryDataError):
        repository.get_for_administration(
            ScriptedTransaction(one=[_unit_row(state="unknown")]),  # type: ignore[arg-type]
            unit_id=UNIT,
        )
    with pytest.raises(RepositoryDataError):
        repository.list_inputs_for_administration(
            ScriptedTransaction(all_rows=[(_input_row(state="completed"),)]),  # type: ignore[arg-type]
            unit_id=UNIT,
        )
