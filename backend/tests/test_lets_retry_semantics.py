"""Receipt-safe retry and uncertain-outcome semantics for governed effects."""

from __future__ import annotations

import pytest

from orchestrator.tool_retry import (
    ActuatorResult,
    EffectResultLostError,
    EffectSemantics,
    PreResponseTransportError,
    ProtectedOutcomeStatus,
    ProtectedRetryError,
    ProtectedToolRetrier,
)


def _retrier(*, physical: int = 3, transport: int = 3) -> ProtectedToolRetrier:
    async def no_sleep(_delay: float) -> None:
        return None

    return ProtectedToolRetrier(
        physical_attempts=physical,
        authorization_transport_attempts=transport,
        backoff_seconds=(0.0,),
        sleep=no_sleep,
    )


async def test_pre_response_retry_reuses_exact_attempt_identity() -> None:
    seen: list[tuple[str, str, int]] = []
    invoked: list[str] = []

    async def authorize(attempt):
        seen.append((attempt.operation_id, attempt.nonce, attempt.ordinal))
        if len(seen) < 3:
            raise PreResponseTransportError()
        return "permit"

    async def invoke(attempt, permit):
        assert permit == "permit"
        invoked.append(attempt.operation_id)
        return ActuatorResult(value="ok")

    outcome = await _retrier().execute(
        semantics=EffectSemantics.NON_IDEMPOTENT,
        authorize=authorize,
        invoke=invoke,
    )

    assert outcome.status is ProtectedOutcomeStatus.SUCCEEDED
    assert outcome.value == "ok"
    assert len(set(seen)) == 1
    assert invoked == [seen[0][0]]


async def test_same_id_conflict_is_terminal_before_effect() -> None:
    invoked = False

    async def authorize(_attempt):
        raise ProtectedRetryError("authorization_request_conflict")

    async def invoke(_attempt, _permit):
        nonlocal invoked
        invoked = True
        return ActuatorResult(value=None)

    with pytest.raises(ProtectedRetryError, match="authorization_request_conflict"):
        await _retrier().execute(
            semantics=EffectSemantics.NON_IDEMPOTENT,
            authorize=authorize,
            invoke=invoke,
        )
    assert invoked is False


async def test_known_retryable_failure_gets_new_operation_nonce_and_permit() -> None:
    authorized: list[tuple[str, str, int]] = []
    invoked: list[tuple[str, str]] = []

    async def authorize(attempt):
        authorized.append((attempt.operation_id, attempt.nonce, attempt.ordinal))
        return f"permit-{attempt.ordinal}"

    async def invoke(attempt, permit):
        invoked.append((attempt.operation_id, permit))
        if attempt.ordinal == 1:
            return ActuatorResult(error_code="known_retryable", retryable=True)
        return ActuatorResult(value="done")

    outcome = await _retrier().execute(
        semantics=EffectSemantics.NON_IDEMPOTENT,
        authorize=authorize,
        invoke=invoke,
    )

    assert outcome.status is ProtectedOutcomeStatus.SUCCEEDED
    assert [item[2] for item in authorized] == [1, 2]
    assert authorized[0][:2] != authorized[1][:2]
    assert invoked == [
        (authorized[0][0], "permit-1"),
        (authorized[1][0], "permit-2"),
    ]


async def test_claimed_receipt_replay_is_not_retried() -> None:
    attempts = 0

    async def authorize(_attempt):
        return "permit"

    async def invoke(_attempt, _permit):
        nonlocal attempts
        attempts += 1
        return ActuatorResult(error_code="receipt_replayed", retryable=False)

    outcome = await _retrier().execute(
        semantics=EffectSemantics.IDEMPOTENT,
        authorize=authorize,
        invoke=invoke,
    )

    assert outcome.status is ProtectedOutcomeStatus.FAILED
    assert outcome.error_code == "receipt_replayed"
    assert attempts == 1


@pytest.mark.parametrize(
    "semantics",
    [EffectSemantics.NON_IDEMPOTENT, EffectSemantics.RECONCILABLE],
)
async def test_lost_result_is_uncertain_and_never_blindly_retried(semantics) -> None:
    authorized: list[str] = []

    async def authorize(attempt):
        authorized.append(attempt.operation_id)
        return "claimed-permit"

    async def invoke(_attempt, _permit):
        raise EffectResultLostError()

    outcome = await _retrier().execute(
        semantics=semantics,
        authorize=authorize,
        invoke=invoke,
    )

    assert outcome.status is ProtectedOutcomeStatus.OUTCOME_UNCERTAIN
    assert outcome.error_code == "effect_result_lost"
    assert authorized == [outcome.attempt.operation_id]


async def test_idempotent_lost_result_recovers_under_fresh_authority() -> None:
    authorized: list[tuple[str, str]] = []

    async def authorize(attempt):
        authorized.append((attempt.operation_id, attempt.nonce))
        return f"permit-{attempt.ordinal}"

    async def invoke(attempt, permit):
        assert permit == f"permit-{attempt.ordinal}"
        if attempt.ordinal == 1:
            raise EffectResultLostError()
        return ActuatorResult(value="recovered")

    outcome = await _retrier().execute(
        semantics=EffectSemantics.IDEMPOTENT,
        authorize=authorize,
        invoke=invoke,
    )

    assert outcome.status is ProtectedOutcomeStatus.SUCCEEDED
    assert outcome.value == "recovered"
    assert len(authorized) == 2
    assert authorized[0] != authorized[1]


async def test_reconcilable_uncertainty_is_returned_for_compensation() -> None:
    compensation: list[str] = []

    async def authorize(_attempt):
        return "permit"

    async def invoke(_attempt, _permit):
        raise EffectResultLostError("remote_commit_unknown")

    outcome = await _retrier(physical=2).execute(
        semantics=EffectSemantics.RECONCILABLE,
        authorize=authorize,
        invoke=invoke,
    )
    if outcome.status is ProtectedOutcomeStatus.OUTCOME_UNCERTAIN:
        compensation.append(outcome.attempt.operation_id)

    assert outcome.error_code == "remote_commit_unknown"
    assert compensation == [outcome.attempt.operation_id]
