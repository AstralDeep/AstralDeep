"""Tests for astralplane.authority.lifecycle: pending/uncertain/reconciled operation
shapes, idempotency-key stability, and terminal-status exactness.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from astralplane.authority.lifecycle import (
    AuthorityLifecycleKind,
    AuthorityLifecycleOperation,
    AuthorityLifecycleStatus,
)
from astralplane.errors import DomainValidationError

REQUEST_DIGEST = "1" * 64
RESULT_DIGEST = "2" * 64
RECONCILIATION_DIGEST = "3" * 64
NOW = datetime(2026, 8, 14, 18, tzinfo=UTC)


def _operation(**changes: object) -> AuthorityLifecycleOperation:
    values: dict[str, object] = {
        "operation_id": "operation-1",
        "owner_id": "owner-1",
        "binding_id": "binding-1",
        "kind": AuthorityLifecycleKind.RENEW,
        "expected_binding_version": 4,
        "expected_lease_sequence": 9,
        "request_fingerprint": REQUEST_DIGEST,
        "status": AuthorityLifecycleStatus.PENDING,
        "remote_request_id": "operation-1",
        "result_digest": None,
        "error_code": None,
        "attempt_count": 0,
        "next_attempt_at": NOW + timedelta(seconds=5),
        "last_attempt_at": None,
        "reconciled_at": None,
        "reconciliation_digest": None,
        "created_at": NOW,
        "updated_at": NOW,
        "version": 0,
    }
    values.update(changes)
    return AuthorityLifecycleOperation(**values)  # type: ignore[arg-type]


def test_pending_operation_has_stable_owner_scoped_idempotency_key() -> None:
    operation = _operation()
    assert operation.idempotency_key == ("owner-1", "operation-1", REQUEST_DIGEST)
    assert operation.status.terminal is False
    with pytest.raises(FrozenInstanceError):
        operation.version = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "status",
    [
        AuthorityLifecycleStatus.SUCCEEDED,
        AuthorityLifecycleStatus.FAILED,
        AuthorityLifecycleStatus.RECONCILED,
    ],
)
def test_declared_terminal_statuses_are_exact(status: AuthorityLifecycleStatus) -> None:
    assert status.terminal is True


def test_succeeded_and_failed_outcome_shapes() -> None:
    succeeded = _operation(
        status=AuthorityLifecycleStatus.SUCCEEDED,
        result_digest=RESULT_DIGEST,
        attempt_count=1,
        next_attempt_at=None,
        last_attempt_at=NOW,
    )
    failed = _operation(
        status=AuthorityLifecycleStatus.FAILED,
        error_code="remote_denied",
        attempt_count=1,
        next_attempt_at=None,
        last_attempt_at=NOW,
    )
    assert succeeded.result_digest == RESULT_DIGEST
    assert failed.error_code == "remote_denied"


def test_uncertain_operation_retains_same_request_for_reconciliation() -> None:
    operation = _operation(
        status=AuthorityLifecycleStatus.UNCERTAIN,
        error_code="response_lost",
        attempt_count=1,
        last_attempt_at=NOW,
    )
    assert operation.remote_request_id == operation.operation_id
    assert operation.status.terminal is False


def test_pending_retry_retains_attempt_history_and_same_fingerprint() -> None:
    retry = _operation(attempt_count=1, last_attempt_at=NOW)
    assert retry.status is AuthorityLifecycleStatus.PENDING
    assert retry.attempt_count == 1
    assert retry.request_fingerprint == REQUEST_DIGEST


def test_reconciled_operation_requires_digest_time_and_outcome() -> None:
    operation = _operation(
        status=AuthorityLifecycleStatus.RECONCILED,
        result_digest=RESULT_DIGEST,
        attempt_count=1,
        next_attempt_at=None,
        last_attempt_at=NOW,
        reconciled_at=NOW + timedelta(seconds=2),
        reconciliation_digest=RECONCILIATION_DIGEST,
        updated_at=NOW + timedelta(seconds=2),
    )
    assert operation.reconciliation_digest == RECONCILIATION_DIGEST
    assert operation.status.terminal is True


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"remote_request_id": "other"}, "remote request id"),
        ({"kind": "renew"}, "kind"),
        ({"status": "pending"}, "status"),
        ({"expected_binding_version": -1}, "expected binding version"),
        ({"expected_lease_sequence": -1}, "expected lease sequence"),
        ({"attempt_count": -1}, "attempt count"),
        ({"version": True}, "version"),
        ({"request_fingerprint": "sha256:bad"}, "request fingerprint"),
        ({"result_digest": "bad"}, "result digest"),
        ({"error_code": "bad value"}, "error code"),
    ],
)
def test_structural_fences_fail_closed(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(DomainValidationError, match=message):
        _operation(**changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"last_attempt_at": NOW}, "unattempted operation"),
        ({"attempt_count": 1}, "requires a last attempt"),
        (
            {
                "status": AuthorityLifecycleStatus.IN_FLIGHT,
                "attempt_count": 0,
            },
            "record an attempt",
        ),
        (
            {
                "status": AuthorityLifecycleStatus.SUCCEEDED,
                "attempt_count": 1,
                "next_attempt_at": None,
                "last_attempt_at": NOW,
            },
            "result digest",
        ),
        (
            {
                "status": AuthorityLifecycleStatus.FAILED,
                "attempt_count": 1,
                "next_attempt_at": None,
                "last_attempt_at": NOW,
            },
            "error code",
        ),
        (
            {
                "status": AuthorityLifecycleStatus.SUCCEEDED,
                "attempt_count": 1,
                "result_digest": RESULT_DIGEST,
                "error_code": "conflict",
                "next_attempt_at": None,
            },
            "mutually exclusive",
        ),
        (
            {"reconciled_at": NOW, "reconciliation_digest": RECONCILIATION_DIGEST},
            "only reconciled",
        ),
        (
            {
                "status": AuthorityLifecycleStatus.RECONCILED,
                "attempt_count": 1,
                "next_attempt_at": None,
                "last_attempt_at": NOW,
            },
            "reconciliation metadata",
        ),
        (
            {
                "status": AuthorityLifecycleStatus.RECONCILED,
                "attempt_count": 1,
                "next_attempt_at": None,
                "last_attempt_at": NOW,
                "reconciled_at": NOW,
                "reconciliation_digest": RECONCILIATION_DIGEST,
            },
            "result or error",
        ),
        (
            {
                "status": AuthorityLifecycleStatus.SUCCEEDED,
                "attempt_count": 1,
                "result_digest": RESULT_DIGEST,
                "last_attempt_at": NOW,
            },
            "cannot schedule",
        ),
    ],
)
def test_status_shapes_fail_closed(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(DomainValidationError, match=message):
        _operation(**changes)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("created_at", datetime(2026, 8, 14, 18)),
        ("updated_at", NOW - timedelta(seconds=1)),
        ("next_attempt_at", NOW - timedelta(seconds=1)),
        ("last_attempt_at", NOW - timedelta(seconds=1)),
        ("reconciled_at", NOW - timedelta(seconds=1)),
    ],
)
def test_timestamps_are_utc_and_monotonic(field: str, value: datetime) -> None:
    with pytest.raises(DomainValidationError):
        _operation(**{field: value})


def test_replace_preserves_validation_and_normalizes_timezones() -> None:
    in_flight = replace(
        _operation(),
        status=AuthorityLifecycleStatus.IN_FLIGHT,
        attempt_count=1,
        last_attempt_at=NOW,
        updated_at=NOW,
    )
    assert in_flight.last_attempt_at == NOW
