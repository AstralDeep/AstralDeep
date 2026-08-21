"""Shared fixtures for the llm_config test suite (feature 054-byo-llm-setup).

The persisted store consumes the application Plane runtime and its typed
``encrypted_llm_config`` repository.  These tests use a narrow in-memory
implementation of that exact repository contract; no SQL or retired Deep
database facade is present in the fixture.

``CREDENTIAL_ENCRYPTION_KEY`` is monkeypatched to a per-test generated
Fernet key so no dev key file is ever written by the suite.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from astralplane.repositories import RepositoryNotFoundError
from astralplane.repositories.secrets import EncryptedLLMConfigRecord
from cryptography.fernet import Fernet

from llm_config.user_store import UserLLMConfigStore


def _stored_time(value: object | None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if value is None:
        return datetime.now(UTC)
    return datetime.fromtimestamp(float(value), UTC)


class InMemoryEncryptedLLMConfigRepository:
    """Typed Plane repository double with inspectable encrypted state."""

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


class CredentialPlaneFixture:
    """Minimal application Plane runtime/catalog for credential-store tests."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.system: dict[str, Any] | None = None
        repository = InMemoryEncryptedLLMConfigRepository(self)
        self.repositories = SimpleNamespace(encrypted_llm_config=repository)
        self.plane_runtime = self
        self.plane_repositories = self.repositories

    @contextmanager
    def transaction(self, *, isolation: object = None):
        del isolation
        yield object()


@pytest.fixture
def fernet_key(monkeypatch) -> str:
    """Set CREDENTIAL_ENCRYPTION_KEY to a fresh Fernet key (no key file)."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
    return key


@pytest.fixture
def credential_plane() -> CredentialPlaneFixture:
    return CredentialPlaneFixture()


@pytest.fixture
def fake_db(credential_plane) -> CredentialPlaneFixture:
    """Inspectable ciphertext state retained for existing security assertions."""

    return credential_plane


@pytest.fixture
def store(fernet_key, credential_plane) -> UserLLMConfigStore:
    return UserLLMConfigStore(
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
