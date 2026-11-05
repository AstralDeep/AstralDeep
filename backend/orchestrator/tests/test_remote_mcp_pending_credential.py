"""Verifies one-use encrypted credential consumption for cross-worker MCP OAuth state.
The fake Plane context preserves the repository transaction and deletion contract.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from orchestrator.credential_manager import CredentialManager, CredentialUndecryptable


class _Repository:
    def __init__(self):
        self.rows = {}
        self.delete_succeeds = True

    def get_credential(self, _tx, *, owner_id, agent_id, credential_key):
        value = self.rows.get((owner_id, agent_id, credential_key))
        return SimpleNamespace(encrypted_value=value) if value is not None else None

    def delete_credential(self, _tx, *, owner_id, agent_id, credential_key):
        if not self.delete_succeeds:
            return False
        return self.rows.pop((owner_id, agent_id, credential_key), None) is not None


class _Context:
    def __init__(self, repository):
        self.repository = repository

    @contextmanager
    def transaction(self):
        yield object()


def _manager():
    manager = object.__new__(CredentialManager)
    manager._fernet = Fernet(Fernet.generate_key())
    repository = _Repository()
    manager._credentials = _Context(repository)
    return manager, repository


def test_pending_credential_is_decrypted_once_for_same_owner():
    manager, repository = _manager()
    repository.rows[("owner", "pending", "state")] = manager._fernet.encrypt(b"verifier").decode()

    assert manager.take_credential("other", "pending", "state") is None
    assert manager.take_credential("owner", "pending", "state") == "verifier"
    assert manager.take_credential("owner", "pending", "state") is None


def test_pending_credential_requires_successful_delete():
    manager, repository = _manager()
    repository.rows[("owner", "pending", "state")] = manager._fernet.encrypt(b"verifier").decode()
    repository.delete_succeeds = False
    assert manager.take_credential("owner", "pending", "state") is None


@pytest.mark.parametrize("value", ["e2e:abc", "corrupt"])
def test_pending_credential_rejects_unreadable_ciphertext(value):
    manager, repository = _manager()
    repository.rows[("owner", "pending", "state")] = value
    with pytest.raises(CredentialUndecryptable):
        manager.take_credential("owner", "pending", "state")


def test_pending_credential_rejects_authenticated_non_utf8_plaintext():
    manager, repository = _manager()
    repository.rows[("owner", "pending", "state")] = manager._fernet.encrypt(b"\xff").decode()

    with pytest.raises(CredentialUndecryptable):
        manager.take_credential("owner", "pending", "state")


def test_transient_state_is_authenticated_and_opaque():
    manager, _ = _manager()
    sealed = manager.seal_transient_state('{"owner_id":"owner"}')
    assert "owner" not in sealed
    assert manager.open_transient_state(sealed, ttl_seconds=600) == '{"owner_id":"owner"}'
    altered = sealed[:20] + ("A" if sealed[20] != "A" else "B") + sealed[21:]
    assert manager.open_transient_state(altered, ttl_seconds=600) is None
