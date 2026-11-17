"""Stores opaque ciphertext for user and remote-machine credentials under owner
predicates and revision fences. Encryption and authorization stay with the caller;
one global-inventory read is reserved for an authorized re-encryption worker.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from astralplane.contracts import Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _non_negative_int,
    _required_id,
    _row_value,
)


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    credential_id: int
    owner_id: str
    agent_id: str
    credential_key: str
    encrypted_value: str = field(repr=False)
    created_at: int | None
    updated_at: int | None


@dataclass(frozen=True, slots=True)
class MachineCredentialRecord:
    machine_id: str
    owner_id: str
    credential_type: str
    encrypted_secret: str = field(repr=False)
    encrypted_passphrase: str | None = field(repr=False)
    created_at: int
    updated_at: int


class CredentialRepository:
    _USER_FIELDS = (
        "id, user_id, agent_id, credential_key, encrypted_value, created_at, updated_at"
    )
    _MACHINE_FIELDS = (
        "machine_id, owner_user_id, cred_type, encrypted_secret, "
        "encrypted_passphrase, created_at, updated_at"
    )

    def upsert_credential(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        credential_key: str,
        encrypted_value: str,
        updated_at: int,
    ) -> CredentialRecord:
        values = _credential_values(
            owner_id=owner_id,
            agent_id=agent_id,
            credential_key=credential_key,
            encrypted_value=encrypted_value,
            updated_at=updated_at,
        )
        row = transaction.fetch_one(
            f"""
            INSERT INTO user_credentials (
                user_id, agent_id, credential_key, encrypted_value, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, agent_id, credential_key) DO UPDATE SET
                encrypted_value = EXCLUDED.encrypted_value,
                updated_at = EXCLUDED.updated_at
            RETURNING {self._USER_FIELDS}
            """,
            (*values[:4], values[4], values[4]),
        )
        if row is None:  # pragma: no cover
            raise RepositoryDataError("credential upsert returned no row")
        return _credential(row)

    def compare_and_set_ciphertext(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        credential_key: str,
        expected_updated_at: int | None,
        encrypted_value: str,
        updated_at: int,
    ) -> CredentialRecord:
        owner, agent, key, ciphertext, observed_at = _credential_values(
            owner_id=owner_id,
            agent_id=agent_id,
            credential_key=credential_key,
            encrypted_value=encrypted_value,
            updated_at=updated_at,
        )
        expected = _optional_non_negative_int(expected_updated_at, "expected_updated_at")
        if expected is not None and observed_at <= expected:
            raise RepositoryConflictError("credential revision must advance")
        row = transaction.fetch_one(
            f"""
            UPDATE user_credentials
               SET encrypted_value = %s, updated_at = %s
             WHERE user_id = %s AND agent_id = %s AND credential_key = %s
               AND updated_at IS NOT DISTINCT FROM %s
            RETURNING {self._USER_FIELDS}
            """,
            (ciphertext, observed_at, owner, agent, key, expected),
        )
        if row is None:
            existing = transaction.fetch_one(
                """
                SELECT id FROM user_credentials
                 WHERE user_id = %s AND agent_id = %s AND credential_key = %s
                """,
                (owner, agent, key),
            )
            if existing is None:
                raise RepositoryConflictError(
                    "owner-scoped credential does not exist for compare-and-set"
                )
            raise RepositoryConflictError("credential revision fence is stale")
        return _credential(row)

    def get_credential(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        credential_key: str,
    ) -> CredentialRecord | None:
        owner, agent, key = _credential_identity(owner_id, agent_id, credential_key)
        row = transaction.fetch_one(
            f"""
            SELECT {self._USER_FIELDS} FROM user_credentials
             WHERE user_id = %s AND agent_id = %s AND credential_key = %s
            """,
            (owner, agent, key),
        )
        return None if row is None else _credential(row)

    def list_credentials(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        limit: int = 200,
    ) -> tuple[CredentialRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        agent = _required_id(agent_id, "agent_id")
        limit = _bounded_limit(limit, maximum=1000)
        rows = transaction.fetch_all(
            f"""
            SELECT {self._USER_FIELDS} FROM user_credentials
             WHERE user_id = %s AND agent_id = %s
             ORDER BY credential_key, id
             LIMIT %s
            """,
            (owner, agent, limit),
        )
        return tuple(_credential(row) for row in rows)

    def list_credential_keys(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        limit: int = 200,
    ) -> tuple[str, ...]:
        owner = _required_id(owner_id, "owner_id")
        agent = _required_id(agent_id, "agent_id")
        limit = _bounded_limit(limit, maximum=1000)
        rows = transaction.fetch_all(
            """
            SELECT credential_key FROM user_credentials
             WHERE user_id = %s AND agent_id = %s
             ORDER BY credential_key
             LIMIT %s
            """,
            (owner, agent, limit),
        )
        return tuple(str(_row_value(row, "credential_key")) for row in rows)

    def list_agent_credentials_for_reencryption(
        self,
        transaction: Transaction,
        *,
        agent_id: str,
        after_credential_id: int = 0,
        limit: int = 200,
    ) -> tuple[CredentialRecord, ...]:
        agent = _required_id(agent_id, "agent_id")
        after = _non_negative_int(after_credential_id, "after_credential_id")
        limit = _bounded_limit(limit, maximum=1000)
        rows = transaction.fetch_all(
            f"""
            SELECT {self._USER_FIELDS} FROM user_credentials
             WHERE agent_id = %s AND id > %s
             ORDER BY id
             LIMIT %s
            """,
            (agent, after, limit),
        )
        return tuple(_credential(row) for row in rows)

    def delete_credential(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        credential_key: str,
    ) -> bool:
        owner, agent, key = _credential_identity(owner_id, agent_id, credential_key)
        result = transaction.execute(
            """
            DELETE FROM user_credentials
             WHERE user_id = %s AND agent_id = %s AND credential_key = %s
            """,
            (owner, agent, key),
        )
        return result.rowcount == 1

    def delete_agent_credentials(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
    ) -> int:
        owner = _required_id(owner_id, "owner_id")
        agent = _required_id(agent_id, "agent_id")
        result = transaction.execute(
            "DELETE FROM user_credentials WHERE user_id = %s AND agent_id = %s",
            (owner, agent),
        )
        return max(0, result.rowcount)

    def create_machine_credential(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        machine_id: str,
        credential_type: str,
        encrypted_secret: str,
        encrypted_passphrase: str | None,
        created_at: int,
    ) -> MachineCredentialRecord:
        values = _machine_values(
            owner_id=owner_id,
            machine_id=machine_id,
            credential_type=credential_type,
            encrypted_secret=encrypted_secret,
            encrypted_passphrase=encrypted_passphrase,
            updated_at=created_at,
        )
        owner, machine, kind, secret, passphrase, observed_at = values
        row = transaction.fetch_one(
            f"""
            INSERT INTO machine_credential (
                machine_id, owner_user_id, cred_type, encrypted_secret,
                encrypted_passphrase, created_at, updated_at
            )
            SELECT machine_id, owner_user_id, %s, %s, %s, %s, %s
              FROM remote_machine
             WHERE machine_id = %s AND owner_user_id = %s
            ON CONFLICT (machine_id) DO NOTHING
            RETURNING {self._MACHINE_FIELDS}
            """,
            (kind, secret, passphrase, observed_at, observed_at, machine, owner),
        )
        if row is None:
            row = transaction.fetch_one(
                f"""
                SELECT {self._MACHINE_FIELDS} FROM machine_credential
                 WHERE machine_id = %s AND owner_user_id = %s
                """,
                (machine, owner),
            )
        if row is None:
            raise RepositoryConflictError(
                "machine is missing, owned by another principal, or already credentialed"
            )
        record = _machine_credential(row)
        if record != MachineCredentialRecord(
            machine_id=machine,
            owner_id=owner,
            credential_type=kind,
            encrypted_secret=secret,
            encrypted_passphrase=passphrase,
            created_at=observed_at,
            updated_at=observed_at,
        ):
            raise RepositoryConflictError("machine credential replay changed stored semantics")
        return record

    def compare_and_set_machine_credential(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        machine_id: str,
        expected_updated_at: int,
        credential_type: str,
        encrypted_secret: str,
        encrypted_passphrase: str | None,
        updated_at: int,
    ) -> MachineCredentialRecord:
        owner, machine, kind, secret, passphrase, observed_at = _machine_values(
            owner_id=owner_id,
            machine_id=machine_id,
            credential_type=credential_type,
            encrypted_secret=encrypted_secret,
            encrypted_passphrase=encrypted_passphrase,
            updated_at=updated_at,
        )
        expected = _non_negative_int(expected_updated_at, "expected_updated_at")
        if observed_at <= expected:
            raise RepositoryConflictError("machine credential revision must advance")
        row = transaction.fetch_one(
            f"""
            UPDATE machine_credential
               SET cred_type = %s, encrypted_secret = %s,
                   encrypted_passphrase = %s, updated_at = %s
             WHERE machine_id = %s AND owner_user_id = %s AND updated_at = %s
            RETURNING {self._MACHINE_FIELDS}
            """,
            (kind, secret, passphrase, observed_at, machine, owner, expected),
        )
        if row is None:
            raise RepositoryConflictError("machine credential owner or revision fence is stale")
        return _machine_credential(row)

    def get_machine_credential(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        machine_id: str,
    ) -> MachineCredentialRecord | None:
        owner = _required_id(owner_id, "owner_id")
        machine = _required_id(machine_id, "machine_id", maximum=128)
        row = transaction.fetch_one(
            f"""
            SELECT {self._MACHINE_FIELDS} FROM machine_credential
             WHERE machine_id = %s AND owner_user_id = %s
            """,
            (machine, owner),
        )
        return None if row is None else _machine_credential(row)

    def delete_machine_credential(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        machine_id: str,
    ) -> bool:
        owner = _required_id(owner_id, "owner_id")
        machine = _required_id(machine_id, "machine_id", maximum=128)
        result = transaction.execute(
            """
            DELETE FROM machine_credential
             WHERE machine_id = %s AND owner_user_id = %s
            """,
            (machine, owner),
        )
        return result.rowcount == 1

    def delete_owner_machine_credentials(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
    ) -> int:
        owner = _required_id(owner_id, "owner_id")
        result = transaction.execute(
            "DELETE FROM machine_credential WHERE owner_user_id = %s",
            (owner,),
        )
        return max(0, result.rowcount)


def _credential_identity(
    owner_id: object,
    agent_id: object,
    credential_key: object,
) -> tuple[str, str, str]:
    return (
        _required_id(owner_id, "owner_id"),
        _required_id(agent_id, "agent_id"),
        _bounded_text(credential_key, "credential_key", maximum=512),
    )


def _credential_values(
    *,
    owner_id: object,
    agent_id: object,
    credential_key: object,
    encrypted_value: object,
    updated_at: object,
) -> tuple[str, str, str, str, int]:
    owner, agent, key = _credential_identity(owner_id, agent_id, credential_key)
    return (
        owner,
        agent,
        key,
        _bounded_text(encrypted_value, "encrypted_value", maximum=1_000_000),
        _non_negative_int(updated_at, "updated_at"),
    )


def _machine_values(
    *,
    owner_id: object,
    machine_id: object,
    credential_type: object,
    encrypted_secret: object,
    encrypted_passphrase: object,
    updated_at: object,
) -> tuple[str, str, str, str, str | None, int]:
    kind = _bounded_text(credential_type, "credential_type", maximum=32)
    if kind not in {"ssh_key", "password"}:
        raise RepositoryValidationError("machine credential type is unsupported")
    passphrase = (
        None
        if encrypted_passphrase is None
        else _bounded_text(
            encrypted_passphrase,
            "encrypted_passphrase",
            maximum=1_000_000,
        )
    )
    return (
        _required_id(owner_id, "owner_id"),
        _required_id(machine_id, "machine_id", maximum=128),
        kind,
        _bounded_text(encrypted_secret, "encrypted_secret", maximum=1_000_000),
        passphrase,
        _non_negative_int(updated_at, "updated_at"),
    )


def _optional_non_negative_int(value: object, field: str) -> int | None:
    return None if value is None else _non_negative_int(value, field)


def _credential(row: Mapping[str, Any]) -> CredentialRecord:
    return CredentialRecord(
        credential_id=int(_row_value(row, "id")),
        owner_id=str(_row_value(row, "user_id")),
        agent_id=str(_row_value(row, "agent_id")),
        credential_key=str(_row_value(row, "credential_key")),
        encrypted_value=str(_row_value(row, "encrypted_value")),
        created_at=_optional_stored_int(row.get("created_at"), "created_at"),
        updated_at=_optional_stored_int(row.get("updated_at"), "updated_at"),
    )


def _machine_credential(row: Mapping[str, Any]) -> MachineCredentialRecord:
    return MachineCredentialRecord(
        machine_id=str(_row_value(row, "machine_id")),
        owner_id=str(_row_value(row, "owner_user_id")),
        credential_type=str(_row_value(row, "cred_type")),
        encrypted_secret=str(_row_value(row, "encrypted_secret")),
        encrypted_passphrase=(
            None
            if row.get("encrypted_passphrase") is None
            else str(row["encrypted_passphrase"])
        ),
        created_at=_stored_int(_row_value(row, "created_at"), "created_at"),
        updated_at=_stored_int(_row_value(row, "updated_at"), "updated_at"),
    )


def _optional_stored_int(value: object, field: str) -> int | None:
    return None if value is None else _stored_int(value, field)


def _stored_int(value: object, field: str) -> int:
    try:
        return _non_negative_int(value, field)
    except ValueError as exc:
        raise RepositoryDataError(
            "persisted timestamp is not a non-negative integer", metadata={"field": field}
        ) from exc


__all__ = (
    "CredentialRecord",
    "CredentialRepository",
    "MachineCredentialRecord",
)
