"""Shared fixtures for the llm_config test suite: in-memory Plane repository doubles for
the encrypted LLM/TypeSafe/data-sharing tables, a generated-per-test Fernet key, and
store/recorder fixtures built on them.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from astralplane.repositories import RepositoryNotFoundError
from astralplane.repositories.preferences import DataSharingAcknowledgmentRecord
from astralplane.repositories.secrets import (
    EncryptedLLMConfigRecord,
    EncryptedTypeSafeCredentialRecord,
)
from cryptography.fernet import Fernet

from llm_config.user_store import UserLLMConfigStore


def _stored_time(value: object | None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if value is None:
        return datetime.now(UTC)
    return datetime.fromtimestamp(float(value), UTC)


class InMemoryEncryptedLLMConfigRepository:
    def __init__(self, storage: "CredentialPlaneFixture") -> None:
        self._storage = storage

    @staticmethod
    def _user_record(
        owner_id: str,
        row: dict[str, Any],
    ) -> EncryptedLLMConfigRecord:
        updated_at = _stored_time(row.get("updated_at"))
        return EncryptedLLMConfigRecord(
            scope="user",
            owner_id=owner_id,
            provider=str(row.get("provider") or "custom"),
            base_url=str(row.get("base_url") or ""),
            model=str(row.get("model") or ""),
            api_key_ciphertext=row.get("api_key_enc"),
            updated_by=None,
            created_at=_stored_time(row.get("created_at") or updated_at),
            updated_at=updated_at,
        )

    @staticmethod
    def _system_record(row: dict[str, Any]) -> EncryptedLLMConfigRecord:
        updated_at = _stored_time(row.get("updated_at"))
        return EncryptedLLMConfigRecord(
            scope="system",
            owner_id=None,
            provider=str(row.get("provider") or "custom"),
            base_url=str(row.get("base_url") or ""),
            model=str(row.get("model") or ""),
            api_key_ciphertext=row.get("api_key_enc"),
            updated_by=str(row.get("updated_by") or ""),
            created_at=_stored_time(row.get("created_at") or updated_at),
            updated_at=updated_at,
        )

    def get_user(
        self,
        _executor: object,
        *,
        owner_id: str,
    ) -> EncryptedLLMConfigRecord | None:
        row = self._storage.users.get(owner_id)
        return None if row is None else self._user_record(owner_id, row)

    def upsert_user(
        self,
        _transaction: object,
        *,
        owner_id: str,
        provider: str,
        base_url: str,
        model: str,
        api_key_ciphertext: str | None,
    ) -> EncryptedLLMConfigRecord:
        now = datetime.now(UTC)
        existing = self._storage.users.get(owner_id)
        self._storage.users[owner_id] = {
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "api_key_enc": api_key_ciphertext,
            "created_at": (
                existing.get("created_at", now) if existing is not None else now
            ),
            "updated_at": now,
        }
        return self._user_record(owner_id, self._storage.users[owner_id])

    def upsert_user_before_deadline(
        self,
        transaction: object,
        *,
        deadline_at: datetime,
        **values: object,
    ) -> EncryptedLLMConfigRecord | None:
        if datetime.now(UTC) >= deadline_at:
            return None
        return self.upsert_user(transaction, **values)

    def delete_user(self, _transaction: object, *, owner_id: str) -> None:
        if self._storage.users.pop(owner_id, None) is None:
            raise RepositoryNotFoundError(
                "owner-scoped LLM configuration was not found"
            )

    def get_system(
        self,
        _executor: object,
    ) -> EncryptedLLMConfigRecord | None:
        row = self._storage.system
        return None if row is None else self._system_record(row)

    def upsert_system(
        self,
        _transaction: object,
        *,
        updated_by: str,
        provider: str,
        base_url: str,
        model: str,
        api_key_ciphertext: str | None,
    ) -> EncryptedLLMConfigRecord:
        now = datetime.now(UTC)
        existing = self._storage.system
        self._storage.system = {
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "api_key_enc": api_key_ciphertext,
            "updated_by": updated_by,
            "created_at": (
                existing.get("created_at", now) if existing is not None else now
            ),
            "updated_at": now,
        }
        return self._system_record(self._storage.system)

    def delete_system(self, _transaction: object) -> None:
        if self._storage.system is None:
            raise RepositoryNotFoundError("system LLM configuration was not found")
        self._storage.system = None



class InMemoryTypeSafeCredentialRepository:
    def __init__(self, storage: "CredentialPlaneFixture") -> None:
        self._storage = storage

    @staticmethod
    def _record(owner_id: str, row: dict[str, Any]) -> EncryptedTypeSafeCredentialRecord:
        return EncryptedTypeSafeCredentialRecord(
            owner_id=owner_id,
            api_key_ciphertext=row["api_key_enc"],
            key_fingerprint=row["key_fingerprint"],
            last_verified_at=row.get("last_verified_at"),
            last_verification_outcome=row.get("last_verification_outcome", "unverified"),
            last_outcome_at=row.get("last_outcome_at"),
            created_at=_stored_time(row.get("created_at")),
            updated_at=_stored_time(row.get("updated_at")),
        )

    def get_user(
        self,
        _executor: object,
        *,
        owner_id: str,
    ) -> EncryptedTypeSafeCredentialRecord | None:
        row = self._storage.typesafe.get(owner_id)
        return None if row is None else self._record(owner_id, row)

    def get_user_for_update(
        self,
        transaction: object,
        *,
        owner_id: str,
    ) -> EncryptedTypeSafeCredentialRecord | None:
        return self.get_user(transaction, owner_id=owner_id)

    def upsert_user(
        self,
        _transaction: object,
        *,
        owner_id: str,
        api_key_ciphertext: str,
        key_fingerprint: str,
        verified_at: datetime,
    ) -> EncryptedTypeSafeCredentialRecord:
        now = datetime.now(UTC)
        existing = self._storage.typesafe.get(owner_id)
        self._storage.typesafe[owner_id] = {
            "api_key_enc": api_key_ciphertext,
            "key_fingerprint": key_fingerprint,
            "last_verified_at": verified_at,
            "last_verification_outcome": "valid",
            "last_outcome_at": verified_at,
            "created_at": (
                existing.get("created_at", now) if existing is not None else now
            ),
            "updated_at": now,
        }
        return self._record(owner_id, self._storage.typesafe[owner_id])

    def delete_user(self, _transaction: object, *, owner_id: str) -> bool:
        return self._storage.typesafe.pop(owner_id, None) is not None

    def record_outcome(
        self,
        _transaction: object,
        *,
        owner_id: str,
        outcome: str,
        at: datetime,
        expected_fingerprint: str,
    ) -> bool:
        row = self._storage.typesafe.get(owner_id)
        if row is None or row["key_fingerprint"] != expected_fingerprint:
            return False
        row["last_verification_outcome"] = outcome
        row["last_outcome_at"] = at
        if outcome == "valid":
            row["last_verified_at"] = at
        row["updated_at"] = datetime.now(UTC)
        return True



class InMemoryDataSharingRepository:
    def __init__(self, storage: "CredentialPlaneFixture") -> None:
        self._storage = storage

    def get_user(self, _executor: object, *, owner_id: str):
        row = self._storage.acknowledgments.get(owner_id)
        if row is None:
            return None
        return DataSharingAcknowledgmentRecord(
            owner_id=owner_id,
            notice_version=row["notice_version"],
            acknowledged_at=row["acknowledged_at"],
            first_acknowledged_at=row["first_acknowledged_at"],
        )

    def acknowledge(
        self,
        _transaction: object,
        *,
        owner_id: str,
        notice_version: str,
        at: datetime,
    ):
        existing = self._storage.acknowledgments.get(owner_id)
        first = existing["first_acknowledged_at"] if existing else at
        self._storage.acknowledgments[owner_id] = {
            "notice_version": notice_version,
            "acknowledged_at": max(at, first),
            "first_acknowledged_at": first,
        }
        return self.get_user(None, owner_id=owner_id)

    def has_acknowledged(
        self, executor: object, *, owner_id: str, notice_version: str
    ) -> bool:
        record = self.get_user(executor, owner_id=owner_id)
        return record is not None and record.notice_version == notice_version


class CredentialPlaneFixture:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.system: dict[str, Any] | None = None
        self.typesafe: dict[str, dict[str, Any]] = {}
        self.acknowledgments: dict[str, dict[str, Any]] = {}
        repository = InMemoryEncryptedLLMConfigRepository(self)
        self.repositories = SimpleNamespace(
            encrypted_llm_config=repository,
            encrypted_typesafe_credential=InMemoryTypeSafeCredentialRepository(self),
            preferences=SimpleNamespace(
                data_sharing=InMemoryDataSharingRepository(self)
            ),
        )
        self.plane_runtime = self
        self.plane_repositories = self.repositories

    @contextmanager
    def transaction(self, *, isolation: object = None):
        del isolation
        yield object()


@pytest.fixture
def fernet_key(monkeypatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
    return key


@pytest.fixture
def credential_plane() -> CredentialPlaneFixture:
    return CredentialPlaneFixture()


@pytest.fixture
def fake_db(credential_plane) -> CredentialPlaneFixture:
    return credential_plane


@pytest.fixture
def store(fernet_key, credential_plane) -> UserLLMConfigStore:
    return UserLLMConfigStore(
        plane_runtime=credential_plane,
        plane_repositories=credential_plane.repositories,
    )


@pytest.fixture
def typesafe_store(fernet_key, credential_plane):
    from llm_config.typesafe_store import TypeSafeCredentialStore

    return TypeSafeCredentialStore(
        plane_runtime=credential_plane,
        plane_repositories=credential_plane.repositories,
    )


@pytest.fixture
def data_sharing_store(credential_plane):
    from llm_config.data_sharing import DataSharingStore

    return DataSharingStore(
        plane_runtime=credential_plane,
        plane_repositories=credential_plane.repositories,
    )


@pytest.fixture
def fake_recorder():
    rec = MagicMock()
    rec.record = AsyncMock()
    return rec


@pytest.fixture
def safe_send():
    return AsyncMock()
