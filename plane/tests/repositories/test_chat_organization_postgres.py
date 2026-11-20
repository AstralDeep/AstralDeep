"""Real-PostgreSQL tests for chat organization in the owner preferences document: owner
isolation, pruning of deleted conversations, persistence across transactions, and coexistence
with the other typed preference keys.
"""

from __future__ import annotations

import pytest

from astralplane.repositories import RepositoryNotFoundError
from astralplane.repositories.history import ConversationRepository
from astralplane.repositories.preferences import ChatOrganizationRecord, PreferencesRepository


def _chat(tx, owner: str, conversation: str) -> None:
    ConversationRepository().create(
        tx, conversation_id=conversation, owner_id=owner, title="t", agent_id=None, created_at=1
    )


def test_pin_and_archive_persist_per_owner_and_prune_deleted_chats(migrated_clone):
    db = migrated_clone.database
    preferences = PreferencesRepository()
    with db.transaction() as tx:
        _chat(tx, "alice", "a1")
        _chat(tx, "alice", "a2")
        _chat(tx, "bob", "b1")
        preferences.set_chat_phi_notice_enabled(tx, owner_id="alice", enabled=False)
        preferences.set_chat_flag(
            tx, owner_id="alice", conversation_id="a1", flag="pinned", value=True
        )
        preferences.set_chat_flag(
            tx, owner_id="alice", conversation_id="a2", flag="archived", value=True
        )
    with db.transaction() as tx:
        assert preferences.get_chat_organization(tx, owner_id="alice") == ChatOrganizationRecord(
            "alice", ("a1",), ("a2",)
        )
        assert preferences.get_chat_organization(tx, owner_id="bob") == ChatOrganizationRecord(
            "bob", (), ()
        )
        assert preferences.get_chat_phi_notice_enabled(tx, owner_id="alice") is False
        with pytest.raises(RepositoryNotFoundError):
            preferences.set_chat_flag(
                tx, owner_id="bob", conversation_id="a1", flag="pinned", value=True
            )
    with db.transaction() as tx:
        ConversationRepository().delete(tx, owner_id="alice", conversation_id="a1")
        record = preferences.set_chat_flag(
            tx, owner_id="alice", conversation_id="a2", flag="pinned", value=True
        )
    assert record == ChatOrganizationRecord("alice", ("a2",), ("a2",))
