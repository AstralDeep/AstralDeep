"""Redacted LETS health/readiness projection tests."""

from __future__ import annotations

import pytest

from orchestrator.lets_config import LetsConfigLoad, LetsReadiness
from orchestrator.lets_health import (
    LetsDenyReason,
    LetsHealthSnapshot,
    LetsRuntimeObservation,
    project_deny_reason,
    project_lets_health,
)


def _load(
    mode: str,
    *,
    configured: bool = True,
) -> LetsConfigLoad:
    blocked = mode == "enforce" and not configured
    config = object() if configured and mode != "off" else None
    return LetsConfigLoad(
        config=config,  # type: ignore[arg-type]
        readiness=LetsReadiness(
            mode=mode,  # type: ignore[arg-type]
            status=(
                "disabled"
                if mode == "off"
                else "blocked"
                if blocked
                else "degraded"
                if not configured
                else "configured"
            ),
            reason="source-value-that-must-not-escape",
            application_ready=not blocked,
            lets_configured=configured and mode != "off",
            governed_effects_permitted=not blocked,
            diagnostic_only=mode == "shadow",
        ),
    )


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        ("healthy", True),
        ("stale", True),
        ("trust_failed", True),
        ("unknown", False),
    ],
)
def test_runtime_observation_rejects_invalid_combinations(
    status: str,
    retryable: bool,
) -> None:
    with pytest.raises(ValueError):
        LetsRuntimeObservation(  # type: ignore[arg-type]
            status=status,
            observed_at_ns=1,
            retryable=retryable,
        )


@pytest.mark.parametrize("observed_at_ns", [-1, True, 1.5, "1"])
def test_runtime_observation_requires_nonnegative_exact_integer(
    observed_at_ns: object,
) -> None:
    with pytest.raises(ValueError, match="^invalid_observed_at$"):
        LetsRuntimeObservation(
            status="unavailable",
            observed_at_ns=observed_at_ns,  # type: ignore[arg-type]
        )


def test_runtime_observation_requires_boolean_retryable() -> None:
    with pytest.raises(ValueError, match="^invalid_retryable$"):
        LetsRuntimeObservation(
            status="unavailable",
            observed_at_ns=1,
            retryable=1,  # type: ignore[arg-type]
        )


def test_off_mode_is_ready_without_claiming_lets_health() -> None:
    snapshot = project_lets_health(
        _load("off", configured=False),
        LetsRuntimeObservation("unavailable", 99, retryable=True),
    )

    assert snapshot.to_dict() == {
        "mode": "off",
        "component_status": "disabled",
        "application_ready": True,
        "existing_behavior_permitted": True,
        "governed_dispatch_ready": False,
        "shadow_probe_ready": False,
        "diagnostic_only": False,
        "operator_code": "lets_disabled",
        "user_reason": None,
        "retryable": False,
        "observed_at_ns": None,
    }


@pytest.mark.parametrize(
    ("mode", "component_status", "application_ready", "has_user_reason"),
    [
        ("shadow", "degraded", True, False),
        ("enforce", "blocked", False, True),
    ],
)
def test_invalid_active_configuration_has_mode_specific_posture(
    mode: str,
    component_status: str,
    application_ready: bool,
    has_user_reason: bool,
) -> None:
    snapshot = project_lets_health(_load(mode, configured=False))

    assert snapshot.component_status == component_status
    assert snapshot.application_ready is application_ready
    assert snapshot.existing_behavior_permitted is application_ready
    assert snapshot.governed_dispatch_ready is False
    assert snapshot.shadow_probe_ready is False
    assert (snapshot.user_reason is not None) is has_user_reason
    assert "source-value" not in repr(snapshot)


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_healthy_runtime_opens_only_the_mode_appropriate_path(mode: str) -> None:
    snapshot = project_lets_health(
        _load(mode),
        LetsRuntimeObservation("healthy", 123),
    )

    assert snapshot.component_status == "healthy"
    assert snapshot.application_ready is True
    assert snapshot.governed_dispatch_ready is (mode == "enforce")
    assert snapshot.shadow_probe_ready is (mode == "shadow")
    assert snapshot.diagnostic_only is (mode == "shadow")
    assert snapshot.user_reason is None
    assert snapshot.observed_at_ns == 123


