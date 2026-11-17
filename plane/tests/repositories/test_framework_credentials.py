"""Scripted tests for astralplane.repositories.framework_credentials: issuance
validation against the closed scope vocabulary, owner-scoped revoke/list, CAS
admission consumption, and unpersisted execution assertion.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.framework_credentials import (
    FRAMEWORK_CREDENTIAL_SCOPES,
    FrameworkCredentialRepository,
)
from astralplane.repositories.history import (
    FrameworkCredentialFence,
    FrameworkCredentialObservation,
)
from tests.repositories._support import ScriptedTransaction

CREDENTIAL_ID = "9ef050be-0d5f-4a82-b3cb-410de6d93168"
_EPOCH_2026 = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": CREDENTIAL_ID,
        "owner_id": "owner-1",
        "name": "test credential",
        "scopes": ["operations.submit", "operations.read"],
        "token_hash": "a" * 64,
        "token_prefix": "afk_test",
        "issuer_kind": "session_incarnation",
        "issuer_reference": str(uuid.uuid4()),
        "max_admissions": 5,
        "consumed_admissions": 0,
        "created_epoch": _EPOCH_2026 - 100,
        "expires_epoch": _EPOCH_2026 + 3_600,
        "revoked_epoch": None,
        "last_used_epoch": None,
    }
    row.update(overrides)
    return row


def _fence(**overrides: object) -> FrameworkCredentialFence:
    values = dict(
        owner_id="owner-1",
        credential_id=CREDENTIAL_ID,
        token_hash="a" * 64,
        scopes=("operations.submit",),
        max_admissions=5,
        consumed_admissions=0,
        created_at=_EPOCH_2026 - 100,
        expires_at=_EPOCH_2026 + 3_600,
        revoked_at=None,
    )
    values.update(overrides)
    return FrameworkCredentialFence(**values)


def _observation(**overrides: object) -> FrameworkCredentialObservation:
    started = overrides.pop("started_at", datetime(2026, 1, 1, tzinfo=UTC))
    valid_until = overrides.pop("valid_until", started + timedelta(seconds=10))
    fence = overrides.pop("credential", _fence())
    return FrameworkCredentialObservation(fence, started, valid_until)


def test_issue_rejects_scopes_outside_the_closed_vocabulary() -> None:
    with pytest.raises(RepositoryValidationError, match="scope"):
        FrameworkCredentialRepository().issue(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            credential_id=CREDENTIAL_ID,
            name="x",
            scopes=("not-a-real-scope",),
            token_hash="a" * 64,
            token_prefix="afk_x",
            issuer_kind="session_incarnation",
            issuer_reference="incarnation-1",
            max_admissions=1,
            ttl_seconds=60,
        )


def test_issue_rejects_an_empty_scope_sequence() -> None:
    with pytest.raises(RepositoryValidationError, match="scopes"):
        FrameworkCredentialRepository().issue(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            credential_id=CREDENTIAL_ID,
            name="x",
            scopes=(),
            token_hash="a" * 64,
            token_prefix="afk_x",
            issuer_kind="session_incarnation",
            issuer_reference="incarnation-1",
            max_admissions=1,
            ttl_seconds=60,
        )


@pytest.mark.parametrize("bad_hash", ["", "not-hex", "a" * 63, "A" * 64, "a" * 65])
def test_issue_rejects_a_non_sha256_token_hash(bad_hash: str) -> None:
    with pytest.raises(RepositoryValidationError, match="token_hash"):
        FrameworkCredentialRepository().issue(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            credential_id=CREDENTIAL_ID,
            name="x",
            scopes=("operations.submit",),
            token_hash=bad_hash,
            token_prefix="afk_x",
            issuer_kind="session_incarnation",
            issuer_reference="incarnation-1",
            max_admissions=1,
            ttl_seconds=60,
        )


def test_issue_rejects_an_unsupported_issuer_kind() -> None:
    with pytest.raises(RepositoryValidationError, match="issuer kind"):
        FrameworkCredentialRepository().issue(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            credential_id=CREDENTIAL_ID,
            name="x",
            scopes=("operations.submit",),
            token_hash="a" * 64,
            token_prefix="afk_x",
            issuer_kind="anonymous",
            issuer_reference="incarnation-1",
            max_admissions=1,
            ttl_seconds=60,
        )


@pytest.mark.parametrize("bad_limit", [0, -1, 10001, True, "5", 5.0])
def test_issue_rejects_an_out_of_bound_or_non_integer_admission_limit(bad_limit: object) -> None:
    with pytest.raises(RepositoryValidationError, match="max_admissions"):
        FrameworkCredentialRepository().issue(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            credential_id=CREDENTIAL_ID,
            name="x",
            scopes=("operations.submit",),
            token_hash="a" * 64,
            token_prefix="afk_x",
            issuer_kind="session_incarnation",
            issuer_reference="incarnation-1",
            max_admissions=bad_limit,
            ttl_seconds=60,
        )


@pytest.mark.parametrize("bad_ttl", [0, -1, 7_776_001, True])
def test_issue_rejects_an_out_of_bound_ttl(bad_ttl: object) -> None:
    with pytest.raises(RepositoryValidationError, match="ttl_seconds"):
        FrameworkCredentialRepository().issue(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            credential_id=CREDENTIAL_ID,
            name="x",
            scopes=("operations.submit",),
            token_hash="a" * 64,
            token_prefix="afk_x",
            issuer_kind="session_incarnation",
            issuer_reference="incarnation-1",
            max_admissions=1,
            ttl_seconds=bad_ttl,
        )


def test_issue_rejects_a_blank_owner_or_credential_id() -> None:
    for kwargs in (
        {"owner_id": "  "},
        {"credential_id": ""},
    ):
        args = dict(
            owner_id="owner-1",
            credential_id=CREDENTIAL_ID,
            name="x",
            scopes=("operations.submit",),
            token_hash="a" * 64,
            token_prefix="afk_x",
            issuer_kind="session_incarnation",
            issuer_reference="incarnation-1",
            max_admissions=1,
            ttl_seconds=60,
        )
        args.update(kwargs)
        with pytest.raises(RepositoryValidationError):
            FrameworkCredentialRepository().issue(ScriptedTransaction(), **args)  # type: ignore[arg-type]


def test_framework_credential_scopes_is_a_small_closed_set() -> None:
    assert {
        "operations.submit",
        "operations.read",
        "operations.control",
        "artifacts.read",
        "agents.read",
    } == FRAMEWORK_CREDENTIAL_SCOPES


def test_revoke_is_owner_scoped_and_idempotent() -> None:
    transaction = ScriptedTransaction(one=[{"acquired": True}, None, _row(revoked_epoch=2_000)])
    record = FrameworkCredentialRepository().revoke(
        transaction,
        owner_id="owner-1",
        credential_id=CREDENTIAL_ID,  # type: ignore[arg-type]
    )
    assert record.revoked_at == 2_000
    assert "UPDATE framework_credential" in transaction.fetch_sql()
    assert "revoked_at IS NULL" in transaction.fetch_sql()


def test_revoke_raises_not_found_for_a_foreign_or_missing_credential() -> None:
    from astralplane.repositories import RepositoryNotFoundError

    transaction = ScriptedTransaction(one=[{"acquired": True}, None, None, None])
    with pytest.raises(RepositoryNotFoundError):
        FrameworkCredentialRepository().revoke(
            transaction,
            owner_id="owner-1",
            credential_id=CREDENTIAL_ID,  # type: ignore[arg-type]
        )


def test_list_for_owner_returns_detached_records_without_the_token_hash() -> None:
    transaction = ScriptedTransaction(all_rows=[(_row(), _row(id=str(uuid.uuid4())))])
    records = FrameworkCredentialRepository().list_for_owner(
        transaction,
        owner_id="owner-1",  # type: ignore[arg-type]
    )
    assert len(records) == 2
    assert all("a" * 64 not in repr(record) for record in records)


def test_consume_admission_returns_the_charged_row() -> None:
    transaction = ScriptedTransaction(one=[{}, None, _row(consumed_admissions=1)])
    record = FrameworkCredentialRepository().consume_admission(
        transaction,
        owner_id="owner-1",
        credential_id=CREDENTIAL_ID,  # type: ignore[arg-type]
    )
    assert record.consumed_admissions == 1
    assert "consumed_admissions + 1" in transaction.fetch_sql()
    assert "consumed_admissions < max_admissions" in transaction.fetch_sql()


def test_consume_admission_refuses_when_the_cas_predicate_misses() -> None:
    transaction = ScriptedTransaction(one=[{}, None, None])
    with pytest.raises(RepositoryConflictError, match="credential_allowance_exhausted"):
        FrameworkCredentialRepository().consume_admission(
            transaction,
            owner_id="owner-1",
            credential_id=CREDENTIAL_ID,  # type: ignore[arg-type]
        )


def test_assert_current_execution_succeeds_for_a_matching_fresh_observation() -> None:
    transaction = ScriptedTransaction(
        one=[{}, None, _row(), {"now": datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)}]
    )
    observation = _observation(
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC),
    )
    state = FrameworkCredentialRepository().assert_current_execution(
        transaction,
        observation=observation,  # type: ignore[arg-type]
    )
    assert state.credential.credential_id == CREDENTIAL_ID
    assert "FOR UPDATE" in transaction.fetch_sql()


def test_assert_current_execution_refuses_a_missing_row() -> None:
    transaction = ScriptedTransaction(
        one=[{}, None, None, {"now": datetime(2026, 1, 1, tzinfo=UTC)}]
    )
    with pytest.raises(RepositoryConflictError, match="credential authority unavailable"):
        FrameworkCredentialRepository().assert_current_execution(
            transaction,
            observation=_observation(),  # type: ignore[arg-type]
        )


def test_assert_current_execution_refuses_a_token_hash_mismatch() -> None:
    transaction = ScriptedTransaction(
        one=[{}, None, _row(token_hash="c" * 64), {"now": datetime(2026, 1, 1, tzinfo=UTC)}]
    )
    with pytest.raises(RepositoryConflictError, match="credential authority unavailable"):
        FrameworkCredentialRepository().assert_current_execution(
            transaction,
            observation=_observation(),  # type: ignore[arg-type]
        )


def test_assert_current_execution_refuses_a_revoked_credential() -> None:
    transaction = ScriptedTransaction(
        one=[{}, None, _row(revoked_epoch=500), {"now": datetime(2026, 1, 1, tzinfo=UTC)}]
    )
    with pytest.raises(RepositoryConflictError, match="credential authority unavailable"):
        FrameworkCredentialRepository().assert_current_execution(
            transaction,
            observation=_observation(),  # type: ignore[arg-type]
        )


def test_assert_current_execution_refuses_an_expired_credential() -> None:
    transaction = ScriptedTransaction(
        one=[
            {},
            None,
            _row(expires_epoch=100),
            {"now": datetime(2026, 1, 1, tzinfo=UTC)},
        ]
    )
    with pytest.raises(RepositoryConflictError, match="credential authority unavailable"):
        FrameworkCredentialRepository().assert_current_execution(
            transaction,
            observation=_observation(),  # type: ignore[arg-type]
        )


def test_assert_current_execution_refuses_an_observation_outside_its_own_freshness_window() -> None:
    transaction = ScriptedTransaction(
        one=[{}, None, _row(), {"now": datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)}]
    )
    observation = _observation(
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC),
    )
    with pytest.raises(RepositoryConflictError, match="credential authority unavailable"):
        FrameworkCredentialRepository().assert_current_execution(
            transaction,
            observation=observation,  # type: ignore[arg-type]
        )


def test_assert_current_execution_rejects_an_untyped_observation() -> None:
    with pytest.raises(RepositoryValidationError):
        FrameworkCredentialRepository().assert_current_execution(
            ScriptedTransaction(),
            observation="not-an-observation",  # type: ignore[arg-type]
        )
