"""Feature 060 guarded migration and representative-data contracts (T006).

The tests intentionally create and drop uniquely named PostgreSQL databases.
They never reset, seed, or otherwise mutate the configured AstralDeep database.
The 057 fixture is advanced only by the product ``Database._init_db`` path.
"""
from __future__ import annotations

import inspect
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2 import sql  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from shared import database as database_module  # noqa: E402
from shared.database import Database, _build_database_url  # noqa: E402


FIXTURE_ROOT = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "runtime_reliability_060"
    / "staging"
)
REPRESENTATIVE_057_SQL = FIXTURE_ROOT / "representative-057.sql"
LEGACY_AGENT_ROOT = FIXTURE_ROOT / "legacy-agent-root"
SCHEMA_LOCK = (1095980114, 60001)
POLICY_LOCK = (1095980114, 60002)
EXPECTED_POLICY_REVISION = "constitution=0.1.0;analyze=1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

NEW_TABLE_COLUMNS = {
    "operation_admission_class": {
        "class_name",
        "parent_class_name",
        "active_limit",
        "queue_limit",
        "max_wait_ms",
        "config_revision",
        "updated_at",
    },
    "operation_record": {
        "operation_id",
        "operation_kind",
        "admission_class",
        "owner_scope",
        "owner_user_id",
        "connection_scope_id",
        "idempotency_namespace",
        "idempotency_key",
        "normalized_input_digest",
        "chat_id",
        "parent_operation_id",
        "connection_generation",
        "request_generation",
        "state",
        "phase_code",
        "terminal_code",
        "safe_summary",
        "retry_after_ms",
        "execution_generation",
        "execution_lease_token",
        "state_revision",
        "accepted_at",
        "updated_at",
        "queue_deadline_at",
        "started_at",
        "terminal_at",
        "cancel_requested_at",
        "purge_after",
    },
    "operation_admission_slot": {
        "class_name",
        "slot_number",
        "operation_id",
        "lease_token",
        "claim_generation",
        "lease_expires_at",
    },
    "operation_submission_result": {
        "submission_result_id",
        "submission_id",
        "owner_scope",
        "owner_user_id",
        "connection_scope_id",
        "accepted",
        "operation_id",
        "refusal_code",
        "retryable",
        "retry_after_ms",
        "observed_at",
        "purge_after",
    },
    "scheduled_occurrence": {
        "occurrence_id",
        "job_id",
        "owner_user_id",
        "scheduled_for",
        "state",
        "lease_token",
        "claim_generation",
        "lease_owner",
        "lease_expires_at",
        "attempt_count",
        "current_operation_id",
        "operation_execution_generation",
        "first_eligible_at",
        "started_at",
        "terminal_at",
        "next_attempt_at",
        "result_code",
        "last_error_code",
        "created_at",
        "updated_at",
    },
    "effect_ledger": {
        "occurrence_id",
        "effect_kind",
        "effect_key",
        "payload_digest",
        "state",
        "operation_id",
        "operation_execution_generation",
        "occurrence_claim_generation",
        "reserved_at",
        "published_at",
        "failed_at",
        "failure_code",
        "downstream_receipt_digest",
    },
    "user_agent_revision": {
        "revision_id",
        "agent_id",
        "owner_user_id",
        "revision_number",
        "parent_revision_id",
        "previous_good_revision_id",
        "artifact_digest",
        "manifest_json",
        "artifact_relative_path",
        "runtime_contract_version",
        "release_lock_digest",
        "compatibility_state",
        "state",
        "promotion_token",
        "state_revision",
        "created_at",
        "confirmed_at",
        "promoted_at",
        "failed_at",
        "failure_code",
    },
    "agent_host_session": {
        "host_session_id",
        "host_id",
        "owner_user_id",
        "connection_scope_id",
        "platform",
        "client_version",
        "host_generation",
        "supersedes_session_id",
        "supported_runtime_contract_versions",
        "runtime_contract_version",
        "release_lock_digest",
        "state",
        "inventory_state",
        "eligible_since",
        "accepted_at",
        "last_seen_at",
        "disconnected_at",
        "inventory_reconciled_at",
        "failure_code",
    },
    "agent_runtime_instance": {
        "runtime_instance_id",
        "agent_id",
        "owner_user_id",
        "host_id",
        "host_session_id",
        "delivery_id",
        "revision_id",
        "process_id",
        "lifecycle_generation",
        "runtime_contract_version",
        "operation_id",
        "operation_execution_generation",
        "state",
        "is_authoritative",
        "state_revision",
        "created_at",
        "started_at",
        "registered_at",
        "last_heartbeat_sequence",
        "ready_at",
        "last_liveness_at",
        "terminal_at",
        "failure_code",
    },
    "agent_runtime_request": {
        "request_id",
        "request_generation",
        "operation_id",
        "operation_execution_generation",
        "runtime_instance_id",
        "agent_id",
        "owner_user_id",
        "state",
        "state_revision",
        "assigned_at",
        "terminal_at",
        "terminal_code",
        "result_digest",
    },
    "draft_transition": {
        "transition_id",
        "draft_uuid",
        "owner_user_id",
        "operation_id",
        "operation_execution_generation",
        "transition_kind",
        "expected_revision",
        "result_revision",
        "outcome",
        "safe_code",
        "created_at",
    },
    "draft_artifact_publication": {
        "publication_id",
        "draft_uuid",
        "owner_user_id",
        "source_state_revision",
        "generation_claim_id",
        "target_agent_id",
        "target_revision_id",
        "operation_id",
        "operation_execution_generation",
        "staging_relative_path",
        "revision_relative_path",
        "artifact_digest",
        "manifest_digest",
        "state",
        "state_revision",
        "created_at",
        "published_at",
        "failed_at",
        "failure_code",
    },
    "maintenance_unit": {
        "unit_id",
        "unit_kind",
        "owner_user_id",
        "scope_key",
        "idempotency_key",
        "state",
        "lease_token",
        "claim_generation",
        "claimed_by",
        "lease_expires_at",
        "attempt_count",
        "max_attempts",
        "operation_id",
        "operation_execution_generation",
        "output_generation",
        "output_relative_path",
        "output_digest",
        "last_error_code",
        "state_revision",
        "created_at",
        "updated_at",
        "terminal_at",
        "next_attempt_at",
    },
    "maintenance_unit_input": {
        "unit_id",
        "input_kind",
        "input_id",
        "input_digest",
        "state",
        "operation_id",
        "operation_execution_generation",
        "completed_at",
    },
    "conversation_commit": {
        "commit_id",
        "chat_id",
        "owner_user_id",
        "request_generation",
        "operation_id",
        "operation_execution_generation",
        "base_render_revision",
        "committed_render_revision",
        "state",
        "started_at",
        "committed_at",
        "aborted_at",
    },
}

