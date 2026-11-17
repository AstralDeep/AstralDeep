"""Tests for astralplane.repositories.secrets: owner-scoped ciphertext reads with
redaction, locked selection matching, upsert via native parameters, and scope-fixed
system-namespace deletes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astralplane.repositories import (
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.secrets import EncryptedLLMConfigRepository
from tests.repositories._support import Result, ScriptedTransaction

NOW = datetime(2026, 8, 13, tzinfo=UTC)


def _user_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "user_id": "owner-1",
        "provider": "custom",
        "base_url": "https://models.invalid/v1",
        "model": "model-1",
        "api_key_enc": "opaque-ciphertext",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def _system_row(**overrides: object) -> dict[str, object]:
    row = _user_row(**overrides)
    row.pop("user_id")
    row["updated_by"] = "admin-1"
    return row


def test_user_read_is_owner_scoped_and_ciphertext_is_redacted() -> None:
    transaction = ScriptedTransaction(one=[_user_row()])

    record = EncryptedLLMConfigRepository().get_user(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
    )

    assert record is not None
    assert record.owner_id == "owner-1"
    assert record.api_key_ciphertext == "opaque-ciphertext"
    assert "opaque-ciphertext" not in repr(record)
    assert "WHERE user_id = %s" in transaction.fetch_sql()
    assert transaction.calls[0][2] == ("owner-1",)


def test_user_read_absent_and_keyless_rows_are_explicit() -> None:
    transaction = ScriptedTransaction(one=[None, _user_row(api_key_enc=None)])
    repository = EncryptedLLMConfigRepository()

    assert repository.get_user(transaction, owner_id="owner-1") is None  # type: ignore[arg-type]
    record = repository.get_user(transaction, owner_id="owner-1")  # type: ignore[arg-type]
    assert record is not None and record.api_key_ciphertext is None


def test_locked_user_selection_matches_existing_opaque_record_and_parameters() -> None:
    repository = EncryptedLLMConfigRepository()
    query = ScriptedTransaction(one=[_user_row(), _user_row()])
    ordinary = repository.get_user(query, owner_id="owner-1")
    locked = repository.get_user_for_update(query, owner_id="owner-1")
    assert locked == ordinary and "opaque-ciphertext" not in repr(locked)
    assert query.calls[1][2] == ("owner-1",)
    assert query.calls[1][1].endswith("WHERE user_id = %s FOR UPDATE")


def test_locked_user_selection_returns_none_without_fallback() -> None:
    query = ScriptedTransaction(one=[None])
    assert EncryptedLLMConfigRepository().get_user_for_update(query, owner_id="missing") is None
    assert len(query.calls) == 1


@pytest.mark.parametrize("owner", [None, True, [], "", " ", "x" * 513])
def test_locked_user_selection_refuses_bad_owner_before_sql(owner) -> None:
    query = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        EncryptedLLMConfigRepository().get_user_for_update(query, owner_id=owner)
    assert query.calls == []


def test_locked_user_selection_retains_stored_record_validation() -> None:
    query = ScriptedTransaction(one=[_user_row(updated_at="invalid")])
    with pytest.raises(RepositoryDataError):
        EncryptedLLMConfigRepository().get_user_for_update(query, owner_id="owner-1")


def test_user_upsert_uses_native_parameters_and_returns_detached_record() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_user_row(provider="local"),))]
    )

    record = EncryptedLLMConfigRepository().upsert_user(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        provider="local",
        base_url="http://runtime.invalid/v1",
        model="model-1",
        api_key_ciphertext=None,
    )

    assert record.provider == "local"
    kind, sql, parameters = transaction.calls[0]
    assert kind == "execute"
    assert "ON CONFLICT (user_id)" in sql
    assert "?" not in sql
    assert parameters == (
        "owner-1",
        "local",
        "http://runtime.invalid/v1",
        "model-1",
        None,
    )


def test_user_upsert_before_deadline_is_one_fenced_statement() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_user_row(provider="local"),))]
    )
    repository = EncryptedLLMConfigRepository()
    written = repository.upsert_user_before_deadline(
        transaction,
        owner_id="owner-1",
        provider="local",
        base_url="http://runtime.invalid/v1",
        model="model-1",
        api_key_ciphertext=None,
        deadline_at=NOW,
    )
    assert written is not None and written.provider == "local"
    assert len(transaction.calls) == 1
    assert "SELECT %s" in transaction.calls[0][1]
    assert "WHERE clock_timestamp() < %s" in transaction.calls[0][1]
    assert transaction.calls[0][2][-1] == NOW

    assert (
        repository.upsert_user_before_deadline(
            ScriptedTransaction(execute=[Result(rowcount=0)]),
            owner_id="owner-1",
            provider="local",
            base_url="http://runtime.invalid/v1",
            model="model-1",
            api_key_ciphertext=None,
            deadline_at=NOW,
        )
        is None
    )
    with pytest.raises(RepositoryValidationError):
        repository.upsert_user_before_deadline(
            ScriptedTransaction(),
            owner_id="owner-1",
            provider="local",
            base_url="http://runtime.invalid/v1",
            model="model-1",
            api_key_ciphertext=None,
            deadline_at=NOW.replace(tzinfo=None),
        )


def test_system_namespace_is_fixed_and_attributed() -> None:
    transaction = ScriptedTransaction(
        one=[_system_row()],
        execute=[Result(returned_records=(_system_row(provider="hosted"),))],
    )
    repository = EncryptedLLMConfigRepository()

    loaded = repository.get_system(transaction)  # type: ignore[arg-type]
    updated = repository.upsert_system(
        transaction,  # type: ignore[arg-type]
        updated_by="admin-1",
        provider="hosted",
        base_url="https://models.invalid/v1",
        model="model-2",
        api_key_ciphertext="ciphertext-2",
    )

    assert loaded is not None and loaded.scope == "system" and loaded.owner_id is None
    assert updated.updated_by == "admin-1"
    assert "WHERE id = 1" in transaction.calls[0][1]
    assert transaction.calls[1][2][-1] == "admin-1"  # type: ignore[index]


@pytest.mark.parametrize("system", [False, True])
def test_delete_reports_missing_state(system: bool) -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)])
    repository = EncryptedLLMConfigRepository()

    with pytest.raises(RepositoryNotFoundError):
        if system:
            repository.delete_system(transaction)  # type: ignore[arg-type]
        else:
            repository.delete_user(transaction, owner_id="owner-1")  # type: ignore[arg-type]


def test_deletes_use_exact_scopes() -> None:
    transaction = ScriptedTransaction(execute=[Result(), Result()])
    repository = EncryptedLLMConfigRepository()

    repository.delete_user(transaction, owner_id="owner-1")  # type: ignore[arg-type]
    repository.delete_system(transaction)  # type: ignore[arg-type]

    assert transaction.calls[0][2] == ("owner-1",)
    assert "user_id = %s" in transaction.calls[0][1]
    assert "id = 1" in transaction.calls[1][1]


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        pytest.param("owner_id", "", id="missing-owner"),
        pytest.param("provider", "", id="missing-provider"),
        pytest.param("base_url", "", id="missing-base-url"),
        pytest.param("model", "", id="missing-model"),
        pytest.param(
            "api_key_ciphertext",
            "x" * 65_537,
            id="oversized-ciphertext",
        ),
    ],
)
def test_user_upsert_rejects_invalid_bounded_values(argument: str, value: str) -> None:
    arguments = {
        "owner_id": "owner-1",
        "provider": "custom",
        "base_url": "https://models.invalid/v1",
        "model": "model-1",
        "api_key_ciphertext": "ciphertext",
    }
    arguments[argument] = value

    with pytest.raises(RepositoryValidationError):
        EncryptedLLMConfigRepository().upsert_user(  # type: ignore[arg-type]
            ScriptedTransaction(),  # type: ignore[arg-type]
            **arguments,
        )


@pytest.mark.parametrize(
    "row",
    [
        _user_row(created_at=datetime(2026, 8, 13)),
        _user_row(updated_at="not-a-time"),
        _user_row(api_key_enc=3),
    ],
)
def test_corrupt_persisted_user_rows_fail_closed(row: dict[str, object]) -> None:
    transaction = ScriptedTransaction(one=[row])

    with pytest.raises((RepositoryDataError, RepositoryValidationError)):
        EncryptedLLMConfigRepository().get_user(  # type: ignore[arg-type]
            transaction,
            owner_id="owner-1",
        )


def test_write_requires_exactly_one_returned_record() -> None:
    transaction = ScriptedTransaction(execute=[Result(returned_records=())])

    with pytest.raises(RepositoryDataError, match="exactly one"):
        EncryptedLLMConfigRepository().upsert_system(  # type: ignore[arg-type]
            transaction,
            updated_by="admin-1",
            provider="custom",
            base_url="https://models.invalid/v1",
            model="model-1",
            api_key_ciphertext=None,
        )
