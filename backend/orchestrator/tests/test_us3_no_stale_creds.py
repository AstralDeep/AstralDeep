"""Regression tests for orchestrator/credential_manager.py: every read hits the database
with no in-process cache that could serve a stale value after save/clear, plus
per-user isolation and internal-key filtering.
"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from astralplane.repositories.credentials import CredentialRecord

from orchestrator.credential_manager import CredentialManager


class FakeCredentialRepository:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], CredentialRecord] = {}
        self.sequence = 0

    def upsert_credential(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
        credential_key,
        encrypted_value,
        updated_at,
    ):
        key = (owner_id, agent_id, credential_key)
        prior = self.rows.get(key)
        if prior is None:
            self.sequence += 1
        record = CredentialRecord(
            credential_id=(
                self.sequence if prior is None else prior.credential_id
            ),
            owner_id=owner_id,
            agent_id=agent_id,
            credential_key=credential_key,
            encrypted_value=encrypted_value,
            created_at=updated_at if prior is None else prior.created_at,
            updated_at=updated_at,
        )
        self.rows[key] = record
        return record

    def get_credential(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
        credential_key,
    ):
        return self.rows.get((owner_id, agent_id, credential_key))

    def list_credentials(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
        limit,
    ):
        return tuple(
            record
            for key, record in sorted(self.rows.items())
            if key[:2] == (owner_id, agent_id)
        )[:limit]

    def list_credential_keys(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
        limit,
    ):
        return tuple(
            record.credential_key
            for record in self.list_credentials(
                _transaction,
                owner_id=owner_id,
                agent_id=agent_id,
                limit=limit,
            )
        )

    def delete_credential(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
        credential_key,
    ):
        return (
            self.rows.pop((owner_id, agent_id, credential_key), None)
            is not None
        )

    def delete_agent_credentials(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
    ):
        keys = [
            key for key in self.rows if key[:2] == (owner_id, agent_id)
        ]
        for key in keys:
            del self.rows[key]
        return len(keys)


class FakePlaneRuntime:
    def __init__(self, credentials: FakeCredentialRepository) -> None:
        self.repositories = SimpleNamespace(credentials=credentials)

    @contextmanager
    def transaction(self):
        yield object()


@pytest.fixture
def cm(monkeypatch) -> CredentialManager:
    monkeypatch.setenv(
        "CREDENTIAL_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    repository = FakeCredentialRepository()
    runtime = FakePlaneRuntime(repository)
    return CredentialManager(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
        plane_repository=repository,
    )


def test_save_then_save_returns_latest_value(cm: CredentialManager) -> None:
    cm.set_credential("alice", "classify-1", "CLASSIFY_URL", "https://first.example/")
    first = cm.get_agent_credentials_encrypted("alice", "classify-1")
    assert cm.get_credential("alice", "classify-1", "CLASSIFY_URL") == (
        "https://first.example/"
    )
    assert "first.example" not in first["CLASSIFY_URL"]

    cm.set_credential("alice", "classify-1", "CLASSIFY_URL", "https://second.example/")
    second = cm.get_agent_credentials_encrypted("alice", "classify-1")
    assert first["CLASSIFY_URL"] != second["CLASSIFY_URL"]
    assert cm.get_credential("alice", "classify-1", "CLASSIFY_URL") == (
        "https://second.example/"
    )
    assert "second.example" not in second["CLASSIFY_URL"]


def test_delete_then_list_returns_empty(cm: CredentialManager) -> None:
    cm.set_credential("alice", "classify-1", "CLASSIFY_API_KEY", "secret")
    assert cm.list_credential_keys("alice", "classify-1") == ["CLASSIFY_API_KEY"]
    cm.delete_credential("alice", "classify-1", "CLASSIFY_API_KEY")
    assert cm.list_credential_keys("alice", "classify-1") == []


def test_remove_agent_credentials_clears_all_keys(cm: CredentialManager) -> None:
    cm.set_credential("alice", "classify-1", "CLASSIFY_URL", "u")
    cm.set_credential("alice", "classify-1", "CLASSIFY_API_KEY", "k")
    assert sorted(cm.list_credential_keys("alice", "classify-1")) == ["CLASSIFY_API_KEY", "CLASSIFY_URL"]
    cm.remove_agent_credentials("alice", "classify-1")
    assert cm.list_credential_keys("alice", "classify-1") == []


def test_user_isolation(cm: CredentialManager) -> None:
    cm.set_credential("alice", "classify-1", "CLASSIFY_API_KEY", "alice-key")
    assert cm.list_credential_keys("alice", "classify-1") == ["CLASSIFY_API_KEY"]
    assert cm.list_credential_keys("bob", "classify-1") == []
    cm.set_credential("bob", "classify-1", "CLASSIFY_API_KEY", "bob-key")
    alice_creds = cm.get_agent_credentials_encrypted("alice", "classify-1")
    bob_creds = cm.get_agent_credentials_encrypted("bob", "classify-1")
    assert alice_creds != bob_creds
    assert cm.get_credential("alice", "classify-1", "CLASSIFY_API_KEY") == (
        "alice-key"
    )
    assert cm.get_credential("bob", "classify-1", "CLASSIFY_API_KEY") == (
        "bob-key"
    )
    assert "alice-key" not in alice_creds["CLASSIFY_API_KEY"]
    assert "bob-key" not in bob_creds["CLASSIFY_API_KEY"]


def test_internal_keys_filtered_out_of_listing(cm: CredentialManager) -> None:
    cm.set_credential("alice", "classify-1", "PUBLIC_KEY", "v")
    cm.set_credential("alice", "classify-1", "_INTERNAL", "v")
    cm.list_credential_keys("alice", "classify-1")
    creds = cm.get_agent_credentials_encrypted("alice", "classify-1")
    assert "PUBLIC_KEY" in creds
    assert "_INTERNAL" not in creds