ADDED_COLUMNS = {
    "background_task": {"operation_id", "operation_execution_generation"},
    "job_run": {
        "occurrence_id",
        "attempt_number",
        "operation_id",
        "operation_execution_generation",
        "occurrence_claim_generation",
    },
    "user_agent": {
        "active_revision_id",
        "last_known_good_revision_id",
        "selected_host_session_id",
        "authoritative_instance_id",
        "lifecycle_generation",
        "generation_counter",
        "state_revision",
        "validated_policy_revision",
    },
    "draft_agents": {
        "draft_uuid",
        "target_agent_id",
        "state_revision",
        "generation_claim_id",
        "generation_claim_expires_at",
        "published_revision_id",
    },
    "chats": {"render_revision", "snapshot_committed_at", "conversation_commit_id"},
    "messages": {
        "conversation_commit_id",
        "commit_position",
        "committed_render_revision",
    },
    "saved_components": {"conversation_commit_id", "committed_render_revision"},
}

OPERATION_RETENTION_TABLES = {
    "operation_record",  # parent_operation_id self-reference
    "operation_admission_slot",
    "operation_submission_result",
    "background_task",
    "scheduled_occurrence",
    "job_run",
    "effect_ledger",
    "agent_runtime_instance",
    "agent_runtime_request",
    "draft_transition",
    "draft_artifact_publication",
    "maintenance_unit",
    "maintenance_unit_input",
    "conversation_commit",
    "voice_turn",
}


class _IsolatedDatabase:
    def __init__(self, *, dsn: str, admin_dsn: str, name: str):
        self.dsn = dsn
        self.admin_dsn = admin_dsn
        self.name = name

    def connect(self):
        return psycopg2.connect(self.dsn, cursor_factory=RealDictCursor)

    def advisory_locks(self, pid: int) -> set[tuple[int, int]]:
        with psycopg2.connect(
            self.admin_dsn, cursor_factory=RealDictCursor
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT classid::bigint AS classid, objid::bigint AS objid "
                    "FROM pg_locks WHERE pid = %s AND locktype = 'advisory' "
                    "AND granted",
                    (pid,),
                )
                return {(row["classid"], row["objid"]) for row in cursor.fetchall()}

    def terminate_backend(self, pid: int) -> bool:
        with psycopg2.connect(
            self.admin_dsn, cursor_factory=RealDictCursor
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_terminate_backend(%s) AS terminated", (pid,))
                return bool(cursor.fetchone()["terminated"])


@pytest.fixture(autouse=True)
def _direct_connections_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_DISABLE", "1")


@pytest.fixture
def isolated_database_factory() -> Callable[[], _IsolatedDatabase]:
    base_dsn = _build_database_url()
    try:
        params = psycopg2.extensions.parse_dsn(base_dsn)
        admin = psycopg2.connect(**params)
        admin.close()
    except Exception as exc:  # pragma: no cover - environment gate
        pytest.skip(f"PostgreSQL unavailable for isolated migration tests: {exc}")

    created: list[_IsolatedDatabase] = []

    def create() -> _IsolatedDatabase:
        name = f"astraldeep_060_test_{uuid.uuid4().hex}"
        try:
            connection = psycopg2.connect(**params)
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            connection.close()
        except Exception as exc:  # pragma: no cover - privilege/environment gate
            pytest.skip(f"cannot create isolated PostgreSQL database: {exc}")
        database_params = dict(params)
        database_params["dbname"] = name
        sandbox = _IsolatedDatabase(
            dsn=psycopg2.extensions.make_dsn(**database_params),
            admin_dsn=psycopg2.extensions.make_dsn(**params),
            name=name,
        )
        created.append(sandbox)
        return sandbox

    yield create

    for sandbox in reversed(created):
        try:
            connection = psycopg2.connect(**params)
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (sandbox.name,),
                )
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        sql.Identifier(sandbox.name)
                    )
                )
            connection.close()
        except Exception:
            pass


