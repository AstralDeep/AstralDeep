"""Feature-065 guarded PostgreSQL migration and retention contracts.

The tests create uniquely named databases through the shared feature-060
isolated-database fixture. They never mutate the configured product database.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from typing import Callable

import pytest

psycopg2 = pytest.importorskip("psycopg2")

pytest_plugins = ("tests.test_migrations_060",)

from shared.database import (  # noqa: E402
    Database,
    SCHEMA_PREDECESSOR_REVISION,
    SCHEMA_REVISION,
    SchemaRevisionError,
)
from tests.test_migrations_060 import (  # noqa: E402
    _IsolatedDatabase,
    _column_names,
    _fetch_all,
    _fetch_one,
)


SESSION_COLUMNS = {
    "session_id",
    "user_id",
    "activation_id",
    "device_id",
    "device_kind",
    "transport",
    "room_name",
    "participant_identity",
    "worker_identity",
    "visible_chat_id",
    "chat_context_revision",
    "applied_visible_chat_id",
    "applied_chat_context_revision",
    "state",
    "speech_muted",
    "microphone_enabled",
    "foreground_active",
    "foreground_reason",
    "generation",
    "media_grant_revision",
    "owner_connection_generation",
    "control_binding_id",
    "control_binding_expires_at",
    "lease_expires_at",
    "control_owner_id",
    "control_lease_expires_at",
    "last_interaction_at",
    "idle_started_at",
    "started_at",
    "updated_at",
    "ended_at",
    "end_reason",
    "chat_unavailable_at",
    "takeover_of_session_id",
    "media_grant_nonce_hash",
    "media_grant_expires_at",
    "media_grant_consumed_at",
    "last_media_refresh_id",
    "media_grant_issued_at",
    "worker_assignment_id",
    "worker_rtc_grant_revision",
    "worker_rtc_grant_issued_at",
    "worker_rtc_grant_expires_at",
}

TURN_COLUMNS = {
    "turn_id",
    "client_turn_id",
    "session_id",
    "session_generation",
    "media_grant_revision",
    "user_id",
    "chat_id",
    "chat_context_revision",
    "detected_language",
    "spoken_output_policy",
    "output_reason",
    "execution_base_render_revision",
    "submission_id",
    "request_generation",
    "result_request_generation",
    "accepted_connection_generation",
    "message_id",
    "acceptance_commit_id",
    "result_commit_id",
    "operation_id",
    "background_task_id",
    "state",
    "is_foreground",
    "terminal_kind",
    "rejection_reason",
    "rejection_retry_policy",
    "origin_chat_unavailable_at",
    "origin_chat_unavailable_reason",
    "result_id",
    "recap_source",
    "sensitivity",
    "sensitive_consent_at",
    "sensitive_consent_method",
    "sensitive_consent_consumed_at",
    "announcement_sequence",
    "result_reserved_samples",
    "result_quantum_count",
    "last_announcement_kind",
    "last_phrase_key",
    "next_announcement_due_at",
    "announcement_claim_id",
    "announcement_claim_expires_at",
    "last_announcement_started_at",
    "last_speech_finished_at",
    "last_client_playout_started_at",
    "last_client_playout_finished_at",
    "last_client_playout_sequence",
    "accepted_at",
    "processing_started_at",
    "waiting_started_at",
    "terminal_at",
    "created_at",
    "updated_at",
}


@pytest.fixture(autouse=True)
def _direct_connections_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_DISABLE", "1")


def _seed_actual_064(sandbox: _IsolatedDatabase) -> dict[str, object]:
    """Build the checked-out final-064 schema and representative durable rows."""
    database = Database.__new__(Database)
    database.database_url = sandbox.dsn
    database._migrate_conversational_voice_065 = (  # type: ignore[method-assign]
        lambda *args, **kwargs: None
    )

    commit_id = uuid.uuid4()
    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        database._apply_full_schema(connection, cursor)
        # Restore the exact final-064 class vocabulary after the current
        # helper's forward-compatible forced-rerun allowance.
        cursor.execute(
            "ALTER TABLE operation_admission_class "
            "DROP CONSTRAINT operation_admission_class_name_check"
        )
        cursor.execute("""
            ALTER TABLE operation_admission_class
            ADD CONSTRAINT operation_admission_class_name_check CHECK (
                class_name IN (
                    'global', 'interactive', 'mcp', 'background', 'scheduled',
                    'maintenance', 'system'
                )
            )
        """)
        cursor.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('revision', %s)",
            (SCHEMA_PREDECESSOR_REVISION,),
        )
        cursor.execute(
            "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
            "VALUES ('voice-fixture-chat', 'voice-user', 'Keep me', 1, 2)"
        )
        cursor.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
            "VALUES ('voice-fixture-chat', 'voice-user', 'user', "
            "'representative typed content', 3) RETURNING id"
        )
        message_id = int(cursor.fetchone()["id"])
        cursor.execute(
            "INSERT INTO background_task (task_id, user_id, chat_id, status, title) "
            "VALUES ('voice-fixture-task', 'voice-user', "
            "'voice-fixture-chat', 'running', 'Existing work')"
        )
        cursor.execute(
            "INSERT INTO conversation_commit ("
            "commit_id, chat_id, owner_user_id, request_generation, "
            "base_render_revision, committed_render_revision, state, committed_at"
            ") VALUES (%s, 'voice-fixture-chat', 'voice-user', %s, "
            "0, 1, 'committed', now())",
            (str(commit_id), str(uuid.uuid4())),
        )
        cursor.execute(
            "INSERT INTO saved_components ("
            "id, chat_id, user_id, component_data, component_type, title, "
            "created_at, component_id, position, updated_at, "
            "conversation_commit_id, committed_render_revision"
            ") VALUES ("
            "'voice-component', 'voice-fixture-chat', 'voice-user', "
            '\'{"type":"Text","text":"preserve"}\', \'Text\', '
            "'Preserved', 4, 'component-a', 0, 5, %s, 1)",
            (str(commit_id),),
        )
        cursor.execute(
            "INSERT INTO workspace_layout ("
            "chat_id, user_id, layout_key, position, layout, created_at, updated_at"
            ") VALUES ('voice-fixture-chat', 'voice-user', 'primary', 0, "
            '\'{"type":"stack"}\', 6, 7)'
        )
        connection.commit()
    return {"commit_id": commit_id, "message_id": message_id}


def _representative_rows(sandbox: _IsolatedDatabase) -> dict[str, list[dict]]:
    return {
        "chat": _fetch_all(
            sandbox,
            "SELECT id, user_id, title, created_at, updated_at FROM chats "
            "WHERE id = 'voice-fixture-chat'",
        ),
        "message": _fetch_all(
            sandbox,
            "SELECT id, chat_id, user_id, role, content, timestamp FROM messages "
            "WHERE chat_id = 'voice-fixture-chat'",
        ),
        "component": _fetch_all(
            sandbox,
            "SELECT id, chat_id, user_id, component_data, component_id, position "
            "FROM saved_components WHERE chat_id = 'voice-fixture-chat'",
        ),
        "layout": _fetch_all(
            sandbox,
            "SELECT chat_id, user_id, layout_key, position, layout, "
            "created_at, updated_at FROM workspace_layout "
            "WHERE chat_id = 'voice-fixture-chat'",
        ),
        "task": _fetch_all(
            sandbox,
            "SELECT task_id, user_id, chat_id, status, title FROM background_task "
            "WHERE task_id = 'voice-fixture-task'",
        ),
    }


def _insert_voice_session(
    sandbox: _IsolatedDatabase,
    *,
    user_id: str = "voice-user",
    activation_id: uuid.UUID | None = None,
    room_name: str | None = None,
) -> uuid.UUID:
    session_id = uuid.uuid4()
    now = datetime(2026, 7, 31, 16, 0, tzinfo=UTC)
    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO voice_session (
                session_id, user_id, activation_id, device_id, device_kind,
                transport, room_name, participant_identity, visible_chat_id,
                owner_connection_generation, control_binding_id,
                control_binding_expires_at, lease_expires_at,
                media_grant_nonce_hash, media_grant_issued_at,
                media_grant_expires_at, started_at, updated_at,
                last_interaction_at
            ) VALUES (
                %s, %s, %s, %s, 'web', 'livekit', %s, %s,
                'voice-fixture-chat', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                str(session_id),
                user_id,
                str(activation_id or uuid.uuid4()),
                str(uuid.uuid4()),
                room_name or f"room-{session_id}",
                f"participant-{session_id}",
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                now + timedelta(minutes=10),
                now + timedelta(minutes=5),
                b"n" * 32,
                now,
                now + timedelta(minutes=5),
                now,
                now,
                now,
            ),
        )
        connection.commit()
    return session_id


def _insert_voice_turn(
    sandbox: _IsolatedDatabase,
    session_id: uuid.UUID,
    *,
    user_id: str = "voice-user",
) -> uuid.UUID:
    turn_id = uuid.uuid4()
    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO voice_turn (
                turn_id, client_turn_id, session_id, session_generation,
                media_grant_revision, user_id, chat_id, chat_context_revision,
                execution_base_render_revision, submission_id,
                request_generation, is_foreground
            ) VALUES (%s, %s, %s, 1, 1, %s, 'voice-fixture-chat', 1, 1, %s, %s, TRUE)
            """,
            (
                str(turn_id),
                str(uuid.uuid4()),
                str(session_id),
                user_id,
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            ),
        )
        connection.commit()
    return turn_id


