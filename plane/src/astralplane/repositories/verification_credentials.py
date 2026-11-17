"""Owner-scoped storage for approval verification credentials and single-use consumed
challenge digests: bounded enrollment, revision-fenced revocation, and exactly-once
challenge consumption inside caller-owned transactions. Biometric verification, possession
proof, and approval policy stay with the host; only public key material and digests are
stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from astralplane.contracts import Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _non_negative_int,
    _required_id,
    _row_value,
)

_MAX_ID = 128
_MAX_OWNER = 512
_MAX_PUBLIC_KEY = 8192
_MAX_CREDENTIALS_PER_OWNER: Final = 32
_MAX_PRUNE_LIMIT: Final = 500
_OWNER_LOCK_SEED: Final = 103
_KEY_FORMATS: Final = ("sec1", "spki")

_CREDENTIAL_FIELDS = (
    "credential_id, owner_id, device_id, algorithm, key_format, public_key, "
    "credential_state, record_version, enrolled_at, revoked_at, updated_at"
)
_CONSUMPTION_FIELDS = "consumption_id, owner_id, challenge_digest, consumed_at"


class VerificationCredentialLimitError(RepositoryConflictError):
    default_code = "verification_credential_limit_reached"


class VerificationCredentialRevokedError(RepositoryConflictError):
    default_code = "verification_credential_revoked"


class VerificationChallengeAlreadyConsumedError(RepositoryConflictError):
    default_code = "verification_challenge_already_consumed"


def _key_format(value: object, field: str) -> str:
    key_format = _bounded_text(value, field, maximum=16)
    if key_format not in _KEY_FORMATS:
        raise RepositoryValidationError(
            f"{field} is not a supported public key encoding",
            metadata={"field": field, "allowed": ",".join(_KEY_FORMATS)},
        )
    return key_format


def _digest(value: object, field: str) -> str:
    digest = _bounded_text(value, field, maximum=64)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise RepositoryValidationError(
            f"{field} must be a lowercase 64-character hex digest",
            metadata={"field": field},
        )
    return digest


def _version(value: object, field: str) -> int:
    version = _non_negative_int(value, field)
    if version < 1:
        raise RepositoryValidationError(
            f"{field} must be a positive record version",
            metadata={"field": field},
        )
    return version


def _lock_owner(transaction: Transaction, owner: str) -> None:
    transaction.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
        (owner, _OWNER_LOCK_SEED),
    )


@dataclass(frozen=True, slots=True)
class VerificationCredentialRecord:
    credential_id: str
    owner_id: str
    device_id: str
    algorithm: str
    key_format: str
    public_key: str
    credential_state: str
    record_version: int
    enrolled_at: Any
    revoked_at: Any
    updated_at: Any


@dataclass(frozen=True, slots=True)
class VerificationChallengeRecord:
    consumption_id: str
    owner_id: str
    challenge_digest: str
    consumed_at: Any


def _credential(row: Any) -> VerificationCredentialRecord:
    return VerificationCredentialRecord(
        credential_id=_row_value(row, "credential_id"),
        owner_id=_row_value(row, "owner_id"),
        device_id=_row_value(row, "device_id"),
        algorithm=_row_value(row, "algorithm"),
        key_format=_row_value(row, "key_format"),
        public_key=_row_value(row, "public_key"),
        credential_state=_row_value(row, "credential_state"),
        record_version=_row_value(row, "record_version"),
        enrolled_at=_row_value(row, "enrolled_at"),
        revoked_at=_row_value(row, "revoked_at"),
        updated_at=_row_value(row, "updated_at"),
    )


def _consumption(row: Any) -> VerificationChallengeRecord:
    return VerificationChallengeRecord(
        consumption_id=_row_value(row, "consumption_id"),
        owner_id=_row_value(row, "owner_id"),
        challenge_digest=_row_value(row, "challenge_digest"),
        consumed_at=_row_value(row, "consumed_at"),
    )


class VerificationCredentialRepository:
    def enroll_credential(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        credential_id: str,
        device_id: str,
        algorithm: str,
        key_format: str,
        public_key: str,
    ) -> VerificationCredentialRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        credential = _required_id(credential_id, "credential_id", maximum=_MAX_ID)
        device = _required_id(device_id, "device_id", maximum=_MAX_ID)
        bounded_algorithm = _bounded_text(algorithm, "algorithm", maximum=64)
        bounded_format = _key_format(key_format, "key_format")
        bounded_key = _bounded_text(public_key, "public_key", maximum=_MAX_PUBLIC_KEY)
        _lock_owner(transaction, owner)
        count_row = transaction.fetch_one(
            "SELECT count(*) AS total FROM verification_credential "
            "WHERE owner_id = %s AND credential_state = 'active'",
            (owner,),
        )
        replay = transaction.fetch_one(
            f"SELECT {_CREDENTIAL_FIELDS} FROM verification_credential "
            "WHERE credential_id = %s AND owner_id = %s",
            (credential, owner),
        )
        if replay is None and int(_row_value(count_row, "total")) >= _MAX_CREDENTIALS_PER_OWNER:
            raise VerificationCredentialLimitError(
                "owner verification credential count is at capacity",
                metadata={"maximum": _MAX_CREDENTIALS_PER_OWNER},
            )
        row = transaction.fetch_one(
            f"""
            INSERT INTO verification_credential (
                credential_id, owner_id, device_id, algorithm, key_format, public_key
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (credential_id) DO NOTHING
            RETURNING {_CREDENTIAL_FIELDS}
            """,
            (credential, owner, device, bounded_algorithm, bounded_format, bounded_key),
        )
        if row is None:
            if replay is None:
                raise RepositoryConflictError("credential identity is bound to another owner")
            immutable = (
                _row_value(replay, "device_id"),
                _row_value(replay, "algorithm"),
                _row_value(replay, "key_format"),
                _row_value(replay, "public_key"),
            )
            if immutable != (device, bounded_algorithm, bounded_format, bounded_key):
                raise RepositoryConflictError("credential replay changed immutable semantics")
            row = replay
        return _credential(row)

    def get_credential(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        credential_id: str,
    ) -> VerificationCredentialRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        credential = _required_id(credential_id, "credential_id", maximum=_MAX_ID)
        row = transaction.fetch_one(
            f"""
            SELECT {_CREDENTIAL_FIELDS} FROM verification_credential
            WHERE credential_id = %s AND owner_id = %s
            """,
            (credential, owner),
        )
        if row is None:
            raise RepositoryNotFoundError("credential not found for this owner")
        return _credential(row)

    def list_credentials(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        limit: int = 200,
    ) -> tuple[VerificationCredentialRecord, ...]:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        bounded = _bounded_limit(limit)
        rows = transaction.fetch_all(
            f"""
            SELECT {_CREDENTIAL_FIELDS} FROM verification_credential
            WHERE owner_id = %s
            ORDER BY enrolled_at, credential_id
            LIMIT %s
            """,
            (owner, bounded),
        )
        return tuple(_credential(row) for row in rows)

    def require_active_credential(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        credential_id: str,
        expected_record_version: int | None = None,
    ) -> VerificationCredentialRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        credential = _required_id(credential_id, "credential_id", maximum=_MAX_ID)
        version = (
            None
            if expected_record_version is None
            else _version(expected_record_version, "expected_record_version")
        )
        row = transaction.fetch_one(
            f"""
            SELECT {_CREDENTIAL_FIELDS} FROM verification_credential
            WHERE credential_id = %s AND owner_id = %s
            FOR UPDATE
            """,
            (credential, owner),
        )
        if row is None:
            raise RepositoryNotFoundError("credential not found for this owner")
        if _row_value(row, "credential_state") != "active":
            raise VerificationCredentialRevokedError(
                "verification credential is revoked and can never verify again"
            )
        if version is not None and _row_value(row, "record_version") != version:
            raise RepositoryConflictError("credential record version fence rejected the read")
        return _credential(row)

    def revoke_credential(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        credential_id: str,
        expected_record_version: int,
    ) -> VerificationCredentialRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        credential = _required_id(credential_id, "credential_id", maximum=_MAX_ID)
        version = _version(expected_record_version, "expected_record_version")
        row = transaction.fetch_one(
            f"""
            UPDATE verification_credential
               SET credential_state = 'revoked',
                   revoked_at = now(),
                   record_version = record_version + 1,
                   updated_at = now()
             WHERE credential_id = %s AND owner_id = %s AND record_version = %s
               AND credential_state <> 'revoked'
            RETURNING {_CREDENTIAL_FIELDS}
            """,
            (credential, owner, version),
        )
        if row is None:
            raise self._revocation_miss(transaction, owner, credential)
        return _credential(row)

    def _revocation_miss(
        self,
        transaction: Transaction,
        owner: str,
        credential: str,
    ) -> RepositoryNotFoundError | RepositoryConflictError:
        row = transaction.fetch_one(
            "SELECT owner_id, credential_state FROM verification_credential "
            "WHERE credential_id = %s AND owner_id = %s",
            (credential, owner),
        )
        if row is None or _row_value(row, "owner_id") != owner:
            return RepositoryNotFoundError("credential not found for this owner")
        return RepositoryConflictError(
            "credential state or version fence rejected the revocation"
        )

    def consume_challenge(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        challenge_digest: str,
        consumption_id: str,
    ) -> VerificationChallengeRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        digest = _digest(challenge_digest, "challenge_digest")
        consumption = _required_id(consumption_id, "consumption_id", maximum=_MAX_ID)
        row = transaction.fetch_one(
            f"""
            INSERT INTO verification_challenge_consumption (
                consumption_id, owner_id, challenge_digest
            )
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING {_CONSUMPTION_FIELDS}
            """,
            (consumption, owner, digest),
        )
        if row is not None:
            return _consumption(row)
        existing = transaction.fetch_one(
            f"SELECT {_CONSUMPTION_FIELDS} FROM verification_challenge_consumption "
            "WHERE consumption_id = %s AND owner_id = %s",
            (consumption, owner),
        )
        if existing is not None:
            if _row_value(existing, "challenge_digest") != digest:
                raise RepositoryConflictError("consumption replay changed immutable semantics")
            return _consumption(existing)
        consumed = transaction.fetch_one(
            f"SELECT {_CONSUMPTION_FIELDS} FROM verification_challenge_consumption "
            "WHERE owner_id = %s AND challenge_digest = %s",
            (owner, digest),
        )
        if consumed is None:
            raise RepositoryConflictError("consumption identity is bound to another owner")
        raise VerificationChallengeAlreadyConsumedError(
            "challenge digest was already consumed exactly once"
        )

    def get_consumed_challenge(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        challenge_digest: str,
    ) -> VerificationChallengeRecord | None:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        digest = _digest(challenge_digest, "challenge_digest")
        row = transaction.fetch_one(
            f"""
            SELECT {_CONSUMPTION_FIELDS} FROM verification_challenge_consumption
            WHERE owner_id = %s AND challenge_digest = %s
            """,
            (owner, digest),
        )
        return None if row is None else _consumption(row)

    def prune_consumed_challenges(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        before_epoch: int,
        limit: int = 200,
    ) -> int:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        cutoff = _non_negative_int(before_epoch, "before_epoch")
        bounded = _bounded_limit(limit, maximum=_MAX_PRUNE_LIMIT)
        result = transaction.execute(
            """
            DELETE FROM verification_challenge_consumption
            WHERE owner_id = %s
              AND consumed_at < to_timestamp(%s)
              AND consumption_id IN (
                  SELECT victim.consumption_id
                  FROM verification_challenge_consumption AS victim
                  WHERE victim.owner_id = %s AND victim.consumed_at < to_timestamp(%s)
                  ORDER BY victim.consumed_at, victim.consumption_id
                  LIMIT %s
              )
            """,
            (owner, cutoff, owner, cutoff, bounded),
        )
        return int(result.rowcount)
