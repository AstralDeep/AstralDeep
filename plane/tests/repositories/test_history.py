"""Tests for astralplane.repositories.history: conversation/message/session CRUD, owner
scoping, replay-safe append and rename, and session generation/refresh CAS, using a
fake transaction.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from astralplane.errors import PlaneError
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.history import (
    ConversationRepository,
    HistoryRepository,
    MessageRepository,
    SessionRecord,
    SessionRepository,
)


@dataclass(frozen=True)
class Result:
    rowcount: int = 1
    status_message: str | None = None
    returned_records: tuple[dict[str, Any], ...] = ()


class FakeTransaction:
    def __init__(self) -> None:
        self.execute_results: deque[Result] = deque()
        self.fetch_one_results: deque[dict[str, Any] | None] = deque()
        self.fetch_all_results: deque[tuple[dict[str, Any], ...]] = deque()
        self.calls: list[tuple[str, str, object]] = []

    def execute(self, statement: str, parameters: object = ()) -> Result:
        self.calls.append(("execute", statement, parameters))
        return self.execute_results.popleft() if self.execute_results else Result()

    def fetch_one(self, statement: str, parameters: object = ()) -> dict[str, Any] | None:
        self.calls.append(("fetch_one", statement, parameters))
        if "pg_advisory_xact_lock" in statement or "astralplane_blob_owner_state" in statement:
            return None
        return self.fetch_one_results.popleft() if self.fetch_one_results else None

    def fetch_all(self, statement: str, parameters: object = ()) -> tuple[dict[str, Any], ...]:
        self.calls.append(("fetch_all", statement, parameters))
        return self.fetch_all_results.popleft() if self.fetch_all_results else ()


def returned(row: dict[str, Any], *, rowcount: int = 1) -> Result:
    return Result(rowcount=rowcount, returned_records=(row,))


def conversation_row(**changes: Any) -> dict[str, Any]:
    row = {
        "id": "chat-1",
        "user_id": "owner-1",
        "title": "A chat",
        "agent_id": "agent-1",
        "created_at": 10,
        "updated_at": 10,
        "render_revision": 0,
        "conversation_commit_id": None,
        "has_saved_components": False,
        "snapshot_committed_at": None,
    }
    row.update(changes)
    return row


def message_row(**changes: Any) -> dict[str, Any]:
    row = {
        "id": 7,
        "chat_id": "chat-1",
        "user_id": "owner-1",
        "role": "user",
        "content": '{"nested":["value"]}',
        "timestamp": 20,
        "conversation_commit_id": None,
        "commit_position": None,
        "committed_render_revision": None,
    }
    row.update(changes)
    return row


INCARNATION = "12345678-1234-4234-8234-123456789abc"


def session_row(**changes: Any) -> dict[str, Any]:
    row = {
        "sid": "session-1",
        "incarnation_id": INCARNATION,
        "issuing_issuer": None,
        "issuing_client_id": None,
        "user_id": "owner-1",
        "access_token_enc": "cipher-a",
        "refresh_token_enc": "cipher-r",
        "interactive_anchor": 10,
        "hard_expires_at": 100,
        "last_refresh_at": 20,
        "resumed": False,
        "created_at": 5,
    }
    row.update(changes)
    return row


def test_conversation_create_is_owner_scoped_and_replay_safe() -> None:
    repository = ConversationRepository()
    transaction = FakeTransaction()
    transaction.execute_results.append(returned(conversation_row()))

    created = repository.create(
        transaction,
        conversation_id="chat-1",
        owner_id="owner-1",
        title="A chat",
        agent_id="agent-1",
        created_at=10,
    )

    assert created.conversation_id == "chat-1"
    assert transaction.calls[0][2][:3] == ("chat-1", "owner-1", "A chat")
    assert "%s" in transaction.calls[0][1]
    assert "?" not in transaction.calls[0][1]

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(conversation_row())
    assert (
        repository.create(
            replay,
            conversation_id="chat-1",
            owner_id="owner-1",
            title="A chat",
            agent_id="agent-1",
            created_at=10,
        )
        == created
    )


def test_conversation_get_can_lock_and_exposes_snapshot_authority_time() -> None:
    committed_at = datetime(2026, 8, 14, tzinfo=UTC)
    query = FakeTransaction()
    query.fetch_one_results.append(conversation_row(snapshot_committed_at=committed_at))

    record = ConversationRepository().get(
        query,
        owner_id="owner-1",
        conversation_id="chat-1",
        for_update=True,
    )

    assert record is not None and record.snapshot_committed_at == committed_at
    assert query.calls[0][1].rstrip().endswith("FOR UPDATE")


def test_conversation_administrative_get_is_explicit_and_can_lock() -> None:
    query = FakeTransaction()
    query.fetch_one_results.append(conversation_row(user_id="foreign-owner"))

    record = ConversationRepository().get_for_administration(
        query,
        conversation_id="chat-1",
        for_update=True,
    )

    assert record is not None and record.owner_id == "foreign-owner"
    statement, parameters = query.calls[0][1:]
    assert statement.rstrip().endswith("WHERE id = %s FOR UPDATE")
    assert parameters == ("chat-1",)


def test_conversation_create_rejects_foreign_and_changed_replay() -> None:
    repository = ConversationRepository()
    foreign = FakeTransaction()
    foreign.execute_results.append(Result(rowcount=0))
    foreign.fetch_one_results.append(None)
    with pytest.raises(RepositoryConflictError, match="another namespace"):
        repository.create(
            foreign,
            conversation_id="chat-1",
            owner_id="owner-1",
            title="A chat",
            agent_id=None,
            created_at=10,
        )

    changed = FakeTransaction()
    changed.execute_results.append(Result(rowcount=0))
    changed.fetch_one_results.append(conversation_row(title="Other", agent_id=None))
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.create(
            changed,
            conversation_id="chat-1",
            owner_id="owner-1",
            title="A chat",
            agent_id=None,
            created_at=10,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("conversation_id", ""), ("owner_id", " "), ("title", ""), ("created_at", -1)],
)
def test_conversation_create_validates_inputs(field: str, value: object) -> None:
    values: dict[str, object] = {
        "conversation_id": "chat-1",
        "owner_id": "owner-1",
        "title": "A chat",
        "agent_id": None,
        "created_at": 1,
    }
    values[field] = value
    with pytest.raises(RepositoryValidationError):
        ConversationRepository().create(FakeTransaction(), **values)  # type: ignore[arg-type]


def test_conversation_queries_rename_cas_and_delete() -> None:
    repository = ConversationRepository()
    transaction = FakeTransaction()
    transaction.fetch_one_results.append(conversation_row())
    assert (
        repository.get(transaction, owner_id="owner-1", conversation_id="chat-1").owner_id
        == "owner-1"
    )  # type: ignore[union-attr]
    assert transaction.calls[-1][2] == ("chat-1", "owner-1")

    transaction.fetch_all_results.append(
        (conversation_row(), conversation_row(id="chat-2", agent_id=None))
    )
    assert len(repository.list_recent(transaction, owner_id="owner-1", limit=2)) == 2

    transaction.execute_results.append(returned(conversation_row(title="Renamed", updated_at=11)))
    renamed = repository.rename(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        title="Renamed",
        expected_updated_at=10,
        updated_at=11,
    )
    assert renamed.title == "Renamed"

    transaction.execute_results.append(Result(rowcount=1))
    assert repository.delete(transaction, owner_id="owner-1", conversation_id="chat-1")
    assert "user_id = %s" in transaction.calls[-1][1]


def test_conversation_recent_nonempty_is_owner_visible_and_single_query() -> None:
    query = FakeTransaction()
    query.fetch_all_results.append(
        (
            conversation_row(
                id="chat-2",
                updated_at=20,
                latest_message_content='{"type":"Card","children":["hi"]}',
            ),
            conversation_row(
                latest_message_content="plain latest message",
            ),
        )
    )

    summaries = ConversationRepository().list_recent_nonempty(
        query,
        owner_id="owner-1",
        limit=2,
    )

    assert tuple(summary.conversation_id for summary in summaries) == (
        "chat-2",
        "chat-1",
    )
    assert summaries[0].latest_message_content["children"] == ("hi",)
    assert summaries[1].latest_message_content == "plain latest message"
    statement, parameters = query.calls[0][1:]
    assert parameters == ("owner-1", 2)
    assert "chat.id NOT LIKE 'draft-test-%%'" in statement
    assert "publication.state = 'committed'" in statement
    assert "publication.committed_render_revision" in statement
    assert "ORDER BY chat.updated_at DESC, chat.id DESC" in statement
    assert len(query.calls) == 1

    with pytest.raises(RepositoryValidationError):
        ConversationRepository().list_recent_nonempty(
            FakeTransaction(),
            owner_id="owner-1",
            limit=201,
        )


def test_conversation_rename_distinguishes_missing_and_conflict() -> None:
    repository = ConversationRepository()
    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.rename(
            missing,
            owner_id="owner-1",
            conversation_id="chat-1",
            title="Changed",
            expected_updated_at=10,
            updated_at=11,
        )

    conflict = FakeTransaction()
    conflict.execute_results.append(Result(rowcount=0))
    conflict.fetch_one_results.append(conversation_row(updated_at=12))
    with pytest.raises(RepositoryConflictError):
        repository.rename(
            conflict,
            owner_id="owner-1",
            conversation_id="chat-1",
            title="Changed",
            expected_updated_at=10,
            updated_at=11,
        )

    with pytest.raises(RepositoryValidationError, match="advance"):
        repository.rename(
            FakeTransaction(),
            owner_id="owner-1",
            conversation_id="chat-1",
            title="Changed",
            expected_updated_at=10,
            updated_at=10,
        )


def test_message_append_legacy_and_revisioned_paths_detach_content() -> None:
    repository = MessageRepository()
    legacy = FakeTransaction()
    legacy.execute_results.append(returned(message_row()))
    record = repository.append(
        legacy,
        owner_id="owner-1",
        conversation_id="chat-1",
        role="user",
        content={"nested": ["value"]},
        timestamp=20,
    )
    assert record.content["nested"] == ("value",)
    with pytest.raises(TypeError):
        record.content["new"] = "no"

    revisioned = FakeTransaction()
    revisioned.execute_results.append(
        returned(
            message_row(
                conversation_commit_id="commit-1",
                commit_position=0,
                committed_render_revision=1,
            )
        )
    )
    stored = repository.append(
        revisioned,
        owner_id="owner-1",
        conversation_id="chat-1",
        role="assistant",
        content="plain prose",
        timestamp=21,
        publication_id="commit-1",
        commit_position=0,
        committed_render_revision=1,
    )
    assert stored.content["nested"] == ("value",)
    assert "publication.owner_user_id = %s" in revisioned.calls[0][1]


@pytest.mark.parametrize("content", ("[]", "null", "7", "true", '{"key":"value"}'))
def test_message_append_canonically_encodes_json_looking_strings(content: str) -> None:
    repository = MessageRepository()
    transaction = FakeTransaction()
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    transaction.execute_results.append(returned(message_row(content=encoded)))

    record = repository.append(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        role="user",
        content=content,
        timestamp=20,
    )

    parameters = transaction.calls[0][2]
    assert isinstance(parameters, tuple)
    assert parameters[3] == encoded
    assert record.content == content


def test_message_revisioned_replay_and_conflicts_are_visible() -> None:
    repository = MessageRepository()
    replay_row = message_row(
        role="assistant",
        content="plain prose",
        timestamp=21,
        conversation_commit_id="commit-1",
        commit_position=1,
        committed_render_revision=1,
    )
    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(replay_row)
    record = repository.append(
        replay,
        owner_id="owner-1",
        conversation_id="chat-1",
        role="assistant",
        content="plain prose",
        timestamp=21,
        publication_id="commit-1",
        commit_position=1,
        committed_render_revision=1,
    )
    assert record.content == "plain prose"

    changed = FakeTransaction()
    changed.execute_results.append(Result(rowcount=0))
    changed.fetch_one_results.append(replay_row | {"role": "user"})
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.append(
            changed,
            owner_id="owner-1",
            conversation_id="chat-1",
            role="assistant",
            content="plain prose",
            timestamp=21,
            publication_id="commit-1",
            commit_position=1,
            committed_render_revision=1,
        )

    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.append(
            missing,
            owner_id="owner-1",
            conversation_id="chat-1",
            role="assistant",
            content="plain prose",
            timestamp=21,
            publication_id="commit-1",
            commit_position=1,
            committed_render_revision=1,
        )


def test_message_append_next_locks_stage_and_allocates_database_time() -> None:
    repository = MessageRepository()
    transaction = FakeTransaction()
    transaction.fetch_one_results.extend(
        (
            {
                "base_render_revision": 2,
                "state": "staged",
                "publication_role": "atomic",
                "chat_render_revision": 2,
            },
            {"next_position": 1},
            {"observed_at": 100},
            {"id": 8},
            {"id": 8},
        )
    )
    transaction.execute_results.append(
        returned(
            message_row(
                id=9,
                role="assistant",
                content='{"answer":true}',
                timestamp=101,
                conversation_commit_id="commit-1",
                commit_position=1,
                committed_render_revision=3,
                parent_message_id=8,
            )
        )
    )

    appended = repository.append_next_to_staged_publication(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="commit-1",
        role="assistant",
        content={"answer": True},
    )

    assert appended.message_id == 9 and appended.commit_position == 1
    assert appended.parent_message_id == 8
    assert "FOR UPDATE OF publication, chat" in transaction.calls[0][1]
    assert transaction.calls[-1][2][4:9] == (101, "commit-1", 1, 3, 8)


@pytest.mark.parametrize(
    ("staged", "error"),
    [
        (None, RepositoryNotFoundError),
        (
            {
                "base_render_revision": 2,
                "state": "committed",
                "publication_role": "atomic",
                "chat_render_revision": 2,
            },
            RepositoryConflictError,
        ),
        (
            {
                "base_render_revision": 2,
                "state": "staged",
                "publication_role": "atomic",
                "chat_render_revision": 3,
            },
            RepositoryConflictError,
        ),
    ],
)
def test_message_append_next_fails_closed_for_unavailable_stage(
    staged: dict[str, object] | None,
    error: type[Exception],
) -> None:
    transaction = FakeTransaction()
    transaction.fetch_one_results.append(staged)

    with pytest.raises(error):
        MessageRepository().append_next_to_staged_publication(
            transaction,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="commit-1",
            role="assistant",
            content="result",
            timestamp=100,
        )


def test_message_append_next_allows_voice_result_rebase() -> None:
    transaction = FakeTransaction()
    transaction.fetch_one_results.extend(
        (
            {
                "base_render_revision": 2,
                "state": "staged",
                "publication_role": "assistant_result",
                "chat_render_revision": 5,
            },
            {"next_position": 0},
        )
    )
    transaction.execute_results.append(
        returned(
            message_row(
                role="assistant",
                content="result",
                timestamp=100,
                conversation_commit_id="commit-1",
                commit_position=0,
                committed_render_revision=3,
            )
        )
    )

    assert MessageRepository().append_next_to_staged_publication(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="commit-1",
        role="assistant",
        content="result",
        timestamp=100,
    ).publication_id == "commit-1"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"commit_position": 1},
        {
            "publication_id": "commit-1",
            "commit_position": 1,
            "committed_render_revision": 0,
        },
    ],
)
def test_message_append_rejects_incomplete_publication_metadata(kwargs: dict[str, Any]) -> None:
    with pytest.raises(RepositoryValidationError):
        MessageRepository().append(
            FakeTransaction(),
            owner_id="owner-1",
            conversation_id="chat-1",
            role="user",
            content="text",
            timestamp=1,
            **kwargs,
        )


def test_message_legacy_missing_and_query_visibility() -> None:
    repository = MessageRepository()
    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    with pytest.raises(RepositoryNotFoundError):
        repository.append(
            missing,
            owner_id="owner-1",
            conversation_id="chat-1",
            role="user",
            content="text",
            timestamp=1,
        )

    query = FakeTransaction()
    query.fetch_all_results.append(
        (
            message_row(content="plain"),
            message_row(id=8, content="[1,2]", timestamp=21),
        )
    )
    rows = repository.list_visible(
        query,
        owner_id="owner-1",
        conversation_id="chat-1",
        through_render_revision=2,
        limit=20,
    )
    assert rows[0].content == "plain"
    assert rows[1].content == (1, 2)
    assert query.calls[-1][2][:2] == ("chat-1", "owner-1")
    assert "COALESCE(message.committed_render_revision, 0) ASC" in (
        query.calls[-1][1]
    )
    assert "ELSE message.commit_position::BIGINT" in query.calls[-1][1]

    publication_query = FakeTransaction()
    publication_query.fetch_all_results.append(
        (
            message_row(
                conversation_commit_id="commit-1",
                commit_position=0,
                committed_render_revision=1,
            ),
        )
    )
    publication_rows = repository.list_for_publication(
        publication_query,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="commit-1",
        limit=10,
    )
    assert publication_rows[0].publication_id == "commit-1"
    assert "JOIN conversation_commit" in publication_query.calls[0][1]
    assert publication_query.calls[0][2] == (
        "chat-1",
        "owner-1",
        "commit-1",
        10,
    )

    query.fetch_one_results.extend(({"id": 8}, None))
    assert repository.latest_visible_id(query, owner_id="owner-1", conversation_id="chat-1") == 8
    assert "COALESCE(message.committed_render_revision, 0) DESC" in (
        query.calls[-1][1]
    )
    assert repository.latest_visible_id(query, owner_id="owner-1", conversation_id="chat-1") is None

    exact = FakeTransaction()
    exact.fetch_one_results.extend((message_row(id=8), None))
    assert repository.get(
        exact,
        owner_id="owner-1",
        conversation_id="chat-1",
        message_id=8,
    ).message_id == 8  # type: ignore[union-attr]
    assert repository.get(
        exact,
        owner_id="owner-1",
        conversation_id="chat-1",
        message_id=9,
    ) is None
    assert "publication.state = 'committed'" in exact.calls[0][1]
    assert exact.calls[0][2] == (8, "chat-1", "owner-1")


def test_session_put_get_delete_and_owner_conflict() -> None:
    repository = SessionRepository()
    record = SessionRecord(
        session_id="session-1",
        owner_id="owner-1",
        access_token_ciphertext="cipher-a",
        refresh_token_ciphertext="cipher-r",
        interactive_anchor=10,
        hard_expires_at=100,
        last_refresh_at=20,
        resumed=False,
        created_at=5,
    )
    transaction = FakeTransaction()
    transaction.execute_results.append(returned(session_row()))
    assert repository.put(transaction, record) == replace(record, incarnation_id=INCARNATION)
    assert "ON CONFLICT (sid) DO NOTHING" in transaction.calls[2][1]

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(session_row())
    assert repository.put(replay, record) == replace(record, incarnation_id=INCARNATION)

    query = FakeTransaction()
    query.fetch_one_results.extend((session_row(resumed=True), None))
    assert repository.get(query, owner_id="owner-1", session_id="session-1").resumed
    assert repository.get(query, owner_id="owner-1", session_id="missing") is None

    transaction.execute_results.extend((Result(rowcount=1), Result(rowcount=0)))
    assert repository.delete(
        transaction, owner_id="owner-1", session_id="session-1", expected_incarnation_id=INCARNATION
    )
    assert not repository.delete(
        transaction, owner_id="owner-1", session_id="session-1", expected_incarnation_id=INCARNATION
    )

    conflict = FakeTransaction()
    conflict.execute_results.append(Result(rowcount=0))
    conflict.fetch_one_results.append(session_row(last_refresh_at=21))
    with pytest.raises(RepositoryConflictError):
        repository.put(conflict, record)


def test_session_delete_returns_only_the_exact_owner_scoped_final_record() -> None:
    repository = SessionRepository()
    tx = FakeTransaction()
    tx.fetch_one_results.extend((session_row(refresh_token_enc="latest-cipher"), None))
    deleted = repository.delete_and_return(
        tx, owner_id="owner-1", session_id="session-1", expected_incarnation_id=INCARNATION
    )
    assert deleted.refresh_token_ciphertext == "latest-cipher"
    assert "DELETE FROM web_session" in tx.calls[0][1]
    assert "RETURNING" in tx.calls[0][1]
    assert tx.calls[0][2] == ("session-1", "owner-1", INCARNATION)
    assert (
        repository.delete_and_return(
            tx, owner_id="owner-1", session_id="missing", expected_incarnation_id=INCARNATION
        )
        is None
    )
    with pytest.raises(RepositoryValidationError):
        repository.delete_and_return(
            FakeTransaction(),
            owner_id="",
            session_id="session-1",
            expected_incarnation_id=INCARNATION,
        )


def test_session_refresh_requires_an_exact_monotonic_generation() -> None:
    repository = SessionRepository()
    refreshed = SessionRecord(
        session_id="session-1",
        owner_id="owner-1",
        access_token_ciphertext="cipher-new-a",
        refresh_token_ciphertext="cipher-new-r",
        interactive_anchor=10,
        hard_expires_at=100,
        last_refresh_at=30,
        resumed=True,
        created_at=5,
        incarnation_id=INCARNATION,
    )
    transaction = FakeTransaction()
    transaction.execute_results.append(
        returned(
            session_row(
                access_token_enc="cipher-new-a",
                refresh_token_enc="cipher-new-r",
                hard_expires_at=100,
                last_refresh_at=30,
                resumed=True,
            )
        )
    )
    assert (
        repository.compare_and_set_refresh(transaction, refreshed, expected_last_refresh_at=20)
        == refreshed
    )
    assert "last_refresh_at = %s" in transaction.calls[0][1]
    assert transaction.calls[0][2][-4:] == (20, INCARNATION, None, None)

    stale = FakeTransaction()
    stale.execute_results.append(Result(rowcount=0))
    stale.fetch_one_results.append(session_row(last_refresh_at=25))
    with pytest.raises(RepositoryConflictError, match="stale"):
        repository.compare_and_set_refresh(stale, refreshed, expected_last_refresh_at=20)
    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.compare_and_set_refresh(missing, refreshed, expected_last_refresh_at=20)
    with pytest.raises(RepositoryValidationError, match="advance"):
        repository.compare_and_set_refresh(
            FakeTransaction(), refreshed, expected_last_refresh_at=30
        )


def test_session_administrative_reads_are_explicit_and_bounded_by_time() -> None:
    repository = SessionRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend((session_row(), None, session_row(), None))

    assert repository.get_by_session_id_for_administration(
        query, session_id="session-1"
    ) == SessionRecord(
        "session-1", "owner-1", "cipher-a", "cipher-r", 10, 100, 20, False, 5, INCARNATION
    )
    assert repository.get_by_session_id_for_administration(query, session_id="missing") is None
    latest = repository.get_latest_live_for_owner(query, owner_id="owner-1", observed_at=50)
    assert latest is not None and latest.session_id == "session-1"
    assert repository.get_latest_live_for_owner(query, owner_id="owner-2", observed_at=50) is None

    assert query.calls[0][2] == ("session-1",)
    assert "hard_expires_at > %s" in query.calls[2][1]
    assert query.calls[2][2] == ("owner-1", 50)
    assert "last_refresh_at DESC, created_at DESC, sid DESC" in query.calls[2][1]


def test_session_mark_resumed_uses_owner_compare_and_set_and_is_replay_safe() -> None:
    repository = SessionRepository()
    changed = FakeTransaction()
    changed.execute_results.append(returned(session_row(resumed=True)))

    record = repository.mark_resumed(
        changed,
        owner_id="owner-1",
        session_id="session-1",
        expected_incarnation_id=INCARNATION,
        expected_resumed=False,
        resumed=True,
    )
    assert record.resumed
    assert "user_id = %s AND resumed = %s" in changed.calls[0][1]
    assert changed.calls[0][2] == (True, "session-1", "owner-1", False, INCARNATION)

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(session_row(resumed=True))
    assert repository.mark_resumed(
        replay,
        owner_id="owner-1",
        session_id="session-1",
        expected_incarnation_id=INCARNATION,
        expected_resumed=False,
        resumed=True,
    ).resumed

    stale = FakeTransaction()
    stale.execute_results.append(Result(rowcount=0))
    stale.fetch_one_results.append(session_row(resumed=False))
    with pytest.raises(RepositoryConflictError, match="compare-and-set"):
        repository.mark_resumed(
            stale,
            owner_id="owner-1",
            session_id="session-1",
            expected_incarnation_id=INCARNATION,
            expected_resumed=False,
            resumed=True,
        )

    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.mark_resumed(
            missing,
            owner_id="owner-1",
            session_id="session-1",
            expected_incarnation_id=INCARNATION,
            expected_resumed=False,
            resumed=True,
        )

    with pytest.raises(RepositoryValidationError, match="booleans"):
        repository.mark_resumed(
            FakeTransaction(),
            owner_id="owner-1",
            session_id="session-1",
            expected_incarnation_id=INCARNATION,
            expected_resumed=0,  # type: ignore[arg-type]
            resumed=True,
        )
    with pytest.raises(RepositoryValidationError, match="differ"):
        repository.mark_resumed(
            FakeTransaction(),
            owner_id="owner-1",
            session_id="session-1",
            expected_incarnation_id=INCARNATION,
            expected_resumed=True,
            resumed=True,
        )


def test_session_administrative_deletes_validate_driver_counts() -> None:
    repository = SessionRepository()
    transaction = FakeTransaction()
    transaction.execute_results.extend((Result(rowcount=3), Result(rowcount=2)))

    assert repository.delete_owner(transaction, owner_id="owner-1") == 3
    assert repository.delete_expired_for_administration(transaction, observed_at=100) == 2
    assert transaction.calls[0][2] == ("owner-1",)
    assert transaction.calls[1][2] == (100,)

    for operation in (
        lambda tx: repository.delete_owner(tx, owner_id="owner-1"),
        lambda tx: repository.delete_expired_for_administration(tx, observed_at=100),
    ):
        invalid = FakeTransaction()
        invalid.execute_results.append(Result(rowcount=-1))
        with pytest.raises(PlaneError, match="invalid row count"):
            operation(invalid)


@pytest.mark.parametrize(
    "record",
    [
        SessionRecord("", "owner", "a", "r", 1, 2, 1, False, 1),
        SessionRecord("sid", "owner", "", "r", 1, 2, 1, False, 1),
        SessionRecord("sid", "owner", "a", "r", -1, 2, 1, False, 1),
    ],
)
def test_session_validation(record: SessionRecord) -> None:
    with pytest.raises(RepositoryValidationError):
        SessionRepository().put(FakeTransaction(), record)


def test_history_facade_exposes_stateless_repositories() -> None:
    facade = HistoryRepository()
    assert isinstance(facade.conversations, ConversationRepository)
    assert isinstance(facade.messages, MessageRepository)
    assert isinstance(facade.sessions, SessionRepository)