def _seed_representative_057(sandbox: _IsolatedDatabase) -> None:
    """Create the legacy full schema, then load the no-DDL fixture."""
    db = Database.__new__(Database)
    db.database_url = sandbox.dsn
    # T011 must keep 060 schema work in one isolated helper. Suppressing only
    # that helper lets this test construct a truthful pre-060 database using
    # the still-supported full startup schema path.
    if hasattr(Database, "_migrate_runtime_reliability_060"):
        db._migrate_runtime_reliability_060 = lambda *args, **kwargs: None
    if hasattr(Database, "_migrate_conversational_voice_065"):
        db._migrate_conversational_voice_065 = lambda *args, **kwargs: None

    with sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db._apply_full_schema(connection, cursor)
            cursor.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('revision', '057.001')"
            )
        connection.commit()

    connection = sandbox.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(REPRESENTATIVE_057_SQL.read_text(encoding="utf-8"))
    finally:
        connection.close()

    # Feature 065 deliberately accepts only its exact 064 predecessor or an
    # absent marker. Clearing this synthetic legacy marker exercises the
    # supported forced-full-schema recovery path without mislabelling the
    # representative 057 data as a 064 database.
    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM schema_meta WHERE key = 'revision'")
        connection.commit()


def _prepare_legacy_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        Database,
        "_legacy_agent_root",
        lambda self: LEGACY_AGENT_ROOT,
        raising=False,
    )


def _fetch_all(sandbox: _IsolatedDatabase, query: str, params: tuple = ()) -> list[dict]:
    with sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]


def _fetch_one(sandbox: _IsolatedDatabase, query: str, params: tuple = ()) -> dict:
    rows = _fetch_all(sandbox, query, params)
    assert len(rows) == 1
    return rows[0]


def _column_names(sandbox: _IsolatedDatabase, table: str) -> set[str]:
    return {
        row["column_name"]
        for row in _fetch_all(
            sandbox,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
    }


def _run_in_threads(count: int, target: Callable[[], Any]) -> list[BaseException]:
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def run() -> None:
        try:
            barrier.wait(timeout=10)
            target()
        except BaseException as exc:  # noqa: BLE001 - returned to asserting thread
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=run, daemon=True) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "isolated migration starter did not terminate"
    return errors


def test_schema_revision_declares_current_revision() -> None:
    assert database_module.SCHEMA_REVISION == "066.001"


def test_startup_source_declares_both_fixed_advisory_transactions() -> None:
    source = inspect.getsource(Database._init_db)
    assert "pg_advisory_xact_lock" in source
    for value in (*SCHEMA_LOCK, POLICY_LOCK):
        assert str(value) in source or str(value) in inspect.getsource(database_module)
    assert "hash(" not in source