@pytest.mark.parametrize(
    ("status", "retryable", "operator_code", "public_code"),
    [
        ("starting", True, "lets_starting", "governance_not_ready"),
        ("unavailable", True, "lets_unavailable", "governance_unavailable"),
        ("stale", False, "lets_state_stale", "authority_expired"),
        ("trust_failed", False, "lets_trust_failed", "governance_trust_failed"),
        (
            "replay_store_unavailable",
            True,
            "lets_replay_store_unavailable",
            "governance_not_ready",
        ),
        (
            "authority_anchor_unavailable",
            False,
            "lets_authority_anchor_unavailable",
            "governance_not_ready",
        ),
    ],
)
def test_enforce_runtime_failure_closes_readiness_and_is_redacted(
    status: str,
    retryable: bool,
    operator_code: str,
    public_code: str,
) -> None:
    snapshot = project_lets_health(
        _load("enforce"),
        LetsRuntimeObservation(  # type: ignore[arg-type]
            status,
            456,
            retryable=retryable,
        ),
    )

    assert snapshot.component_status == "blocked"
    assert snapshot.application_ready is False
    assert snapshot.existing_behavior_permitted is False
    assert snapshot.governed_dispatch_ready is False
    assert snapshot.operator_code == operator_code
    assert snapshot.user_reason is not None
    assert snapshot.user_reason.code == public_code
    assert snapshot.retryable is retryable


def test_shadow_runtime_failure_remains_diagnostic_only() -> None:
    snapshot = project_lets_health(
        _load("shadow"),
        LetsRuntimeObservation("unavailable", 456, retryable=True),
    )

    assert snapshot.component_status == "degraded"
    assert snapshot.application_ready is True
    assert snapshot.existing_behavior_permitted is True
    assert snapshot.governed_dispatch_ready is False
    assert snapshot.shadow_probe_ready is False
    assert snapshot.diagnostic_only is True
    assert snapshot.user_reason is None
    assert snapshot.retryable is True


def test_missing_runtime_observation_is_starting_not_healthy() -> None:
    snapshot = project_lets_health(_load("enforce"))

    assert snapshot.component_status == "blocked"
    assert snapshot.operator_code == "lets_starting"
    assert snapshot.governed_dispatch_ready is False
    assert snapshot.observed_at_ns == 0


@pytest.mark.parametrize(
    ("source", "expected", "retryable"),
    [
        ("permission_denied", "authority_denied", False),
        ("request_conflict", "governance_state_conflict", False),
        ("transport_unavailable", "governance_unavailable", True),
        ("remote_validation_failed", "governance_invalid_response", False),
        ("receipt_expired", "authority_expired", False),
        ("claimed_receipt", "authority_replay_rejected", False),
    ],
)
def test_known_internal_codes_map_to_stable_public_reasons(
    source: str,
    expected: str,
    retryable: bool,
) -> None:
    reason = project_deny_reason(source, retryable=not retryable)

    assert reason.code == expected
    assert reason.retryable is retryable
    assert source not in reason.message


@pytest.mark.parametrize("source", [None, 1, "token=secret-value", object()])
def test_unknown_internal_details_collapse_without_echo(source: object) -> None:
    reason = project_deny_reason(source, retryable=True)
    rendered = repr(reason) + repr(reason.to_dict())

    assert reason.code == "governance_failed"
    assert reason.retryable is True
    assert "secret-value" not in rendered


def test_invalid_config_load_type_fails_closed() -> None:
    with pytest.raises(TypeError, match="^invalid_config_load$"):
        project_lets_health(object())  # type: ignore[arg-type]


def test_invalid_readiness_mode_fails_closed() -> None:
    with pytest.raises(ValueError, match="^invalid_readiness_mode$"):
        project_lets_health(_load("audit"))


def test_public_reason_cannot_be_constructed_with_secret_text() -> None:
    with pytest.raises(ValueError, match="^invalid_public_deny_reason$"):
        LetsDenyReason("governance_failed", "token=secret-value", False)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("mode", "audit", "invalid_health_mode"),
        ("component_status", "unknown", "invalid_component_status"),
        ("operator_code", "secret-value", "invalid_operator_code"),
        ("application_ready", 1, "invalid_health_boolean"),
        ("user_reason", "secret-value", "invalid_user_reason"),
        ("observed_at_ns", -1, "invalid_health_observed_at"),
    ],
)
def test_health_snapshot_direct_construction_fails_closed(
    field: str,
    value: object,
    error: str,
) -> None:
    values: dict[str, object] = {
        "mode": "off",
        "component_status": "disabled",
        "application_ready": True,
        "existing_behavior_permitted": True,
        "governed_dispatch_ready": False,
        "shadow_probe_ready": False,
        "diagnostic_only": False,
        "operator_code": "lets_disabled",
        "user_reason": None,
        "retryable": False,
        "observed_at_ns": None,
    }
    values[field] = value

    with pytest.raises(ValueError, match=f"^{error}$"):
        LetsHealthSnapshot(**values)  # type: ignore[arg-type]
