"""Exercises chat and message rows derived from events against real PostgreSQL: idempotent
upserts keyed by event id, refusal when another event or owner holds the row, parent links
resolved through event ids, and the existing history reads seeing the derived rows.
"""

import pytest

from astralplane import create_derived_chat_repository
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.history import ConversationRepository, MessageRepository
from tests.fixtures.database_errors import INTEGRITY_ERRORS
from tests.repositories._mesh_support import OTHER, OWNER, ref

repo = create_derived_chat_repository()
CHAT_EVENT = ref(b"chat-created")
FIRST = ref(b"message-one")
SECOND = ref(b"message-two")


def chat(tx, owner=OWNER, event=CHAT_EVENT, chat_id=CHAT_EVENT, **changes):
    values = {"title": "Derived chat", "agent_id": None, "created_at": 10}
    values.update(changes)
    return repo.upsert_chat(tx, owner_id=owner, event_id=event, chat_id=chat_id, **values)


def message(tx, event=FIRST, owner=OWNER, chat_id=CHAT_EVENT, **changes):
    values = {"role": "user", "content": {"text": "hello"}, "timestamp": 11}
    values.update(changes)
    return repo.upsert_message(tx, owner_id=owner, chat_id=chat_id, event_id=event, **values)


def test_chat_upsert_is_idempotent_and_updates_mutable_fields(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        created = chat(tx)
        assert (created.chat_id, created.owner_id, created.event_id) == (
            CHAT_EVENT,
            OWNER,
            CHAT_EVENT,
        )
        assert chat(tx) == created
        renamed = chat(tx, title="Renamed", updated_at=50)
        assert (renamed.title, renamed.updated_at, renamed.created_at) == ("Renamed", 50, 10)
        stale = chat(tx, title="Renamed again", updated_at=20)
        assert (stale.title, stale.updated_at) == ("Renamed again", 50)
        assert repo.get_chat_by_event(tx, owner_id=OWNER, event_id=CHAT_EVENT) == stale
        assert repo.get_chat_by_event(tx, owner_id=OTHER, event_id=CHAT_EVENT) is None
        head = tx.fetch_one(
            "SELECT version FROM chat_branch_head WHERE chat_id = %s", (CHAT_EVENT,)
        )
        assert head["version"] == 0
        conversation = ConversationRepository().get(tx, owner_id=OWNER, conversation_id=CHAT_EVENT)
        assert conversation.title == "Renamed again"


def test_chat_upsert_refuses_foreign_and_unlinked_identities(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        with pytest.raises(RepositoryConflictError, match="different chat"):
            chat(tx, chat_id="another-chat")
        with pytest.raises(RepositoryConflictError, match="different chat"):
            chat(tx, owner=OTHER)
        ConversationRepository().create(
            tx,
            owner_id=OWNER,
            conversation_id="legacy-chat",
            title="Legacy",
            agent_id=None,
            created_at=1,
        )
        with pytest.raises(RepositoryConflictError, match="already exists"):
            chat(tx, event=ref(b"other-event"), chat_id="legacy-chat")
        with pytest.raises(RepositoryConflictError, match="already exists"):
            chat(tx, owner=OTHER, event=ref(b"other-event"), chat_id=CHAT_EVENT)


def test_message_upsert_is_idempotent_and_links_parents_by_event(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        first = message(tx)
        assert first.event_id == FIRST and first.parent_message_id is None
        assert first.content == {"text": "hello"} and first.role == "user"
        assert message(tx) == first
        second = message(tx, event=SECOND, role="assistant", parent_event_id=FIRST, timestamp=12)
        assert second.parent_message_id == first.message_id
        assert (
            message(tx, event=SECOND, role="assistant", parent_event_id=FIRST, timestamp=12)
            == second
        )
        assert repo.get_message_by_event(tx, owner_id=OWNER, event_id=SECOND) == second
        assert repo.get_message_by_event(tx, owner_id=OTHER, event_id=SECOND) is None
        count = tx.fetch_one("SELECT count(*) AS n FROM messages WHERE chat_id = %s", (CHAT_EVENT,))
        assert count["n"] == 2
        history = MessageRepository().get(
            tx, owner_id=OWNER, conversation_id=CHAT_EVENT, message_id=second.message_id
        )
        assert history.parent_message_id == first.message_id


def test_message_upsert_refuses_changed_replays_missing_parents_and_foreign_chats(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        message(tx)
        for changes in (
            {"content": {"text": "changed"}},
            {"role": "assistant"},
            {"timestamp": 99},
        ):
            with pytest.raises(RepositoryConflictError, match="different message"):
                message(tx, **changes)
        with pytest.raises(RepositoryConflictError, match="different message"):
            message(tx, owner=OTHER)
        with pytest.raises(RepositoryNotFoundError, match="parent"):
            message(tx, event=SECOND, parent_event_id=ref(b"never derived"))
        with pytest.raises(RepositoryNotFoundError, match="chat was not found"):
            message(tx, event=SECOND, chat_id="missing-chat")
        with pytest.raises(RepositoryNotFoundError, match="chat was not found"):
            message(tx, event=SECOND, owner=OTHER)
        with pytest.raises(RepositoryValidationError):
            message(tx, event="not-a-hash")
        with pytest.raises(RepositoryValidationError):
            message(tx, event=SECOND, role="")
        with pytest.raises(RepositoryValidationError):
            message(tx, event=SECOND, timestamp=-1)


def test_a_parent_from_another_owner_is_not_visible(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        message(tx)
        other_chat = ref(b"other-chat")
        chat(tx, owner=OTHER, event=other_chat, chat_id=other_chat)
        with pytest.raises(RepositoryNotFoundError, match="parent"):
            message(tx, event=SECOND, owner=OTHER, chat_id=other_chat, parent_event_id=FIRST)


def test_deleting_the_chat_removes_derived_messages(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        message(tx)
        assert ConversationRepository().delete(tx, owner_id=OWNER, conversation_id=CHAT_EVENT)
        assert repo.get_message_by_event(tx, owner_id=OWNER, event_id=FIRST) is None
        assert repo.get_chat_by_event(tx, owner_id=OWNER, event_id=CHAT_EVENT) is None
        assert chat(tx).chat_id == CHAT_EVENT


def test_database_enforces_the_event_link_shape_and_uniqueness(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        message(tx)
        message(tx, event=SECOND)
    with pytest.raises(INTEGRITY_ERRORS), migrated_clone.database.transaction() as tx:
        tx.execute("UPDATE messages SET event_id = 'sha256:short'")
    with pytest.raises(INTEGRITY_ERRORS), migrated_clone.database.transaction() as tx:
        tx.execute("UPDATE messages SET event_id = %s", (FIRST,))
    with pytest.raises(INTEGRITY_ERRORS), migrated_clone.database.transaction() as tx:
        tx.execute("UPDATE chats SET event_id = 'not a hash'")


def test_public_derived_chat_contract_behaviors():
    from tests.fixtures.migrated_template import bound_clone, current_lineages

    for check in (
        test_chat_upsert_is_idempotent_and_updates_mutable_fields,
        test_message_upsert_is_idempotent_and_links_parents_by_event,
        test_message_upsert_refuses_changed_replays_missing_parents_and_foreign_chats,
        test_a_parent_from_another_owner_is_not_visible,
    ):
        with bound_clone(current_lineages().current()) as clone:
            check(clone)