def test_revision_contract_is_exact_and_documents_recovery() -> None:
    assert SCHEMA_PREDECESSOR_REVISION == "064.001"
    assert SCHEMA_REVISION == "065.001"
    source = inspect.getsource(Database._init_db)
    assert "SCHEMA_PREDECESSOR_REVISION" in source
    assert "SchemaRevisionError" in source
    assert "source_revision not in" in source
    migration_doc = inspect.getdoc(Database._migrate_conversational_voice_065)
    assert migration_doc is not None
    for phrase in (
        "disable voice",
        "older application code",
        "failed transaction",
        "destructive retirement",
    ):
        assert phrase in migration_doc


def test_fresh_database_bootstraps_directly_to_065(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    Database(sandbox.dsn)

    assert (
        _fetch_one(sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'")[
            "value"
        ]
        == "065.001"
    )
    assert (
        _fetch_one(sandbox, "SELECT to_regclass('voice_session') AS relation")[
            "relation"
        ]
        == "voice_session"
    )
    assert (
        _fetch_one(sandbox, "SELECT to_regclass('voice_turn') AS relation")["relation"]
        == "voice_turn"
    )


def test_actual_064_upgrade_preserves_representative_rows_and_adds_exact_shape(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    _seed_actual_064(sandbox)
    before = _representative_rows(sandbox)

    Database(sandbox.dsn)

    assert _representative_rows(sandbox) == before
    assert (
        _fetch_one(sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'")[
            "value"
        ]
        == "065.001"
    )
    assert SESSION_COLUMNS == _column_names(sandbox, "voice_session")
    assert TURN_COLUMNS == _column_names(sandbox, "voice_turn")
    commit = _fetch_one(
        sandbox,
        "SELECT publication_role, parent_commit_id, execution_base_commit_id, "
        "execution_base_render_revision, execution_base_components_sha256, "
        "execution_base_layouts_sha256, publication_rebase_count "
        "FROM conversation_commit WHERE chat_id = 'voice-fixture-chat'",
    )
    assert commit == {
        "publication_role": "atomic",
        "parent_commit_id": None,
        "execution_base_commit_id": None,
        "execution_base_render_revision": None,
        "execution_base_components_sha256": None,
        "execution_base_layouts_sha256": None,
        "publication_rebase_count": 0,
    }
    layout = _fetch_one(
        sandbox,
        "SELECT conversation_commit_id, committed_render_revision "
        "FROM workspace_layout WHERE layout_key = 'primary'",
    )
    assert layout == {
        "conversation_commit_id": None,
        "committed_render_revision": None,
    }
    assert _fetch_one(sandbox, "SELECT COUNT(*) AS n FROM voice_session")["n"] == 0
    assert _fetch_one(sandbox, "SELECT COUNT(*) AS n FROM voice_turn")["n"] == 0

    voice_config = _fetch_one(
        sandbox,
        "SELECT parent_class_name, active_limit, queue_limit, max_wait_ms, "
        "config_revision FROM operation_admission_class "
        "WHERE class_name = 'voice_interactive'",
    )
    assert voice_config == {
        "parent_class_name": "interactive",
        "active_limit": 10,
        "queue_limit": 0,
        "max_wait_ms": 0,
        "config_revision": "065-defaults",
    }
    assert (
        _fetch_one(
            sandbox,
            "SELECT COUNT(*) AS n FROM operation_admission_slot "
            "WHERE class_name = 'voice_interactive'",
        )["n"]
        == 10
    )


def test_constraints_indexes_delete_actions_and_tombstone_retention(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    fixture = _seed_actual_064(sandbox)
    Database(sandbox.dsn)

    constraint_names = {
        row["conname"]
        for row in _fetch_all(
            sandbox,
            "SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace",
        )
    }
    assert {
        "conversation_commit_parent_065_fk",
        "conversation_commit_execution_base_065_fk",
        "conversation_commit_voice_metadata_065_check",
        "conversation_commit_voice_role_065_check",
        "workspace_layout_conversation_commit_065_fk",
        "workspace_layout_commit_metadata_065_check",
        "voice_session_terminal_065_check",
        "voice_session_foreground_065_check",
        "voice_session_media_grant_065_check",
        "voice_turn_session_owner_065_fk",
        "voice_turn_terminal_065_check",
        "voice_turn_rejection_065_check",
        "voice_turn_announcement_065_check",
    } <= constraint_names

    index_defs = {
        row["indexname"]: row["indexdef"].lower()
        for row in _fetch_all(
            sandbox,
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'",
        )
    }
    assert "where (ended_at is null)" in index_defs["ux_voice_session_live_owner_065"]
    assert "where (is_foreground" in index_defs["ux_voice_turn_foreground_065"]
    assert (
        "00000000-0000-0000-0000-000000000000"
        in index_defs["ux_workspace_layout_chat_key"]
    )
    assert "conversation_commit_id" in index_defs["ux_workspace_layout_chat_key"]
    assert (
        "committed_render_revision"
        in index_defs["idx_workspace_layout_commit_revision_065"]
    )

    delete_actions = {
        row["conname"]: row["confdeltype"]
        for row in _fetch_all(
            sandbox,
            "SELECT conname, confdeltype FROM pg_constraint "
            "WHERE conname LIKE '%%065_fk'",
        )
    }
    assert delete_actions == {
        "conversation_commit_parent_065_fk": "n",
        "conversation_commit_execution_base_065_fk": "r",
        "workspace_layout_conversation_commit_065_fk": "c",
        "voice_turn_session_owner_065_fk": "r",
    }
    turn_delete_actions = {
        row["column_name"]: row["confdeltype"]
        for row in _fetch_all(
            sandbox,
            """
            SELECT attribute.attname AS column_name, constraint_row.confdeltype
            FROM pg_constraint AS constraint_row
            JOIN LATERAL unnest(constraint_row.conkey) AS key(attnum) ON TRUE
            JOIN pg_attribute AS attribute
              ON attribute.attrelid = constraint_row.conrelid
             AND attribute.attnum = key.attnum
            WHERE constraint_row.conrelid = 'voice_turn'::regclass
              AND constraint_row.contype = 'f'
            """,
        )
    }
    assert turn_delete_actions == {
        "session_id": "r",
        "user_id": "r",
        "message_id": "n",
        "acceptance_commit_id": "n",
        "result_commit_id": "n",
        "operation_id": "n",
        "background_task_id": "n",
    }
    assert (
        _fetch_all(
            sandbox,
            "SELECT conname FROM pg_constraint WHERE contype = 'f' "
            "AND conrelid IN ('voice_session'::regclass, 'voice_turn'::regclass) "
            "AND confrelid = 'chats'::regclass",
        )
        == []
    )

    session_id = _insert_voice_session(sandbox)
    turn_id = _insert_voice_turn(sandbox, session_id)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _insert_voice_session(sandbox, user_id="voice-user")

    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE voice_turn SET message_id = %s, acceptance_commit_id = %s "
            "WHERE turn_id = %s",
            (
                fixture["message_id"],
                str(fixture["commit_id"]),
                str(turn_id),
            ),
        )
        cursor.execute("DELETE FROM chats WHERE id = 'voice-fixture-chat'")
        connection.commit()

    retained_session = _fetch_one(
        sandbox,
        "SELECT visible_chat_id FROM voice_session WHERE session_id = %s",
        (str(session_id),),
    )
    retained_turn = _fetch_one(
        sandbox,
        "SELECT chat_id, message_id, acceptance_commit_id FROM voice_turn "
        "WHERE turn_id = %s",
        (str(turn_id),),
    )
    assert retained_session["visible_chat_id"] == "voice-fixture-chat"
    assert retained_turn == {
        "chat_id": "voice-fixture-chat",
        "message_id": None,
        "acceptance_commit_id": None,
    }


def test_voice_and_commit_checks_reject_invalid_rows(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    _seed_actual_064(sandbox)
    Database(sandbox.dsn)
    session_id = _insert_voice_session(sandbox)
    turn_id = _insert_voice_turn(sandbox, session_id)

    with pytest.raises(psycopg2.errors.CheckViolation), sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE voice_turn SET result_reserved_samples = 720001 "
                "WHERE turn_id = %s",
                (str(turn_id),),
            )

    with pytest.raises(psycopg2.errors.CheckViolation), sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO workspace_layout ("
                "chat_id, user_id, layout_key, position, layout, created_at, "
                "updated_at, committed_render_revision"
                ") VALUES ('voice-fixture-chat', 'voice-user', 'invalid', 1, "
                "'{}', 8, 8, 1)"
            )

    with pytest.raises(psycopg2.errors.CheckViolation), sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO workspace_layout ("
                "chat_id, user_id, layout_key, position, layout, created_at, "
                "updated_at, conversation_commit_id"
                ") VALUES ('voice-fixture-chat', 'voice-user', 'invalid-commit', "
                "1, '{}', 8, 8, %s)",
                (
                    str(
                        _fetch_one(
                            sandbox,
                            "SELECT commit_id FROM conversation_commit "
                            "WHERE chat_id = 'voice-fixture-chat'",
                        )["commit_id"]
                    ),
                ),
            )

    with pytest.raises(psycopg2.errors.CheckViolation), sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO conversation_commit ("
                "commit_id, chat_id, owner_user_id, request_generation, "
                "base_render_revision, state, publication_role, "
                "execution_base_render_revision"
                ") VALUES (%s, 'voice-fixture-chat', 'voice-user', %s, "
                "1, 'staged', 'user_acceptance', 0)",
                (str(uuid.uuid4()), str(uuid.uuid4())),
            )

    with pytest.raises(psycopg2.errors.CheckViolation), sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE voice_turn SET detected_language = NULL, "
                "spoken_output_policy = 'full_recap', output_reason = 'ready' "
                "WHERE turn_id = %s",
                (str(turn_id),),
            )

    with pytest.raises(psycopg2.errors.CheckViolation), sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE voice_turn SET state = 'failed', terminal_at = now(), "
                "terminal_kind = NULL WHERE turn_id = %s",
                (str(turn_id),),
            )

    with pytest.raises(psycopg2.errors.CheckViolation), sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE voice_session SET applied_visible_chat_id = "
                "'voice-fixture-chat', applied_chat_context_revision = NULL "
                "WHERE session_id = %s",
                (str(session_id),),
            )

    with pytest.raises(psycopg2.errors.CheckViolation), sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE voice_session SET worker_assignment_id = %s, "
                "worker_rtc_grant_issued_at = now(), "
                "worker_rtc_grant_expires_at = NULL WHERE session_id = %s",
                (str(uuid.uuid4()), str(session_id)),
            )

    with (
        pytest.raises(psycopg2.errors.ForeignKeyViolation),
        sandbox.connect() as connection,
    ):
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM voice_session WHERE session_id = %s",
                (str(session_id),),
            )


