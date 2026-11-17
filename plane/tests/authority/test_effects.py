"""Tests for astralplane.authority.effects: protected-effect operation shape,
terminal-status exclusivity, receipt/result digest fencing, and identifier
validation.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from astralplane.authority.effects import (
    AstralToolScope,
    ProtectedEffectOperation,
    ProtectedEffectStatus,
)
from astralplane.errors import DomainValidationError

EFFECT_DIGEST = "1" * 64
RECEIPT_DIGEST = "2" * 64
RESULT_DIGEST = "3" * 64
NONCE = "nonce-0123456789abcdef"
NOW = datetime(2026, 8, 14, 18, tzinfo=UTC)


def _operation(**changes: object) -> ProtectedEffectOperation:
    values: dict[str, object] = {
        "operation_id": "operation-1",
        "owner_id": "owner-1",
        "agent_id": "agent-1",
        "binding_id": "binding-1",
        "tool_id": "search_records",
        "astral_scope": AstralToolScope.READ,
        "lets_capability": "astral.tools.read",
        "lets_transition": "tool_read",
        "executor_audience": "astraldeep.tool-gateway/v1",
        "nonce": NONCE,
        "effect_digest": EFFECT_DIGEST,
        "expected_sequence": 8,
        "audit_correlation_id": "audit-1",
        "status": ProtectedEffectStatus.CREATED,
        "receipt_id": None,
        "receipt_digest": None,
        "effect_result_digest": None,
        "error_code": None,
        "created_at": datetime(
            2026,
            8,
            14,
            14,
            tzinfo=timezone(timedelta(hours=-4)),
        ),
        "updated_at": NOW,
        "version": 0,
    }
    values.update(changes)
    return ProtectedEffectOperation(**values)  # type: ignore[arg-type]


def _receipt_fields() -> dict[str, str]:
    return {
        "receipt_id": "receipt-1",
        "receipt_digest": RECEIPT_DIGEST,
    }


def test_operation_is_immutable_owner_scoped_and_audit_correlated() -> None:
    operation = _operation()

    assert operation.created_at == NOW
    assert operation.owner_operation_key == ("owner-1", "operation-1")
    assert operation.audit_key == ("owner-1", "audit-1", "operation-1")
    with pytest.raises(FrozenInstanceError):
        operation.version = 1  # type: ignore[misc]


def test_fixed_scope_profile_is_exact() -> None:
    assert {scope.value for scope in AstralToolScope} == {
        "tools:read",
        "tools:write",
        "tools:search",
        "tools:system",
        "tools:files",
        "tools:execute",
    }


def test_terminal_statuses_exclude_unresolved_uncertainty() -> None:
    terminals = {
        ProtectedEffectStatus.SUCCEEDED,
        ProtectedEffectStatus.DENIED,
        ProtectedEffectStatus.FAILED_CLOSED,
        ProtectedEffectStatus.EFFECT_FAILED,
    }
    assert {status for status in ProtectedEffectStatus if status.terminal} == terminals
    assert ProtectedEffectStatus.OUTCOME_UNCERTAIN.terminal is False


@pytest.mark.parametrize(
    "field",
    [
        "operation_id",
        "owner_id",
        "agent_id",
        "binding_id",
        "tool_id",
        "lets_capability",
        "lets_transition",
        "executor_audience",
        "audit_correlation_id",
    ],
)
def test_identifiers_fail_closed_without_echoing_values(field: str) -> None:
    invalid = " secret value "
    with pytest.raises(DomainValidationError) as caught:
        _operation(**{field: invalid})
    assert invalid not in str(caught.value)


@pytest.mark.parametrize(
    "nonce",
    [
        "short",
        "n" * 257,
        " nonce-0123456789abcdef",
        "nonce-0123456789abcdef\n",
        17,
    ],
)
def test_nonce_enforces_lets_wire_bounds_without_echoing_value(nonce: object) -> None:
    with pytest.raises(DomainValidationError) as caught:
        _operation(nonce=nonce)
    assert str(nonce) not in str(caught.value)


def test_nonce_wire_boundaries_are_accepted() -> None:
    assert _operation(nonce="n" * 16).nonce == "n" * 16
    assert _operation(nonce="n" * 256).nonce == "n" * 256


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"astral_scope": "tools:read"}, "astral scope"),
        ({"status": "created"}, "status"),
        ({"expected_sequence": -1}, "expected sequence"),
        ({"expected_sequence": False}, "expected sequence"),
        ({"version": -1}, "version"),
        ({"version": True}, "version"),
    ],
)
def test_typed_and_integer_fences_fail_closed(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(DomainValidationError, match=message):
        _operation(**changes)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("effect_digest", "sha256:" + "1" * 64, "effect digest"),
        ("effect_digest", "A" * 64, "effect digest"),
        ("receipt_digest", "bad", "receipt digest"),
        ("effect_result_digest", "bad", "effect result digest"),
    ],
)
def test_plane_internal_digests_are_lowercase_raw_sha256(
    field: str,
    value: str,
    message: str,
) -> None:
    changes: dict[str, object] = {field: value}
    if field == "receipt_digest":
        changes.update(receipt_id="receipt-1")
    if field == "effect_result_digest":
        changes.update(
            status=ProtectedEffectStatus.SUCCEEDED,
            **_receipt_fields(),
        )
    with pytest.raises(DomainValidationError, match=message):
        _operation(**changes)


@pytest.mark.parametrize(
    "status",
    [
        ProtectedEffectStatus.RECEIPT_RECEIVED,
        ProtectedEffectStatus.RECEIPT_CLAIMED,
        ProtectedEffectStatus.EXECUTING,
        ProtectedEffectStatus.SUCCEEDED,
        ProtectedEffectStatus.EFFECT_FAILED,
        ProtectedEffectStatus.OUTCOME_UNCERTAIN,
    ],
)
def test_receipt_bound_states_require_complete_receipt_metadata(
    status: ProtectedEffectStatus,
) -> None:
    changes: dict[str, object] = {"status": status}
    if status in {
        ProtectedEffectStatus.SUCCEEDED,
        ProtectedEffectStatus.EFFECT_FAILED,
    }:
        changes["effect_result_digest"] = RESULT_DIGEST
    if status in {
        ProtectedEffectStatus.EFFECT_FAILED,
        ProtectedEffectStatus.OUTCOME_UNCERTAIN,
    }:
        changes["error_code"] = "effect_error"
    with pytest.raises(DomainValidationError, match="receipt metadata"):
        _operation(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"receipt_id": "receipt-1"},
        {"receipt_digest": RECEIPT_DIGEST},
    ],
)
def test_receipt_id_and_digest_are_atomic(changes: dict[str, object]) -> None:
    with pytest.raises(DomainValidationError, match="recorded together"):
        _operation(**changes)


@pytest.mark.parametrize(
    "status",
    [
        ProtectedEffectStatus.CREATED,
        ProtectedEffectStatus.ASTRAL_AUTHORIZED,
        ProtectedEffectStatus.LETS_PENDING,
    ],
)
def test_pre_receipt_statuses_reject_receipt_metadata(
    status: ProtectedEffectStatus,
) -> None:
    with pytest.raises(DomainValidationError, match="cannot carry receipt"):
        _operation(status=status, **_receipt_fields())


@pytest.mark.parametrize(
    "status",
    [
        ProtectedEffectStatus.DENIED,
        ProtectedEffectStatus.FAILED_CLOSED,
        ProtectedEffectStatus.EFFECT_FAILED,
        ProtectedEffectStatus.OUTCOME_UNCERTAIN,
    ],
)
def test_error_statuses_require_typed_error_code(status: ProtectedEffectStatus) -> None:
    changes: dict[str, object] = {"status": status}
    if status in {
        ProtectedEffectStatus.EFFECT_FAILED,
        ProtectedEffectStatus.OUTCOME_UNCERTAIN,
    }:
        changes.update(_receipt_fields())
    if status is ProtectedEffectStatus.EFFECT_FAILED:
        changes["effect_result_digest"] = RESULT_DIGEST
    with pytest.raises(DomainValidationError, match="requires an error code"):
        _operation(**changes)


def test_non_error_status_rejects_error_code() -> None:
    with pytest.raises(DomainValidationError, match="non-error"):
        _operation(error_code="unexpected_error")


def test_known_outcomes_require_redacted_result_digest() -> None:
    with pytest.raises(DomainValidationError, match="result digest"):
        _operation(
            status=ProtectedEffectStatus.SUCCEEDED,
            **_receipt_fields(),
        )
    with pytest.raises(DomainValidationError, match="result digest"):
        _operation(
            status=ProtectedEffectStatus.EFFECT_FAILED,
            error_code="tool_failed",
            **_receipt_fields(),
        )


def test_unknown_or_unstarted_outcome_rejects_result_digest() -> None:
    with pytest.raises(DomainValidationError, match="without a known"):
        _operation(effect_result_digest=RESULT_DIGEST)
    with pytest.raises(DomainValidationError, match="without a known"):
        _operation(
            status=ProtectedEffectStatus.OUTCOME_UNCERTAIN,
            error_code="result_lost",
            effect_result_digest=RESULT_DIGEST,
            **_receipt_fields(),
        )


def test_success_failure_and_uncertainty_shapes_are_distinct() -> None:
    succeeded = _operation(
        status=ProtectedEffectStatus.SUCCEEDED,
        effect_result_digest=RESULT_DIGEST,
        **_receipt_fields(),
    )
    failed = _operation(
        status=ProtectedEffectStatus.EFFECT_FAILED,
        effect_result_digest=RESULT_DIGEST,
        error_code="tool_failed",
        **_receipt_fields(),
    )
    uncertain = _operation(
        status=ProtectedEffectStatus.OUTCOME_UNCERTAIN,
        error_code="result_lost",
        **_receipt_fields(),
    )

    assert succeeded.error_code is None
    assert failed.effect_result_digest == RESULT_DIGEST
    assert uncertain.effect_result_digest is None
    assert uncertain.status.terminal is False


def test_failed_closed_can_retain_a_received_receipt_for_audit() -> None:
    operation = _operation(
        status=ProtectedEffectStatus.FAILED_CLOSED,
        error_code="receipt_binding_mismatch",
        **_receipt_fields(),
    )
    assert operation.receipt_id == "receipt-1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("receipt_id", "bad value"),
        ("error_code", "bad value"),
    ],
)
def test_optional_identifiers_are_canonical(field: str, value: str) -> None:
    changes: dict[str, object] = {
        "status": ProtectedEffectStatus.FAILED_CLOSED,
        "error_code": "verification_failed",
        **_receipt_fields(),
        field: value,
    }
    with pytest.raises(DomainValidationError):
        _operation(**changes)


def test_timestamps_are_utc_and_monotonic() -> None:
    with pytest.raises(DomainValidationError, match="timezone-aware"):
        _operation(created_at=datetime(2026, 8, 14, 18))
    with pytest.raises(DomainValidationError, match="cannot precede"):
        _operation(updated_at=NOW - timedelta(seconds=1))


def test_replace_preserves_validation_across_effect_boundary() -> None:
    received = replace(
        _operation(),
        status=ProtectedEffectStatus.RECEIPT_RECEIVED,
        receipt_id="receipt-1",
        receipt_digest=RECEIPT_DIGEST,
        version=1,
    )
    assert received.status is ProtectedEffectStatus.RECEIPT_RECEIVED
    assert received.version == 1
