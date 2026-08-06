"""Feature-066 per-chat index contracts (P1/P2).

``messages`` shipped with only ``idx_messages_user_id`` — one user's ENTIRE
history across every chat — so the per-chat hot paths (transcript load,
latest-id turn marker, recent-chats preview subquery, auto-title COUNT) had no
usable index. ``message_attachment`` had no index on ``message_id`` at all.

These tests pin that both indexes exist on a fresh database, that they also
land when an already-deployed database is upgraded from the one approved
predecessor (an index added without a SCHEMA_REVISION bump would be skipped by
the fast path and silently never apply), and that repeat and forced re-runs
leave the index set unchanged.

Databases are created through the shared feature-060 isolated-database
fixture; the configured product database is never touched.
"""

from __future__ import annotations

from typing import Callable

import pytest

psycopg2 = pytest.importorskip("psycopg2")

pytest_plugins = ("tests.test_migrations_060",)

from shared.database import (  # noqa: E402
    Database,
    SCHEMA_PREDECESSOR_REVISION,
    SCHEMA_REVISION,
)
from tests.test_migrations_060 import (  # noqa: E402
    _IsolatedDatabase,
    _fetch_all,
    _fetch_one,
)

MESSAGES_CHAT_INDEX = "idx_messages_chat_user_ts"
MESSAGE_ATTACHMENT_MESSAGE_INDEX = "idx_message_attachment_message"


@pytest.fixture(autouse=True)
def _direct_connections_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_DISABLE", "1")


def _index_defs(sandbox: _IsolatedDatabase, table: str) -> dict[str, str]:
    rows = _fetch_all(
        sandbox,
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s",
        (table,),
    )
    return {row["indexname"]: row["indexdef"] for row in rows}


def _seed_deployed_predecessor(sandbox: _IsolatedDatabase) -> None:
    """A database on the approved predecessor, without the 066 indexes."""
    database = Database.__new__(Database)
    database.database_url = sandbox.dsn
    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        database._apply_full_schema(connection, cursor)
        cursor.execute(f"DROP INDEX {MESSAGES_CHAT_INDEX}")
        cursor.execute(f"DROP INDEX {MESSAGE_ATTACHMENT_MESSAGE_INDEX}")
        cursor.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('revision', %s)",
            (SCHEMA_PREDECESSOR_REVISION,),
        )
        connection.commit()


