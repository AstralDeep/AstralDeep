"""Tests for astralplane.repositories.credentials: user and machine ciphertext CRUD stay
owner-scoped and redacted, with CAS-fenced rotation and a bounded re-encryption
inventory.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
)
from astralplane.repositories.credentials import CredentialRepository
from tests.repositories._support import Result, ScriptedTransaction


def _credential_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 7,
        "user_id": "owner-1",
        "agent_id": "agent-1",
        "credential_key": "api_key",
        "encrypted_value": "ciphertext-1",
        "created_at": 100,
        "updated_at": 200,
    }
    row.update(overrides)
    return row


def _machine_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "machine_id": "machine-1",
        "owner_user_id": "owner-1",
        "cred_type": "ssh_key",
        "encrypted_secret": "secret-ciphertext",
        "encrypted_passphrase": "passphrase-ciphertext",
        "created_at": 100,
        "updated_at": 100,
    }
    row.update(overrides)
    return row


def test_user_upsert_preserves_tuple_identity_and_redacts_ciphertext() -> None:
    transaction = ScriptedTransaction(one=[_credential_row(updated_at=300)])

    record = CredentialRepository().upsert_credential(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        agent_id="agent-1",
        credential_key="api_key",
        encrypted_value="ciphertext-1",
        updated_at=300,
    )

    assert record.updated_at == 300
    assert record.encrypted_value == "ciphertext-1"
    assert "ciphertext-1" not in repr(record)
    assert "ON CONFLICT (user_id, agent_id, credential_key)" in transaction.fetch_sql()
    assert transaction.calls[0][2] == (
        "owner-1",
        "agent-1",
        "api_key",
        "ciphertext-1",
        300,
        300,
    )


def test_user_reads_lists_and_key_inventory_remain_owner_scoped() -> None:
    transaction = ScriptedTransaction(
        one=[_credential_row()],
        all_rows=[
            (_credential_row(), _credential_row(id=8, credential_key="token")),
            ({"credential_key": "api_key"}, {"credential_key": "token"}),
        ],
    )
    repository = CredentialRepository()

    loaded = repository.get_credential(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        agent_id="agent-1",
        credential_key="api_key",
    )
    listed = repository.list_credentials(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        agent_id="agent-1",
        limit=2,
    )
    keys = repository.list_credential_keys(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        agent_id="agent-1",
        limit=2,
    )

    assert loaded is not None and loaded.credential_id == 7
    assert [record.credential_key for record in listed] == ["api_key", "token"]
    assert keys == ("api_key", "token")
    assert transaction.calls[0][2] == ("owner-1", "agent-1", "api_key")
    assert transaction.calls[1][2] == ("owner-1", "agent-1", 2)
    assert transaction.calls[2][2] == ("owner-1", "agent-1", 2)


def test_absent_user_credential_is_explicit() -> None:
    transaction = ScriptedTransaction(one=[None])

    assert (
        CredentialRepository().get_credential(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            agent_id="agent-1",
            credential_key="api_key",
        )
        is None
    )


def test_global_reencryption_inventory_is_named_bounded_and_cursor_paged() -> None:
    transaction = ScriptedTransaction(all_rows=[(_credential_row(),)])

    rows = CredentialRepository().list_agent_credentials_for_reencryption(
        transaction,  # type: ignore[arg-type]
        agent_id="agent-1",
        after_credential_id=6,
        limit=10,
    )

    assert rows[0].owner_id == "owner-1"
    assert "id > %s" in transaction.fetch_sql()
    assert transaction.calls[0][2] == ("agent-1", 6, 10)


@pytest.mark.parametrize("existing", [None, {"id": 7}])
def test_user_ciphertext_cas_distinguishes_but_rejects_all_misses(
    existing: dict[str, int] | None,
) -> None:
    transaction = ScriptedTransaction(one=[None, existing])

    with pytest.raises(RepositoryConflictError):
        CredentialRepository().compare_and_set_ciphertext(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            agent_id="agent-1",
            credential_key="api_key",
            expected_updated_at=100,
            encrypted_value="ciphertext-2",
            updated_at=200,
        )

    assert "user_id = %s" in transaction.fetch_sql()
    assert transaction.calls[0][2][2:5] == ("owner-1", "agent-1", "api_key")  # type: ignore[index]
    assert transaction.calls[1][2] == ("owner-1", "agent-1", "api_key")


def test_user_ciphertext_cas_supports_legacy_null_revision() -> None:
    transaction = ScriptedTransaction(one=[_credential_row(updated_at=300)])

    record = CredentialRepository().compare_and_set_ciphertext(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        agent_id="agent-1",
        credential_key="api_key",
        expected_updated_at=None,
        encrypted_value="ciphertext-2",
        updated_at=300,
    )

    assert record.updated_at == 300
    assert "IS NOT DISTINCT FROM" in transaction.fetch_sql()
    assert transaction.calls[0][2][-1] is None  # type: ignore[index]


def test_user_ciphertext_cas_requires_revision_advance() -> None:
    with pytest.raises(RepositoryConflictError, match="advance"):
        CredentialRepository().compare_and_set_ciphertext(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            agent_id="agent-1",
            credential_key="api_key",
            expected_updated_at=200,
            encrypted_value="ciphertext-2",
            updated_at=200,
        )


def test_user_deletes_are_idempotent_and_owner_scoped() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(rowcount=1), Result(rowcount=0), Result(rowcount=3)]
    )
    repository = CredentialRepository()

    assert repository.delete_credential(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        agent_id="agent-1",
        credential_key="api_key",
    )
    assert not repository.delete_credential(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        agent_id="agent-1",
        credential_key="missing",
    )
    assert (
        repository.delete_agent_credentials(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            agent_id="agent-1",
        )
        == 3
    )
    assert all("user_id = %s" in call[1] for call in transaction.calls)


def test_machine_create_checks_remote_owner_and_accepts_exact_replay() -> None:
    repository = CredentialRepository()
    created_transaction = ScriptedTransaction(one=[_machine_row()])
    replay_transaction = ScriptedTransaction(one=[None, _machine_row()])

    created = repository.create_machine_credential(
        created_transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        machine_id="machine-1",
        credential_type="ssh_key",
        encrypted_secret="secret-ciphertext",
        encrypted_passphrase="passphrase-ciphertext",
        created_at=100,
    )
    replayed = repository.create_machine_credential(
        replay_transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        machine_id="machine-1",
        credential_type="ssh_key",
        encrypted_secret="secret-ciphertext",
        encrypted_passphrase="passphrase-ciphertext",
        created_at=100,
    )

    assert created == replayed
    assert "FROM remote_machine" in created_transaction.fetch_sql()
    assert created_transaction.calls[0][2][-2:] == ("machine-1", "owner-1")  # type: ignore[index]
    assert "secret-ciphertext" not in repr(created)


@pytest.mark.parametrize(
    "rows",
    [
        [None, None],
        [None, _machine_row(encrypted_secret="different")],
    ],
)
def test_machine_create_rejects_unknown_owner_or_changed_replay(
    rows: list[dict[str, object] | None],
) -> None:
    transaction = ScriptedTransaction(one=rows)

    with pytest.raises(RepositoryConflictError):
        CredentialRepository().create_machine_credential(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            machine_id="machine-1",
            credential_type="ssh_key",
            encrypted_secret="secret-ciphertext",
            encrypted_passphrase="passphrase-ciphertext",
            created_at=100,
        )


def test_machine_cas_and_owner_scoped_crud() -> None:
    transaction = ScriptedTransaction(
        one=[
            _machine_row(cred_type="password", encrypted_passphrase=None, updated_at=200),
            _machine_row(cred_type="password", encrypted_passphrase=None, updated_at=200),
        ],
        execute=[Result(rowcount=1), Result(rowcount=2)],
    )
    repository = CredentialRepository()

    updated = repository.compare_and_set_machine_credential(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        machine_id="machine-1",
        expected_updated_at=100,
        credential_type="password",
        encrypted_secret="secret-ciphertext",
        encrypted_passphrase=None,
        updated_at=200,
    )
    loaded = repository.get_machine_credential(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        machine_id="machine-1",
    )
    removed = repository.delete_machine_credential(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        machine_id="machine-1",
    )
    removed_all = repository.delete_owner_machine_credentials(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
    )

    assert updated.updated_at == 200
    assert loaded is not None
    assert removed and removed_all == 2
    assert all("owner_user_id = %s" in sql for _, sql, _ in transaction.calls)


def test_machine_cas_rejects_stale_or_nonadvancing_revision() -> None:
    repository = CredentialRepository()
    with pytest.raises(RepositoryConflictError, match="advance"):
        repository.compare_and_set_machine_credential(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            machine_id="machine-1",
            expected_updated_at=100,
            credential_type="password",
            encrypted_secret="secret-ciphertext",
            encrypted_passphrase=None,
            updated_at=100,
        )

    with pytest.raises(RepositoryConflictError, match="stale"):
        repository.compare_and_set_machine_credential(
            ScriptedTransaction(one=[None]),  # type: ignore[arg-type]
            owner_id="owner-1",
            machine_id="machine-1",
            expected_updated_at=100,
            credential_type="password",
            encrypted_secret="secret-ciphertext",
            encrypted_passphrase=None,
            updated_at=200,
        )


class _TruncatingFence:
    def __int__(self) -> int:
        return int(1.9)


@pytest.mark.parametrize(
    "fence",
    [1.9, float("inf"), float("nan"), Decimal("1.9"), True, _TruncatingFence(), "1.9", -1],
)
def test_cas_fences_reject_malformed_revisions_before_sql(fence: object) -> None:
    repository = CredentialRepository()

    with pytest.raises(RepositoryValidationError, match="expected_updated_at"):
        repository.compare_and_set_machine_credential(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            machine_id="machine-1",
            expected_updated_at=fence,
            credential_type="password",
            encrypted_secret="secret-ciphertext",
            encrypted_passphrase=None,
            updated_at=200,
        )
    with pytest.raises(RepositoryValidationError, match="expected_updated_at"):
        repository.compare_and_set_ciphertext(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            agent_id="agent-1",
            credential_key="api_key",
            expected_updated_at=fence,
            encrypted_value="ciphertext-2",
            updated_at=200,
        )


def test_machine_cas_keeps_valid_stale_and_owner_misses_typed_and_owner_scoped() -> None:
    repository = CredentialRepository()

    advanced = repository.compare_and_set_machine_credential(
        ScriptedTransaction(one=[_machine_row(cred_type="password", updated_at=200)]),  # type: ignore[arg-type]
        owner_id="owner-1",
        machine_id="machine-1",
        expected_updated_at=100,
        credential_type="password",
        encrypted_secret="secret-ciphertext",
        encrypted_passphrase=None,
        updated_at=200,
    )
    assert advanced.updated_at == 200

    with pytest.raises(RepositoryConflictError, match="stale"):
        repository.compare_and_set_machine_credential(
            ScriptedTransaction(one=[None]),  # type: ignore[arg-type]
            owner_id="owner-1",
            machine_id="machine-1",
            expected_updated_at=50,
            credential_type="password",
            encrypted_secret="secret-ciphertext",
            encrypted_passphrase=None,
            updated_at=200,
        )

    with pytest.raises(RepositoryConflictError, match="stale"):
        repository.compare_and_set_machine_credential(
            ScriptedTransaction(one=[None]),  # type: ignore[arg-type]
            owner_id="other-owner",
            machine_id="machine-1",
            expected_updated_at=100,
            credential_type="password",
            encrypted_secret="secret-ciphertext",
            encrypted_passphrase=None,
            updated_at=200,
        )


def test_user_cas_owner_mismatch_conflicts_without_disclosing_the_missing_row() -> None:
    with pytest.raises(RepositoryConflictError, match="does not exist"):
        CredentialRepository().compare_and_set_ciphertext(
            ScriptedTransaction(one=[None, None]),  # type: ignore[arg-type]
            owner_id="other-owner",
            agent_id="agent-1",
            credential_key="api_key",
            expected_updated_at=100,
            encrypted_value="ciphertext-2",
            updated_at=200,
        )


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("owner_id", ""),
        ("agent_id", ""),
        ("credential_key", ""),
        ("encrypted_value", ""),
        ("updated_at", -1),
    ],
)
def test_user_input_is_bounded(argument: str, value: object) -> None:
    arguments: dict[str, object] = {
        "owner_id": "owner-1",
        "agent_id": "agent-1",
        "credential_key": "api_key",
        "encrypted_value": "ciphertext",
        "updated_at": 100,
    }
    arguments[argument] = value

    with pytest.raises(RepositoryValidationError):
        CredentialRepository().upsert_credential(  # type: ignore[arg-type]
            ScriptedTransaction(),  # type: ignore[arg-type]
            **arguments,
        )


@pytest.mark.parametrize("credential_type", ["token", "", "x" * 33])
def test_machine_credential_type_is_rejected(credential_type: str) -> None:
    with pytest.raises((RepositoryValidationError, RepositoryConflictError)):
        CredentialRepository().create_machine_credential(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            machine_id="machine-1",
            credential_type=credential_type,
            encrypted_secret="ciphertext",
            encrypted_passphrase=None,
            created_at=100,
        )


@pytest.mark.parametrize(
    "row",
    [
        _credential_row(created_at=-1),
        _credential_row(updated_at="bad"),
        _machine_row(updated_at=-1),
    ],
)
def test_corrupt_persisted_timestamps_fail_closed(row: dict[str, object]) -> None:
    transaction = ScriptedTransaction(one=[row])
    repository = CredentialRepository()

    with pytest.raises(RepositoryDataError):
        if "user_id" in row:
            repository.get_credential(
                transaction,  # type: ignore[arg-type]
                owner_id="owner-1",
                agent_id="agent-1",
                credential_key="api_key",
            )
        else:
            repository.get_machine_credential(
                transaction,  # type: ignore[arg-type]
                owner_id="owner-1",
                machine_id="machine-1",
            )
