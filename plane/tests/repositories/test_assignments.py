"""Tests for astralplane.repositories.assignment_models and assignments:
assignment-definition immutability, resource-amount-basis vocabulary, currency-cap
trust rules, and one-shot operation-definition bounds.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from astralplane.repositories import RepositoryValidationError
from astralplane.repositories.assignments import (
    AssignmentDefinition,
    AssignmentRepository,
    AssignmentResourceAmount,
    canonical,
    plain,
)


def definition(**changes):
    return AssignmentDefinition(
        name="Public changes",
        instructions="Summarize relevant release changes",
        source={"reader": "web-research-1.fetch_page", "url": "https://example.org/releases"},
        allowed_tools=("web-research-1.fetch_page",),
        consented_scopes=("tools:read",),
        offline_grant_id="11111111-1111-4111-8111-111111111111",
        limits={
            "cadence_seconds": 60,
            "max_retries": 3,
            "max_concurrent_tasks": 2,
            "max_depth": 4,
            "max_tasks": 32,
            "model_calls": 100,
            "tool_calls": 1000,
            "tokens": 100000,
            "elapsed_ms": 1000000,
            "daily_model_calls": 100,
            "daily_tool_calls": 1000,
            "daily_tokens": 100000,
            "daily_elapsed_ms": 1000000,
        },
        **changes,
    )


def test_definition_is_deeply_immutable():
    value = definition()
    with pytest.raises(TypeError):
        value.source["url"] = "https://other.invalid"
    AssignmentRepository.validate_definition(value)


@pytest.mark.parametrize("limits", [{}, {"cadence_seconds": 1}, {"tokens": True}])
def test_missing_or_invalid_hard_limits_are_refused(limits):
    with pytest.raises(RepositoryValidationError):
        AssignmentRepository.validate_definition(replace(definition(), limits=limits))


def test_usage_only_has_unknown_money_not_zero():
    amount = AssignmentResourceAmount(model_calls=1, tokens=200, elapsed_ms=1000)
    assert amount.spend_micro_units is None
    assert amount.currency is None


def test_resource_amount_basis_is_absent_by_default_and_never_a_duplicate_counter():
    amount = AssignmentResourceAmount(model_calls=1, tokens=200, elapsed_ms=1000)
    assert amount.basis is None
    validated = AssignmentRepository._amount(amount)
    assert "basis" not in validated or validated["basis"] is None


def test_resource_amount_basis_accepts_the_closed_vocabulary_per_dimension():
    amount = AssignmentResourceAmount(
        model_calls=1,
        tokens=200,
        elapsed_ms=1000,
        spend_micro_units=50,
        currency="USD",
        basis={"tokens": "estimated", "spend_micro_units": "uncertain"},
    )
    validated = AssignmentRepository._amount(amount)
    assert dict(validated["basis"]) == {"tokens": "estimated", "spend_micro_units": "uncertain"}


@pytest.mark.parametrize(
    "basis",
    [
        {},
        {"tokens": "guessed"},
        {"not_a_dimension": "observed"},
        "observed",
        ["observed"],
    ],
)
def test_resource_amount_basis_rejects_unknown_dimensions_or_values(basis):
    amount = AssignmentResourceAmount(model_calls=1, tokens=200, elapsed_ms=1000, basis=basis)
    with pytest.raises(RepositoryValidationError):
        AssignmentRepository._amount(amount)


def test_resource_amount_basis_round_trips_through_a_legacy_reconstructed_dict():
    legacy = {
        "model_calls": 1,
        "tool_calls": 0,
        "tokens": 200,
        "elapsed_ms": 1000,
        "spend_micro_units": None,
        "currency": None,
    }
    amount = AssignmentResourceAmount(**legacy)
    assert amount.basis is None
    AssignmentRepository._amount(amount)


def test_currency_cap_without_trusted_quote_coverage_is_refused():
    limits = dict(
        definition().limits, currency="USD", spend_micro_units=1000, daily_spend_micro_units=1000
    )
    with pytest.raises(RepositoryValidationError):
        AssignmentRepository.validate_definition(replace(definition(), limits=limits))


@pytest.mark.parametrize(
    "change",
    [
        {"name": ""},
        {"instructions": "x" * 9000},
        {"source": {}},
        {"source": []},
        {"allowed_tools": ()},
        {"allowed_tools": ("a", "a")},
        {"consented_scopes": ("",)},
        {"offline_grant_id": "bad"},
        {"offline_grant_id": "11111111-1111-5111-8111-111111111111"},
        {"limits": dict(definition().limits, currency="USD")},
        {"limits": dict(definition().limits, max_depth=5)},
        {"limits": dict(definition().limits, max_concurrent_tasks=True)},
    ],
)
def test_definition_rejects_unbounded_invalid_authority(change):
    with pytest.raises(RepositoryValidationError):
        AssignmentRepository.validate_definition(replace(definition(), **change))


@pytest.mark.parametrize(
    "value",
    [float("inf"), object(), {"a": set()}, "x" * 300000],
    ids=("infinity", "object", "set", "oversized"),
)
def test_noncanonical_and_oversized_data_is_refused(value):
    with pytest.raises(RepositoryValidationError):
        canonical(value)


def test_naive_time_is_refused():
    from datetime import datetime

    with pytest.raises(RepositoryValidationError):
        plain(datetime(2026, 1, 1))


def operation_definition(**changes):
    limits = {
        k: v
        for k, v in definition().limits.items()
        if not k.startswith("daily_") and k != "cadence_seconds"
    }
    return replace(
        definition(), source={}, allowed_tools=(), offline_grant_id=None, limits=limits, **changes
    )


def test_one_shot_definition_is_explicit_and_does_not_relax_legacy():
    value = operation_definition()
    AssignmentRepository.validate_operation_definition(value)
    with pytest.raises(RepositoryValidationError):
        AssignmentRepository.validate_definition(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"allowed_tools": ("a", "a")},
        {"source": []},
        {"limits": {"tokens": 1}},
        {"instructions": ""},
    ],
)
def test_one_shot_definition_rejects_unbounded_inputs(changes):
    with pytest.raises(RepositoryValidationError):
        AssignmentRepository.validate_operation_definition(
            replace(operation_definition(), **changes)
        )


def test_operation_authority_is_a_private_reference_not_a_claim_or_token():
    from astralplane.repositories.assignment_models import AssignmentOperationAuthority

    authority = AssignmentOperationAuthority(
        owner_id="private-owner",
        origin="interactive",
        reference_kind="session",
        reference_id="private-session-handle",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert "private-owner" not in repr(authority)
    assert "private-session-handle" not in repr(authority)
    with pytest.raises(TypeError):
        AssignmentOperationAuthority(**dict(plain(authority), roles=["admin"]))


@pytest.mark.parametrize(
    "key,value", [("unbounded", True), ("max_retries", 4), ("cadence_seconds", 60)]
)
def test_one_shot_limits_are_closed_and_finite(key, value):
    base = operation_definition()
    with pytest.raises(RepositoryValidationError):
        AssignmentRepository.validate_operation_definition(
            replace(base, limits=dict(base.limits, **{key: value}))
        )