def test_empty_database_has_complete_additive_schema(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    Database(sandbox.dsn)

    marker = _fetch_one(
        sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'"
    )
    assert marker["value"] == "066.001"
    for table, expected_columns in NEW_TABLE_COLUMNS.items():
        assert expected_columns <= _column_names(sandbox, table), table
    for table, expected_columns in ADDED_COLUMNS.items():
        assert expected_columns <= _column_names(sandbox, table), table

    mcp_class = _fetch_one(
        sandbox,
        "SELECT parent_class_name, active_limit, queue_limit, max_wait_ms, "
        "config_revision FROM operation_admission_class WHERE class_name = 'mcp'",
    )
    assert mcp_class == {
        "parent_class_name": "global",
        "active_limit": 8,
        "queue_limit": 32,
        "max_wait_ms": 5000,
        "config_revision": "064-defaults",
    }
    assert _fetch_one(
        sandbox,
        "SELECT COUNT(*) AS n FROM operation_admission_slot WHERE class_name = 'mcp'",
    )["n"] == 8

    operation_fks = {
        row["table_name"]
        for row in _fetch_all(
            sandbox,
            "SELECT conrelid::regclass::text AS table_name "
            "FROM pg_constraint WHERE contype = 'f' "
            "AND confrelid = 'operation_record'::regclass "
            "AND confdeltype = 'n'",
        )
    }
    assert operation_fks == OPERATION_RETENTION_TABLES

    indexes = "\n".join(
        row["indexdef"].lower()
        for row in _fetch_all(
            sandbox,
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public'",
        )
    )
    required_index_fragments = {
        "(state, accepted_at, operation_id)",
        "(owner_scope, owner_user_id, accepted_at desc)",
        "(connection_scope_id, state)",
        "(job_id, scheduled_for)",
        "(occurrence_id, attempt_number)",
        "(agent_id, revision_number)",
        "(owner_user_id, host_id, host_generation)",
        "(agent_id, lifecycle_generation)",
        "(host_id, process_id)",
        "(draft_uuid, source_state_revision)",
        "(unit_kind, idempotency_key)",
        "(chat_id, request_generation)",
    }
    for fragment in required_index_fragments:
        assert fragment in indexes, fragment


def _assert_integrity_rejected(cursor, statement: str, params: tuple = ()) -> None:
    cursor.execute("SAVEPOINT invalid_runtime_coordination")
    with pytest.raises(psycopg2.IntegrityError):
        cursor.execute(statement, params)
    cursor.execute("ROLLBACK TO SAVEPOINT invalid_runtime_coordination")


def test_operation_and_slot_constraints_fail_closed(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    Database(sandbox.dsn)

    with sandbox.connect() as connection, connection.cursor() as cursor:
        operation_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO operation_record ("
            "operation_id, operation_kind, admission_class, owner_scope, "
            "owner_user_id, state, execution_generation, state_revision, "
            "accepted_at, updated_at, queue_deadline_at) VALUES ("
            "%s, 'connection_frame', 'interactive', 'user', "
            "'fixture-owner-a', 'queued', 0, 0, now(), now(), now() + interval '5 seconds')",
            (operation_id,),
        )

        _assert_integrity_rejected(
            cursor,
            "UPDATE operation_admission_slot SET operation_id = %s "
            "WHERE class_name = 'interactive' AND slot_number = 1",
            (operation_id,),
        )
        _assert_integrity_rejected(
            cursor,
            "UPDATE operation_admission_slot SET lease_token = %s, "
            "lease_expires_at = now() + interval '30 seconds' "
            "WHERE class_name = 'interactive' AND slot_number = 1",
            (str(uuid.uuid4()),),
        )
        _assert_integrity_rejected(
            cursor,
            "UPDATE operation_record SET state = 'running', execution_generation = 1, "
            "execution_lease_token = %s, queue_deadline_at = NULL "
            "WHERE operation_id = %s",
            (str(uuid.uuid4()), operation_id),
        )
        _assert_integrity_rejected(
            cursor,
            "UPDATE operation_record SET terminal_code = 'operation_failed' "
            "WHERE operation_id = %s",
            (operation_id,),
        )
        _assert_integrity_rejected(
            cursor,
            "INSERT INTO operation_submission_result ("
            "submission_result_id, submission_id, owner_scope, owner_user_id, "
            "accepted, refusal_code, retryable, retry_after_ms, purge_after) VALUES ("
            "%s, %s, 'user', 'fixture-owner-a', FALSE, "
            "'capacity_exceeded', FALSE, 1000, now() + interval '24 hours')",
            (str(uuid.uuid4()), str(uuid.uuid4())),
        )

        execution_token = str(uuid.uuid4())
        cursor.execute(
            "UPDATE operation_record SET state = 'running', execution_generation = 1, "
            "execution_lease_token = %s, started_at = now(), queue_deadline_at = NULL "
            "WHERE operation_id = %s",
            (execution_token, operation_id),
        )
        cursor.execute(
            "UPDATE operation_admission_slot SET operation_id = %s, lease_token = %s, "
            "lease_expires_at = now() + interval '30 seconds' "
            "WHERE class_name = 'interactive' AND slot_number = 1",
            (operation_id, execution_token),
        )
        connection.commit()


def test_representative_057_migration_preserves_legacy_truth(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = isolated_database_factory()
    _seed_representative_057(sandbox)
    _prepare_legacy_root(monkeypatch)

    before_messages = _fetch_all(
        sandbox, "SELECT id, chat_id, role, content FROM messages ORDER BY id"
    )
    before_components = _fetch_all(
        sandbox,
        "SELECT id, chat_id, component_data, component_id, position "
        "FROM saved_components ORDER BY id",
    )
    before_synthesis = _fetch_all(
        sandbox, "SELECT id, synthesized FROM interaction_log ORDER BY id"
    )

    Database(sandbox.dsn)

    assert _fetch_one(
        sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'"
    )["value"] == "066.001"
    assert _fetch_all(
        sandbox, "SELECT id, chat_id, role, content FROM messages ORDER BY id"
    ) == before_messages
    assert _fetch_all(
        sandbox,
        "SELECT id, chat_id, component_data, component_id, position "
        "FROM saved_components ORDER BY id",
    ) == before_components
    assert _fetch_all(
        sandbox, "SELECT id, synthesized FROM interaction_log ORDER BY id"
    ) == before_synthesis
    assert _fetch_one(
        sandbox,
        "SELECT active_limit, queue_limit, max_wait_ms "
        "FROM operation_admission_class WHERE class_name = 'mcp'",
    ) == {"active_limit": 8, "queue_limit": 32, "max_wait_ms": 5000}
    assert _fetch_one(
        sandbox,
        "SELECT COUNT(*) AS n FROM operation_admission_slot WHERE class_name = 'mcp'",
    )["n"] == 8

    chats = _fetch_all(
        sandbox,
        "SELECT id, render_revision, snapshot_committed_at, conversation_commit_id "
        "FROM chats ORDER BY id",
    )
    assert chats and all(row["render_revision"] == 0 for row in chats)
    assert all(row["snapshot_committed_at"] is None for row in chats)
    assert all(row["conversation_commit_id"] is None for row in chats)
    assert _fetch_one(sandbox, "SELECT COUNT(*) AS n FROM conversation_commit")["n"] == 0

    message_commit_fields = _fetch_all(
        sandbox,
        "SELECT conversation_commit_id, commit_position, committed_render_revision "
        "FROM messages",
    )
    assert all(not any(row.values()) for row in message_commit_fields)
    component_commit_fields = _fetch_all(
        sandbox,
        "SELECT conversation_commit_id, committed_render_revision "
        "FROM saved_components",
    )
    assert all(not any(row.values()) for row in component_commit_fields)

    background = _fetch_all(
        sandbox,
        "SELECT operation_id, operation_execution_generation FROM background_task",
    )
    assert all(not any(row.values()) for row in background)
    runs = _fetch_all(
        sandbox,
        "SELECT occurrence_id, attempt_number, operation_id, "
        "operation_execution_generation, occurrence_claim_generation FROM job_run",
    )
    assert all(not any(row.values()) for row in runs)
    assert _fetch_one(sandbox, "SELECT COUNT(*) AS n FROM maintenance_unit_input")["n"] == 0

    drafts = _fetch_all(
        sandbox,
        "SELECT id, draft_uuid::text, target_agent_id FROM draft_agents "
        "WHERE id IN ('06000000-0000-4000-8000-000000000301', "
        "'06000000-0000-4000-8000-000000000302') ORDER BY id",
    )
    assert len(drafts) == 2
    assert all(row["draft_uuid"] == row["id"] for row in drafts)
    assert [row["target_agent_id"] for row in drafts] == [
        "fixture-server-agent",
        "fixture-host-agent",
    ]

    revisions = _fetch_all(
        sandbox,
        "SELECT agent_id, artifact_digest, artifact_relative_path, manifest_json, "
        "runtime_contract_version, release_lock_digest, compatibility_state, state "
        "FROM user_agent_revision ORDER BY agent_id",
    )
    by_agent = {row["agent_id"]: row for row in revisions}
    assert "fixture-deleted-agent" not in by_agent
    assert SHA256_RE.fullmatch(by_agent["fixture-server-agent"]["artifact_digest"])
    assert by_agent["fixture-server-agent"]["artifact_relative_path"].endswith(
        "synthetic-same-name"
    )
    host_revision = by_agent["fixture-host-agent"]
    for field in (
        "artifact_digest",
        "artifact_relative_path",
        "manifest_json",
        "runtime_contract_version",
        "release_lock_digest",
    ):
        assert host_revision[field] is None
    assert host_revision["compatibility_state"] == "legacy_pending"
    assert host_revision["state"] == "legacy_pending"


def test_commit_versioned_component_index_upgrades_representative_057(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = isolated_database_factory()
    _seed_representative_057(sandbox)
    _prepare_legacy_root(monkeypatch)
    Database(sandbox.dsn)

    commit_id = str(uuid.uuid4())
    request_generation = str(uuid.uuid4())
    with sandbox.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO conversation_commit ("
            "commit_id, chat_id, owner_user_id, request_generation, "
            "base_render_revision, state"
            ") VALUES (%s, 'fixture-chat-structured', 'fixture-owner-a', %s, 0, 'staged')",
            (commit_id, request_generation),
        )
        cursor.execute(
            "INSERT INTO saved_components ("
            "id, chat_id, user_id, component_data, component_type, title, "
            "created_at, component_id, position, updated_at, "
            "conversation_commit_id, committed_render_revision"
            ") SELECT 'fixture-staged-component-card', chat_id, user_id, "
            "component_data, component_type, title, created_at, component_id, "
            "position, updated_at, %s, 1 FROM saved_components "
            "WHERE id = 'fixture-saved-component-card'",
            (commit_id,),
        )
        cursor.execute(
            "SELECT COUNT(*) AS count FROM saved_components "
            "WHERE chat_id = 'fixture-chat-structured' "
            "AND component_id = 'fixture-component-card'"
        )
        assert cursor.fetchone()["count"] == 2

        cursor.execute("SAVEPOINT duplicate_staged_component")
        with pytest.raises(psycopg2.IntegrityError):
            cursor.execute(
                "INSERT INTO saved_components ("
                "id, chat_id, user_id, component_data, component_type, title, "
                "created_at, component_id, position, updated_at, "
                "conversation_commit_id, committed_render_revision"
                ") SELECT 'fixture-staged-component-card-duplicate', chat_id, user_id, "
                "component_data, component_type, title, created_at, component_id, "
                "position, updated_at, conversation_commit_id, "
                "committed_render_revision FROM saved_components "
                "WHERE id = 'fixture-staged-component-card'"
            )
        cursor.execute("ROLLBACK TO SAVEPOINT duplicate_staged_component")

        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "AND indexname = 'ux_saved_components_chat_component'"
        )
        index_definition = cursor.fetchone()["indexdef"].lower()
        assert "conversation_commit_id" in index_definition
        assert "coalesce" in index_definition
        connection.commit()


def _insert_valid_host(connection, *, session_id: str, host_id: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO agent_host_session ("
            "host_session_id, host_id, owner_user_id, connection_scope_id, platform, "
            "client_version, host_generation, supported_runtime_contract_versions, "
            "runtime_contract_version, release_lock_digest, state, inventory_state, "
            "eligible_since, accepted_at, last_seen_at) VALUES ("
            "%s, %s, 'fixture-owner-a', %s, 'windows', '0.4.0', 1, "
            "ARRAY[2], 2, %s, 'connected', 'reconciled', now(), now(), now())",
            (session_id, host_id, str(uuid.uuid4()), "a" * 64),
        )


def _assert_host_rejected(connection, values: tuple) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SAVEPOINT invalid_host")
        with pytest.raises(psycopg2.IntegrityError):
            cursor.execute(
                "INSERT INTO agent_host_session ("
                "host_session_id, host_id, owner_user_id, connection_scope_id, "
                "platform, client_version, host_generation, "
                "supported_runtime_contract_versions, runtime_contract_version, "
                "release_lock_digest, state, inventory_state, eligible_since, "
                "accepted_at, last_seen_at) VALUES ("
                "%s, %s, 'fixture-owner-a', %s, %s, %s, 1, %s, %s, %s, "
                "'connected', 'reconciled', now(), now(), now())",
                values,
            )
        cursor.execute("ROLLBACK TO SAVEPOINT invalid_host")


def test_host_contract_and_nullable_bind_once_constraints(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
) -> None:
    sandbox = isolated_database_factory()
    Database(sandbox.dsn)

    session_id = str(uuid.uuid4())
    host_id = str(uuid.uuid4())
    with sandbox.connect() as connection:
        _insert_valid_host(connection, session_id=session_id, host_id=host_id)
        invalid = (
            ("linux", "0.4.0", [2], 2, "a" * 64),
            ("windows", "v0.4.0", [2], 2, "a" * 64),
            ("windows", "01.4.0", [2], 2, "a" * 64),
            ("windows", "0.4.0\n", [2], 2, "a" * 64),
            ("windows", "0.4.0", [], 2, "a" * 64),
            ("windows", "0.4.0", [2, 2], 2, "a" * 64),
            ("windows", "0.4.0", [2], 3, "a" * 64),
            ("windows", "0.4.0", [2], 2, "A" * 64),
        )
        for platform, version, supported, selected, digest in invalid:
            _assert_host_rejected(
                connection,
                (
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    platform,
                    version,
                    supported,
                    selected,
                    digest,
                ),
            )
        connection.commit()

    with sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO user_agent (agent_id, owner_user_id, display_name, status, "
                "created_at, updated_at) VALUES ("
                "'runtime-fixture-agent', 'fixture-owner-a', 'Runtime fixture', "
                "'validated', 1, 1)"
            )
            revision_id = str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO user_agent_revision ("
                "revision_id, agent_id, owner_user_id, revision_number, "
                "compatibility_state, state, state_revision, created_at) VALUES ("
                "%s, 'runtime-fixture-agent', 'fixture-owner-a', 0, "
                "'legacy_pending', 'legacy_pending', 0, now())",
                (revision_id,),
            )
            runtime_id = str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO agent_runtime_instance ("
                "runtime_instance_id, agent_id, owner_user_id, host_id, "
                "host_session_id, delivery_id, revision_id, process_id, "
                "lifecycle_generation, runtime_contract_version, operation_id, "
                "operation_execution_generation, state, is_authoritative, "
                "state_revision, created_at) VALUES ("
                "%s, 'runtime-fixture-agent', 'fixture-owner-a', %s, %s, %s, %s, "
                "NULL, 1, 2, NULL, 1, 'delivering', FALSE, 0, now())",
                (runtime_id, host_id, session_id, str(uuid.uuid4()), revision_id),
            )
            cursor.execute(
                "SELECT registered_at, last_heartbeat_sequence "
                "FROM agent_runtime_instance WHERE runtime_instance_id = %s",
                (runtime_id,),
            )
            prelaunch = cursor.fetchone()
            assert prelaunch["registered_at"] is None
            assert prelaunch["last_heartbeat_sequence"] is None
            _assert_integrity_rejected(
                cursor,
                "UPDATE agent_runtime_instance SET last_heartbeat_sequence = 0 "
                "WHERE runtime_instance_id = %s",
                (runtime_id,),
            )
            _assert_integrity_rejected(
                cursor,
                "UPDATE agent_runtime_instance SET registered_at = now() "
                "WHERE runtime_instance_id = %s",
                (runtime_id,),
            )
            _assert_integrity_rejected(
                cursor,
                "UPDATE agent_runtime_instance SET last_heartbeat_sequence = 1, "
                "last_liveness_at = now() WHERE runtime_instance_id = %s",
                (runtime_id,),
            )
            process_id = str(uuid.uuid4())
            cursor.execute(
                "UPDATE agent_runtime_instance SET process_id = %s, "
                "state = 'starting', state_revision = state_revision + 1 "
                "WHERE runtime_instance_id = %s AND host_session_id = %s "
                "AND state = 'delivering' AND state_revision = 0 AND process_id IS NULL",
                (process_id, runtime_id, session_id),
            )
            assert cursor.rowcount == 1
            cursor.execute(
                "UPDATE agent_runtime_instance SET process_id = %s "
                "WHERE runtime_instance_id = %s AND host_session_id = %s "
                "AND state_revision = 1 AND process_id IS NULL",
                (str(uuid.uuid4()), runtime_id, session_id),
            )
            assert cursor.rowcount == 0
            _assert_integrity_rejected(
                cursor,
                "UPDATE agent_runtime_instance SET registered_at = now(), "
                "last_heartbeat_sequence = 1 WHERE runtime_instance_id = %s",
                (runtime_id,),
            )
            _assert_integrity_rejected(
                cursor,
                "UPDATE agent_runtime_instance SET registered_at = now(), "
                "last_liveness_at = now() WHERE runtime_instance_id = %s",
                (runtime_id,),
            )
            cursor.execute(
                "UPDATE agent_runtime_instance SET registered_at = now() "
                "WHERE runtime_instance_id = %s",
                (runtime_id,),
            )
            cursor.execute(
                "SELECT registered_at, last_heartbeat_sequence, last_liveness_at "
                "FROM agent_runtime_instance WHERE runtime_instance_id = %s",
                (runtime_id,),
            )
            registered_only = cursor.fetchone()
            assert registered_only["registered_at"] is not None
            assert registered_only["last_heartbeat_sequence"] is None
            assert registered_only["last_liveness_at"] is None
            cursor.execute(
                "UPDATE agent_runtime_instance SET last_heartbeat_sequence = 1, "
                "last_liveness_at = now() WHERE runtime_instance_id = %s",
                (runtime_id,),
            )
            cursor.execute(
                "SELECT registered_at, last_heartbeat_sequence, last_liveness_at "
                "FROM agent_runtime_instance WHERE runtime_instance_id = %s",
                (runtime_id,),
            )
            registered = cursor.fetchone()
            assert registered["registered_at"] is not None
            assert registered["last_heartbeat_sequence"] == 1
            assert registered["last_liveness_at"] is not None
        connection.commit()


def test_two_starters_apply_schema_once_after_lock_recheck(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = isolated_database_factory()
    _seed_representative_057(sandbox)
    _prepare_legacy_root(monkeypatch)
    migrate = getattr(Database, "_migrate_runtime_reliability_060", None)
    assert migrate is not None, "T011 must isolate the repeat-safe 060 migration helper"
    calls = 0
    calls_lock = threading.Lock()

    def counted(self, *args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        return migrate(self, *args, **kwargs)

    monkeypatch.setattr(Database, "_migrate_runtime_reliability_060", counted)
    errors = _run_in_threads(2, lambda: Database(sandbox.dsn))
    assert errors == []
    assert calls == 1
    assert _fetch_one(
        sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'"
    )["value"] == "066.001"


def test_killed_schema_owner_rolls_back_and_waiter_reapplies(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = isolated_database_factory()
    _seed_representative_057(sandbox)
    _prepare_legacy_root(monkeypatch)
    migrate = getattr(Database, "_migrate_runtime_reliability_060", None)
    assert migrate is not None, "T011 must isolate the repeat-safe 060 migration helper"

    first_ready = threading.Event()
    release_first = threading.Event()
    first_pid: list[int] = []
    call_lock = threading.Lock()
    calls = 0

    def pause_first(self, *args, **kwargs):
        nonlocal calls
        with call_lock:
            calls += 1
            call_number = calls
        result = migrate(self, *args, **kwargs)
        if call_number == 1:
            cursor = next(arg for arg in args if hasattr(arg, "execute"))
            cursor.execute("SELECT pg_backend_pid() AS pid")
            first_pid.append(cursor.fetchone()["pid"])
            first_ready.set()
            assert release_first.wait(timeout=20)
        return result

    monkeypatch.setattr(Database, "_migrate_runtime_reliability_060", pause_first)
    errors: list[BaseException] = []

    def start() -> None:
        try:
            Database(sandbox.dsn)
        except BaseException as exc:  # noqa: BLE001 - asserted after join
            errors.append(exc)

    first = threading.Thread(target=start, daemon=True)
    first.start()
    assert first_ready.wait(timeout=20)
    assert sandbox.advisory_locks(first_pid[0]) == {SCHEMA_LOCK}

    second = threading.Thread(target=start, daemon=True)
    second.start()
    assert sandbox.terminate_backend(first_pid[0])
    release_first.set()
    first.join(timeout=30)
    second.join(timeout=30)
    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 1
    assert calls == 2
    assert _fetch_one(
        sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'"
    )["value"] == "066.001"


def test_fifty_two_starter_schema_and_policy_trials_converge_once(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-017: 50 two-replica forced revisions have one owner each."""

    sandbox = isolated_database_factory()
    _seed_representative_057(sandbox)
    _prepare_legacy_root(monkeypatch)
    Database(sandbox.dsn)

    migrate = getattr(Database, "_migrate_runtime_reliability_060", None)
    sweep = getattr(Database, "_sweep_user_agent_policy_060", None)
    assert migrate is not None and sweep is not None
    calls_lock = threading.Lock()
    schema_calls = 0
    policy_calls = 0

    def counted_migrate(self, *args, **kwargs):
        nonlocal schema_calls
        with calls_lock:
            schema_calls += 1
        return migrate(self, *args, **kwargs)

    def counted_sweep(self, *args, **kwargs):
        nonlocal policy_calls
        with calls_lock:
            policy_calls += 1
        return sweep(self, *args, **kwargs)

    monkeypatch.setattr(
        Database,
        "_migrate_runtime_reliability_060",
        counted_migrate,
    )
    monkeypatch.setattr(Database, "_sweep_user_agent_policy_060", counted_sweep)

    trial_count = 50
    started = time.perf_counter()
    for trial in range(trial_count):
        old_policy = f"old-policy-{trial}"
        with sandbox.connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM schema_meta WHERE key = 'revision'")
            cursor.execute(
                "INSERT INTO schema_meta (key, value) VALUES ("
                "'user_agent_policy_revision', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (old_policy,),
            )
            cursor.execute(
                "UPDATE user_agent SET validated_policy_revision = %s, "
                "revalidation_required = FALSE",
                (old_policy,),
            )
            connection.commit()

        outcomes = []
        outcomes_lock = threading.Lock()

        def boot() -> None:
            database = Database(sandbox.dsn)
            with outcomes_lock:
                outcomes.append(database.user_agent_policy_outcome)

        assert _run_in_threads(2, boot) == []
        assert len(outcomes) == 2
        assert sorted(outcome.marker_changed for outcome in outcomes) == [
            False,
            True,
        ]
        assert sorted(
            outcome.agents_marked_for_revalidation for outcome in outcomes
        ) == [0, 2]
        assert {
            outcome.policy_revision for outcome in outcomes
        } == {EXPECTED_POLICY_REVISION}
        assert _fetch_one(
            sandbox,
            "SELECT value FROM schema_meta WHERE key = 'revision'",
        )["value"] == "066.001"
        assert _fetch_one(
            sandbox,
            "SELECT value FROM schema_meta "
            "WHERE key = 'user_agent_policy_revision'",
        )["value"] == EXPECTED_POLICY_REVISION
        agents = _fetch_all(
            sandbox,
            "SELECT deleted_at, revalidation_required FROM user_agent",
        )
        assert all(
            agent["revalidation_required"] is (agent["deleted_at"] is None)
            for agent in agents
        )

    duration_seconds = time.perf_counter() - started
    print(
        "US6 migration profile: "
        f"trials={trial_count} starters={trial_count * 2} "
        f"schema_owners={schema_calls} policy_owners={policy_calls} "
        f"duration_seconds={duration_seconds:.3f}"
    )
    assert schema_calls == trial_count
    assert policy_calls == trial_count


def test_policy_only_change_uses_independent_fixed_lock(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = isolated_database_factory()
    _seed_representative_057(sandbox)
    _prepare_legacy_root(monkeypatch)
    Database(sandbox.dsn)

    with sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE user_agent SET validated_policy_revision = 'old-policy', "
                "revalidation_required = FALSE"
            )
            cursor.execute(
                "INSERT INTO schema_meta (key, value) VALUES ("
                "'user_agent_policy_revision', 'old-policy') "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            )
        connection.commit()

    monkeypatch.setattr(
        database_module,
        "USER_AGENT_POLICY_REVISION",
        EXPECTED_POLICY_REVISION,
        raising=False,
    )
    schema_migrate = getattr(Database, "_migrate_runtime_reliability_060", None)
    assert schema_migrate is not None

    def schema_must_not_run(*args, **kwargs):
        raise AssertionError("policy-only boot must not execute schema migration")

    monkeypatch.setattr(
        Database, "_migrate_runtime_reliability_060", schema_must_not_run
    )
    sweep = getattr(Database, "_sweep_user_agent_policy_060", None)
    assert sweep is not None, "T011 must expose one policy sweep under the policy lock"
    sweep_ready = threading.Event()
    release_sweep = threading.Event()
    sweep_pid: list[int] = []

    def paused_sweep(self, *args, **kwargs):
        cursor = next(arg for arg in args if hasattr(arg, "execute"))
        cursor.execute("SELECT pg_backend_pid() AS pid")
        sweep_pid.append(cursor.fetchone()["pid"])
        sweep_ready.set()
        assert release_sweep.wait(timeout=20)
        return sweep(self, *args, **kwargs)

    monkeypatch.setattr(Database, "_sweep_user_agent_policy_060", paused_sweep)
    errors: list[BaseException] = []
    booted: list[Database] = []

    def boot() -> None:
        try:
            booted.append(Database(sandbox.dsn))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    thread = threading.Thread(target=boot, daemon=True)
    thread.start()
    assert sweep_ready.wait(timeout=20)
    assert sandbox.advisory_locks(sweep_pid[0]) == {POLICY_LOCK}
    release_sweep.set()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert errors == []
    assert len(booted) == 1
    assert booted[0].user_agent_policy_outcome.policy_revision == (
        EXPECTED_POLICY_REVISION
    )
    assert booted[0].user_agent_policy_outcome.marker_changed is True
    assert (
        booted[0].user_agent_policy_outcome.agents_marked_for_revalidation
        == 2
    )

    fast_path = Database(sandbox.dsn).user_agent_policy_outcome
    assert fast_path.marker_changed is False
    assert fast_path.agents_marked_for_revalidation == 0

    assert _fetch_one(
        sandbox,
        "SELECT value FROM schema_meta WHERE key = 'user_agent_policy_revision'",
    )["value"] == EXPECTED_POLICY_REVISION
    agents = _fetch_all(
        sandbox,
        "SELECT agent_id, deleted_at, validated_policy_revision, "
        "revalidation_required FROM user_agent ORDER BY agent_id",
    )
    for agent in agents:
        assert agent["validated_policy_revision"] == "old-policy"
        if agent["deleted_at"] is None:
            assert agent["revalidation_required"] is True
        else:
            assert agent["revalidation_required"] is False


def test_failed_migration_rolls_back_before_marker_and_repeats_cleanly(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = isolated_database_factory()
    _seed_representative_057(sandbox)
    _prepare_legacy_root(monkeypatch)
    migrate = getattr(Database, "_migrate_runtime_reliability_060", None)
    assert migrate is not None

    def fail_after_ddl(self, *args, **kwargs):
        migrate(self, *args, **kwargs)
        raise RuntimeError("injected pre-commit migration failure")

    monkeypatch.setattr(Database, "_migrate_runtime_reliability_060", fail_after_ddl)
    with pytest.raises(RuntimeError, match="injected pre-commit"):
        Database(sandbox.dsn)
    assert _fetch_all(
        sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'"
    ) == []
    assert _fetch_one(
        sandbox, "SELECT to_regclass('public.operation_record') AS table_name"
    )["table_name"] is None

    monkeypatch.setattr(Database, "_migrate_runtime_reliability_060", migrate)
    Database(sandbox.dsn)
    assert _fetch_one(
        sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'"
    )["value"] == "066.001"


def test_current_and_forced_repeat_runs_are_idempotent(
    isolated_database_factory: Callable[[], _IsolatedDatabase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = isolated_database_factory()
    _seed_representative_057(sandbox)
    _prepare_legacy_root(monkeypatch)
    Database(sandbox.dsn)

    def snapshot() -> tuple[list[dict], list[dict], list[dict]]:
        return (
            _fetch_all(
                sandbox,
                "SELECT agent_id, revision_id::text, artifact_digest, state "
                "FROM user_agent_revision ORDER BY agent_id, revision_number",
            ),
            _fetch_all(
                sandbox,
                "SELECT id, draft_uuid::text, target_agent_id FROM draft_agents ORDER BY id",
            ),
            _fetch_all(
                sandbox,
                "SELECT tablename, indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' ORDER BY tablename, indexname",
            ),
        )

    expected = snapshot()
    Database(sandbox.dsn)
    assert snapshot() == expected

    with sandbox.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM schema_meta WHERE key = 'revision'")
        connection.commit()
    Database(sandbox.dsn)
    assert snapshot() == expected