def test_versioned_layout_uniqueness_allows_same_key_per_commit(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    fixture = _seed_actual_064(sandbox)
    Database(sandbox.dsn)

    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO workspace_layout ("
            "chat_id, user_id, layout_key, position, layout, created_at, updated_at, "
            "conversation_commit_id, committed_render_revision"
            ") VALUES ('voice-fixture-chat', 'voice-user', 'primary', 1, "
            '\'{"type":"grid"}\', 8, 8, %s, 1)',
            (str(fixture["commit_id"]),),
        )
        connection.commit()
    assert (
        _fetch_one(
            sandbox,
            "SELECT COUNT(*) AS n FROM workspace_layout "
            "WHERE chat_id = 'voice-fixture-chat' AND layout_key = 'primary'",
        )["n"]
        == 2
    )


def test_wrong_predecessor_fails_closed_without_mutating_marker_or_schema(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    _seed_actual_064(sandbox)
    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE schema_meta SET value = '064.999' WHERE key = 'revision'"
        )
        connection.commit()

    with pytest.raises(SchemaRevisionError, match="expected '064.001'.*'064.999'"):
        Database(sandbox.dsn)

    assert (
        _fetch_one(sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'")[
            "value"
        ]
        == "064.999"
    )
    assert (
        _fetch_one(sandbox, "SELECT to_regclass('voice_session') AS relation")[
            "relation"
        ]
        is None
    )
    assert "publication_role" not in _column_names(sandbox, "conversation_commit")


def test_incomplete_predecessor_fails_without_advancing_revision(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        cursor.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('revision', '064.001')"
        )
        connection.commit()

    database = Database.__new__(Database)
    database.database_url = sandbox.dsn
    with sandbox.connect() as connection, connection.cursor() as cursor:
        with pytest.raises(
            SchemaRevisionError,
            match="missing a required 064.001 relation",
        ):
            database._migrate_conversational_voice_065(cursor)
        connection.rollback()

    assert _fetch_one(
        sandbox,
        "SELECT value FROM schema_meta WHERE key = 'revision'",
    )["value"] == "064.001"
    assert _fetch_one(
        sandbox,
        "SELECT to_regclass('voice_session') AS relation",
    )["relation"] is None


def test_failed_065_transaction_rolls_back_then_retries_cleanly(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = isolated_database_factory()
    _seed_actual_064(sandbox)
    migrate = Database._migrate_conversational_voice_065

    def fail_after_ddl(self: Database, cursor: object) -> None:
        migrate(self, cursor)
        raise RuntimeError("injected 065 migration failure")

    monkeypatch.setattr(Database, "_migrate_conversational_voice_065", fail_after_ddl)
    with pytest.raises(RuntimeError, match="injected 065"):
        Database(sandbox.dsn)

    assert (
        _fetch_one(sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'")[
            "value"
        ]
        == "064.001"
    )
    assert (
        _fetch_one(sandbox, "SELECT to_regclass('voice_session') AS relation")[
            "relation"
        ]
        is None
    )
    assert "publication_role" not in _column_names(sandbox, "conversation_commit")

    monkeypatch.setattr(Database, "_migrate_conversational_voice_065", migrate)
    Database(sandbox.dsn)
    assert (
        _fetch_one(sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'")[
            "value"
        ]
        == "065.001"
    )


def test_fast_path_and_marker_clear_repeat_are_idempotent_and_preserve_policy(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    _seed_actual_064(sandbox)
    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE operation_admission_class "
            "DROP CONSTRAINT operation_admission_class_name_check"
        )
        cursor.execute("""
            ALTER TABLE operation_admission_class
            ADD CONSTRAINT operation_admission_class_name_check CHECK (
                class_name IN (
                    'global', 'interactive', 'voice_interactive', 'mcp',
                    'background', 'scheduled', 'maintenance', 'system'
                )
            )
        """)
        cursor.execute(
            "INSERT INTO operation_admission_class ("
            "class_name, parent_class_name, active_limit, queue_limit, "
            "max_wait_ms, config_revision"
            ") VALUES ('voice_interactive', 'interactive', 6, 0, 0, "
            "'operator-narrowed')"
        )
        cursor.execute(
            "INSERT INTO operation_admission_slot (class_name, slot_number) "
            "VALUES ('voice_interactive', 1), ('voice_interactive', 2)"
        )
        connection.commit()

    Database(sandbox.dsn)

    def snapshot() -> tuple[dict, int, dict[str, list[dict]]]:
        return (
            _fetch_one(
                sandbox,
                "SELECT parent_class_name, active_limit, queue_limit, max_wait_ms, "
                "config_revision FROM operation_admission_class "
                "WHERE class_name = 'voice_interactive'",
            ),
            int(
                _fetch_one(
                    sandbox,
                    "SELECT COUNT(*) AS n FROM operation_admission_slot "
                    "WHERE class_name = 'voice_interactive' "
                    "AND slot_number <= 6",
                )["n"]
            ),
            _representative_rows(sandbox),
        )

    expected = snapshot()
    assert expected[0]["active_limit"] == 6
    assert expected[0]["config_revision"] == "operator-narrowed"
    assert expected[1] == 6
    Database(sandbox.dsn)
    assert snapshot() == expected

    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM schema_meta WHERE key = 'revision'")
        connection.commit()
    Database(sandbox.dsn)
    assert snapshot() == expected
    assert (
        _fetch_one(sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'")[
            "value"
        ]
        == "065.001"
    )


def test_conflicting_preexisting_voice_policy_is_not_overwritten(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    _seed_actual_064(sandbox)
    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE operation_admission_class "
            "DROP CONSTRAINT operation_admission_class_name_check"
        )
        cursor.execute("""
            ALTER TABLE operation_admission_class
            ADD CONSTRAINT operation_admission_class_name_check CHECK (
                class_name IN (
                    'global', 'interactive', 'voice_interactive', 'mcp',
                    'background', 'scheduled', 'maintenance', 'system'
                )
            )
        """)
        cursor.execute(
            "INSERT INTO operation_admission_class ("
            "class_name, parent_class_name, active_limit, queue_limit, "
            "max_wait_ms, config_revision"
            ") VALUES ('voice_interactive', 'interactive', 10, 1, 5000, "
            "'operator-conflict')"
        )
        connection.commit()

    with pytest.raises(
        psycopg2.errors.CheckViolation,
        match="bounded no-queue interactive child",
    ):
        Database(sandbox.dsn)

    assert (
        _fetch_one(sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'")[
            "value"
        ]
        == "064.001"
    )
    assert _fetch_one(
        sandbox,
        "SELECT queue_limit, max_wait_ms, config_revision "
        "FROM operation_admission_class WHERE class_name = 'voice_interactive'",
    ) == {
        "queue_limit": 1,
        "max_wait_ms": 5000,
        "config_revision": "operator-conflict",
    }
    assert (
        _fetch_one(sandbox, "SELECT to_regclass('voice_session') AS relation")[
            "relation"
        ]
        is None
    )


def test_voice_tables_store_no_audio_transcript_or_recap_content_columns(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    Database(sandbox.dsn)

    columns = SESSION_COLUMNS | TURN_COLUMNS
    forbidden = {
        "audio",
        "audio_bytes",
        "transcript",
        "transcript_text",
        "partial_transcript",
        "final_transcript",
        "recap_text",
        "api_key",
        "media_grant",
        "control_binding",
    }
    assert forbidden.isdisjoint(columns)
    assert not any(
        name.endswith("_token") or name.endswith("_ticket") for name in columns
    )
