"""Contract tests for scheduler/runner.py's assess_unattended_handler: missing or legacy
handlers are refused, reviewed transaction/downstream boundaries are eligible,
best-effort is never an idempotency boundary, and refusal projections stay safe.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from scheduler import runner as runner_module


_EXPECTED_RED = (
    "EXPECTED RED (T029): scheduler.runner must expose "
    "HandlerIdempotencyBoundary, ScheduledHandlerDeclaration, and "
    "assess_unattended_handler before T024 can pass"
)


def _eligibility_contract() -> tuple[type[Any], type[Any], Callable[..., Any]]:
    names = (
        "HandlerIdempotencyBoundary",
        "ScheduledHandlerDeclaration",
        "assess_unattended_handler",
    )
    missing = [name for name in names if not hasattr(runner_module, name)]
    assert not missing, f"{_EXPECTED_RED}; missing={missing}"
    return (
        runner_module.HandlerIdempotencyBoundary,
        runner_module.ScheduledHandlerDeclaration,
        runner_module.assess_unattended_handler,
    )


def _declaration(
    *,
    supports_unattended: bool,
    boundary: Any,
    effect_kinds: tuple[str, ...] = ("chat_history", "notification"),
) -> Any:
    _, declaration_type, _ = _eligibility_contract()
    return declaration_type(
        supports_unattended=supports_unattended,
        idempotency_boundary=boundary,
        effect_kinds=effect_kinds,
    )


def test_missing_or_legacy_handler_is_refused_before_acceptance_side_effects() -> None:
    boundary_type, _, assess = _eligibility_contract()
    legacy = _declaration(
        supports_unattended=False,
        boundary=boundary_type.ASTRALDEEP_TRANSACTION,
    )
    no_boundary = _declaration(
        supports_unattended=True,
        boundary=None,
    )
    accepted_jobs: list[str] = []
    materialized_occurrences: list[str] = []

    for declaration in (None, legacy, no_boundary):
        decision = assess(declaration)
        if decision.eligible:
            accepted_jobs.append("created")
            materialized_occurrences.append("materialized")

        assert decision.eligible is False
        assert decision.code == "handler_not_idempotent"
        assert decision.retryable is False
        assert not hasattr(decision, "operation_id")

    assert accepted_jobs == []
    assert materialized_occurrences == []


@pytest.mark.parametrize(
    "boundary_name,effect_kinds",
    (
        (
            "ASTRALDEEP_TRANSACTION",
            ("chat_history", "notification", "audit_record"),
        ),
        (
            "DOWNSTREAM_IDEMPOTENCY_KEY",
            ("downstream_request",),
        ),
    ),
)
def test_reviewed_transaction_and_downstream_boundaries_are_eligible(
    boundary_name: str,
    effect_kinds: tuple[str, ...],
) -> None:
    boundary_type, _, assess = _eligibility_contract()
    declaration = _declaration(
        supports_unattended=True,
        boundary=getattr(boundary_type, boundary_name),
        effect_kinds=effect_kinds,
    )

    decision = assess(declaration)

    assert decision.eligible is True
    assert decision.code is None
    assert decision.retryable is False
    assert declaration.idempotency_boundary.value in {
        "astraldeep_transaction",
        "downstream_idempotency_key",
    }
    assert declaration.effect_kinds == effect_kinds


def test_best_effort_is_never_an_idempotency_boundary() -> None:
    _, declaration_type, _ = _eligibility_contract()

    with pytest.raises(ValueError, match="idempotency|boundary|best_effort"):
        declaration_type(
            supports_unattended=True,
            idempotency_boundary="best_effort",
            effect_kinds=("chat_history",),
        )


@pytest.mark.parametrize(
    "effect_kinds",
    (
        ("ChatHistory",),
        ("chat-history",),
        ("chat_history", "chat_history"),
        ("user_123",),
        ("target_chat_456",),
        ("bearer_token",),
        ("",),
    ),
)
def test_effect_kinds_are_unique_reviewed_safe_names(
    effect_kinds: tuple[str, ...],
) -> None:
    boundary_type, declaration_type, _ = _eligibility_contract()

    with pytest.raises(ValueError, match="effect_kind|reviewed|duplicate|safe"):
        declaration_type(
            supports_unattended=True,
            idempotency_boundary=boundary_type.ASTRALDEEP_TRANSACTION,
            effect_kinds=effect_kinds,
        )


def test_eligibility_result_contains_only_a_safe_refusal_projection() -> None:
    _, _, assess = _eligibility_contract()

    decision = assess(None)
    assert dataclasses.is_dataclass(decision)
    projection = dataclasses.asdict(decision)

    assert projection == {
        "eligible": False,
        "code": "handler_not_idempotent",
        "retryable": False,
    }
    assert "skipped_ineligible" not in projection.values()
    assert not {
        "handler",
        "instruction",
        "output",
        "owner_user_id",
        "token",
        "target_chat_id",
    }.intersection(projection)
