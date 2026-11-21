"""Owner-isolated persistence for encrypted offline refresh-token grants and their
optional finite admission allowance. Token exchange, encryption, and IdP-side
revocation stay with the caller; exposes only opaque bytes and lifecycle mechanics.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from astralplane.contracts import Transaction
from astralplane.errors import LockContentionError, is_lock_contention
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
    _non_negative_int,
    _required_id,
    _row_value,
)


class OfflineGrantRevocationState(StrEnum):
    REVOKED = "revoked"
    ALREADY_REVOKED = "already_revoked"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class OfflineGrantRecord:
    grant_id: str
    owner_id: str
    agent_id: str | None
    encrypted_refresh_token: bytes = field(repr=False)
    issued_at: int
    expires_at: int
    revoked_at: int | None
    created_at: int | None
    updated_at: int | None
    max_admissions: int | None = None
    consumed_admissions: int | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    @property
    def admissions_remaining(self) -> int | None:
        if self.max_admissions is None:
            return None
        return self.max_admissions - (self.consumed_admissions or 0)


@dataclass(frozen=True, slots=True)
class OfflineGrantReference:
    grant_id: str
    owner_id: str
    agent_id: str | None
    issued_at: int
    expires_at: int


class OfflineGrantRepository:
    _FIELDS = (
        "id, user_id, agent_id, refresh_token_enc, issued_at, expires_at, "
        "revoked_at, created_at, updated_at, max_admissions, consumed_admissions"
    )

    def create_grant(
        self,
        transaction: Transaction,
        *,
        grant_id: str,
        owner_id: str,
        agent_id: str | None,
        encrypted_refresh_token: bytes,
        issued_at: int,
        expires_at: int,
        max_admissions: int | None = None,
    ) -> OfflineGrantRecord:
        grant = _uuid_text(grant_id, "grant_id")
        owner = _required_id(owner_id, "owner_id")
        agent = _optional_id(agent_id, "agent_id")
        ciphertext = _opaque_bytes(encrypted_refresh_token)
        issued = _non_negative_int(issued_at, "issued_at")
        expires = _non_negative_int(expires_at, "expires_at")
        if expires <= issued:
            raise RepositoryValidationError("expires_at must be later than issued_at")
        limit = _optional_admission_limit(max_admissions)
        consumed = None if limit is None else 0
        row = transaction.fetch_one(
            f"""
            INSERT INTO user_offline_grant (
                id, user_id, agent_id, refresh_token_enc, issued_at, expires_at,
                revoked_at, created_at, updated_at, max_admissions, consumed_admissions
            ) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._FIELDS}
            """,
            (grant, owner, agent, ciphertext, issued, expires, issued, issued, limit, consumed),
        )
        if row is None:
            row = transaction.fetch_one(
                f"""
                SELECT {self._FIELDS} FROM user_offline_grant
                 WHERE id = %s AND user_id = %s
                """,
                (grant, owner),
            )
        if row is None:
            raise RepositoryConflictError("offline grant identity is bound to another owner")
        record = _grant(row)
        if (
            record.owner_id != owner
            or record.agent_id != agent
            or record.encrypted_refresh_token != ciphertext
            or record.issued_at != issued
            or record.expires_at != expires
        ):
            raise RepositoryConflictError("offline grant replay changed immutable semantics")
        return record

    # Racing chargers: exactly one wins, never double-charged
    def consume_admission(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        grant_id: str,
        as_of: int,
    ) -> OfflineGrantRecord:
        owner = _required_id(owner_id, "owner_id")
        grant = _uuid_text(grant_id, "grant_id")
        observed_at = _non_negative_int(as_of, "as_of")
        row = transaction.fetch_one(
            f"""
            UPDATE user_offline_grant
               SET consumed_admissions = CASE
                       WHEN max_admissions IS NOT NULL
                           THEN COALESCE(consumed_admissions, 0) + 1
                       ELSE consumed_admissions
                   END,
                   updated_at = %s
             WHERE id = %s AND user_id = %s AND revoked_at IS NULL AND expires_at > %s
               AND (max_admissions IS NULL OR COALESCE(consumed_admissions, 0) < max_admissions)
            RETURNING {self._FIELDS}
            """,
            (observed_at, grant, owner, observed_at),
        )
        if row is not None:
            return _grant(row)
        existing = transaction.fetch_one(
            f"SELECT {self._FIELDS} FROM user_offline_grant WHERE id = %s AND user_id = %s",
            (grant, owner),
        )
        if existing is None:
            raise RepositoryConflictError("offline grant is unavailable")
        record = _grant(existing)
        if record.revoked_at is not None or not record.issued_at <= observed_at < record.expires_at:
            raise RepositoryConflictError("offline grant is unavailable")
        raise RepositoryConflictError("offline grant allowance exhausted")

    def get_grant(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        grant_id: str,
    ) -> OfflineGrantRecord | None:
        owner = _required_id(owner_id, "owner_id")
        grant = _uuid_text(grant_id, "grant_id")
        row = transaction.fetch_one(
            f"SELECT {self._FIELDS} FROM user_offline_grant WHERE id = %s AND user_id = %s",
            (grant, owner),
        )
        return None if row is None else _grant(row)

    def get_active_for_exchange(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        grant_id: str,
        as_of: int,
    ) -> OfflineGrantRecord | None:
        owner = _required_id(owner_id, "owner_id")
        grant = _uuid_text(grant_id, "grant_id")
        observed_at = _non_negative_int(as_of, "as_of")
        row = transaction.fetch_one(
            f"""
            SELECT {self._FIELDS} FROM user_offline_grant
             WHERE id = %s AND user_id = %s
               AND revoked_at IS NULL AND expires_at > %s
            """,
            (grant, owner, observed_at),
        )
        return None if row is None else _grant(row)

    def assert_current_grant(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        grant_id: str,
    ) -> OfflineGrantRecord:
        owner = str(_required_id(owner_id, "owner_id"))
        grant = _uuid_text(grant_id, "grant_id")
        try:
            with transaction.savepoint("offline_grant_current_read"):
                locked = transaction.fetch_one(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,79)) AS acquired",
                    (owner,),
                )
                if locked is None or locked["acquired"] is not True:
                    if locked is not None and locked["acquired"] is False:
                        raise LockContentionError("offline grant is unavailable") from None
                    raise RepositoryConflictError("offline grant is unavailable")
                state = transaction.fetch_one(
                    "SELECT state FROM astralplane_blob_owner_state "
                    "WHERE owner_id=%s FOR UPDATE NOWAIT", (owner,),
                )
                if state is not None and state["state"] != "active":
                    raise RepositoryConflictError("offline grant is unavailable")
                row = transaction.fetch_one(
                    f"SELECT {self._FIELDS} FROM user_offline_grant "
                    "WHERE id=%s AND user_id=%s FOR UPDATE", (grant, owner),
                )
                clock = transaction.fetch_one(
                    "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS now_ms"
                )
                if clock is None or type(clock["now_ms"]) is not int:
                    raise RepositoryDataError("offline grant clock is unavailable")
                if row is None:
                    raise RepositoryConflictError("offline grant is unavailable")
                record = _grant(row)
                if (record.revoked_at is not None
                        or not record.issued_at <= clock["now_ms"] < record.expires_at):
                    raise RepositoryConflictError("offline grant is unavailable")
                return record
        except Exception as exc:
            if is_lock_contention(exc):
                raise LockContentionError("offline grant is unavailable") from None
            if getattr(exc, "pgcode", None) == "55P03":
                raise RepositoryConflictError("offline grant is unavailable") from None
            raise

    def replace_refresh_token_if_current(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        grant_id: str,
        expected_encrypted_refresh_token: bytes,
        encrypted_refresh_token: bytes,
        as_of: int,
    ) -> OfflineGrantRecord | None:
        owner = _required_id(owner_id, "owner_id")
        grant = _uuid_text(grant_id, "grant_id")
        expected = _opaque_bytes(expected_encrypted_refresh_token)
        replacement = _opaque_bytes(encrypted_refresh_token)
        observed = _non_negative_int(as_of, "as_of")
        row = transaction.fetch_one(
            f"""
            UPDATE user_offline_grant
               SET refresh_token_enc = %s, updated_at = %s
             WHERE id = %s AND user_id = %s AND refresh_token_enc = %s
               AND revoked_at IS NULL AND expires_at > %s
            RETURNING {self._FIELDS}
            """,
            (replacement, observed, grant, owner, expected, observed),
        )
        return None if row is None else _grant(row)

    def find_latest_valid(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        as_of: int,
        agent_id: str | None = None,
    ) -> OfflineGrantReference | None:
        owner = _required_id(owner_id, "owner_id")
        agent = _optional_id(agent_id, "agent_id")
        observed_at = _non_negative_int(as_of, "as_of")
        row = transaction.fetch_one(
            """
            SELECT id, user_id, agent_id, issued_at, expires_at
              FROM user_offline_grant
             WHERE user_id = %s AND revoked_at IS NULL AND expires_at > %s
             ORDER BY CASE
                          WHEN %s::text IS NOT NULL AND agent_id = %s THEN 0
                          ELSE 1
                      END,
                      issued_at DESC, id
             LIMIT 1
            """,
            (owner, observed_at, agent, agent),
        )
        return None if row is None else _reference(row)

    def revoke_grant(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        grant_id: str,
        revoked_at: int,
    ) -> OfflineGrantRevocationState:
        owner = _required_id(owner_id, "owner_id")
        grant = _uuid_text(grant_id, "grant_id")
        observed_at = _non_negative_int(revoked_at, "revoked_at")
        result = transaction.execute(
            """
            UPDATE user_offline_grant
               SET revoked_at = %s, updated_at = %s
             WHERE id = %s AND user_id = %s AND revoked_at IS NULL
            """,
            (observed_at, observed_at, grant, owner),
        )
        if result.rowcount == 1:
            return OfflineGrantRevocationState.REVOKED
        row = transaction.fetch_one(
            "SELECT revoked_at FROM user_offline_grant WHERE id = %s AND user_id = %s",
            (grant, owner),
        )
        if row is None:
            return OfflineGrantRevocationState.MISSING
        return OfflineGrantRevocationState.ALREADY_REVOKED

    def revoke_owner(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        revoked_at: int,
    ) -> int:
        owner = _required_id(owner_id, "owner_id")
        observed_at = _non_negative_int(revoked_at, "revoked_at")
        result = transaction.execute(
            """
            UPDATE user_offline_grant
               SET revoked_at = %s, updated_at = %s
             WHERE user_id = %s AND revoked_at IS NULL
            """,
            (observed_at, observed_at, owner),
        )
        return max(0, result.rowcount)


def _uuid_text(value: object, field: str) -> str:
    if not isinstance(value, (str, uuid.UUID)):
        raise RepositoryValidationError(f"{field} must be a UUID")
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise RepositoryValidationError(f"{field} must be a UUID") from exc


def _optional_id(value: object, field: str) -> str | None:
    return None if value is None else _required_id(value, field)


def _optional_admission_limit(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RepositoryValidationError("max_admissions must be an integer")
    if not 1 <= value <= 10000:
        raise RepositoryValidationError("max_admissions must be between 1 and 10000")
    return value


def _opaque_bytes(value: object) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise RepositoryValidationError("encrypted_refresh_token must be bytes")
    ciphertext = bytes(value)
    if not ciphertext:
        raise RepositoryValidationError("encrypted_refresh_token must not be empty")
    if len(ciphertext) > 1_000_000:
        raise RepositoryValidationError("encrypted_refresh_token exceeds its maximum length")
    return ciphertext


def _grant(row: Mapping[str, Any]) -> OfflineGrantRecord:
    issued_at = _stored_int(_row_value(row, "issued_at"), "issued_at")
    expires_at = _stored_int(_row_value(row, "expires_at"), "expires_at")
    if expires_at <= issued_at:
        raise RepositoryDataError("persisted offline grant expiry is invalid")
    try:
        ciphertext = _opaque_bytes(_row_value(row, "refresh_token_enc"))
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted offline grant ciphertext is invalid") from exc
    max_admissions = _optional_stored_int(row.get("max_admissions"), "max_admissions")
    consumed_admissions = _optional_stored_int(
        row.get("consumed_admissions"), "consumed_admissions"
    )
    if (max_admissions is None) != (consumed_admissions is None) or (
        max_admissions is not None and consumed_admissions > max_admissions
    ):
        raise RepositoryDataError("persisted offline grant allowance is inconsistent")
    return OfflineGrantRecord(
        grant_id=_stored_uuid(_row_value(row, "id")),
        owner_id=str(_row_value(row, "user_id")),
        agent_id=None if row.get("agent_id") is None else str(row["agent_id"]),
        encrypted_refresh_token=ciphertext,
        issued_at=issued_at,
        expires_at=expires_at,
        revoked_at=_optional_stored_int(row.get("revoked_at"), "revoked_at"),
        created_at=_optional_stored_int(row.get("created_at"), "created_at"),
        updated_at=_optional_stored_int(row.get("updated_at"), "updated_at"),
        max_admissions=max_admissions,
        consumed_admissions=consumed_admissions,
    )


def _reference(row: Mapping[str, Any]) -> OfflineGrantReference:
    issued_at = _stored_int(_row_value(row, "issued_at"), "issued_at")
    expires_at = _stored_int(_row_value(row, "expires_at"), "expires_at")
    if expires_at <= issued_at:
        raise RepositoryDataError("persisted offline grant expiry is invalid")
    return OfflineGrantReference(
        grant_id=_stored_uuid(_row_value(row, "id")),
        owner_id=str(_row_value(row, "user_id")),
        agent_id=None if row.get("agent_id") is None else str(row["agent_id"]),
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _optional_stored_int(value: object, field: str) -> int | None:
    return None if value is None else _stored_int(value, field)


def _stored_uuid(value: object) -> str:
    try:
        return _uuid_text(value, "persisted grant id")
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted offline grant id is invalid") from exc


def _stored_int(value: object, field: str) -> int:
    try:
        return _non_negative_int(value, field)
    except ValueError as exc:
        raise RepositoryDataError(
            "persisted grant timestamp is not a non-negative integer",
            metadata={"field": field},
        ) from exc


__all__ = (
    "OfflineGrantRecord",
    "OfflineGrantReference",
    "OfflineGrantRepository",
    "OfflineGrantRevocationState",
)
