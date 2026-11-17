"""Opaque-ciphertext persistence for encrypted LLM provider config and TypeSafe
credentials, one owner-scoped row each. Encryption, decryption, and provider
validation stay with the caller; used by AstralDeep's llm_config/user_store.py.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_text,
    _required_id,
    _row_value,
    _single_returned,
)

TYPESAFE_OUTCOMES: Final = ("unverified", "valid", "rejected", "unavailable")
_FINGERPRINT_PATTERN: Final = re.compile(r"^[0-9a-f]{12}$")


@dataclass(frozen=True, slots=True)
class EncryptedLLMConfigRecord:
    scope: str
    owner_id: str | None
    provider: str
    base_url: str
    model: str
    api_key_ciphertext: str | None = field(repr=False)
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


def _stored_time(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RepositoryDataError(
            "persisted timestamp is not timezone-aware",
            metadata={"field": field_name},
        )
    return value


def _optional_ciphertext(value: object) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, "api key ciphertext", maximum=65_536)


def _user_record(row: Mapping[str, Any]) -> EncryptedLLMConfigRecord:
    return EncryptedLLMConfigRecord(
        scope="user",
        owner_id=str(_row_value(row, "user_id")),
        provider=str(_row_value(row, "provider")),
        base_url=str(_row_value(row, "base_url")),
        model=str(_row_value(row, "model")),
        api_key_ciphertext=_optional_ciphertext(row.get("api_key_enc")),
        updated_by=None,
        created_at=_stored_time(_row_value(row, "created_at"), "created_at"),
        updated_at=_stored_time(_row_value(row, "updated_at"), "updated_at"),
    )


def _system_record(row: Mapping[str, Any]) -> EncryptedLLMConfigRecord:
    return EncryptedLLMConfigRecord(
        scope="system",
        owner_id=None,
        provider=str(_row_value(row, "provider")),
        base_url=str(_row_value(row, "base_url")),
        model=str(_row_value(row, "model")),
        api_key_ciphertext=_optional_ciphertext(row.get("api_key_enc")),
        updated_by=str(_row_value(row, "updated_by")),
        created_at=_stored_time(_row_value(row, "created_at"), "created_at"),
        updated_at=_stored_time(_row_value(row, "updated_at"), "updated_at"),
    )


def _config_values(
    *,
    provider: object,
    base_url: object,
    model: object,
    api_key_ciphertext: object,
) -> tuple[str, str, str, str | None]:
    return (
        _bounded_text(provider, "provider", maximum=128),
        _bounded_text(base_url, "base url", maximum=4_096),
        _bounded_text(model, "model", maximum=512),
        _optional_ciphertext(api_key_ciphertext),
    )


class EncryptedLLMConfigRepository:
    _USER_FIELDS = "user_id, provider, base_url, model, api_key_enc, created_at, updated_at"
    _SYSTEM_FIELDS = "provider, base_url, model, api_key_enc, updated_by, created_at, updated_at"

    def get_user(
        self,
        executor: QueryExecutor,
        *,
        owner_id: str,
    ) -> EncryptedLLMConfigRecord | None:
        owner = _required_id(owner_id, "owner id")
        row = executor.fetch_one(
            f"SELECT {self._USER_FIELDS} FROM user_llm_config WHERE user_id = %s",
            (owner,),
        )
        return None if row is None else _user_record(row)

    def get_user_for_update(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
    ) -> EncryptedLLMConfigRecord | None:
        owner = _required_id(owner_id, "owner id")
        row = transaction.fetch_one(
            f"SELECT {self._USER_FIELDS} FROM user_llm_config WHERE user_id = %s FOR UPDATE",
            (owner,),
        )
        return None if row is None else _user_record(row)

    def upsert_user(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        provider: str,
        base_url: str,
        model: str,
        api_key_ciphertext: str | None,
    ) -> EncryptedLLMConfigRecord:
        owner = _required_id(owner_id, "owner id")
        values = _config_values(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key_ciphertext=api_key_ciphertext,
        )
        result = transaction.execute(
            f"""
            INSERT INTO user_llm_config (
                user_id, provider, base_url, model, api_key_enc, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, clock_timestamp(), clock_timestamp())
            ON CONFLICT (user_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                base_url = EXCLUDED.base_url,
                model = EXCLUDED.model,
                api_key_enc = EXCLUDED.api_key_enc,
                updated_at = clock_timestamp()
            RETURNING {self._USER_FIELDS}
            """,
            (owner, *values),
        )
        return _user_record(_single_returned(result, "upsert user LLM configuration"))

    def upsert_user_before_deadline(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        provider: str,
        base_url: str,
        model: str,
        api_key_ciphertext: str | None,
        deadline_at: datetime,
    ) -> EncryptedLLMConfigRecord | None:
        owner = _required_id(owner_id, "owner id")
        values = _config_values(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key_ciphertext=api_key_ciphertext,
        )
        if (
            not isinstance(deadline_at, datetime)
            or deadline_at.tzinfo is None
            or deadline_at.utcoffset() is None
        ):
            raise RepositoryValidationError("deadline_at must be a timezone-aware datetime")
        result = transaction.execute(
            f"""
            INSERT INTO user_llm_config (
                user_id, provider, base_url, model, api_key_enc, created_at, updated_at
            )
            SELECT %s, %s, %s, %s, %s, clock_timestamp(), clock_timestamp()
            WHERE clock_timestamp() < %s
            ON CONFLICT (user_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                base_url = EXCLUDED.base_url,
                model = EXCLUDED.model,
                api_key_enc = EXCLUDED.api_key_enc,
                updated_at = clock_timestamp()
            RETURNING {self._USER_FIELDS}
            """,
            (owner, *values, deadline_at),
        )
        rows = getattr(result, "returned_records", ())
        if not rows:
            return None
        return _user_record(_single_returned(result, "deadline-fenced user LLM configuration"))

    def delete_user(self, transaction: Transaction, *, owner_id: str) -> None:
        owner = _required_id(owner_id, "owner id")
        result = transaction.execute(
            "DELETE FROM user_llm_config WHERE user_id = %s",
            (owner,),
        )
        if result.rowcount != 1:
            raise RepositoryNotFoundError(
                "owner-scoped LLM configuration was not found",
                metadata={"operation": "delete user LLM configuration"},
            )

    def get_system(self, executor: QueryExecutor) -> EncryptedLLMConfigRecord | None:
        row = executor.fetch_one(
            f"SELECT {self._SYSTEM_FIELDS} FROM system_llm_config WHERE id = 1"
        )
        return None if row is None else _system_record(row)

    def upsert_system(
        self,
        transaction: Transaction,
        *,
        updated_by: str,
        provider: str,
        base_url: str,
        model: str,
        api_key_ciphertext: str | None,
    ) -> EncryptedLLMConfigRecord:
        actor = _required_id(updated_by, "updated by")
        values = _config_values(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key_ciphertext=api_key_ciphertext,
        )
        result = transaction.execute(
            f"""
            INSERT INTO system_llm_config (
                id, provider, base_url, model, api_key_enc, updated_by, created_at, updated_at
            ) VALUES (1, %s, %s, %s, %s, %s, clock_timestamp(), clock_timestamp())
            ON CONFLICT (id) DO UPDATE SET
                provider = EXCLUDED.provider,
                base_url = EXCLUDED.base_url,
                model = EXCLUDED.model,
                api_key_enc = EXCLUDED.api_key_enc,
                updated_by = EXCLUDED.updated_by,
                updated_at = clock_timestamp()
            RETURNING {self._SYSTEM_FIELDS}
            """,
            (*values, actor),
        )
        return _system_record(_single_returned(result, "upsert system LLM configuration"))

    def delete_system(self, transaction: Transaction) -> None:
        result = transaction.execute("DELETE FROM system_llm_config WHERE id = 1")
        if result.rowcount != 1:
            raise RepositoryNotFoundError(
                "system LLM configuration was not found",
                metadata={"operation": "delete system LLM configuration"},
            )




@dataclass(frozen=True, slots=True)
class EncryptedTypeSafeCredentialRecord:
    owner_id: str
    api_key_ciphertext: str = field(repr=False)
    key_fingerprint: str = ""
    last_verified_at: datetime | None = None
    last_verification_outcome: str = "unverified"
    last_outcome_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _optional_time(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _stored_time(value, field_name)


def _fingerprint(value: object) -> str:
    text = _bounded_text(value, "key fingerprint", maximum=12)
    if _FINGERPRINT_PATTERN.fullmatch(text) is None:
        raise RepositoryValidationError(
            "key fingerprint must be 12 lowercase hexadecimal characters"
        )
    return text


def _outcome(value: object) -> str:
    text = _bounded_text(value, "verification outcome", maximum=16)
    if text not in TYPESAFE_OUTCOMES:
        raise RepositoryValidationError(
            "verification outcome is not a declared value",
            metadata={"accepted": list(TYPESAFE_OUTCOMES)},
        )
    return text


def _aware_time(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise RepositoryValidationError(
            f"{field_name} must be a timezone-aware datetime"
        )
    return value


def _ciphertext_text(value: object) -> str:
    if value is None:
        raise RepositoryDataError(
            "persisted TypeSafe credential has no ciphertext",
            metadata={"field": "api_key_enc"},
        )
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("ascii")
        except UnicodeDecodeError as exc:
            raise RepositoryDataError(
                "persisted TypeSafe ciphertext is not an ASCII Fernet token",
                metadata={"field": "api_key_enc"},
            ) from exc
    return _bounded_text(value, "api key ciphertext", maximum=65_536)


def _typesafe_record(row: Mapping[str, Any]) -> EncryptedTypeSafeCredentialRecord:
    return EncryptedTypeSafeCredentialRecord(
        owner_id=str(_row_value(row, "user_id")),
        api_key_ciphertext=_ciphertext_text(_row_value(row, "api_key_enc")),
        key_fingerprint=_fingerprint(_row_value(row, "key_fingerprint")),
        last_verified_at=_optional_time(
            row.get("last_verified_at"), "last_verified_at"
        ),
        last_verification_outcome=_outcome(
            _row_value(row, "last_verification_outcome")
        ),
        last_outcome_at=_optional_time(row.get("last_outcome_at"), "last_outcome_at"),
        created_at=_stored_time(_row_value(row, "created_at"), "created_at"),
        updated_at=_stored_time(_row_value(row, "updated_at"), "updated_at"),
    )


class EncryptedTypeSafeCredentialRepository:
    _FIELDS = (
        "user_id, api_key_enc, key_fingerprint, last_verified_at, "
        "last_verification_outcome, last_outcome_at, created_at, updated_at"
    )

    def get_user(
        self,
        executor: QueryExecutor,
        *,
        owner_id: str,
    ) -> EncryptedTypeSafeCredentialRecord | None:
        owner = _required_id(owner_id, "owner id")
        row = executor.fetch_one(
            f"SELECT {self._FIELDS} FROM user_typesafe_credential WHERE user_id = %s",
            (owner,),
        )
        return None if row is None else _typesafe_record(row)

    def get_user_for_update(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
    ) -> EncryptedTypeSafeCredentialRecord | None:
        owner = _required_id(owner_id, "owner id")
        row = transaction.fetch_one(
            f"SELECT {self._FIELDS} FROM user_typesafe_credential "
            "WHERE user_id = %s FOR UPDATE",
            (owner,),
        )
        return None if row is None else _typesafe_record(row)

    def upsert_user(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        api_key_ciphertext: str,
        key_fingerprint: str,
        verified_at: datetime,
    ) -> EncryptedTypeSafeCredentialRecord:
        owner = _required_id(owner_id, "owner id")
        ciphertext = _bounded_text(
            api_key_ciphertext, "api key ciphertext", maximum=65_536
        )
        fingerprint = _fingerprint(key_fingerprint)
        verified = _aware_time(verified_at, "verified_at")
        result = transaction.execute(
            f"""
            INSERT INTO user_typesafe_credential (
                user_id, api_key_enc, key_fingerprint, last_verified_at,
                last_verification_outcome, last_outcome_at, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, 'valid', %s, clock_timestamp(), clock_timestamp()
            )
            ON CONFLICT (user_id) DO UPDATE SET
                api_key_enc = EXCLUDED.api_key_enc,
                key_fingerprint = EXCLUDED.key_fingerprint,
                last_verified_at = EXCLUDED.last_verified_at,
                last_verification_outcome = 'valid',
                last_outcome_at = EXCLUDED.last_outcome_at,
                updated_at = clock_timestamp()
            RETURNING {self._FIELDS}
            """,
            (owner, ciphertext.encode("ascii"), fingerprint, verified, verified),
        )
        return _typesafe_record(
            _single_returned(result, "upsert user TypeSafe credential")
        )

    def delete_user(self, transaction: Transaction, *, owner_id: str) -> bool:
        owner = _required_id(owner_id, "owner id")
        result = transaction.execute(
            "DELETE FROM user_typesafe_credential WHERE user_id = %s",
            (owner,),
        )
        if result.rowcount > 1:
            raise RepositoryDataError(
                "delete removed more than one owner-scoped credential",
                metadata={"operation": "delete user TypeSafe credential"},
            )
        return result.rowcount == 1

    # Scoped by fingerprint; a stale result can't clobber a new key
    def record_outcome(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        outcome: str,
        at: datetime,
        expected_fingerprint: str,
    ) -> bool:
        owner = _required_id(owner_id, "owner id")
        value = _outcome(outcome)
        if value == "unverified":
            raise RepositoryValidationError(
                "unverified is the initial state, not a recordable outcome"
            )
        observed_at = _aware_time(at, "at")
        fingerprint = _fingerprint(expected_fingerprint)
        result = transaction.execute(
            """
            UPDATE user_typesafe_credential SET
                last_verification_outcome = %s,
                last_outcome_at = %s,
                last_verified_at =
                    CASE WHEN %s = 'valid' THEN %s ELSE last_verified_at END,
                updated_at = clock_timestamp()
            WHERE user_id = %s AND key_fingerprint = %s
            """,
            (value, observed_at, value, observed_at, owner, fingerprint),
        )
        if result.rowcount > 1:
            raise RepositoryDataError(
                "outcome update touched more than one owner-scoped credential",
                metadata={"operation": "record TypeSafe verification outcome"},
            )
        return result.rowcount == 1


__all__ = (
    "TYPESAFE_OUTCOMES",
    "EncryptedLLMConfigRecord",
    "EncryptedLLMConfigRepository",
    "EncryptedTypeSafeCredentialRecord",
    "EncryptedTypeSafeCredentialRepository",
)
