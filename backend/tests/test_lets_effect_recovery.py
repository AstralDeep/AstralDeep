"""Owner-scoped recovery of stale LETS protected-effect checkpoints."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from astralplane.authority import (
    AstralToolScope,
    ProtectedEffectOperation,
    ProtectedEffectStatus,
)

from orchestrator.lets_lifecycle import LetsLifecycleError
from orchestrator.lets_reconciler import (
    EffectRecoveryResolution,
    LetsEffectReconciler,
)


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


class MemoryPlane:
    @contextmanager
    def transaction(self, **_options: object) -> Iterator[object]:
        yield object()


class MemoryRepository:
    def __init__(self, operations: list[ProtectedEffectOperation]) -> None:
        self.operations = {
            operation.owner_operation_key: operation for operation in operations
        }

    def list_recoverable_protected_effects(
        self,
        _transaction: object,
        *,
        owner_id: str,
        updated_before: datetime,
        limit: int,
    ) -> tuple[ProtectedEffectOperation, ...]:
        return tuple(
            sorted(
                (
                    operation
                    for operation in self.operations.values()
                    if operation.owner_id == owner_id
                    and operation.updated_at < updated_before
                    and not operation.status.terminal
                ),
                key=lambda operation: (operation.updated_at, operation.operation_id),
            )[:limit]
        )

    def transition_protected_effect(
        self,
        _transaction: object,
        replacement: ProtectedEffectOperation,
        *,
        expected_status: ProtectedEffectStatus,
        expected_version: int,
    ) -> ProtectedEffectOperation:
        current = self.operations[replacement.owner_operation_key]
        assert current.status is expected_status
        assert current.version == expected_version
        assert replacement.version == expected_version + 1
        self.operations[replacement.owner_operation_key] = replacement
        return replacement


def _effect(
    operation_id: str,
    status: ProtectedEffectStatus,
    *,
    owner_id: str = "owner-a",
) -> ProtectedEffectOperation:
    has_receipt = status in {
        ProtectedEffectStatus.RECEIPT_RECEIVED,
        ProtectedEffectStatus.RECEIPT_CLAIMED,
        ProtectedEffectStatus.EXECUTING,
        ProtectedEffectStatus.OUTCOME_UNCERTAIN,
    }
    return ProtectedEffectOperation(
        operation_id=operation_id,
        owner_id=owner_id,
        agent_id="agent-a",
        binding_id="binding-a",
        tool_id="clinical.search",
        astral_scope=AstralToolScope.READ,
        lets_capability="astral.tools.read",
        lets_transition="tool_read",
        executor_audience="executor-a",
        nonce=(operation_id[0] or "a") * 16,
        effect_digest="1" * 64,
        expected_sequence=0,
        audit_correlation_id=f"audit-{operation_id}",
        status=status,
        receipt_id=(f"receipt-{operation_id}" if has_receipt else None),
        receipt_digest=("2" * 64 if has_receipt else None),
        effect_result_digest=None,
        error_code=(
            "effect_result_lost"
            if status is ProtectedEffectStatus.OUTCOME_UNCERTAIN
            else None
        ),
        created_at=NOW - timedelta(minutes=5),
        updated_at=NOW - timedelta(minutes=4),
        version=3,
    )


def _reconciler(
    repository: MemoryRepository,
    resolver=None,
) -> LetsEffectReconciler:
    return LetsEffectReconciler(
        plane=MemoryPlane(),
        repository=repository,  # type: ignore[arg-type]
        resolver=resolver,
    )


@pytest.mark.parametrize(
    "status",
    [
        ProtectedEffectStatus.CREATED,
        ProtectedEffectStatus.ASTRAL_AUTHORIZED,
        ProtectedEffectStatus.LETS_PENDING,
        ProtectedEffectStatus.RECEIPT_RECEIVED,
        ProtectedEffectStatus.RECEIPT_CLAIMED,
    ],
)
def test_stale_pre_execution_checkpoint_fails_closed(status) -> None:
    repository = MemoryRepository([_effect("operation-a", status)])
    batch = _reconciler(repository).recover_owner(
        "owner-a",
        updated_before=NOW,
    )

    recovered = repository.operations[("owner-a", "operation-a")]
    assert recovered.status is ProtectedEffectStatus.FAILED_CLOSED
    assert recovered.error_code == f"recovery_abandoned_{status.value}"
    assert batch.selected == batch.transitioned == batch.failed_closed == 1
    assert batch.uncertain == batch.resolved == batch.deferred == 0


def test_stale_executing_effect_becomes_uncertain_without_false_failure() -> None:
    repository = MemoryRepository(
        [_effect("operation-a", ProtectedEffectStatus.EXECUTING)]
    )
    batch = _reconciler(repository).recover_owner(
        "owner-a",
        updated_before=NOW,
    )

    recovered = repository.operations[("owner-a", "operation-a")]
    assert recovered.status is ProtectedEffectStatus.OUTCOME_UNCERTAIN
    assert recovered.error_code == "effect_result_unavailable"
    assert recovered.effect_result_digest is None
    assert batch.uncertain == 1


@pytest.mark.parametrize(
    ("initial", "resolution", "expected"),
    [
        (
            ProtectedEffectStatus.EXECUTING,
            EffectRecoveryResolution("succeeded"),
            ProtectedEffectStatus.SUCCEEDED,
        ),
        (
            ProtectedEffectStatus.OUTCOME_UNCERTAIN,
            EffectRecoveryResolution("effect_failed", "compensated"),
            ProtectedEffectStatus.EFFECT_FAILED,
        ),
    ],
)
def test_domain_resolver_can_prove_a_known_terminal_outcome(
    initial,
    resolution,
    expected,
) -> None:
    repository = MemoryRepository([_effect("operation-a", initial)])
    batch = _reconciler(repository, lambda _operation: resolution).recover_owner(
        "owner-a",
        updated_before=NOW,
    )

    recovered = repository.operations[("owner-a", "operation-a")]
    assert recovered.status is expected
    assert recovered.effect_result_digest is not None
    assert recovered.error_code == resolution.error_code
    assert batch.resolved == batch.transitioned == 1


def test_existing_uncertainty_is_deferred_without_domain_evidence() -> None:
    original = _effect("operation-a", ProtectedEffectStatus.OUTCOME_UNCERTAIN)
    repository = MemoryRepository([original])
    batch = _reconciler(repository).recover_owner(
        "owner-a",
        updated_before=NOW,
    )

    assert repository.operations[original.owner_operation_key] == original
    assert batch.selected == batch.deferred == 1
    assert batch.transitioned == 0
    assert batch.error_codes == ("effect_resolution_deferred",)


def test_recovery_rejects_naive_cutoff_and_nonpositive_staleness() -> None:
    repository = MemoryRepository([])
    reconciler = _reconciler(repository)
    with pytest.raises(LetsLifecycleError, match="invalid_effect_recovery_cutoff"):
        reconciler.recover_owner(
            "owner-a",
            updated_before=datetime(2026, 8, 14, 12, 0),
        )
    with pytest.raises(LetsLifecycleError, match="invalid_effect_recovery_staleness"):
        reconciler.recover_owner("owner-a", stale_after=timedelta(0))