def test_fresh_database_indexes_the_per_chat_message_paths(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    Database(sandbox.dsn)

    indexes = _index_defs(sandbox, "messages")
    assert MESSAGES_CHAT_INDEX in indexes
    assert indexes[MESSAGES_CHAT_INDEX].endswith(
        "(chat_id, user_id, \"timestamp\", id)"
    ), indexes[MESSAGES_CHAT_INDEX]
    # The pre-existing single-column index stays: migrate_user_ids.py recreates
    # it and the per-user teardown deletes in the suite rely on it.
    assert "idx_messages_user_id" in indexes


def test_fresh_database_indexes_the_per_message_attachment_reads(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    Database(sandbox.dsn)

    indexes = _index_defs(sandbox, "message_attachment")
    assert MESSAGE_ATTACHMENT_MESSAGE_INDEX in indexes
    assert indexes[MESSAGE_ATTACHMENT_MESSAGE_INDEX].endswith(
        "(message_id, user_id)"
    ), indexes[MESSAGE_ATTACHMENT_MESSAGE_INDEX]
    # The chat-scoped index that serves list_for_chat must survive.
    assert "idx_message_attachment_chat" in indexes
    assert "idx_message_attachment_att" in indexes


def test_deployed_predecessor_upgrade_creates_both_indexes(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    _seed_deployed_predecessor(sandbox)
    assert MESSAGES_CHAT_INDEX not in _index_defs(sandbox, "messages")

    Database(sandbox.dsn)

    assert MESSAGES_CHAT_INDEX in _index_defs(sandbox, "messages")
    assert MESSAGE_ATTACHMENT_MESSAGE_INDEX in _index_defs(
        sandbox, "message_attachment"
    )
    assert (
        _fetch_one(sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'")[
            "value"
        ]
        == SCHEMA_REVISION
    )


def test_repeat_and_forced_runs_leave_the_index_set_unchanged(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    Database(sandbox.dsn)

    def snapshot() -> dict[str, dict[str, str]]:
        return {
            table: _index_defs(sandbox, table)
            for table in ("messages", "message_attachment")
        }

    expected = snapshot()
    Database(sandbox.dsn)
    assert snapshot() == expected

    # Forced full re-run (the documented rollback) must also be repeat-safe.
    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM schema_meta WHERE key = 'revision'")
        connection.commit()
    Database(sandbox.dsn)
    assert snapshot() == expected
    assert (
        _fetch_one(sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'")[
            "value"
        ]
        == SCHEMA_REVISION
    )


def test_per_chat_message_queries_use_the_composite_index(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    """The index shape must actually serve the hot predicates, not just exist."""
    sandbox = isolated_database_factory()
    Database(sandbox.dsn)

    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
            "SELECT 'chat-' || n, 'user-' || (n % 5), 't', n, n "
            "FROM generate_series(1, 400) AS n"
        )
        cursor.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
            "SELECT 'chat-' || n, 'user-' || (n % 5), 'user', 'c', t "
            "FROM generate_series(1, 400) AS n, generate_series(1, 20) AS t"
        )
        cursor.execute("ANALYZE messages")
        connection.commit()

    def plan(query: str) -> str:
        return "\n".join(
            row["QUERY PLAN"]
            for row in _fetch_all(sandbox, "EXPLAIN " + query, ("chat-7", "user-2"))
        )

    count_plan = plan(
        "SELECT COUNT(*) FROM messages WHERE chat_id = %s AND user_id = %s"
    )
    assert MESSAGES_CHAT_INDEX in count_plan, count_plan

    transcript_plan = plan(
        "SELECT * FROM messages WHERE chat_id = %s AND user_id = %s "
        "ORDER BY timestamp ASC, id ASC"
    )
    assert MESSAGES_CHAT_INDEX in transcript_plan, transcript_plan

    marker_plan = plan(
        "SELECT id FROM messages WHERE chat_id = %s AND user_id = %s "
        "ORDER BY id DESC LIMIT 1"
    )
    assert MESSAGES_CHAT_INDEX in marker_plan, marker_plan


def test_list_for_chat_bulk_read_matches_per_message_reads_on_real_rows(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    """The chat-scoped bulk read replaces the per-message attachment N+1.

    Against real Postgres, where ``messages.id`` is an integer PK and
    ``message_attachment.message_id`` is TEXT — a grouping keyed on the wrong
    type would match nothing here but pass against an in-memory fake.
    """
    from orchestrator.attachments.message_attachment_repo import (
        MessageAttachmentRepository,
    )

    sandbox = isolated_database_factory()
    database = Database(sandbox.dsn)
    repo = MessageAttachmentRepository(database)

    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
            "VALUES ('chat-a', 'owner', 't', 1, 1), ('chat-b', 'owner', 't', 1, 1)"
        )
        cursor.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
            "VALUES ('chat-a', 'owner', 'user', 'first', 1), "
            "('chat-a', 'owner', 'user', 'second', 2), "
            "('chat-b', 'owner', 'user', 'other chat', 3) RETURNING id"
        )
        first, second, other = (int(row["id"]) for row in cursor.fetchall())
        connection.commit()

    repo.insert(chat_id="chat-a", attachment_id="att-1", user_id="owner",
                message_id=first)
    repo.insert(chat_id="chat-a", attachment_id="att-2", user_id="owner",
                message_id=first)
    repo.insert(chat_id="chat-a", attachment_id="att-3", user_id="owner",
                message_id=second)
    repo.insert(chat_id="chat-a", attachment_id="att-4", user_id="intruder",
                message_id=first)
    repo.insert(chat_id="chat-b", attachment_id="att-5", user_id="owner",
                message_id=other)

    grouped: dict[str, list[str]] = {}
    for link in repo.list_for_chat("chat-a", "owner"):
        grouped.setdefault(str(link["message_id"]), []).append(link["attachment_id"])

    assert grouped == {
        str(first): ["att-1", "att-2"],
        str(second): ["att-3"],
    }
    for message_id, attachment_ids in grouped.items():
        assert [
            r["attachment_id"] for r in repo.list_for_message(message_id, "owner")
        ] == attachment_ids
