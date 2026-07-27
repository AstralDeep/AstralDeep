import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor
import asyncio
import atexit
import hashlib
import logging
import os
import json
import threading
import uuid
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from orchestrator.agent_constitution import (
    USER_AGENT_POLICY_REVISION,
    UserAgentPolicyOutcome,
)

logger = logging.getLogger('Database')

# 054.001: + user_llm_config, + system_llm_config (bring-your-own-LLM stores)
# 055.001: + component_version (refine/restore history), + share_grant
#          (snapshot share links) — both additive, inert while their flags
#          are off (FF_COMPONENT_REFINE / FF_ARTIFACT_SHARING)
# 055.002: + background_task (durable cross-device task records) — additive,
#          inert while FF_BG_CONTINUITY is off
# 060.004: + durable operation/admission, occurrence/effect, personal-agent
#          runtime, draft publication, maintenance, conversation-commit
#          coordination, and owner-scoped Run-now reconciliation. Additive and
#          guarded by fixed PostgreSQL advisory transaction identities.
SCHEMA_REVISION = '063.002'

_SCHEMA_ADVISORY_LOCK = (1095980114, 60001)
_USER_AGENT_POLICY_ADVISORY_LOCK = (1095980114, 60002)
_LEGACY_AGENT_REVISION_NAMESPACE = uuid.UUID('f5f7b28d-9a9c-4c51-a3be-47e832627060')

_POOLS: Dict[str, dict] = {}
_POOLS_LOCK = threading.Lock()
_POOL_ACQUIRE_TIMEOUT_S = 30.0


def _close_all_pools() -> None:
    """Close every shared connection pool (atexit hook / test teardown)."""
    with _POOLS_LOCK:
        for entry in _POOLS.values():
            try:
                entry['pool'].closeall()
            except Exception:
                pass
        _POOLS.clear()


atexit.register(_close_all_pools)


class _PooledConnectionProxy:
    """Wraps a pooled connection handed to ``_get_connection()`` callers.

    Repository callers ``close()`` what they borrow; closing this proxy
    returns the underlying connection to the shared pool instead of
    destroying it. All other attribute access passes through.
    """

    def __init__(self, conn, release):
        self._conn = conn
        self._release_cb = release
        self._released = False

    def close(self):
        """Return the underlying connection to its pool (idempotent)."""
        if not self._released:
            self._released = True
            self._release_cb(self._conn)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _KeepIdleToMaxPool(psycopg2.pool.ThreadedConnectionPool):
    """ThreadedConnectionPool that keeps idle connections up to ``maxconn``.

    The stock pool closes a returned connection once ``minconn`` are idle, so
    every concurrency burst re-creates connections one at a time INSIDE the
    pool's global lock — serializing all borrowers behind TCP+auth handshakes
    (FR-018 reuse / SC-011 burst latency). Idle retention up to ``maxconn``
    makes growth a one-time cost. ``putconn`` already holds the pool lock
    when ``_putconn`` runs, so the temporary ``minconn`` swap is race-free.
    """

    def _putconn(self, conn, key=None, close=False):
        real_min = self.minconn
        self.minconn = self.maxconn
        try:
            return super()._putconn(conn, key=key, close=close)
        finally:
            self.minconn = real_min


def _build_database_url() -> str:
    """Build a PostgreSQL connection URL from individual DB_* env vars.

    ``localhost`` is normalized to ``127.0.0.1``: libpq's IPv6-first attempt
    against a Docker-published port stalls ~2s per NEW connection on Windows
    hosts, and psycopg2's pool grows lazily while holding its global lock —
    so one slow connect serializes every borrower during a burst (SC-011).
    An operator who really wants IPv6 can set ``DB_HOST=::1`` explicitly.
    """
    host = os.getenv("DB_HOST", "localhost")
    if host.strip().lower() == "localhost":
        host = "127.0.0.1"
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "astraldeep")
    user = os.getenv("DB_USER", "astral")
    password = os.getenv("DB_PASSWORD", "astral_dev")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


class Database:
    def __init__(self, database_url: str = None):
        self.database_url = database_url or os.getenv("DATABASE_URL") or _build_database_url()
        self._user_agent_policy_sweep_count = None
        self._init_db()
        sweep_count = self._user_agent_policy_sweep_count
        self.user_agent_policy_outcome = UserAgentPolicyOutcome(
            policy_revision=USER_AGENT_POLICY_REVISION,
            marker_changed=sweep_count is not None,
            agents_marked_for_revalidation=(
                0 if sweep_count is None else sweep_count
            ),
        )
        del self._user_agent_policy_sweep_count

    @classmethod
    def close(cls) -> None:
        """Close all shared connection pools; the next call reopens lazily."""
        _close_all_pools()

    def _pool_entry(self) -> dict:
        """Return the ``{'pool', 'sem'}`` entry for this database_url.

        Pools live in a module-level dict keyed by URL because many
        ``Database`` instances are constructed across the codebase for the
        same database; they all share one bounded pool.
        """
        with _POOLS_LOCK:
            entry = _POOLS.get(self.database_url)
            if entry is None:
                max_conn = int(os.getenv('DB_POOL_MAX', '10'))
                entry = {
                    'pool': _KeepIdleToMaxPool(
                        int(os.getenv('DB_POOL_MIN', '2')),
                        max_conn,
                        dsn=self.database_url,
                        cursor_factory=RealDictCursor,
                    ),
                    'sem': threading.BoundedSemaphore(max_conn),
                }
                _POOLS[self.database_url] = entry
            return entry

    def _borrow(self):
        """Borrow a connection; returns ``(conn, pooled)``.

        ``DB_POOL_DISABLE=1`` restores the legacy connect-per-call path.
        When the pool is fully checked out, borrowers queue on a bounded
        semaphore instead of failing immediately.
        """
        if os.getenv('DB_POOL_DISABLE') == '1':
            return psycopg2.connect(self.database_url, cursor_factory=RealDictCursor), False
        entry = self._pool_entry()
        if not entry['sem'].acquire(timeout=_POOL_ACQUIRE_TIMEOUT_S):
            raise psycopg2.OperationalError('connection pool exhausted (DB_POOL_MAX)')
        try:
            return entry['pool'].getconn(), True
        except Exception:
            entry['sem'].release()
            raise

    def _release(self, conn, pooled: bool, discard: bool = False) -> None:
        """Return a borrowed connection; ``discard=True`` drops a stale one."""
        if not pooled:
            try:
                conn.close()
            except Exception:
                pass
            return
        entry = _POOLS.get(self.database_url)
        try:
            if entry is None:
                conn.close()
            else:
                entry['pool'].putconn(conn, close=discard)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
        finally:
            if entry is not None:
                try:
                    entry['sem'].release()
                except ValueError:
                    pass

    def _run_with_retry(self, op):
        """Run ``op(conn)`` on a borrowed connection, always returning it.

        A stale pooled connection (``OperationalError``/``InterfaceError``)
        is discarded via ``putconn(close=True)`` and the operation retried
        once on a freshly borrowed connection; a second failure propagates.
        The non-pooled kill-switch path never retries (legacy behavior).
        """
        conn, pooled = self._borrow()
        try:
            try:
                return op(conn)
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                if not pooled:
                    raise
                self._release(conn, pooled, discard=True)
                conn = None
                conn, pooled = self._borrow()
                return op(conn)
        finally:
            if conn is not None:
                self._release(conn, pooled)

    def _get_connection(self):
        """Get a database connection with dict-like row factory.

        In pooled mode this returns a proxy whose ``close()`` returns the
        connection to the shared pool — existing repository callers close
        what they borrow, and that must not destroy pooled connections.
        """
        conn, pooled = self._borrow()
        if not pooled:
            return conn
        return _PooledConnectionProxy(conn, lambda c: self._release(c, True))

    def _translate_query(self, query: str) -> str:
        """Convert SQLite ? placeholders to PostgreSQL %s placeholders.

        Also escapes literal % (e.g. in LIKE patterns) to %% so psycopg2
        doesn't interpret them as parameter placeholders.
        """
        # First escape any existing % that aren't parameter placeholders
        query = query.replace('%', '%%')
        # Then convert ? placeholders to %s
        return query.replace('?', '%s')

    def _column_exists(self, cursor, table_name: str, column_name: str) -> bool:
        """Check if a column exists on a table via information_schema."""
        cursor.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
            (table_name, column_name)
        )
        return cursor.fetchone() is not None

    def _init_db(self):
        """Ensure the database schema is current.

        Schema and user-agent-policy revisions have independent fixed
        PostgreSQL advisory transaction owners. Each marker is re-read only
        after its lock is acquired, so concurrent starters observe the
        winning transaction rather than repeating its work. Rollback:
        ``DELETE FROM schema_meta WHERE key='revision'`` forces a full schema
        run on the next boot.
        """
        from shared.perf import perf_span
        with perf_span('boot.init_db'):
            conn, pooled = self._borrow()
            try:
                cursor = conn.cursor()
                # This minimal bootstrap is committed before either advisory
                # transaction. It is the only schema statement outside the
                # fixed schema owner.
                cursor.execute(
                    'CREATE TABLE IF NOT EXISTS schema_meta ('
                    'key TEXT PRIMARY KEY, value TEXT NOT NULL)'
                )
                conn.commit()

                cursor.execute(
                    'SELECT pg_advisory_xact_lock(%s, %s)',
                    _SCHEMA_ADVISORY_LOCK,
                )
                cursor.execute("SELECT value FROM schema_meta WHERE key = 'revision'")
                row = cursor.fetchone()
                if row is None or row['value'] != SCHEMA_REVISION:
                    self._apply_full_schema(conn, cursor)
                    cursor.execute(
                        "INSERT INTO schema_meta (key, value) VALUES ('revision', %s) "
                        'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
                        (SCHEMA_REVISION,),
                    )
                conn.commit()

                cursor.execute(
                    'SELECT pg_advisory_xact_lock(%s, %s)',
                    _USER_AGENT_POLICY_ADVISORY_LOCK,
                )
                cursor.execute(
                    "SELECT value FROM schema_meta "
                    "WHERE key = 'user_agent_policy_revision'"
                )
                row = cursor.fetchone()
                if row is None or row['value'] != USER_AGENT_POLICY_REVISION:
                    self._sweep_user_agent_policy_060(cursor)
                    cursor.execute(
                        "INSERT INTO schema_meta (key, value) VALUES ("
                        "'user_agent_policy_revision', %s) "
                        'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
                        (USER_AGENT_POLICY_REVISION,),
                    )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    # A killed/crashed migration owner can lose its backend
                    # before local cleanup; preserve the original failure.
                    pass
                raise
            finally:
                self._release(conn, pooled)

    def _apply_full_schema(self, conn, cursor):
        """Run the full idempotent schema DDL + guarded migration set."""
        # Chats table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                title TEXT,
                created_at BIGINT,
                updated_at BIGINT
            )
        ''')

        # Messages table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                user_id TEXT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp BIGINT,
                FOREIGN KEY (chat_id) REFERENCES chats (id) ON DELETE CASCADE
            )
        ''')

        # Logs table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id SERIAL PRIMARY KEY,
                level TEXT,
                component TEXT,
                message TEXT,
                timestamp BIGINT
            )
        ''')

        # Saved UI components table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS saved_components (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                user_id TEXT,
                component_data TEXT NOT NULL,
                component_type TEXT NOT NULL,
                title TEXT,
                created_at BIGINT,
                FOREIGN KEY (chat_id) REFERENCES chats (id) ON DELETE CASCADE
            )
        ''')

        # Chat files mapping table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_files (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                user_id TEXT,
                original_name TEXT NOT NULL,
                backend_path TEXT NOT NULL,
                uploaded_at BIGINT,
                FOREIGN KEY (chat_id) REFERENCES chats (id) ON DELETE CASCADE
            )
        ''')

        # Add has_saved_components flag to chats table
        if not self._column_exists(cursor, 'chats', 'has_saved_components'):
            cursor.execute("ALTER TABLE chats ADD COLUMN has_saved_components BOOLEAN DEFAULT FALSE")

        # Feature 013: bind a chat session to its active agent so the UI can
        # render the active-agent indicator (FR-006) and detect when the
        # bound agent becomes unavailable (FR-009). NULL is allowed for
        # backward compatibility with chats created before this column
        # existed; the frontend renders an "Unknown agent" state for those.
        if not self._column_exists(cursor, 'chats', 'agent_id'):
            cursor.execute("ALTER TABLE chats ADD COLUMN agent_id TEXT NULL")

        # Auto-migrate user_id column for all tables
        for table in ['chats', 'messages', 'saved_components', 'chat_files']:
            if not self._column_exists(cursor, table, 'user_id'):
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT DEFAULT 'legacy'")

        # Tool permissions table (per-user, per-agent, per-tool)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tool_permissions (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                allowed BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at BIGINT,
                UNIQUE(user_id, agent_id, tool_name)
            )
        ''')

        # Per-user credentials for agents requiring external API keys
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_credentials (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                credential_key TEXT NOT NULL,
                encrypted_value TEXT NOT NULL,
                created_at BIGINT,
                updated_at BIGINT,
                UNIQUE(user_id, agent_id, credential_key)
            )
        ''')

        # Agent ownership and visibility
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS agent_ownership (
                id SERIAL PRIMARY KEY,
                agent_id TEXT NOT NULL UNIQUE,
                owner_email TEXT NOT NULL,
                is_public BOOLEAN NOT NULL DEFAULT FALSE,
                created_at BIGINT,
                updated_at BIGINT
            )
        ''')

        # Agent scopes — per-user, per-agent scope-based authorization
        # Replaces per-tool permissions with 4 scopes: tools:read, tools:write, tools:search, tools:system
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS agent_scopes (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at BIGINT,
                UNIQUE(user_id, agent_id, scope)
            )
        ''')

        # Tool overrides — per-user, per-agent, per-tool, per-permission-kind
        # permission rows (Feature 013).
        #
        # Pre-013 semantics: one row per (user, agent, tool) capturing a
        # tool-wide disable override (legacy "tool_overrides"). Rows have
        # `permission_kind = NULL`.
        #
        # Post-013 semantics: one row per (user, agent, tool, permission_kind)
        # capturing the per-tool, per-kind enable/disable state (FR-010).
        # Legacy NULL rows continue to work as tool-wide overrides until
        # superseded by per-kind rows during user edits or the FR-015
        # backfill (executed by ToolPermissionManager.backfill_per_tool_rows).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tool_overrides (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at BIGINT,
                UNIQUE(user_id, agent_id, tool_name)
            )
        ''')

        # Feature 013: extend tool_overrides with permission_kind. Legacy
        # rows have NULL kinds; new rows carry the specific scope kind.
        if not self._column_exists(cursor, 'tool_overrides', 'permission_kind'):
            cursor.execute("ALTER TABLE tool_overrides ADD COLUMN permission_kind TEXT NULL")
            # Drop the (user_id, agent_id, tool_name) unique constraint so
            # multiple per-kind rows can coexist for the same (user, agent,
            # tool). Replace with a unique index that includes permission_kind
            # via COALESCE so legacy NULL rows remain unique tool-wide.
            # PostgreSQL auto-generated constraint names follow the pattern
            # <table>_<col1>_<col2>_..._key. We use IF EXISTS to be safe in
            # case the constraint name varies.
            cursor.execute("""
                DO $$
                DECLARE
                    constraint_rec RECORD;
                BEGIN
                    FOR constraint_rec IN
                        SELECT conname FROM pg_constraint
                        WHERE conrelid = 'tool_overrides'::regclass
                          AND contype = 'u'
                          AND conname <> 'tool_overrides_user_agent_tool_kind_uniq'
                    LOOP
                        EXECUTE format('ALTER TABLE tool_overrides DROP CONSTRAINT %I', constraint_rec.conname);
                    END LOOP;
                END $$;
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS tool_overrides_user_agent_tool_kind_uniq
                ON tool_overrides (user_id, agent_id, tool_name, COALESCE(permission_kind, ''))
            """)

        # Users table — persists user profiles from Keycloak/OIDC
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT,
                username TEXT,
                display_name TEXT,
                roles TEXT,
                last_login_at BIGINT,
                created_at BIGINT,
                updated_at BIGINT
            )
        ''')

        # User preferences table — stores per-user settings (e.g. theme)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY,
                preferences TEXT NOT NULL DEFAULT '{}',
                updated_at BIGINT
            )
        ''')

        # Draft agents table — user-created agents in draft/testing/live states
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS draft_agents (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                agent_slug TEXT NOT NULL,
                description TEXT NOT NULL,
                tools_spec TEXT,
                skill_tags TEXT,
                packages TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                generation_log TEXT,
                security_report TEXT,
                error_message TEXT,
                port INTEGER,
                review_notes TEXT,
                reviewed_by TEXT,
                refinement_history TEXT,
                validation_report TEXT,
                required_credentials TEXT,
                created_at BIGINT,
                updated_at BIGINT
            )
        ''')

        # Feature 027 — agentic creation provenance on drafts. Additive,
        # idempotent (Constitution IX); all columns nullable or defaulted so
        # pre-027 code paths are unaffected. Rollback: redeploy prior image
        # (columns ignored) or ALTER TABLE draft_agents DROP COLUMN <col>.
        cursor.execute("ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'manual'")
        cursor.execute("ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS source_chat_id TEXT")
        cursor.execute("ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS gap_fingerprint TEXT")
        cursor.execute("ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS revises_agent_id TEXT")
        cursor.execute("ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS self_test TEXT")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_draft_gap "
            "ON draft_agents (user_id, source_chat_id, gap_fingerprint)"
        )

        # Feature 057 — bring-your-own client-side agents. Additive authoring
        # state on drafts (the 5-phase Specify→Clarify→Plan→Tasks→Analyze
        # journey rides draft_agents; origin gains the value 'byo_client').
        # Idempotent (Constitution IX); rollback: DROP COLUMN or redeploy prior
        # image. See specs/057-byo-client-agents/data-model.md.
        cursor.execute("ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS phase TEXT")
        cursor.execute("ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS clarify_answers TEXT")
        cursor.execute("ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS plan_json TEXT")
        cursor.execute("ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS analyze_result TEXT")
        cursor.execute("ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS constitution_version TEXT")
        cursor.execute("ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS host_binding TEXT")

        # Feature 057 — the durable user-agent registry. Distinct from
        # draft_agents (a transient authoring/codegen artifact) and from
        # in-memory liveness (socket presence). ``status`` is the durable
        # lifecycle (authoring|validated|live|disabled); running/offline is
        # DERIVED from socket presence and never persisted. ``is_public`` is
        # CHECK-pinned FALSE so privacy-by-construction is structural
        # (FR-019/020, Constitution K). Canonical owner key is
        # ``owner_user_id`` (OIDC sub). Rollback: DROP TABLE user_agent.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_agent (
                agent_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                owner_email TEXT,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'authoring',
                declared_tools TEXT NOT NULL DEFAULT '[]',
                declared_scopes TEXT NOT NULL DEFAULT '[]',
                declared_egress TEXT,
                constitution_version TEXT,
                validated_at BIGINT,
                revalidation_required BOOLEAN NOT NULL DEFAULT FALSE,
                draft_id TEXT,
                host_client_id TEXT,
                host_session_id TEXT,
                host_last_seen_at BIGINT,
                is_public BOOLEAN NOT NULL DEFAULT FALSE CHECK (is_public = FALSE),
                deleted_at BIGINT,
                created_at BIGINT,
                updated_at BIGINT
            )
        ''')
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_agent_owner "
            "ON user_agent (owner_user_id)"
        )

        # User attachments — chat-message file uploads, user-scoped (feature 002-file-uploads)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_attachments (
                attachment_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                category TEXT NOT NULL,
                extension TEXT NOT NULL,
                size_bytes BIGINT NOT NULL,
                sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                created_at BIGINT NOT NULL,
                deleted_at BIGINT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_attachments_user ON user_attachments(user_id, created_at DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_attachments_live ON user_attachments(user_id) WHERE deleted_at IS NULL')

        # --- Feature 031-attachment-upload-parsing -------------------------------
        # message_attachment: links a sent chat turn to the attachments the user
        # included, so the orchestrator can deliver structured references to the
        # handling agent and re-hydrate them on load_chat. App-enforced FKs.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_attachment (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                message_id TEXT,
                attachment_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                created_at BIGINT NOT NULL
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_message_attachment_chat ON message_attachment(chat_id, created_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_message_attachment_att ON message_attachment(attachment_id)')

        # attachment_parser: registry of globally-available parsers keyed by file
        # type, plus the dedup/provenance for the auto-creation flow. One row per
        # file-type gap (unique gap_fingerprint). status: pending|live|failed|discarded.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS attachment_parser (
                id TEXT PRIMARY KEY,
                extension TEXT,
                category TEXT NOT NULL,
                gap_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                draft_agent_id TEXT,
                live_agent_id TEXT,
                tool_name TEXT,
                source_attachment_id TEXT,
                source_chat_id TEXT,
                requested_by TEXT,
                approved_by TEXT,
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL
            )
        ''')
        cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_attachment_parser_gap ON attachment_parser(gap_fingerprint)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_attachment_parser_status ON attachment_parser(status)')

        # Guarded column add: lets an approved auto-created parser re-parse the
        # exact file that triggered its creation (FR-017). draft_agents is created
        # above, so the column add is safe here.
        if not self._column_exists(cursor, 'draft_agents', 'source_attachment_id'):
            cursor.execute("ALTER TABLE draft_agents ADD COLUMN source_attachment_id TEXT")
        # --- end Feature 031 ------------------------------------------------------

        # Interaction log — captures tool call outcomes for knowledge synthesis
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS interaction_log (
                id SERIAL PRIMARY KEY,
                agent_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                success BOOLEAN NOT NULL,
                error_message TEXT,
                response_time_ms INTEGER,
                chat_id TEXT,
                synthesized BOOLEAN DEFAULT FALSE,
                created_at BIGINT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_interaction_log_synthesized ON interaction_log(synthesized)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_interaction_log_agent ON interaction_log(agent_id)')

        # Audit events — feature 003-agent-audit-log
        # HIPAA + NIST SP 800-53 AU compliant per-user audit log.
        # Append-only (no UPDATE/DELETE except the retention CLI under a
        # session GUC). See backend/audit/ for the recorder/repository.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id UUID PRIMARY KEY,
                actor_user_id TEXT NOT NULL,
                auth_principal TEXT NOT NULL,
                agent_id TEXT,
                event_class TEXT NOT NULL,
                action_type TEXT NOT NULL,
                description TEXT NOT NULL,
                conversation_id TEXT,
                correlation_id UUID NOT NULL,
                outcome TEXT NOT NULL,
                outcome_detail TEXT,
                inputs_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
                outputs_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
                artifact_pointers JSONB NOT NULL DEFAULT '[]'::jsonb,
                started_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ,
                recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                prev_hash BYTEA NOT NULL,
                entry_hash BYTEA NOT NULL,
                key_id TEXT NOT NULL,
                schema_version SMALLINT NOT NULL DEFAULT 1,
                CONSTRAINT audit_events_outcome_check CHECK (outcome IN ('in_progress','success','failure','interrupted'))
            )
        ''')
        # Chain-ordering fix: recorded_at must be the INSERT instant, not the
        # transaction-start time. Two near-simultaneous chained inserts serialize
        # via pg_advisory_xact_lock (one inserts only after the other commits),
        # but both transactions START at ~the same instant while contending for
        # the lock, so `now()` (txn-start) TIES — and verify_chain's
        # recorded_at/event_id ordering then mis-orders by the random event_id,
        # falsely flagging the chain. `clock_timestamp()` is evaluated at INSERT
        # execution, which the lock guarantees is strictly ordered. Idempotent.
        cursor.execute('ALTER TABLE audit_events ALTER COLUMN recorded_at SET DEFAULT clock_timestamp()')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_user_recorded ON audit_events(actor_user_id, recorded_at DESC, event_id DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_correlation ON audit_events(correlation_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_user_class_recorded ON audit_events(actor_user_id, event_class, recorded_at DESC)')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_user_failures ON audit_events(actor_user_id, recorded_at DESC) WHERE outcome IN ('failure','interrupted')")

        # Append-only enforcement: a trigger that raises on UPDATE/DELETE
        # unless the session GUC ``audit.allow_purge`` is set to 'true'.
        # The retention CLI sets that GUC; application code never does.
        cursor.execute('''
            CREATE OR REPLACE FUNCTION audit_events_protect() RETURNS trigger AS $func$
            BEGIN
                IF current_setting('audit.allow_purge', true) IS DISTINCT FROM 'true' THEN
                    RAISE EXCEPTION 'audit_events is append-only (TG_OP=%)', TG_OP
                        USING ERRCODE = '42501';
                END IF;
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $func$ LANGUAGE plpgsql
        ''')
        cursor.execute("DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events")
        cursor.execute('''
            CREATE TRIGGER audit_events_no_update
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION audit_events_protect()
        ''')

        # Indexes on user_id for query performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_chats_user_id ON chats(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_saved_components_user_id ON saved_components(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_files_user_id ON chat_files(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_agent_scopes_user_id ON agent_scopes(user_id, agent_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tool_overrides_user_agent ON tool_overrides(user_id, agent_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_draft_agents_user_id ON draft_agents(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_draft_agents_status ON draft_agents(status)')

        # ------------------------------------------------------------------
        # Feature 004 — component feedback & tool-improvement loop
        # ------------------------------------------------------------------
        # ComponentFeedback: append-only with logical supersession; lifecycle
        # enforced at the repository layer. Per-user isolation enforced at
        # the application layer (mirrors audit_events from feature 003).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS component_feedback (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id TEXT NOT NULL,
                conversation_id TEXT,
                correlation_id TEXT,
                source_agent TEXT,
                source_tool TEXT,
                component_id TEXT,
                sentiment TEXT NOT NULL CHECK (sentiment IN ('positive','negative')),
                category TEXT NOT NULL DEFAULT 'unspecified'
                    CHECK (category IN
                        ('wrong-data','irrelevant','layout-broken','too-slow','other','unspecified')),
                comment_raw TEXT,
                comment_safety TEXT NOT NULL DEFAULT 'clean'
                    CHECK (comment_safety IN ('clean','quarantined')),
                comment_safety_reason TEXT,
                lifecycle TEXT NOT NULL DEFAULT 'active'
                    CHECK (lifecycle IN ('active','superseded','retracted')),
                superseded_by UUID REFERENCES component_feedback(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cf_user_created ON component_feedback(user_id, created_at DESC)')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cf_tool_created ON component_feedback(source_agent, source_tool, created_at DESC) WHERE lifecycle = 'active'")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cf_quarantine ON component_feedback(comment_safety, created_at DESC) WHERE comment_safety = 'quarantined'")
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cf_dedup_lookup ON component_feedback(user_id, correlation_id, component_id, created_at DESC)')

        # ToolQualitySignal: per-(agent, tool) snapshot per evaluation cycle.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tool_quality_signal (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                window_start TIMESTAMPTZ NOT NULL,
                window_end TIMESTAMPTZ NOT NULL,
                dispatch_count INTEGER NOT NULL,
                failure_count INTEGER NOT NULL,
                negative_feedback_count INTEGER NOT NULL,
                failure_rate REAL NOT NULL,
                negative_feedback_rate REAL NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('healthy','insufficient-data','underperforming')),
                computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (agent_id, tool_name, window_end)
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tqs_underperforming ON tool_quality_signal(computed_at DESC) WHERE status = 'underperforming'")

        # KnowledgeUpdateProposal: system-generated change to a synthesizer
        # knowledge artifact under backend/knowledge/. Always admin-gated.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS knowledge_update_proposal (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                diff_payload TEXT NOT NULL,
                artifact_sha_at_gen TEXT NOT NULL,
                evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','accepted','applied','rejected','superseded')),
                reviewer_user_id TEXT,
                reviewed_at TIMESTAMPTZ,
                reviewer_rationale TEXT,
                applied_at TIMESTAMPTZ,
                generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kup_pending ON knowledge_update_proposal(generated_at DESC) WHERE status = 'pending'")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kup_tool ON knowledge_update_proposal(agent_id, tool_name, generated_at DESC)")

        # QuarantineEntry: pointer to a flagged ComponentFeedback. One per
        # feedback record (enforced by PRIMARY KEY on feedback_id).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS quarantine_entry (
                feedback_id UUID PRIMARY KEY REFERENCES component_feedback(id) ON DELETE CASCADE,
                reason TEXT NOT NULL,
                detector TEXT NOT NULL CHECK (detector IN ('inline','loop_pre_pass')),
                detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                status TEXT NOT NULL DEFAULT 'held'
                    CHECK (status IN ('held','released','dismissed')),
                actor_user_id TEXT,
                actioned_at TIMESTAMPTZ
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_qe_held ON quarantine_entry(detected_at DESC) WHERE status = 'held'")

        # ------------------------------------------------------------------
        # Feature 005 — tool tips and getting started tutorial
        # ------------------------------------------------------------------
        # tutorial_step holds the canonical (current) copy of each step and
        # is editable by admins via /api/admin/tutorial/steps. Soft-delete
        # via archived_at so revisions and in-flight resumes stay valid.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tutorial_step (
                id BIGSERIAL PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                audience TEXT NOT NULL CHECK (audience IN ('user','admin')),
                display_order INTEGER NOT NULL,
                target_kind TEXT NOT NULL CHECK (target_kind IN ('static','sdui','none')),
                target_key TEXT,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                archived_at TIMESTAMPTZ,
                CONSTRAINT tutorial_step_target_consistent CHECK (
                    (target_kind = 'none' AND target_key IS NULL)
                    OR (target_kind IN ('static','sdui') AND target_key IS NOT NULL AND length(target_key) > 0)
                ),
                CONSTRAINT tutorial_step_title_nonempty CHECK (length(btrim(title)) > 0 AND length(title) <= 120),
                CONSTRAINT tutorial_step_body_nonempty CHECK (length(btrim(body)) > 0 AND length(body) <= 1000)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tutorial_step_user_view ON tutorial_step(archived_at, audience, display_order)')

        # onboarding_state — one row per user; absence is the implicit
        # "not_started" default so first-run is distinguishable from a
        # deliberately persisted state.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS onboarding_state (
                user_id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN ('not_started','in_progress','completed','skipped')),
                last_step_id BIGINT REFERENCES tutorial_step(id) ON DELETE SET NULL,
                started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                completed_at TIMESTAMPTZ,
                skipped_at TIMESTAMPTZ,
                dismissed_at TIMESTAMPTZ,
                dismiss_count INTEGER NOT NULL DEFAULT 0
            )
        ''')

        # Migration: US-17 — add dimissed_at / dismiss_count columns for
        # "not now" cooldown. Safe to run idempotently.
        for col, col_def in [
            ("dismissed_at", "TIMESTAMPTZ"),
            ("dismiss_count", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                cursor.execute(
                    f"ALTER TABLE onboarding_state ADD COLUMN IF NOT EXISTS {col} {col_def}"
                )
            except Exception:
                conn.rollback()

        # tutorial_step_revision — append-only history of admin edits.
        # Full before/after snapshots live here; the audit log only carries
        # a structured changed-fields summary.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tutorial_step_revision (
                id BIGSERIAL PRIMARY KEY,
                step_id BIGINT NOT NULL REFERENCES tutorial_step(id) ON DELETE CASCADE,
                editor_user_id TEXT NOT NULL,
                edited_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                previous JSONB,
                current JSONB NOT NULL,
                change_kind TEXT NOT NULL CHECK (change_kind IN ('create','update','archive','restore'))
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tutorial_step_revision_step_time ON tutorial_step_revision(step_id, edited_at DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tutorial_step_revision_editor ON tutorial_step_revision(editor_user_id, edited_at DESC)')

        # ------------------------------------------------------------------
        # Feature 014 — in-chat progress notifications & persistent step trail
        # ------------------------------------------------------------------
        # One row per persistent step entry (tool_call / agent_handoff / phase)
        # captured by ChatStepRecorder. Persisted alongside the chat's
        # messages so step history rehydrates with the chat.
        # See specs/014-progress-notifications/data-model.md.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_steps (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL DEFAULT 'legacy',
                turn_message_id INTEGER,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'in_progress',
                args_truncated TEXT,
                args_was_truncated BOOLEAN NOT NULL DEFAULT FALSE,
                result_summary TEXT,
                result_was_truncated BOOLEAN NOT NULL DEFAULT FALSE,
                error_message TEXT,
                started_at BIGINT NOT NULL,
                ended_at BIGINT,
                FOREIGN KEY (chat_id) REFERENCES chats (id) ON DELETE CASCADE,
                FOREIGN KEY (turn_message_id) REFERENCES messages (id) ON DELETE SET NULL
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_steps_chat_id ON chat_steps(chat_id, started_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_steps_turn ON chat_steps(turn_message_id)')

        # Render-time cache: number of step rows per turn-anchoring message.
        # Maintained by ChatStepRecorder on lifecycle transitions so the chat
        # list endpoint can show counts without joining chat_steps.
        if not self._column_exists(cursor, 'messages', 'step_count'):
            cursor.execute("ALTER TABLE messages ADD COLUMN step_count INTEGER NOT NULL DEFAULT 0")

        # ------------------------------------------------------------------
        # Feature 025 — agentic soul integration
        # Per-user personalization (profile + personality/"soul"), durable
        # non-PHI memory, scheduled jobs ("cron"), background consolidation
        # ("dreaming"), and the encrypted offline-grant store that authorizes
        # unattended job runs. All rows are strictly user-scoped.
        # See specs/025-agentic-soul-integration/data-model.md.
        # ------------------------------------------------------------------

        # One row per user: profession, goals, personality, dreaming toggle.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_personalization (
                user_id TEXT PRIMARY KEY,
                profession TEXT,
                goals JSONB NOT NULL DEFAULT '[]'::jsonb,
                personality JSONB NOT NULL DEFAULT '{}'::jsonb,
                dreaming_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at BIGINT,
                updated_at BIGINT
            )
        ''')

        # Durable, non-PHI personalization facts (structured-only categories).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS memory_item (
                id UUID PRIMARY KEY,
                user_id TEXT NOT NULL,
                category TEXT NOT NULL CHECK (category IN ('profession','goal','preference','workflow_tag','context')),
                value TEXT NOT NULL,
                source TEXT NOT NULL CHECK (source IN ('explicit','promoted')),
                salience REAL NOT NULL DEFAULT 0,
                created_at BIGINT,
                updated_at BIGINT,
                superseded_by UUID,
                superseded_at BIGINT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_memory_item_user_cat ON memory_item(user_id, category)')
        # Feature 033 (C-M1): reconcile-don't-append. A memory updated/deleted by
        # the LLM-mediated write path is soft-deleted (superseded_at set, optional
        # superseded_by pointer to its replacement) rather than hard-removed, so
        # history stays auditable and retrieval simply excludes superseded rows.
        # Idempotent ADD COLUMN for databases seeded before the columns existed.
        cursor.execute('ALTER TABLE memory_item ADD COLUMN IF NOT EXISTS superseded_by UUID')
        cursor.execute('ALTER TABLE memory_item ADD COLUMN IF NOT EXISTS superseded_at BIGINT')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_memory_item_live ON memory_item(user_id, superseded_at)')
        # Feature 033 (C-M2): A-MEM-style linked notes. Each memory carries
        # derived `keywords` (a self-organizing retrieval signal), and related
        # memories are connected in the undirected `memory_link` graph so recall
        # can pull in a hit's linked neighbours (single-step multi-hop).
        cursor.execute('ALTER TABLE memory_item ADD COLUMN IF NOT EXISTS keywords TEXT')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS memory_link (
                user_id TEXT NOT NULL,
                memory_id UUID NOT NULL,
                linked_id UUID NOT NULL,
                created_at BIGINT,
                PRIMARY KEY (user_id, memory_id, linked_id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_memory_link_from ON memory_link(user_id, memory_id)')
        # Feature 033 (C-S9): memory-poisoning defense. Optional HMAC signature
        # over the row's identifying fields (keyed by MEMORY_HMAC_KEY) so direct
        # tampering of a durable memory is detectable at retrieval. Nullable —
        # unsigned rows (no key, or pre-C-S9) are simply not flagged.
        cursor.execute('ALTER TABLE memory_item ADD COLUMN IF NOT EXISTS signature TEXT')

        # Feature 033 (C-M6): temporal validity. valid_from/valid_to bound when a
        # fact is in force (NULL = open); ingested_at records when it was learned
        # (vs created_at). Enables as-of queries + contradiction/abstention.
        cursor.execute('ALTER TABLE memory_item ADD COLUMN IF NOT EXISTS valid_from BIGINT')
        cursor.execute('ALTER TABLE memory_item ADD COLUMN IF NOT EXISTS valid_to BIGINT')
        cursor.execute('ALTER TABLE memory_item ADD COLUMN IF NOT EXISTS ingested_at BIGINT')
        # Feature 033 (C-M7): principled forgetting. recall_count + last_recalled_at
        # drive the Ebbinghaus retention curve (reinforcement-on-recall); a decayed
        # low-strength memory becomes a forgetting candidate.
        cursor.execute('ALTER TABLE memory_item ADD COLUMN IF NOT EXISTS recall_count INTEGER DEFAULT 0')
        cursor.execute('ALTER TABLE memory_item ADD COLUMN IF NOT EXISTS last_recalled_at BIGINT')
        # Feature 033 (C-U9): project-scoped memory partitioning. A memory tagged
        # to a project_id is private to that project; a NULL project_id is the
        # GLOBAL slice (visible in every project). NULL preserves today's behavior
        # (FF_PROJECT_MEMORY off ⇒ every write is global ⇒ this column stays NULL).
        cursor.execute('ALTER TABLE memory_item ADD COLUMN IF NOT EXISTS project_id TEXT')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_memory_item_user_project ON memory_item(user_id, project_id)')

        # Feature 033 (C-M8): evolving per-user persona (human-readable steering
        # text refined by keep-best; updated by recent turns + 004 feedback).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_persona (
                user_id TEXT PRIMARY KEY,
                persona TEXT NOT NULL DEFAULT '',
                score DOUBLE PRECISION DEFAULT 0,
                updated_at BIGINT
            )
        ''')

        # Transient promotion candidates; consumed/aged by the dreaming sweep.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS short_term_signal (
                id UUID PRIMARY KEY,
                user_id TEXT NOT NULL,
                category TEXT NOT NULL CHECK (category IN ('profession','goal','preference','workflow_tag','context')),
                value TEXT NOT NULL,
                recall_count INTEGER NOT NULL DEFAULT 0,
                last_seen_at BIGINT,
                created_at BIGINT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_short_term_signal_user_seen ON short_term_signal(user_id, last_seen_at)')

        # Encrypted offline-grant store (authority for unattended job runs).
        # refresh_token_enc is encrypted at rest and never returned by any API.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_offline_grant (
                id UUID PRIMARY KEY,
                user_id TEXT NOT NULL,
                agent_id TEXT,
                refresh_token_enc BYTEA NOT NULL,
                issued_at BIGINT NOT NULL,
                expires_at BIGINT NOT NULL,
                revoked_at BIGINT,
                created_at BIGINT,
                updated_at BIGINT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_offline_grant_active ON user_offline_grant(user_id, agent_id) WHERE revoked_at IS NULL')

        # User-defined recurring/future tasks.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_job (
                id UUID PRIMARY KEY,
                user_id TEXT NOT NULL,
                agent_id TEXT,
                name TEXT NOT NULL,
                instruction TEXT NOT NULL,
                schedule_kind TEXT NOT NULL CHECK (schedule_kind IN ('one_shot','interval','cron')),
                schedule_expr TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                consented_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
                delivery TEXT NOT NULL DEFAULT 'in_app' CHECK (delivery = 'in_app'),
                status TEXT NOT NULL CHECK (status IN ('active','paused','expired','completed','disabled')),
                target_chat_id TEXT,
                next_run_at BIGINT,
                last_run_at BIGINT,
                offline_grant_id UUID REFERENCES user_offline_grant(id) ON DELETE SET NULL,
                created_at BIGINT,
                updated_at BIGINT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_scheduled_job_due ON scheduled_job(status, next_run_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_scheduled_job_user ON scheduled_job(user_id, status)')

        # One execution record per job run.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS job_run (
                id UUID PRIMARY KEY,
                job_id UUID NOT NULL REFERENCES scheduled_job(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                started_at BIGINT NOT NULL,
                ended_at BIGINT,
                outcome TEXT NOT NULL CHECK (outcome IN ('running','success','failure','interrupted','skipped_auth')),
                auth_ref TEXT,
                correlation_id UUID NOT NULL,
                summary TEXT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_job_run_job_time ON job_run(job_id, started_at DESC)')

        # A "dream": one consolidation sweep's human-readable review record.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS consolidation_sweep (
                id UUID PRIMARY KEY,
                user_id TEXT NOT NULL,
                ran_at BIGINT NOT NULL,
                candidates_considered INTEGER NOT NULL DEFAULT 0,
                promoted_count INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL,
                trigger TEXT NOT NULL CHECK (trigger IN ('scheduled','manual'))
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_consolidation_sweep_user_time ON consolidation_sweep(user_id, ran_at DESC)')

        # ── Feature 028 — workspace-auth-revival ────────────────────────────
        # Durable server-side OIDC sessions (replaces web_auth's in-memory
        # dict as source of truth; tokens Fernet-encrypted at rest).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS web_session (
                sid TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                access_token_enc TEXT NOT NULL,
                refresh_token_enc TEXT NOT NULL,
                interactive_anchor BIGINT NOT NULL,
                hard_expires_at BIGINT NOT NULL,
                last_refresh_at BIGINT NOT NULL,
                resumed BOOLEAN DEFAULT FALSE,
                created_at BIGINT NOT NULL
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS ix_web_session_user ON web_session(user_id)')

        # Offline-tolerant sign-out: refresh tokens awaiting best-effort
        # revocation at Keycloak (server-side analog of 016's client queue).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS auth_revocation_queue (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                refresh_token_enc TEXT NOT NULL,
                enqueued_at BIGINT NOT NULL,
                attempts INTEGER DEFAULT 0
            )
        ''')
        # Feature 044 — native logout: Keycloak only revokes a token for its
        # issuing client, so queued retries must remember the originating
        # first-party client id (NULL = pre-044 row → web client id).
        # Rollback: ALTER TABLE auth_revocation_queue DROP COLUMN client_id.
        cursor.execute('''
            ALTER TABLE auth_revocation_queue
            ADD COLUMN IF NOT EXISTS client_id TEXT
        ''')

        # Per-turn full-state workspace snapshots (read-only timeline).
        # components carries message-grade content; lifecycle == the chat's.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS workspace_snapshot (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                turn_message_id INTEGER,
                cause TEXT NOT NULL,
                components TEXT NOT NULL,
                created_at BIGINT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats (id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS ix_workspace_snapshot_chat ON workspace_snapshot(chat_id, created_at)')
        # data-model.md declares turn_message_id as FK -> messages(id) ON
        # DELETE CASCADE. Added as a named constraint (idempotent; covers
        # deployments whose table predates the FK). NOT VALID skips
        # re-validating historic rows; new writes are enforced.
        cursor.execute('''
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'workspace_snapshot'
              AND constraint_name = 'fk_workspace_snapshot_turn_message'
        ''')
        if not cursor.fetchone():
            cursor.execute('''
                ALTER TABLE workspace_snapshot
                ADD CONSTRAINT fk_workspace_snapshot_turn_message
                FOREIGN KEY (turn_message_id) REFERENCES messages (id)
                ON DELETE CASCADE NOT VALID
            ''')

        # saved_components becomes the live workspace store: stable identity,
        # ordering, and in-place update timestamps (additive; legacy rows keep
        # NULLs and sort by created_at).
        for col, ddl in (("component_id", "TEXT"), ("position", "INTEGER"), ("updated_at", "BIGINT")):
            if not self._column_exists(cursor, 'saved_components', col):
                cursor.execute(f"ALTER TABLE saved_components ADD COLUMN {col} {ddl}")
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_saved_components_chat_component
            ON saved_components (chat_id, component_id) WHERE component_id IS NOT NULL
        ''')

        # ── Feature 029: adaptive UI designer — canvas arrangements ─────────
        # A layout is a per-round designed tree whose leaves REFERENCE live
        # saved_components rows by component_id (overlay model: layouts never
        # own component content, so dropping this table degrades cleanly to
        # the flat position-ordered canvas). Rollback: DROP TABLE.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS workspace_layout (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                layout_key TEXT NOT NULL,
                position INTEGER NOT NULL,
                layout TEXT NOT NULL,
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats (id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workspace_layout_chat_key
            ON workspace_layout (chat_id, layout_key)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS ix_workspace_layout_chat_pos
            ON workspace_layout (chat_id, position)
        ''')
        # Snapshots additionally capture the live arrangements at turn
        # boundaries so the read-only timeline can materialize historical
        # designed states. NULL == pre-029 snapshot (rendered flat).
        # Rollback: ALTER TABLE workspace_snapshot DROP COLUMN layouts.
        if not self._column_exists(cursor, 'workspace_snapshot', 'layouts'):
            cursor.execute("ALTER TABLE workspace_snapshot ADD COLUMN layouts TEXT NULL")

        # ── Feature 040: per-agent owner-approved "safe" trust record ───────
        # Distinct from agent_ownership.is_public (visibility). Drives the
        # check-time safe permission baseline (orchestrator/agent_trust.py).
        # Rollback: DROP TABLE IF EXISTS agent_trust;
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS agent_trust (
                agent_id TEXT PRIMARY KEY,
                is_safe BOOLEAN NOT NULL DEFAULT FALSE,
                marked_by TEXT,
                marked_at TIMESTAMPTZ,
                prior_state BOOLEAN,
                revised_reset_at TIMESTAMPTZ
            )
        ''')

        # ── Feature 063: remote-compute agents — inventory + per-machine creds ─
        # User-owned SSH targets (clusters + plain hosts). Never seeded; the
        # inventory starts empty for every user, and one user's machines are
        # invisible to every other (owner_user_id scoping).
        # Rollback: DROP TABLE IF EXISTS remote_machine;
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS remote_machine (
                machine_id            TEXT PRIMARY KEY,
                owner_user_id         TEXT NOT NULL,
                label                 TEXT NOT NULL,
                address               TEXT NOT NULL,
                port                  INTEGER NOT NULL DEFAULT 22,
                username              TEXT NOT NULL,
                os_family             TEXT NOT NULL,
                role                  TEXT NOT NULL,
                host_key_type         TEXT,
                host_key_fingerprint  TEXT,
                host_key_blob         TEXT,
                last_verdict          TEXT,
                last_checked_at       BIGINT,
                created_at            BIGINT NOT NULL,
                updated_at            BIGINT NOT NULL,
                CONSTRAINT ck_remote_machine_os   CHECK (os_family IN ('linux','windows','macos')),
                CONSTRAINT ck_remote_machine_role CHECK (role IN ('cluster','plain'))
            )
        ''')
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_remote_machine_owner "
            "ON remote_machine (owner_user_id)")

        # machine_credential: the secret for one machine, one user. Fernet at rest
        # under CREDENTIAL_ENCRYPTION_KEY. FR-014: because the agent runs in-process,
        # decrypted material transiently exists in orchestrator memory — the
        # protection is encryption at rest + per-user isolation, NOT process
        # isolation. Rollback: DROP TABLE IF EXISTS machine_credential;
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS machine_credential (
                machine_id           TEXT PRIMARY KEY
                                     REFERENCES remote_machine (machine_id) ON DELETE CASCADE,
                owner_user_id        TEXT NOT NULL,
                cred_type            TEXT NOT NULL,
                encrypted_secret     TEXT NOT NULL,
                encrypted_passphrase TEXT,
                created_at           BIGINT NOT NULL,
                updated_at           BIGINT NOT NULL,
                CONSTRAINT ck_machine_credential_type CHECK (cred_type IN ('ssh_key','password'))
            )
        ''')
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_machine_credential_owner "
            "ON machine_credential (owner_user_id)")

        # remote_operation_proposal: the durable, single-use, expiring, user- and
        # argument-bound record of an intended DESTRUCTIVE remote operation awaiting
        # explicit approval (feature 063 US3 / FR-029..FR-033). Survives restart.
        # Rollback: DROP TABLE IF EXISTS remote_operation_proposal;
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS remote_operation_proposal (
                proposal_id       TEXT PRIMARY KEY,
                owner_user_id     TEXT NOT NULL,
                chat_id           TEXT,
                machine_id        TEXT NOT NULL,
                agent_id          TEXT NOT NULL,
                verb              TEXT NOT NULL,
                args_json         TEXT NOT NULL,
                args_fingerprint  TEXT NOT NULL,
                summary           TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'pending',
                created_at        BIGINT NOT NULL,
                expires_at        BIGINT NOT NULL,
                decided_at        BIGINT,
                consumed_at       BIGINT,
                CONSTRAINT ck_rop_status
                    CHECK (status IN ('pending','approved','declined','expired','consumed'))
            )
        ''')
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_rop_owner_status "
            "ON remote_operation_proposal (owner_user_id, status)")

        # ── Feature 054: bring-your-own-LLM credential stores ───────────────
        # user_llm_config: one row per user who has completed provider setup;
        # api_key_enc is Fernet ciphertext under CREDENTIAL_ENCRYPTION_KEY
        # (NULL for keyless local-runtime presets). Absence of a decryptable
        # row IS the "unconfigured" state that triggers the mandatory
        # first-run provider-setup gate. Additive, no FKs.
        # Rollback: DROP TABLE IF EXISTS user_llm_config;
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_llm_config (
                user_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                base_url TEXT NOT NULL,
                model TEXT NOT NULL,
                api_key_enc TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        ''')
        # system_llm_config: zero-or-one admin-managed deployment credential,
        # used EXCLUSIVELY for system-context LLM calls (scheduled jobs,
        # codegen, knowledge synthesis, compaction, combine/condense,
        # narration). Never serves user chat and vice versa (FR-019).
        # Rollback: DROP TABLE IF EXISTS system_llm_config;
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_llm_config (
                id SMALLINT PRIMARY KEY CHECK (id = 1),
                provider TEXT NOT NULL,
                base_url TEXT NOT NULL,
                model TEXT NOT NULL,
                api_key_enc TEXT,
                updated_by TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        ''')

        # ── Feature 055 (US4): bounded per-component version history ────────
        # Archived component dicts written before a refine/restore overwrite;
        # retention pruned to the newest 5 per (chat, component) by the store.
        # Rollback: DROP TABLE IF EXISTS component_version;
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS component_version (
                id BIGSERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                component_id TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                component JSONB NOT NULL,
                reason TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (chat_id, component_id, version_no)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_component_version_lookup
            ON component_version (chat_id, component_id, version_no DESC)
        ''')

        # ── Feature 055 (US5): snapshot-scoped public share grants ──────────
        # Raw tokens are never stored (sha256 only); snapshots are immutable
        # renditions captured at mint — public serving never reads live rows.
        # Rollback: DROP TABLE IF EXISTS share_grant;
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS share_grant (
                id BIGSERIAL PRIMARY KEY,
                token_sha256 TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                component_id TEXT,
                snapshot_html TEXT NOT NULL,
                snapshot_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ,
                revoked_at TIMESTAMPTZ,
                open_count INTEGER NOT NULL DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_share_grant_owner
            ON share_grant (user_id, created_at DESC)
        ''')

        # ── Feature 055 (bg continuity): durable background-task records ────
        # Write-through bookkeeping from BackgroundTaskManager (fail-open)
        # that powers reconnect/late-join replay; `notified` flips TRUE once a
        # completion frame reached (or was replayed to) a client.
        # Rollback: DROP TABLE IF EXISTS background_task;
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS background_task (
                task_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'async_chat',
                status TEXT NOT NULL,
                title TEXT,
                summary TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                completed_at TIMESTAMPTZ,
                notified BOOLEAN NOT NULL DEFAULT FALSE
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_background_task_user
            ON background_task (user_id, created_at DESC)
        ''')

        # ── Feature 029: agent catalog migrations (data-model.md) ───────────
        self._migrate_agent_catalog_029(cursor)

        # ── Feature 039 (C-2): purge phantom Windows-tools agent rows ───────
        self._cleanup_phantom_windows_tools_ids(cursor)

        # ── Feature 040: retire etf_tracker_1 (orphan row purge) ────────────
        self._cleanup_retired_agents_040(cursor)

        # ── Feature 030: first-party agent visibility backfill ──────────────
        self._migrate_agent_visibility_030(cursor)

        # ── Feature 030: guided-tour content refresh ─────────────────────────
        self._migrate_tutorial_steps_030(cursor)

        # ── Feature 045: workspace-timeline tour step moved to the top bar ───
        self._migrate_tutorial_timeline_target_045(cursor)

        self._migrate_backfill_tool_kinds_052(cursor)

        # ── Feature 057: re-validate user agents against a bumped constitution ─
        self._migrate_revalidate_on_constitution_change_057(cursor)

        # ── Feature 060: runtime reliability coordination schema ───────────
        self._migrate_runtime_reliability_060(cursor)

    def _migrate_runtime_reliability_060(self, cursor):
        """Install the additive, repeat-safe feature-060 coordination schema."""
        self._migrate_operation_coordination_060(cursor)
        self._migrate_personal_agent_runtime_060(cursor)
        self._migrate_draft_publication_060(cursor)
        self._migrate_maintenance_coordination_060(cursor)
        self._migrate_conversation_commit_060(cursor)
        self._migrate_legacy_runtime_truth_060(cursor)

    def _migrate_operation_coordination_060(self, cursor):
        """Create operation, admission, occurrence, and effect authorities."""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS operation_admission_class (
                class_name TEXT PRIMARY KEY,
                parent_class_name TEXT REFERENCES operation_admission_class(class_name)
                    ON DELETE RESTRICT,
                active_limit INTEGER NOT NULL CHECK (active_limit > 0),
                queue_limit INTEGER NOT NULL CHECK (queue_limit >= 0),
                max_wait_ms INTEGER NOT NULL CHECK (
                    max_wait_ms >= 0 AND (queue_limit = 0 OR max_wait_ms > 0)
                ),
                config_revision TEXT NOT NULL CHECK (length(config_revision) BETWEEN 1 AND 128),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT operation_admission_class_name_check CHECK (
                    class_name IN (
                        'global', 'interactive', 'background', 'scheduled',
                        'maintenance', 'system'
                    )
                ),
                CONSTRAINT operation_admission_parent_check CHECK (
                    parent_class_name IS NULL OR parent_class_name <> class_name
                )
            )
        ''')
        cursor.execute('''
            INSERT INTO operation_admission_class (
                class_name, parent_class_name, active_limit, queue_limit,
                max_wait_ms, config_revision
            ) VALUES
                ('global', NULL, 20, 0, 0, '060-defaults'),
                ('interactive', 'global', 20, 100, 5000, '060-defaults'),
                ('background', 'global', 5, 100, 30000, '060-defaults'),
                ('scheduled', 'global', 5, 100, 30000, '060-defaults'),
                ('maintenance', 'global', 2, 100, 30000, '060-defaults'),
                ('system', 'global', 5, 100, 30000, '060-defaults')
            ON CONFLICT (class_name) DO NOTHING
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS operation_record (
                operation_id UUID PRIMARY KEY,
                operation_kind TEXT NOT NULL
                    CHECK (operation_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
                admission_class TEXT NOT NULL REFERENCES operation_admission_class(class_name)
                    ON DELETE RESTRICT CHECK (admission_class <> 'global'),
                owner_scope TEXT NOT NULL CHECK (
                    owner_scope IN ('connection', 'user', 'schedule', 'maintenance', 'system')
                ),
                owner_user_id TEXT,
                connection_scope_id UUID,
                idempotency_namespace TEXT,
                idempotency_key TEXT,
                normalized_input_digest CHAR(64),
                chat_id TEXT,
                parent_operation_id UUID REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL,
                connection_generation UUID,
                request_generation UUID,
                state TEXT NOT NULL CHECK (
                    state IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'retryable')
                ),
                phase_code TEXT CHECK (
                    phase_code IS NULL OR phase_code ~ '^[a-z][a-z0-9_]{0,127}$'
                ),
                terminal_code TEXT
                    CHECK (
                        terminal_code IS NULL
                        OR terminal_code ~ '^[a-z][a-z0-9_]{0,127}$'
                    ),
                safe_summary TEXT
                    CHECK (safe_summary IS NULL OR length(safe_summary) <= 512),
                retry_after_ms INTEGER CHECK (retry_after_ms IS NULL OR retry_after_ms >= 0),
                execution_generation BIGINT NOT NULL DEFAULT 0
                    CHECK (execution_generation >= 0),
                execution_lease_token UUID,
                state_revision BIGINT NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
                accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                queue_deadline_at TIMESTAMPTZ,
                started_at TIMESTAMPTZ,
                terminal_at TIMESTAMPTZ,
                cancel_requested_at TIMESTAMPTZ,
                purge_after TIMESTAMPTZ,
                CONSTRAINT operation_owner_partition_check CHECK (
                    (owner_scope IN ('user', 'schedule') AND owner_user_id IS NOT NULL)
                    OR (owner_scope = 'connection' AND owner_user_id IS NULL
                        AND connection_scope_id IS NOT NULL)
                    OR (owner_scope IN ('maintenance', 'system') AND owner_user_id IS NULL)
                ),
                CONSTRAINT operation_idempotency_tuple_check CHECK (
                    (idempotency_namespace IS NULL AND idempotency_key IS NULL
                        AND normalized_input_digest IS NULL)
                    OR (idempotency_namespace IS NOT NULL AND idempotency_key IS NOT NULL
                        AND normalized_input_digest ~ '^[0-9a-f]{64}$'
                        AND length(idempotency_namespace) BETWEEN 1 AND 128
                        AND length(idempotency_key) BETWEEN 1 AND 256)
                ),
                CONSTRAINT operation_state_times_check CHECK (
                    (state IN ('completed', 'failed', 'cancelled', 'retryable')
                        AND terminal_at IS NOT NULL AND purge_after IS NOT NULL
                        AND (state = 'completed' OR terminal_code IS NOT NULL))
                    OR (state IN ('queued', 'running')
                        AND terminal_at IS NULL AND purge_after IS NULL)
                ),
                CONSTRAINT operation_start_time_check CHECK (
                    (state = 'queued' AND started_at IS NULL)
                    OR (state = 'running' AND started_at IS NOT NULL)
                    OR (state IN ('completed', 'failed', 'cancelled', 'retryable')
                        AND (
                            (execution_generation = 0 AND started_at IS NULL)
                            OR (execution_generation > 0 AND started_at IS NOT NULL)
                        ))
                ),
                CONSTRAINT operation_terminal_payload_check CHECK (
                    (state IN ('queued', 'running')
                        AND terminal_code IS NULL
                        AND safe_summary IS NULL
                        AND retry_after_ms IS NULL)
                    OR (state = 'completed' AND retry_after_ms IS NULL)
                    OR (state IN ('failed', 'cancelled') AND retry_after_ms IS NULL)
                    OR state = 'retryable'
                ),
                CONSTRAINT operation_queue_deadline_check CHECK (
                    state <> 'queued' OR queue_deadline_at IS NOT NULL
                ),
                CONSTRAINT operation_retry_delay_check CHECK (
                    retry_after_ms IS NULL OR state = 'retryable'
                ),
                CONSTRAINT operation_parent_check CHECK (
                    parent_operation_id IS NULL OR parent_operation_id <> operation_id
                ),
                CONSTRAINT operation_execution_fence_check CHECK (
                    (state = 'queued' AND execution_generation = 0
                        AND execution_lease_token IS NULL)
                    OR (state = 'running' AND execution_generation > 0
                        AND execution_lease_token IS NOT NULL)
                    OR (state IN ('completed', 'failed', 'cancelled', 'retryable')
                        AND execution_lease_token IS NULL)
                )
            )
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_operation_idempotency_owner
            ON operation_record (
                owner_scope,
                (CASE
                    WHEN owner_scope = 'connection' THEN connection_scope_id::text
                    WHEN owner_scope IN ('user', 'schedule') THEN owner_user_id
                    ELSE ''
                END),
                idempotency_namespace,
                idempotency_key
            )
            WHERE idempotency_namespace IS NOT NULL
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_operation_state_fifo
            ON operation_record (state, accepted_at, operation_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_operation_owner_recent
            ON operation_record (owner_scope, owner_user_id, accepted_at DESC)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_operation_connection_state
            ON operation_record (connection_scope_id, state)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_operation_terminal_purge
            ON operation_record (purge_after) WHERE purge_after IS NOT NULL
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS operation_admission_slot (
                class_name TEXT NOT NULL REFERENCES operation_admission_class(class_name)
                    ON DELETE CASCADE,
                slot_number INTEGER NOT NULL CHECK (slot_number > 0),
                operation_id UUID REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL,
                lease_token UUID,
                claim_generation BIGINT NOT NULL DEFAULT 0 CHECK (claim_generation >= 0),
                lease_expires_at TIMESTAMPTZ,
                PRIMARY KEY (class_name, slot_number),
                CONSTRAINT operation_slot_occupancy_check CHECK (
                    (operation_id IS NULL AND lease_token IS NULL
                        AND lease_expires_at IS NULL)
                    OR (operation_id IS NOT NULL AND lease_token IS NOT NULL
                        AND lease_expires_at IS NOT NULL)
                )
            )
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_operation_slot_class_operation
            ON operation_admission_slot (class_name, operation_id)
            WHERE operation_id IS NOT NULL
        ''')
        cursor.execute('''
            INSERT INTO operation_admission_slot (class_name, slot_number)
            SELECT c.class_name, series.slot_number
            FROM operation_admission_class AS c
            CROSS JOIN LATERAL generate_series(1, c.active_limit) AS series(slot_number)
            ON CONFLICT (class_name, slot_number) DO NOTHING
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS operation_submission_result (
                submission_result_id UUID PRIMARY KEY,
                submission_id UUID NOT NULL,
                owner_scope TEXT NOT NULL CHECK (
                    owner_scope IN ('connection', 'user', 'schedule', 'maintenance', 'system')
                ),
                owner_user_id TEXT,
                connection_scope_id UUID,
                accepted BOOLEAN NOT NULL,
                operation_id UUID REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL,
                refusal_code TEXT CHECK (
                    refusal_code IS NULL
                    OR refusal_code ~ '^[a-z][a-z0-9_]{0,127}$'
                ),
                retryable BOOLEAN NOT NULL DEFAULT FALSE,
                retry_after_ms INTEGER CHECK (retry_after_ms IS NULL OR retry_after_ms >= 0),
                observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                purge_after TIMESTAMPTZ NOT NULL,
                CONSTRAINT operation_submission_owner_check CHECK (
                    (owner_scope IN ('user', 'schedule') AND owner_user_id IS NOT NULL)
                    OR (owner_scope = 'connection' AND owner_user_id IS NULL
                        AND connection_scope_id IS NOT NULL)
                    OR (owner_scope IN ('maintenance', 'system') AND owner_user_id IS NULL)
                ),
                CONSTRAINT operation_submission_outcome_check CHECK (
                    (accepted AND refusal_code IS NULL
                        AND retryable = FALSE AND retry_after_ms IS NULL)
                    OR (NOT accepted AND operation_id IS NULL AND refusal_code IS NOT NULL
                        AND (retry_after_ms IS NULL OR retryable))
                )
            )
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_operation_submission_owner
            ON operation_submission_result (
                submission_id,
                owner_scope,
                (CASE
                    WHEN owner_scope = 'connection' THEN connection_scope_id::text
                    WHEN owner_scope IN ('user', 'schedule') THEN owner_user_id
                    ELSE ''
                END)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_operation_submission_purge
            ON operation_submission_result (purge_after)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_occurrence (
                occurrence_id UUID PRIMARY KEY,
                job_id UUID NOT NULL REFERENCES scheduled_job(id) ON DELETE RESTRICT,
                owner_user_id TEXT NOT NULL,
                scheduled_for TIMESTAMPTZ NOT NULL,
                run_now_submission_id UUID,
                state TEXT NOT NULL CHECK (
                    state IN ('pending', 'claimed', 'running', 'completed', 'failed',
                              'retryable', 'cancelled')
                ),
                lease_token UUID,
                claim_generation BIGINT NOT NULL DEFAULT 0 CHECK (claim_generation >= 0),
                lease_owner TEXT,
                lease_expires_at TIMESTAMPTZ,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                current_operation_id UUID REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL,
                operation_execution_generation BIGINT
                    CHECK (operation_execution_generation IS NULL
                           OR operation_execution_generation > 0),
                first_eligible_at TIMESTAMPTZ NOT NULL,
                started_at TIMESTAMPTZ,
                terminal_at TIMESTAMPTZ,
                next_attempt_at TIMESTAMPTZ,
                result_code TEXT,
                last_error_code TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (job_id, scheduled_for),
                CONSTRAINT scheduled_occurrence_lease_check CHECK (
                    (state IN ('claimed', 'running') AND lease_token IS NOT NULL
                        AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
                    OR (state NOT IN ('claimed', 'running'))
                ),
                CONSTRAINT scheduled_occurrence_terminal_check CHECK (
                    (state IN ('completed', 'failed', 'cancelled') AND terminal_at IS NOT NULL)
                    OR (state NOT IN ('completed', 'failed', 'cancelled'))
                )
            )
        ''')
        cursor.execute('''
            ALTER TABLE scheduled_occurrence
            ADD COLUMN IF NOT EXISTS run_now_submission_id UUID
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_scheduled_occurrence_run_now_submission
            ON scheduled_occurrence (owner_user_id, run_now_submission_id)
            WHERE run_now_submission_id IS NOT NULL
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_scheduled_occurrence_state_due
            ON scheduled_occurrence (state, scheduled_for)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_scheduled_occurrence_lease
            ON scheduled_occurrence (state, lease_expires_at)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS effect_ledger (
                occurrence_id UUID NOT NULL REFERENCES scheduled_occurrence(occurrence_id)
                    ON DELETE RESTRICT,
                effect_kind TEXT NOT NULL CHECK (effect_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
                effect_key TEXT NOT NULL CHECK (length(effect_key) BETWEEN 1 AND 256),
                payload_digest CHAR(64) NOT NULL
                    CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
                state TEXT NOT NULL CHECK (state IN ('reserved', 'published', 'failed')),
                operation_id UUID REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL,
                operation_execution_generation BIGINT NOT NULL
                    CHECK (operation_execution_generation > 0),
                occurrence_claim_generation BIGINT NOT NULL
                    CHECK (occurrence_claim_generation > 0),
                reserved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                published_at TIMESTAMPTZ,
                failed_at TIMESTAMPTZ,
                failure_code TEXT,
                downstream_receipt_digest CHAR(64)
                    CHECK (downstream_receipt_digest IS NULL
                           OR downstream_receipt_digest ~ '^[0-9a-f]{64}$'),
                PRIMARY KEY (occurrence_id, effect_kind, effect_key),
                CONSTRAINT effect_ledger_terminal_check CHECK (
                    (state = 'reserved' AND published_at IS NULL AND failed_at IS NULL)
                    OR (state = 'published' AND published_at IS NOT NULL AND failed_at IS NULL)
                    OR (state = 'failed' AND published_at IS NULL AND failed_at IS NOT NULL)
                )
            )
        ''')

        cursor.execute(
            "ALTER TABLE background_task "
            "ADD COLUMN IF NOT EXISTS operation_id UUID"
        )
        cursor.execute(
            "ALTER TABLE background_task ADD COLUMN IF NOT EXISTS "
            "operation_execution_generation BIGINT"
        )
        cursor.execute('''
            ALTER TABLE job_run ADD COLUMN IF NOT EXISTS occurrence_id UUID
        ''')
        cursor.execute('''
            ALTER TABLE job_run ADD COLUMN IF NOT EXISTS attempt_number INTEGER
        ''')
        cursor.execute('''
            ALTER TABLE job_run ADD COLUMN IF NOT EXISTS operation_id UUID
        ''')
        cursor.execute('''
            ALTER TABLE job_run ADD COLUMN IF NOT EXISTS operation_execution_generation BIGINT
        ''')
        cursor.execute('''
            ALTER TABLE job_run ADD COLUMN IF NOT EXISTS occurrence_claim_generation BIGINT
        ''')
        cursor.execute('''
            DO $migration$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'background_task_operation_060_fk'
                      AND conrelid = 'background_task'::regclass
                ) THEN
                    ALTER TABLE background_task
                    ADD CONSTRAINT background_task_operation_060_fk
                    FOREIGN KEY (operation_id) REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'job_run_occurrence_060_fk'
                      AND conrelid = 'job_run'::regclass
                ) THEN
                    ALTER TABLE job_run
                    ADD CONSTRAINT job_run_occurrence_060_fk
                    FOREIGN KEY (occurrence_id) REFERENCES scheduled_occurrence(occurrence_id)
                    ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'job_run_operation_060_fk'
                      AND conrelid = 'job_run'::regclass
                ) THEN
                    ALTER TABLE job_run
                    ADD CONSTRAINT job_run_operation_060_fk
                    FOREIGN KEY (operation_id) REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL;
                END IF;
            END
            $migration$;
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_job_run_occurrence_attempt
            ON job_run (occurrence_id, attempt_number)
            WHERE occurrence_id IS NOT NULL
        ''')

    def _migrate_personal_agent_runtime_060(self, cursor):
        """Create immutable agent revision, host, runtime, and request fences."""
        cursor.execute('''
            CREATE OR REPLACE FUNCTION astraldeep_positive_unique_int_array(
                input_values INTEGER[]
            ) RETURNS BOOLEAN
            LANGUAGE SQL IMMUTABLE STRICT
            AS $function$
                SELECT cardinality(input_values) > 0
                   AND NOT EXISTS (
                       SELECT 1 FROM unnest(input_values) AS item(value)
                       WHERE value <= 0
                   )
                   AND cardinality(input_values) = (
                       SELECT count(DISTINCT value) FROM unnest(input_values) AS item(value)
                   )
            $function$
        ''')

        cursor.execute(
            "ALTER TABLE user_agent ADD COLUMN IF NOT EXISTS active_revision_id UUID"
        )
        cursor.execute(
            "ALTER TABLE user_agent ADD COLUMN IF NOT EXISTS last_known_good_revision_id UUID"
        )
        cursor.execute(
            "ALTER TABLE user_agent ADD COLUMN IF NOT EXISTS selected_host_session_id UUID"
        )
        cursor.execute(
            "ALTER TABLE user_agent ADD COLUMN IF NOT EXISTS authoritative_instance_id UUID"
        )
        cursor.execute(
            "ALTER TABLE user_agent ADD COLUMN IF NOT EXISTS "
            "lifecycle_generation BIGINT NOT NULL DEFAULT 0"
        )
        cursor.execute(
            "ALTER TABLE user_agent ADD COLUMN IF NOT EXISTS "
            "generation_counter BIGINT NOT NULL DEFAULT 0"
        )
        cursor.execute(
            "ALTER TABLE user_agent ADD COLUMN IF NOT EXISTS "
            "state_revision BIGINT NOT NULL DEFAULT 0"
        )
        cursor.execute(
            "ALTER TABLE user_agent ADD COLUMN IF NOT EXISTS validated_policy_revision TEXT"
        )
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_user_agent_agent_owner
            ON user_agent (agent_id, owner_user_id)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_agent_revision (
                revision_id UUID PRIMARY KEY,
                agent_id TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                revision_number BIGINT NOT NULL CHECK (revision_number >= 0),
                parent_revision_id UUID,
                previous_good_revision_id UUID,
                artifact_digest CHAR(64)
                    CHECK (artifact_digest IS NULL OR artifact_digest ~ '^[0-9a-f]{64}$'),
                manifest_json JSONB,
                artifact_relative_path TEXT,
                runtime_contract_version INTEGER
                    CHECK (runtime_contract_version IS NULL OR runtime_contract_version > 0),
                release_lock_digest CHAR(64)
                    CHECK (release_lock_digest IS NULL OR release_lock_digest ~ '^[0-9a-f]{64}$'),
                compatibility_state TEXT NOT NULL CHECK (
                    compatibility_state IN ('compatible', 'incompatible', 'legacy_pending')
                ),
                state TEXT NOT NULL CHECK (
                    state IN ('legacy_pending', 'prepared', 'starting', 'ready',
                              'active', 'retired', 'failed')
                ),
                promotion_token UUID,
                state_revision BIGINT NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                confirmed_at TIMESTAMPTZ,
                promoted_at TIMESTAMPTZ,
                failed_at TIMESTAMPTZ,
                failure_code TEXT,
                UNIQUE (agent_id, revision_number),
                UNIQUE (revision_id, agent_id, owner_user_id),
                FOREIGN KEY (agent_id, owner_user_id)
                    REFERENCES user_agent(agent_id, owner_user_id) ON DELETE RESTRICT,
                FOREIGN KEY (parent_revision_id, agent_id, owner_user_id)
                    REFERENCES user_agent_revision(revision_id, agent_id, owner_user_id)
                    ON DELETE SET NULL (parent_revision_id),
                FOREIGN KEY (previous_good_revision_id, agent_id, owner_user_id)
                    REFERENCES user_agent_revision(revision_id, agent_id, owner_user_id)
                    ON DELETE SET NULL (previous_good_revision_id),
                CONSTRAINT user_agent_revision_artifact_check CHECK (
                    (compatibility_state = 'legacy_pending' AND state = 'legacy_pending')
                    OR (compatibility_state <> 'legacy_pending'
                        AND artifact_digest IS NOT NULL
                        AND manifest_json IS NOT NULL
                        AND artifact_relative_path IS NOT NULL
                        AND runtime_contract_version IS NOT NULL
                        AND release_lock_digest IS NOT NULL
                        AND promotion_token IS NOT NULL)
                ),
                CONSTRAINT user_agent_revision_relative_path_check CHECK (
                    artifact_relative_path IS NULL
                    OR (artifact_relative_path !~ '^/'
                        AND artifact_relative_path !~ '(^|/)\\.\\.(/|$)')
                )
            )
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_user_agent_revision_artifact
            ON user_agent_revision (agent_id, artifact_digest)
            WHERE artifact_digest IS NOT NULL
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS agent_host_session (
                host_session_id UUID PRIMARY KEY,
                host_id UUID NOT NULL,
                owner_user_id TEXT NOT NULL,
                connection_scope_id UUID NOT NULL,
                platform TEXT NOT NULL CHECK (platform IN ('windows', 'macos')),
                client_version TEXT NOT NULL CHECK (
                    client_version ~ '^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(-(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(\\.(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(\\+[0-9A-Za-z-]+(\\.[0-9A-Za-z-]+)*)?$'
                ),
                host_generation BIGINT NOT NULL CHECK (host_generation > 0),
                supersedes_session_id UUID REFERENCES agent_host_session(host_session_id)
                    ON DELETE SET NULL,
                supported_runtime_contract_versions INTEGER[] NOT NULL CHECK (
                    astraldeep_positive_unique_int_array(supported_runtime_contract_versions)
                ),
                runtime_contract_version INTEGER NOT NULL CHECK (
                    runtime_contract_version = 2
                    AND runtime_contract_version = ANY(supported_runtime_contract_versions)
                ),
                release_lock_digest CHAR(64) NOT NULL
                    CHECK (release_lock_digest ~ '^[0-9a-f]{64}$'),
                state TEXT NOT NULL CHECK (
                    state IN ('connected', 'disconnected', 'incompatible')
                ),
                inventory_state TEXT NOT NULL CHECK (
                    inventory_state IN ('pending', 'reconciled', 'failed')
                ),
                eligible_since TIMESTAMPTZ NOT NULL,
                accepted_at TIMESTAMPTZ NOT NULL,
                last_seen_at TIMESTAMPTZ NOT NULL,
                disconnected_at TIMESTAMPTZ,
                inventory_reconciled_at TIMESTAMPTZ,
                failure_code TEXT,
                UNIQUE (owner_user_id, host_id, host_generation),
                CONSTRAINT agent_host_session_connected_check CHECK (
                    state <> 'connected' OR disconnected_at IS NULL
                )
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_agent_host_owner_state
            ON agent_host_session (owner_user_id, state, eligible_since)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS agent_runtime_instance (
                runtime_instance_id UUID PRIMARY KEY,
                agent_id TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                host_id UUID NOT NULL,
                host_session_id UUID NOT NULL REFERENCES agent_host_session(host_session_id)
                    ON DELETE RESTRICT,
                delivery_id UUID NOT NULL UNIQUE,
                revision_id UUID NOT NULL,
                process_id UUID,
                lifecycle_generation BIGINT NOT NULL CHECK (lifecycle_generation > 0),
                runtime_contract_version INTEGER NOT NULL
                    CHECK (runtime_contract_version > 0),
                operation_id UUID REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL,
                operation_execution_generation BIGINT NOT NULL
                    CHECK (operation_execution_generation > 0),
                state TEXT NOT NULL CHECK (
                    state IN ('delivering', 'starting', 'ready', 'online', 'updating',
                              'stopping', 'stopped', 'failed', 'offline', 'superseded')
                ),
                is_authoritative BOOLEAN NOT NULL DEFAULT FALSE,
                state_revision BIGINT NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                started_at TIMESTAMPTZ,
                registered_at TIMESTAMPTZ,
                last_heartbeat_sequence BIGINT,
                ready_at TIMESTAMPTZ,
                last_liveness_at TIMESTAMPTZ,
                terminal_at TIMESTAMPTZ,
                failure_code TEXT,
                UNIQUE (agent_id, lifecycle_generation),
                UNIQUE (runtime_instance_id, agent_id, owner_user_id),
                FOREIGN KEY (agent_id, owner_user_id)
                    REFERENCES user_agent(agent_id, owner_user_id) ON DELETE RESTRICT,
                FOREIGN KEY (revision_id, agent_id, owner_user_id)
                    REFERENCES user_agent_revision(revision_id, agent_id, owner_user_id)
                    ON DELETE RESTRICT,
                CONSTRAINT agent_runtime_process_bind_check CHECK (
                    (state = 'delivering' AND process_id IS NULL)
                    OR state = 'failed'
                    OR (state NOT IN ('delivering', 'failed') AND process_id IS NOT NULL)
                ),
                CONSTRAINT agent_runtime_registration_liveness_060_check CHECK (
                    (registered_at IS NULL
                        AND last_heartbeat_sequence IS NULL
                        AND last_liveness_at IS NULL)
                    OR (registered_at IS NOT NULL AND process_id IS NOT NULL
                        AND (
                            (last_heartbeat_sequence IS NULL
                                AND last_liveness_at IS NULL)
                            OR (last_heartbeat_sequence IS NOT NULL
                                AND last_heartbeat_sequence > 0
                                AND last_liveness_at IS NOT NULL)
                        ))
                )
            )
        ''')
        # ``CREATE TABLE IF NOT EXISTS`` does not add columns to a database that
        # already created an earlier feature-060 draft of this table. Keep these
        # ALTERs repeat-safe so every pre-release feature-060 database converges.
        cursor.execute(
            "ALTER TABLE agent_runtime_instance "
            "ADD COLUMN IF NOT EXISTS registered_at TIMESTAMPTZ"
        )
        cursor.execute(
            "ALTER TABLE agent_runtime_instance "
            "ADD COLUMN IF NOT EXISTS last_heartbeat_sequence BIGINT"
        )
        cursor.execute(
            "ALTER TABLE agent_runtime_instance DROP CONSTRAINT IF EXISTS "
            "agent_runtime_heartbeat_sequence_060_check"
        )
        cursor.execute('''
            DO $migration$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'agent_runtime_registration_liveness_060_check'
                      AND conrelid = 'agent_runtime_instance'::regclass
                ) THEN
                    ALTER TABLE agent_runtime_instance
                    ADD CONSTRAINT agent_runtime_registration_liveness_060_check CHECK (
                        (registered_at IS NULL
                            AND last_heartbeat_sequence IS NULL
                            AND last_liveness_at IS NULL)
                        OR (registered_at IS NOT NULL AND process_id IS NOT NULL
                            AND (
                                (last_heartbeat_sequence IS NULL
                                    AND last_liveness_at IS NULL)
                                OR (last_heartbeat_sequence IS NOT NULL
                                    AND last_heartbeat_sequence > 0
                                    AND last_liveness_at IS NOT NULL)
                            ))
                    );
                END IF;
            END
            $migration$;
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_runtime_authoritative
            ON agent_runtime_instance (agent_id) WHERE is_authoritative
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_runtime_host_process
            ON agent_runtime_instance (host_id, process_id) WHERE process_id IS NOT NULL
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_agent_runtime_host_state
            ON agent_runtime_instance (host_session_id, state)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS agent_runtime_request (
                request_id UUID PRIMARY KEY,
                request_generation UUID NOT NULL,
                operation_id UUID REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL,
                operation_execution_generation BIGINT NOT NULL
                    CHECK (operation_execution_generation > 0),
                runtime_instance_id UUID NOT NULL,
                agent_id TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('assigned', 'running', 'completed', 'failed',
                              'cancelled', 'retryable')
                ),
                state_revision BIGINT NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
                assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                terminal_at TIMESTAMPTZ,
                terminal_code TEXT,
                result_digest CHAR(64)
                    CHECK (result_digest IS NULL OR result_digest ~ '^[0-9a-f]{64}$'),
                UNIQUE (runtime_instance_id, request_generation),
                FOREIGN KEY (runtime_instance_id, agent_id, owner_user_id)
                    REFERENCES agent_runtime_instance(
                        runtime_instance_id, agent_id, owner_user_id
                    ) ON DELETE RESTRICT,
                CONSTRAINT agent_runtime_request_terminal_check CHECK (
                    (state IN ('completed', 'failed', 'cancelled', 'retryable')
                        AND terminal_at IS NOT NULL)
                    OR (state IN ('assigned', 'running') AND terminal_at IS NULL)
                )
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_agent_runtime_request_instance_state
            ON agent_runtime_request (runtime_instance_id, state)
        ''')

        cursor.execute('''
            DO $migration$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'user_agent_active_revision_060_fk'
                      AND conrelid = 'user_agent'::regclass
                ) THEN
                    ALTER TABLE user_agent
                    ADD CONSTRAINT user_agent_active_revision_060_fk
                    FOREIGN KEY (active_revision_id) REFERENCES user_agent_revision(revision_id)
                    ON DELETE SET NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'user_agent_last_good_revision_060_fk'
                      AND conrelid = 'user_agent'::regclass
                ) THEN
                    ALTER TABLE user_agent
                    ADD CONSTRAINT user_agent_last_good_revision_060_fk
                    FOREIGN KEY (last_known_good_revision_id)
                    REFERENCES user_agent_revision(revision_id) ON DELETE SET NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'user_agent_selected_host_060_fk'
                      AND conrelid = 'user_agent'::regclass
                ) THEN
                    ALTER TABLE user_agent
                    ADD CONSTRAINT user_agent_selected_host_060_fk
                    FOREIGN KEY (selected_host_session_id)
                    REFERENCES agent_host_session(host_session_id) ON DELETE SET NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'user_agent_authoritative_instance_060_fk'
                      AND conrelid = 'user_agent'::regclass
                ) THEN
                    ALTER TABLE user_agent
                    ADD CONSTRAINT user_agent_authoritative_instance_060_fk
                    FOREIGN KEY (authoritative_instance_id)
                    REFERENCES agent_runtime_instance(runtime_instance_id) ON DELETE SET NULL;
                END IF;
            END
            $migration$;
        ''')
        cursor.execute('''
            DO $migration$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'user_agent_generation_060_check'
                      AND conrelid = 'user_agent'::regclass
                ) THEN
                    ALTER TABLE user_agent
                    ADD CONSTRAINT user_agent_generation_060_check CHECK (
                        lifecycle_generation >= 0
                        AND generation_counter >= lifecycle_generation
                        AND state_revision >= 0
                    );
                END IF;
            END
            $migration$;
        ''')

    def _migrate_draft_publication_060(self, cursor):
        """Add draft CAS identities and immutable publication records."""
        cursor.execute(
            "ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS draft_uuid UUID"
        )
        cursor.execute(
            "ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS target_agent_id TEXT"
        )
        cursor.execute(
            "ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS "
            "state_revision BIGINT NOT NULL DEFAULT 0"
        )
        cursor.execute(
            "ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS generation_claim_id UUID"
        )
        cursor.execute(
            "ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS "
            "generation_claim_expires_at TIMESTAMPTZ"
        )
        cursor.execute(
            "ALTER TABLE draft_agents ADD COLUMN IF NOT EXISTS published_revision_id UUID"
        )
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_draft_agents_draft_uuid
            ON draft_agents (draft_uuid)
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_draft_agents_draft_owner
            ON draft_agents (draft_uuid, user_id)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS draft_transition (
                transition_id UUID PRIMARY KEY,
                draft_uuid UUID NOT NULL,
                owner_user_id TEXT NOT NULL,
                operation_id UUID REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL,
                operation_execution_generation BIGINT NOT NULL
                    CHECK (operation_execution_generation > 0),
                transition_kind TEXT NOT NULL
                    CHECK (transition_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
                expected_revision BIGINT NOT NULL CHECK (expected_revision >= 0),
                result_revision BIGINT NOT NULL CHECK (result_revision >= 0),
                outcome TEXT NOT NULL CHECK (
                    outcome IN ('applied', 'conflict', 'replayed', 'failed')
                ),
                safe_code TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                FOREIGN KEY (draft_uuid, owner_user_id)
                    REFERENCES draft_agents(draft_uuid, user_id) ON DELETE RESTRICT,
                CONSTRAINT draft_transition_revision_check CHECK (
                    (outcome IN ('applied', 'replayed')
                        AND result_revision >= expected_revision)
                    OR outcome IN ('conflict', 'failed')
                )
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS draft_artifact_publication (
                publication_id UUID PRIMARY KEY,
                draft_uuid UUID NOT NULL,
                owner_user_id TEXT NOT NULL,
                source_state_revision BIGINT NOT NULL CHECK (source_state_revision >= 0),
                generation_claim_id UUID NOT NULL,
                target_agent_id TEXT NOT NULL,
                target_revision_id UUID NOT NULL,
                operation_id UUID REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL,
                operation_execution_generation BIGINT
                    CHECK (operation_execution_generation IS NULL
                           OR operation_execution_generation > 0),
                staging_relative_path TEXT NOT NULL,
                revision_relative_path TEXT NOT NULL,
                artifact_digest CHAR(64)
                    CHECK (artifact_digest IS NULL OR artifact_digest ~ '^[0-9a-f]{64}$'),
                manifest_digest CHAR(64)
                    CHECK (manifest_digest IS NULL OR manifest_digest ~ '^[0-9a-f]{64}$'),
                state TEXT NOT NULL CHECK (
                    state IN ('claimed', 'staged', 'validated', 'published', 'failed')
                ),
                state_revision BIGINT NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                published_at TIMESTAMPTZ,
                failed_at TIMESTAMPTZ,
                failure_code TEXT,
                UNIQUE (draft_uuid, source_state_revision),
                UNIQUE (target_revision_id),
                FOREIGN KEY (draft_uuid, owner_user_id)
                    REFERENCES draft_agents(draft_uuid, user_id) ON DELETE RESTRICT,
                FOREIGN KEY (target_revision_id, target_agent_id, owner_user_id)
                    REFERENCES user_agent_revision(revision_id, agent_id, owner_user_id)
                    ON DELETE RESTRICT,
                CONSTRAINT draft_publication_paths_check CHECK (
                    staging_relative_path !~ '^/'
                    AND staging_relative_path !~ '(^|/)\\.\\.(/|$)'
                    AND revision_relative_path !~ '^/'
                    AND revision_relative_path !~ '(^|/)\\.\\.(/|$)'
                ),
                CONSTRAINT draft_publication_digest_check CHECK (
                    (state IN ('claimed', 'staged')
                        AND artifact_digest IS NULL AND manifest_digest IS NULL)
                    OR (state IN ('validated', 'published')
                        AND artifact_digest IS NOT NULL AND manifest_digest IS NOT NULL)
                    OR state = 'failed'
                ),
                CONSTRAINT draft_publication_terminal_check CHECK (
                    (state = 'published' AND published_at IS NOT NULL AND failed_at IS NULL)
                    OR (state = 'failed' AND published_at IS NULL AND failed_at IS NOT NULL)
                    OR (state NOT IN ('published', 'failed')
                        AND published_at IS NULL AND failed_at IS NULL)
                )
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_draft_publication_state
            ON draft_artifact_publication (state, created_at)
        ''')

        cursor.execute('''
            DO $migration$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'draft_agents_published_revision_060_fk'
                      AND conrelid = 'draft_agents'::regclass
                ) THEN
                    ALTER TABLE draft_agents
                    ADD CONSTRAINT draft_agents_published_revision_060_fk
                    FOREIGN KEY (published_revision_id, target_agent_id, user_id)
                    REFERENCES user_agent_revision(revision_id, agent_id, owner_user_id)
                    ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'draft_agents_state_revision_060_check'
                      AND conrelid = 'draft_agents'::regclass
                ) THEN
                    ALTER TABLE draft_agents
                    ADD CONSTRAINT draft_agents_state_revision_060_check
                    CHECK (state_revision >= 0);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'draft_agents_generation_claim_060_check'
                      AND conrelid = 'draft_agents'::regclass
                ) THEN
                    ALTER TABLE draft_agents
                    ADD CONSTRAINT draft_agents_generation_claim_060_check CHECK (
                        (generation_claim_id IS NULL
                            AND generation_claim_expires_at IS NULL)
                        OR (generation_claim_id IS NOT NULL
                            AND generation_claim_expires_at IS NOT NULL)
                    );
                END IF;
            END
            $migration$;
        ''')

    def _migrate_maintenance_coordination_060(self, cursor):
        """Create durable, retry-safe maintenance unit and membership rows."""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS maintenance_unit (
                unit_id UUID PRIMARY KEY,
                unit_kind TEXT NOT NULL CHECK (unit_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
                owner_user_id TEXT,
                scope_key TEXT NOT NULL CHECK (length(scope_key) BETWEEN 1 AND 256),
                idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 256),
                state TEXT NOT NULL CHECK (
                    state IN ('pending', 'claimed', 'running', 'succeeded',
                              'failed_retryable', 'failed_terminal', 'cancelled')
                ),
                lease_token UUID,
                claim_generation BIGINT NOT NULL DEFAULT 0 CHECK (claim_generation >= 0),
                claimed_by TEXT,
                lease_expires_at TIMESTAMPTZ,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
                operation_id UUID REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL,
                operation_execution_generation BIGINT
                    CHECK (operation_execution_generation IS NULL
                           OR operation_execution_generation > 0),
                output_generation UUID,
                output_relative_path TEXT,
                output_digest CHAR(64)
                    CHECK (output_digest IS NULL OR output_digest ~ '^[0-9a-f]{64}$'),
                last_error_code TEXT,
                state_revision BIGINT NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                terminal_at TIMESTAMPTZ,
                next_attempt_at TIMESTAMPTZ,
                UNIQUE (unit_kind, idempotency_key),
                CONSTRAINT maintenance_unit_attempt_check CHECK (
                    attempt_count <= max_attempts
                ),
                CONSTRAINT maintenance_unit_lease_check CHECK (
                    (state IN ('claimed', 'running') AND lease_token IS NOT NULL
                        AND claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL)
                    OR state NOT IN ('claimed', 'running')
                ),
                CONSTRAINT maintenance_unit_output_check CHECK (
                    state <> 'succeeded'
                    OR (output_generation IS NOT NULL
                        AND output_relative_path IS NOT NULL
                        AND output_digest IS NOT NULL
                        AND terminal_at IS NOT NULL)
                ),
                CONSTRAINT maintenance_unit_relative_path_check CHECK (
                    output_relative_path IS NULL
                    OR (output_relative_path !~ '^/'
                        AND output_relative_path !~ '(^|/)\\.\\.(/|$)')
                )
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_maintenance_unit_claim
            ON maintenance_unit (state, next_attempt_at, created_at)
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS maintenance_unit_input (
                unit_id UUID NOT NULL REFERENCES maintenance_unit(unit_id) ON DELETE CASCADE,
                input_kind TEXT NOT NULL CHECK (input_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
                input_id TEXT NOT NULL CHECK (length(input_id) BETWEEN 1 AND 256),
                input_digest CHAR(64)
                    CHECK (input_digest IS NULL OR input_digest ~ '^[0-9a-f]{64}$'),
                state TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
                operation_id UUID REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL,
                operation_execution_generation BIGINT
                    CHECK (operation_execution_generation IS NULL
                           OR operation_execution_generation > 0),
                completed_at TIMESTAMPTZ,
                PRIMARY KEY (unit_id, input_kind, input_id),
                CONSTRAINT maintenance_input_completion_check CHECK (
                    (state = 'pending' AND operation_execution_generation IS NULL
                        AND completed_at IS NULL)
                    OR (state = 'completed' AND operation_execution_generation IS NOT NULL
                        AND completed_at IS NOT NULL)
                )
            )
        ''')

    def _migrate_conversation_commit_060(self, cursor):
        """Add one atomic logical-turn visibility boundary to legacy chats."""
        cursor.execute(
            "ALTER TABLE chats ADD COLUMN IF NOT EXISTS "
            "render_revision BIGINT NOT NULL DEFAULT 0"
        )
        cursor.execute(
            "ALTER TABLE chats ADD COLUMN IF NOT EXISTS snapshot_committed_at TIMESTAMPTZ"
        )
        cursor.execute(
            "ALTER TABLE chats ADD COLUMN IF NOT EXISTS conversation_commit_id UUID"
        )
        cursor.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS conversation_commit_id UUID"
        )
        cursor.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS commit_position INTEGER"
        )
        cursor.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS committed_render_revision BIGINT"
        )
        cursor.execute(
            "ALTER TABLE saved_components ADD COLUMN IF NOT EXISTS conversation_commit_id UUID"
        )
        cursor.execute(
            "ALTER TABLE saved_components ADD COLUMN IF NOT EXISTS "
            "committed_render_revision BIGINT"
        )

        # Feature 060 stages a complete next canvas beside the current
        # authoritative canvas. The pre-060 index keyed only by
        # (chat_id, component_id) made that impossible whenever a stable
        # component identity was carried into the next revision. Keep legacy
        # NULL-commit rows unique through the impossible UUID4 sentinel while
        # allowing one copy per durable conversation commit. Recovery to the
        # former index first deletes/archives non-authoritative staged rows,
        # then recreates the two-column definition.
        cursor.execute("DROP INDEX IF EXISTS ux_saved_components_chat_component")
        cursor.execute('''
            CREATE UNIQUE INDEX ux_saved_components_chat_component
            ON saved_components (
                chat_id,
                component_id,
                COALESCE(
                    conversation_commit_id,
                    '00000000-0000-0000-0000-000000000000'::uuid
                )
            )
            WHERE component_id IS NOT NULL
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conversation_commit (
                commit_id UUID PRIMARY KEY,
                chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                owner_user_id TEXT NOT NULL,
                request_generation UUID NOT NULL,
                operation_id UUID REFERENCES operation_record(operation_id)
                    ON DELETE SET NULL,
                operation_execution_generation BIGINT
                    CHECK (operation_execution_generation IS NULL
                           OR operation_execution_generation > 0),
                base_render_revision BIGINT NOT NULL CHECK (base_render_revision >= 0),
                committed_render_revision BIGINT
                    CHECK (committed_render_revision IS NULL
                           OR committed_render_revision > 0),
                state TEXT NOT NULL CHECK (state IN ('staged', 'committed', 'aborted')),
                started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                committed_at TIMESTAMPTZ,
                aborted_at TIMESTAMPTZ,
                UNIQUE (chat_id, request_generation),
                CONSTRAINT conversation_commit_lifecycle_check CHECK (
                    (state = 'staged' AND committed_render_revision IS NULL
                        AND committed_at IS NULL AND aborted_at IS NULL)
                    OR (state = 'committed'
                        AND committed_render_revision = base_render_revision + 1
                        AND committed_at IS NOT NULL AND aborted_at IS NULL)
                    OR (state = 'aborted' AND committed_render_revision IS NULL
                        AND committed_at IS NULL AND aborted_at IS NOT NULL)
                )
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_conversation_commit_chat_revision
            ON conversation_commit (chat_id, committed_render_revision DESC)
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_message_commit_position
            ON messages (conversation_commit_id, commit_position)
            WHERE conversation_commit_id IS NOT NULL
        ''')

        cursor.execute('''
            DO $migration$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chats_conversation_commit_060_fk'
                      AND conrelid = 'chats'::regclass
                ) THEN
                    ALTER TABLE chats
                    ADD CONSTRAINT chats_conversation_commit_060_fk
                    FOREIGN KEY (conversation_commit_id)
                    REFERENCES conversation_commit(commit_id) ON DELETE SET NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'messages_conversation_commit_060_fk'
                      AND conrelid = 'messages'::regclass
                ) THEN
                    ALTER TABLE messages
                    ADD CONSTRAINT messages_conversation_commit_060_fk
                    FOREIGN KEY (conversation_commit_id)
                    REFERENCES conversation_commit(commit_id) ON DELETE SET NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'saved_components_conversation_commit_060_fk'
                      AND conrelid = 'saved_components'::regclass
                ) THEN
                    ALTER TABLE saved_components
                    ADD CONSTRAINT saved_components_conversation_commit_060_fk
                    FOREIGN KEY (conversation_commit_id)
                    REFERENCES conversation_commit(commit_id) ON DELETE SET NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chats_render_revision_060_check'
                      AND conrelid = 'chats'::regclass
                ) THEN
                    ALTER TABLE chats
                    ADD CONSTRAINT chats_render_revision_060_check
                    CHECK (render_revision >= 0);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'messages_commit_metadata_060_check'
                      AND conrelid = 'messages'::regclass
                ) THEN
                    ALTER TABLE messages
                    ADD CONSTRAINT messages_commit_metadata_060_check CHECK (
                        (conversation_commit_id IS NULL AND commit_position IS NULL
                            AND committed_render_revision IS NULL)
                        OR (conversation_commit_id IS NOT NULL AND commit_position >= 0
                            AND committed_render_revision > 0)
                    );
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'saved_components_commit_metadata_060_check'
                      AND conrelid = 'saved_components'::regclass
                ) THEN
                    ALTER TABLE saved_components
                    ADD CONSTRAINT saved_components_commit_metadata_060_check CHECK (
                        (conversation_commit_id IS NULL
                            AND committed_render_revision IS NULL)
                        OR (conversation_commit_id IS NOT NULL
                            AND committed_render_revision > 0)
                    );
                END IF;
            END
            $migration$;
        ''')

    def _migrate_legacy_runtime_truth_060(self, cursor):
        """Backfill only identities and artifact facts the 057 state proves."""
        cursor.execute(
            "SELECT id FROM draft_agents WHERE draft_uuid IS NULL ORDER BY id"
        )
        for row in cursor.fetchall():
            legacy_id = str(row['id'])
            try:
                parsed_id = uuid.UUID(legacy_id)
                draft_uuid = (
                    parsed_id
                    if str(parsed_id) == legacy_id.lower()
                    else uuid.uuid4()
                )
            except (ValueError, AttributeError):
                draft_uuid = uuid.uuid4()
            cursor.execute(
                "UPDATE draft_agents SET draft_uuid = %s "
                "WHERE id = %s AND draft_uuid IS NULL",
                (str(draft_uuid), legacy_id),
            )

        # A revising draft keeps the durable target it explicitly names. A
        # legacy draft already linked to a live user_agent keeps that exact
        # agent alias. Only genuinely new/unmatched drafts receive UUID4 text.
        cursor.execute('''
            UPDATE draft_agents
            SET target_agent_id = revises_agent_id
            WHERE target_agent_id IS NULL AND revises_agent_id IS NOT NULL
        ''')
        cursor.execute('''
            UPDATE draft_agents AS draft
            SET target_agent_id = agent.agent_id
            FROM user_agent AS agent
            WHERE draft.target_agent_id IS NULL
              AND agent.draft_id = draft.id
        ''')
        cursor.execute(
            "SELECT id FROM draft_agents WHERE target_agent_id IS NULL ORDER BY id"
        )
        for row in cursor.fetchall():
            cursor.execute(
                "UPDATE draft_agents SET target_agent_id = %s "
                "WHERE id = %s AND target_agent_id IS NULL",
                (str(uuid.uuid4()), row['id']),
            )

        cursor.execute('''
            SELECT agent.agent_id, agent.owner_user_id, agent.draft_id,
                   draft.agent_slug, draft.host_binding
            FROM user_agent AS agent
            LEFT JOIN draft_agents AS draft ON draft.id = agent.draft_id
            WHERE agent.deleted_at IS NULL
              AND agent.status IN ('validated', 'live')
            ORDER BY agent.agent_id
        ''')
        for row in cursor.fetchall():
            agent_id = str(row['agent_id'])
            owner_user_id = str(row['owner_user_id'])
            revision_id = uuid.uuid5(
                _LEGACY_AGENT_REVISION_NAMESPACE,
                f"{owner_user_id}\0{agent_id}",
            )
            artifact_digest = None
            artifact_relative_path = None
            slug = row.get('agent_slug')
            host_binding = row.get('host_binding')
            if slug and not host_binding:
                root = Path(self._legacy_agent_root()).resolve()
                candidate = (root / str(slug)).resolve()
                try:
                    relative = candidate.relative_to(root)
                except ValueError:
                    logger.warning(
                        "Skipping unsafe legacy agent path for %s", agent_id
                    )
                else:
                    if candidate.is_dir():
                        artifact_digest = self._digest_legacy_agent_directory(candidate)
                        artifact_relative_path = relative.as_posix()

            cursor.execute('''
                INSERT INTO user_agent_revision (
                    revision_id, agent_id, owner_user_id, revision_number,
                    artifact_digest, manifest_json, artifact_relative_path,
                    runtime_contract_version, release_lock_digest,
                    compatibility_state, state, promotion_token,
                    state_revision, created_at
                ) VALUES (
                    %s, %s, %s, 0, %s, NULL, %s, NULL, NULL,
                    'legacy_pending', 'legacy_pending', NULL, 0, now()
                )
                ON CONFLICT (agent_id, revision_number) DO NOTHING
            ''', (
                str(revision_id), agent_id, owner_user_id,
                artifact_digest, artifact_relative_path,
            ))
            cursor.execute(
                "SELECT revision_id FROM user_agent_revision "
                "WHERE agent_id = %s AND revision_number = 0",
                (agent_id,),
            )
            persisted_revision = cursor.fetchone()
            if persisted_revision is not None:
                cursor.execute(
                    "UPDATE user_agent SET active_revision_id = %s "
                    "WHERE agent_id = %s AND active_revision_id IS NULL",
                    (persisted_revision['revision_id'], agent_id),
                )

    def _legacy_agent_root(self) -> Path:
        """Return the configured legacy server-owned agent bundle root."""
        return Path(__file__).resolve().parents[1] / 'agents'

    @staticmethod
    def _digest_legacy_agent_directory(directory: Path) -> str:
        """Hash one legacy bundle as a framed, path-aware deterministic tree."""
        root = Path(directory)
        digest = hashlib.sha256(b'astraldeep-legacy-agent-tree-v1\0')
        for entry in sorted(root.rglob('*'), key=lambda item: item.relative_to(root).as_posix()):
            relative_bytes = entry.relative_to(root).as_posix().encode('utf-8')
            digest.update(len(relative_bytes).to_bytes(4, 'big'))
            digest.update(relative_bytes)
            if entry.is_symlink():
                target = os.readlink(entry).encode('utf-8')
                digest.update(b'L')
                digest.update(len(target).to_bytes(8, 'big'))
                digest.update(target)
            elif entry.is_dir():
                digest.update(b'D')
            elif entry.is_file():
                digest.update(b'F')
                digest.update(entry.stat().st_size.to_bytes(8, 'big'))
                with entry.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(chunk)
            else:
                raise ValueError(f"unsupported legacy agent artifact: {entry}")
        return digest.hexdigest()

    def _sweep_user_agent_policy_060(self, cursor):
        """Mark non-deleted agents stale under the exact combined policy."""
        cursor.execute(
            "UPDATE user_agent SET revalidation_required = TRUE "
            "WHERE deleted_at IS NULL "
            "AND validated_policy_revision IS DISTINCT FROM %s "
            "AND revalidation_required = FALSE",
            (USER_AGENT_POLICY_REVISION,),
        )
        count = max(0, int(cursor.rowcount))
        self._user_agent_policy_sweep_count = count
        return count

    def _migrate_revalidate_on_constitution_change_057(self, cursor):
        """Feature 057 (Constitution L / FR-028): when the agent constitution's
        MAJOR version advances beyond what a user agent was validated against,
        mark it ``revalidation_required`` so the boundary fail-closed refuses to
        route it until it re-passes Analyze. Best-effort: a loader/parse failure
        skips the sweep (the per-request boundary check is the real gate)."""
        try:
            from orchestrator.agent_constitution import AGENT_CONSTITUTION_VERSION
        except Exception:
            logger.debug("057 revalidate migration: constitution loader unavailable", exc_info=True)
            return
        try:
            current_major = int(str(AGENT_CONSTITUTION_VERSION).split('.', 1)[0])
        except (ValueError, AttributeError):
            return
        # split_part is Postgres; the stored value is a plain semver string.
        cursor.execute(
            "UPDATE user_agent SET revalidation_required = TRUE "
            "WHERE constitution_version IS NOT NULL "
            "AND CAST(split_part(constitution_version, '.', 1) AS INTEGER) < %s "
            "AND revalidation_required = FALSE",
            (current_major,),
        )

    # Feature 029 identity sets (specs/029-agents-adaptive-ui-ci/baseline.md).
    # Both the declared hyphenated agent ids and the legacy underscore
    # directory-name rows exist in deployed databases — cover both.
    _ML_MERGED_AGENT_IDS = ('classify', 'classify-1', 'forecaster', 'forecaster-1',
                            'llm_factory', 'llm-factory-1')
    _ML_AGENT_ID = 'ml-services-1'
    # The five verb names classify and forecaster shared pre-merge; each is
    # exposed service-prefixed by the consolidated registry.
    _ML_PREFIXED_VERBS = ('submit_dataset', 'start_training_job', 'get_job_status',
                          'get_results', 'delete_dataset')
    _RETIRED_AGENT_IDS = ('email_tracker', 'email-tracker-1',
                          'grant_budgets', 'grant-budgets-1',
                          'grants', 'grants-1',
                          'linkedin', 'linkedin-1',
                          'nefarious', 'nefarious-1',
                          'nocodb', 'nocodb-1')

    # Feature 040: etf_tracker_1 retired (agent directory + code removed). Both
    # the hyphenated agent id and the legacy underscore directory-name row may
    # exist in deployed databases — cover both.
    _RETIRED_AGENT_IDS_040 = ('etf-tracker-1-1', 'etf_tracker_1')

    def _cleanup_phantom_windows_tools_ids(self, cursor):
        """Feature 039 (C-2): delete phantom Windows-tools agent rows.

        The A2A discovery fallback used to slugify the agent's display name
        ("Windows Tools (code & system)") into an id instead of its real id
        ``windows-tools-1`` — leaving stray rows under ``windows-tools`` and
        ``windows-tools-(code-&-system)``. This deletes those rows. Targeted +
        idempotent: a real agent id never equals the bare ``windows-tools`` nor
        starts with ``windows-tools-(`` (no real id contains a parenthesis), so
        ``windows-tools-1`` is never touched. Safe at every boot.
        """
        for table in ('agent_scopes', 'agent_ownership', 'tool_overrides'):
            try:
                cursor.execute(
                    f"DELETE FROM {table} WHERE agent_id = %s OR agent_id LIKE %s",
                    ('windows-tools', 'windows-tools-(%'),
                )
            except Exception:  # noqa: BLE001 — table may be absent on older schema
                pass

    def _cleanup_retired_agents_040(self, cursor):
        """Feature 040: purge permission/credential/ownership/trust rows for the
        retired ``etf_tracker_1`` agent.

        Idempotent: each DELETE matches nothing on re-run, so it is safe at
        every boot. Chats and saved_components are intentionally preserved and
        route through the runtime retired-agent handling
        (``orchestrator.RETIRED_AGENT_IDS``) so old transcripts degrade
        gracefully rather than dangling. Lead-approved destructive removal per
        ``specs/040-inprocess-agents-skills-commands/data-model.md``.
        """
        retired = self._RETIRED_AGENT_IDS_040
        ph = ", ".join(["%s"] * len(retired))
        for table in ('agent_ownership', 'agent_scopes', 'tool_overrides',
                      'tool_permissions', 'user_credentials', 'agent_trust'):
            try:
                cursor.execute(
                    f"DELETE FROM {table} WHERE agent_id IN ({ph})", retired
                )
            except Exception:  # noqa: BLE001 — table may be absent on older schema
                pass

    def _migrate_agent_catalog_029(self, cursor):
        """Feature 029 one-time (idempotent) catalog migrations.

        1. classify/forecaster/llm_factory → ml-services-1 identity remap with
           settings carry-forward (FR-008): scopes merge with OR semantics
           (granted on any predecessor stays granted), per-tool overrides
           merge with AND semantics (disabled on any predecessor stays
           disabled — fail-safe), credentials/ownership carry over, and the
           five colliding verb names gain their service prefixes in stored
           per-tool rows.
        2. Permission/credential rows for the six retired agents are deleted
           (FR-003; lead-approved destructive — audit/chats/saved_components
           rows are intentionally preserved per data-model.md).

        Every statement is a no-op on re-run (UPDATE/DELETE match nothing,
        INSERT...SELECT reads an empty source), so this is safe at every boot.
        """
        ml_ids = self._ML_MERGED_AGENT_IDS
        ml_ph = ", ".join(["%s"] * len(ml_ids))
        retired = self._RETIRED_AGENT_IDS
        retired_ph = ", ".join(["%s"] * len(retired))
        verb_ph = ", ".join(["%s"] * len(self._ML_PREFIXED_VERBS))

        # (1a) Service-prefix the colliding verbs BEFORE the id remap (the
        # old agent id is what tells us which prefix a row needs).
        for table in ('tool_overrides', 'tool_permissions'):
            for prefix, old_ids in (('classify_', ('classify', 'classify-1')),
                                    ('forecaster_', ('forecaster', 'forecaster-1'))):
                cursor.execute(
                    f"UPDATE {table} SET tool_name = %s || tool_name "
                    f"WHERE agent_id IN (%s, %s) AND tool_name IN ({verb_ph})",
                    (prefix, *old_ids, *self._ML_PREFIXED_VERBS),
                )

        # (1b) Ownership: one surviving row (UNIQUE(agent_id)).
        cursor.execute(
            f"""INSERT INTO agent_ownership (agent_id, owner_email, is_public, created_at, updated_at)
                SELECT %s, owner_email, is_public, created_at, updated_at
                FROM agent_ownership WHERE agent_id IN ({ml_ph})
                ORDER BY updated_at DESC NULLS LAST LIMIT 1
                ON CONFLICT (agent_id) DO NOTHING""",
            (self._ML_AGENT_ID, *ml_ids),
        )

        # (1c) Scopes: OR-merge per (user, scope).
        cursor.execute(
            f"""INSERT INTO agent_scopes (user_id, agent_id, scope, enabled, updated_at)
                SELECT user_id, %s, scope, BOOL_OR(enabled), MAX(updated_at)
                FROM agent_scopes WHERE agent_id IN ({ml_ph})
                GROUP BY user_id, scope
                ON CONFLICT DO NOTHING""",
            (self._ML_AGENT_ID, *ml_ids),
        )

        # (1d) Per-tool overrides: AND-merge per (user, tool, kind) — a tool a
        # user disabled on any predecessor stays disabled on the merged agent.
        cursor.execute(
            f"""INSERT INTO tool_overrides (user_id, agent_id, tool_name, enabled, updated_at, permission_kind)
                SELECT user_id, %s, tool_name, BOOL_AND(enabled), MAX(updated_at), permission_kind
                FROM tool_overrides WHERE agent_id IN ({ml_ph})
                GROUP BY user_id, tool_name, permission_kind
                ON CONFLICT DO NOTHING""",
            (self._ML_AGENT_ID, *ml_ids),
        )
        cursor.execute(
            f"""INSERT INTO tool_permissions (user_id, agent_id, tool_name, allowed, updated_at)
                SELECT user_id, %s, tool_name, BOOL_AND(allowed), MAX(updated_at)
                FROM tool_permissions WHERE agent_id IN ({ml_ph})
                GROUP BY user_id, tool_name
                ON CONFLICT DO NOTHING""",
            (self._ML_AGENT_ID, *ml_ids),
        )

        # (1e) Credentials: key names are service-distinct (CLASSIFY_*,
        # FORECASTER_*, LLM_FACTORY_*), so rows carry over verbatim.
        cursor.execute(
            f"""INSERT INTO user_credentials (user_id, agent_id, credential_key, encrypted_value, created_at, updated_at)
                SELECT user_id, %s, credential_key, encrypted_value, created_at, updated_at
                FROM user_credentials WHERE agent_id IN ({ml_ph})
                ON CONFLICT DO NOTHING""",
            (self._ML_AGENT_ID, *ml_ids),
        )

        # (1f) Chat bindings follow the merged identity, then the old rows go.
        cursor.execute(f"UPDATE chats SET agent_id = %s WHERE agent_id IN ({ml_ph})",
                       (self._ML_AGENT_ID, *ml_ids))
        for table in ('agent_ownership', 'agent_scopes', 'tool_overrides',
                      'tool_permissions', 'user_credentials'):
            cursor.execute(f"DELETE FROM {table} WHERE agent_id IN ({ml_ph})", ml_ids)

        # (2) Retired agents: permission/credential rows are destroyed.
        for table in ('agent_ownership', 'agent_scopes', 'tool_overrides',
                      'tool_permissions', 'user_credentials'):
            cursor.execute(f"DELETE FROM {table} WHERE agent_id IN ({retired_ph})", retired)

    # Feature 030: the operator-bundled catalog (post-029 agent ids). These
    # are the agents every user should be able to SEE in the Agents surface
    # (visibility ≠ authorization — scopes stay fail-closed per user).
    # Drafts and user-created agents are never listed here.
    _FIRST_PARTY_PUBLIC_AGENT_IDS = (
        'connectors-1', 'dice-roller-1', 'general-1',
        'journal-review-1', 'medical-1', 'ml-services-1', 'summarizer-1',
        'weather-1', 'web-research-1',
        # Feature 063: the remote-compute agents are VISIBLE always (discoverable
        # before granting, FR-025), but visibility does NOT imply authorization
        # (FR-004). remote-observe-1 (read-only) is safe-seeded only when
        # FF_REMOTE_COMPUTE is on; remote-control-1 (mutating) is NEVER safe-seeded
        # (FR-003) — both filters live in the boot seed in orchestrator.start.
        'remote-observe-1',
        'remote-control-1',
    )

    def _migrate_agent_visibility_030(self, cursor):
        """Feature 030 one-time (idempotent) visibility backfill.

        The 029 plug-and-play agents (web_research, summarizer) and bundled
        dice_roller registered through the ownerless auto-assign path, which
        hard-coded ``is_public=false`` — leaving them invisible in every tab
        of the Agents surface, so users could not discover or enable them
        (verified 030 walkthrough finding). Mark the fixed first-party
        catalog public; registration now defaults non-draft ownerless agents
        to public going forward.

        Idempotent: the UPDATE matches nothing once the flags are set.
        Rollback: ``UPDATE agent_ownership SET is_public = FALSE WHERE
        agent_id IN (<_FIRST_PARTY_PUBLIC_AGENT_IDS>)``.
        """
        ph = ", ".join(["%s"] * len(self._FIRST_PARTY_PUBLIC_AGENT_IDS))
        cursor.execute(
            f"UPDATE agent_ownership SET is_public = TRUE "
            f"WHERE agent_id IN ({ph}) AND is_public = FALSE",
            self._FIRST_PARTY_PUBLIC_AGENT_IDS,
        )

    # Feature 030: pre-030 tutorial-step slugs (features 005/008/025/027).
    # Four targeted UI removed by feature 026 (the React feedback control and
    # the sdui ParamPicker panels), the admin set described a Quarantine tab
    # that no longer exists, and the rest predate the 030 consent-enable flow.
    _LEGACY_TUTORIAL_SLUGS_030 = (
        'welcome', 'chat-with-agent', 'personalize-profession',
        'personalize-skills', 'personalize-personality', 'open-agents-panel',
        'enable-agents', 'open-audit-log', 'give-feedback', 'finish',
        'admin-feedback-flagged', 'admin-feedback-proposals',
        'admin-feedback-quarantine', 'admin-tutorial-editor',
    )
    # First slug of the rewritten seed (seeds/tutorial_steps_seed.sql): its
    # presence means the refresh already happened on this database.
    _TUTORIAL_REWRITE_SENTINEL_030 = 'welcome-tour'

    def _migrate_tutorial_steps_030(self, cursor):
        """Feature 030 one-time guarded guided-tour content refresh.

        The tutorial seed is ``ON CONFLICT (slug) DO NOTHING`` so admin edits
        survive reboots — which also means rewritten copy under an existing
        slug never reaches an already-seeded database. The 030 rewrite uses
        all-new slugs; this migration archives the legacy rows so they stop
        appearing in the tour. Archive, not DELETE: rows stay restorable from
        Tutorial admin and there are no FK side effects
        (``onboarding_state.last_step_id`` SET NULL, revision CASCADE).

        Guarded on the absence of the rewritten seed's sentinel slug rather
        than on ``archived_at`` so it runs exactly once per database: an
        admin who later RESTOREs a legacy step is not re-archived on the
        next boot. Boot-time SQL bypasses tutorial_step_revision history,
        same as the 029 catalog remap.

        Rollback: ``UPDATE tutorial_step SET archived_at = NULL WHERE slug
        IN (<_LEGACY_TUTORIAL_SLUGS_030>)``.
        """
        cursor.execute(
            "SELECT 1 FROM tutorial_step WHERE slug = %s LIMIT 1",
            (self._TUTORIAL_REWRITE_SENTINEL_030,),
        )
        if cursor.fetchone() is not None:
            return
        ph = ", ".join(["%s"] * len(self._LEGACY_TUTORIAL_SLUGS_030))
        cursor.execute(
            f"UPDATE tutorial_step SET archived_at = NOW(), updated_at = NOW() "
            f"WHERE slug IN ({ph}) AND archived_at IS NULL",
            self._LEGACY_TUTORIAL_SLUGS_030,
        )

    def _migrate_tutorial_timeline_target_045(self, cursor):
        """Feature 045: the workspace-timeline tour step moved from the Settings
        menu (anchor ``sidebar.timeline``) to a dedicated top-bar icon (anchor
        ``topbar.timeline``). The seed is ``ON CONFLICT (slug) DO NOTHING``, so
        an already-seeded database keeps the stale anchor and its tour step would
        point at a control that no longer exists. Repoint it in place.

        Guarded on the OLD anchor value so it runs effectively once and never
        clobbers an admin who later re-targets the step; body copy is left
        untouched so admin edits survive. Boot-time SQL bypasses
        tutorial_step_revision history (same as the 030 refresh).

        Rollback: ``UPDATE tutorial_step SET target_key = 'sidebar.timeline'
        WHERE slug = 'workspace-timeline'``.
        """
        cursor.execute(
            "UPDATE tutorial_step SET target_key = 'topbar.timeline', updated_at = NOW() "
            "WHERE slug = 'workspace-timeline' AND target_key = 'sidebar.timeline'"
        )

    _PERMISSION_KINDS_052 = ('tools:read', 'tools:write', 'tools:search', 'tools:system')

    def _migrate_backfill_tool_kinds_052(self, cursor):
        """Feature 052: boot-time promotion of the per-render tool backfill.

        Copies each legacy tool-wide disable row (``permission_kind IS
        NULL``, ``enabled = FALSE``) to per-kind rows carrying the same
        ``enabled`` value for every permission kind, so per-tool resolution
        does not depend on a per-surface-open backfill
        (``ToolPermissionManager.backfill_per_tool_rows`` remains for the
        registration-aware agent_scopes carry-forward, which needs the
        in-memory tool->scope map unavailable at boot). Copying the disable
        across all kinds is outcome-preserving: ``is_tool_allowed`` consults
        only the tool's required kind, and the legacy row previously blocked
        regardless of kind. Only ``enabled = FALSE`` rows are copied — a
        hypothetical legacy enable row defers to scope state today, and
        copying it would widen access (fail-closed posture). Existing
        per-kind rows always win (``ON CONFLICT DO NOTHING``); legacy rows
        are left in place for the runtime layers that still read them.
        Idempotent: re-runs insert nothing. Rollback: none needed — the
        rows are the same ones the runtime path would have produced.
        """
        kinds_values = ', '.join(['(%s)'] * len(self._PERMISSION_KINDS_052))
        cursor.execute(
            f"""INSERT INTO tool_overrides
                    (user_id, agent_id, tool_name, permission_kind, enabled, updated_at)
                SELECT t.user_id, t.agent_id, t.tool_name, k.kind, t.enabled, t.updated_at
                FROM tool_overrides t
                CROSS JOIN (VALUES {kinds_values}) AS k(kind)
                WHERE t.permission_kind IS NULL AND t.enabled = FALSE
                ON CONFLICT (user_id, agent_id, tool_name, COALESCE(permission_kind, ''))
                DO NOTHING""",
            self._PERMISSION_KINDS_052,
        )

    def execute(self, query: str, params: Tuple = ()):
        """Execute a write operation (INSERT, UPDATE, DELETE)."""
        def op(conn):
            cursor = conn.cursor()
            try:
                cursor.execute(self._translate_query(query), params)
                conn.commit()
                return cursor
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                raise
            except Exception as e:
                conn.rollback()
                logger.error(f"Database error executing {query}: {e}")
                raise
        return self._run_with_retry(op)

    def fetch_one(self, query: str, params: Tuple = ()) -> Optional[Dict]:
        """Fetch a single row."""
        def op(conn):
            cursor = conn.cursor()
            cursor.execute(self._translate_query(query), params)
            return cursor.fetchone()
        return self._run_with_retry(op)

    def fetch_all(self, query: str, params: Tuple = ()) -> List[Dict]:
        """Fetch all rows."""
        def op(conn):
            cursor = conn.cursor()
            cursor.execute(self._translate_query(query), params)
            return cursor.fetchall()
        return self._run_with_retry(op)

    async def afetch_one(self, query: str, params: Tuple = ()) -> Optional[Dict]:
        """Async twin of :meth:`fetch_one`, run off the event loop."""
        return await asyncio.to_thread(self.fetch_one, query, params)

    async def afetch_all(self, query: str, params: Tuple = ()) -> List[Dict]:
        """Async twin of :meth:`fetch_all`, run off the event loop."""
        return await asyncio.to_thread(self.fetch_all, query, params)

    async def aexecute(self, query: str, params: Tuple = ()):
        """Async twin of :meth:`execute`, run off the event loop."""
        return await asyncio.to_thread(self.execute, query, params)

    # ── Agent Ownership ──────────────────────────────────────────────────

    def get_agent_ownership(self, agent_id: str) -> Optional[Dict]:
        """Get ownership info for an agent."""
        row = self.fetch_one(
            "SELECT agent_id, owner_email, is_public, created_at, updated_at FROM agent_ownership WHERE agent_id = ?",
            (agent_id,)
        )
        if row:
            return dict(row)
        return None

    def set_agent_ownership(self, agent_id: str, owner_email: str, is_public: bool = False) -> None:
        """Set or update ownership for an agent."""
        import time
        now = int(time.time() * 1000)
        existing = self.get_agent_ownership(agent_id)
        if existing:
            self.execute(
                "UPDATE agent_ownership SET owner_email = ?, is_public = ?, updated_at = ? WHERE agent_id = ?",
                (owner_email, is_public, now, agent_id)
            )
        else:
            self.execute(
                "INSERT INTO agent_ownership (agent_id, owner_email, is_public, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (agent_id, owner_email, is_public, now, now)
            )

    def set_agent_visibility(self, agent_id: str, is_public: bool) -> bool:
        """Toggle public/private visibility. Returns True if updated."""
        import time
        now = int(time.time() * 1000)
        cursor = self.execute(
            "UPDATE agent_ownership SET is_public = ?, updated_at = ? WHERE agent_id = ?",
            (is_public, now, agent_id)
        )
        return cursor.rowcount > 0

    def get_all_agent_ownership(self) -> List[Dict]:
        """Get ownership info for all agents."""
        rows = self.fetch_all("SELECT agent_id, owner_email, is_public FROM agent_ownership")
        return [dict(r) for r in rows]

    # ── Feature 040: agent "safe" trust marker (agent_trust) ─────────────

    def get_agent_is_safe(self, agent_id: str) -> bool:
        """Return True if the agent carries the owner-approved 'safe' marker.

        Distinct from ``agent_ownership.is_public`` (visibility). Drives the
        check-time safe permission baseline in ``tool_permissions``.
        """
        row = self.fetch_one(
            "SELECT is_safe FROM agent_trust WHERE agent_id = ?", (agent_id,)
        )
        return bool(row["is_safe"]) if row else False

    def upsert_agent_safe(self, agent_id: str, is_safe: bool, marked_by: str) -> bool:
        """Set the safe marker for an agent. Returns the prior ``is_safe`` state.

        Records ``marked_by``/``marked_at`` and the ``prior_state`` for the
        audit trail. Admin/owner gating is enforced by the caller
        (``orchestrator/agent_trust.py``); this is the storage primitive.
        """
        prior = self.get_agent_is_safe(agent_id)
        self.execute(
            """INSERT INTO agent_trust (agent_id, is_safe, marked_by, marked_at, prior_state)
               VALUES (?, ?, ?, now(), ?)
               ON CONFLICT (agent_id) DO UPDATE SET
                 is_safe = EXCLUDED.is_safe,
                 marked_by = EXCLUDED.marked_by,
                 marked_at = EXCLUDED.marked_at,
                 prior_state = ?""",
            (agent_id, bool(is_safe), marked_by, prior, prior),
        )
        return prior

    def reset_agent_safe(self, agent_id: str, marked_by: str) -> bool:
        """Clear the safe marker (revision reset, feature 040). Returns prior state."""
        prior = self.get_agent_is_safe(agent_id)
        self.execute(
            """INSERT INTO agent_trust
                 (agent_id, is_safe, marked_by, marked_at, prior_state, revised_reset_at)
               VALUES (?, FALSE, ?, now(), ?, now())
               ON CONFLICT (agent_id) DO UPDATE SET
                 is_safe = FALSE,
                 marked_by = EXCLUDED.marked_by,
                 marked_at = EXCLUDED.marked_at,
                 prior_state = ?,
                 revised_reset_at = now()""",
            (agent_id, marked_by, prior, prior),
        )
        return prior

    # ── Chat ↔ Agent Binding (Feature 013) ───────────────────────────────

    def get_chat_agent(self, chat_id: str) -> Optional[str]:
        """Return the agent_id bound to the chat, or None if unbound (legacy chats)."""
        row = self.fetch_one("SELECT agent_id FROM chats WHERE id = ?", (chat_id,))
        if not row:
            return None
        return row.get("agent_id")

    def set_chat_agent(self, chat_id: str, agent_id: Optional[str]) -> bool:
        """Bind (or unbind) a chat to an agent. Returns True if a row was updated."""
        cursor = self.execute(
            "UPDATE chats SET agent_id = ? WHERE id = ?",
            (agent_id, chat_id),
        )
        return cursor.rowcount > 0

    # ── Per-User Tool Selection Preference (Feature 013 / FR-024) ────────

    def get_user_tool_selection(self, user_id: str, agent_id: str) -> Optional[List[str]]:
        """Return the user's saved tool selection for the given agent.

        Returns:
            A list of tool names if the user has narrowed the selection,
            or None when the user has not narrowed (orchestrator falls
            back to the agent's full permission-allowed set per FR-019).
        """
        prefs = self.get_user_preferences(user_id)
        sel_map = prefs.get("tool_selection") or {}
        if not isinstance(sel_map, dict):
            return None
        value = sel_map.get(agent_id)
        if value is None:
            return None
        if not isinstance(value, list):
            return None
        return [str(v) for v in value]

    def set_user_tool_selection(
        self, user_id: str, agent_id: str, tool_names: List[str]
    ) -> None:
        """Save the user's tool selection for an agent (FR-024).

        Callers MUST pass a non-empty list — empty selection is blocked
        at the UI layer (FR-021) and rejected by the API (T035). This
        helper does not validate against permission scopes; that check
        belongs at the API boundary.
        """
        prefs = self.get_user_preferences(user_id)
        sel_map = prefs.get("tool_selection")
        if not isinstance(sel_map, dict):
            sel_map = {}
        sel_map[agent_id] = list(tool_names)
        prefs["tool_selection"] = sel_map
        self.set_user_preferences(user_id, prefs)

    def clear_user_tool_selection(self, user_id: str, agent_id: str) -> bool:
        """Clear the user's saved selection for an agent (FR-025 reset).

        Returns True if a saved selection was present and was removed,
        False if no selection existed (idempotent no-op).
        """
        prefs = self.get_user_preferences(user_id)
        sel_map = prefs.get("tool_selection")
        if not isinstance(sel_map, dict) or agent_id not in sel_map:
            return False
        del sel_map[agent_id]
        prefs["tool_selection"] = sel_map
        self.set_user_preferences(user_id, prefs)
        return True

    # ── Per-User Agent Disable (Feature 013 follow-up) ───────────────────
    # Lets a user temporarily disable an entire agent without changing its
    # scopes/permissions or affecting other users. Stored under
    # `user_preferences.disabled_agents` as a list of agent_ids. Absence
    # in the list means the agent is enabled (default).

    def get_user_disabled_agents(self, user_id: str) -> List[str]:
        """Return the list of agent_ids the user has disabled (may be empty)."""
        prefs = self.get_user_preferences(user_id)
        value = prefs.get("disabled_agents")
        if not isinstance(value, list):
            return []
        return [str(v) for v in value]

    def is_user_agent_disabled(self, user_id: str, agent_id: str) -> bool:
        """Return True iff the user has disabled this agent."""
        return agent_id in self.get_user_disabled_agents(user_id)

    def set_user_agent_disabled(
        self, user_id: str, agent_id: str, disabled: bool
    ) -> bool:
        """Toggle the user's per-agent disabled state.

        Returns True if the stored value changed, False if it was already
        in the requested state (idempotent).
        """
        prefs = self.get_user_preferences(user_id)
        current = prefs.get("disabled_agents")
        disabled_list: List[str] = (
            [str(v) for v in current] if isinstance(current, list) else []
        )
        present = agent_id in disabled_list
        if disabled and not present:
            disabled_list.append(agent_id)
        elif not disabled and present:
            disabled_list = [a for a in disabled_list if a != agent_id]
        else:
            return False  # already in the requested state
        prefs["disabled_agents"] = disabled_list
        self.set_user_preferences(user_id, prefs)
        return True

    # ── Users ─────────────────────────────────────────────────────────────

    def upsert_user(self, user_id: str, email: str = None, username: str = None,
                    display_name: str = None, roles: List[str] = None) -> None:
        """Create or update a user profile from JWT claims."""
        import time
        now = int(time.time() * 1000)
        roles_json = json.dumps(roles) if roles else None
        existing = self.get_user(user_id)
        if existing:
            self.execute(
                """UPDATE users SET email = COALESCE(?, email), username = COALESCE(?, username),
                   display_name = COALESCE(?, display_name), roles = COALESCE(?, roles),
                   last_login_at = ?, updated_at = ? WHERE id = ?""",
                (email, username, display_name, roles_json, now, now, user_id)
            )
        else:
            self.execute(
                """INSERT INTO users (id, email, username, display_name, roles, last_login_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, email, username, display_name, roles_json, now, now, now)
            )

    def get_user(self, user_id: str) -> Optional[Dict]:
        """Get a user profile by ID."""
        row = self.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row:
            result = dict(row)
            if result.get("roles"):
                try:
                    result["roles"] = json.loads(result["roles"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return result
        return None

    def get_all_users(self) -> List[Dict]:
        """Get all user profiles."""
        rows = self.fetch_all("SELECT * FROM users ORDER BY last_login_at DESC")
        results = []
        for row in rows:
            r = dict(row)
            if r.get("roles"):
                try:
                    r["roles"] = json.loads(r["roles"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(r)
        return results

    # ── User Preferences ─────────────────────────────────────────────────

    def get_user_preferences(self, user_id: str) -> Dict:
        """Get user preferences (returns empty dict if none stored)."""
        row = self.fetch_one(
            "SELECT preferences FROM user_preferences WHERE user_id = ?",
            (user_id,)
        )
        if row:
            try:
                return json.loads(row["preferences"])
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    def set_user_preferences(self, user_id: str, preferences: Dict) -> None:
        """Set or update user preferences (merges with existing)."""
        import time
        now = int(time.time() * 1000)
        existing = self.get_user_preferences(user_id)
        merged = {**existing, **preferences}
        prefs_json = json.dumps(merged)
        self.execute(
            """INSERT INTO user_preferences (user_id, preferences, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET preferences = ?, updated_at = ?""",
            (user_id, prefs_json, now, prefs_json, now)
        )

    # ── Draft Agents ────────────────────────────────────────────────────

    def create_draft_agent(self, draft_id: str, user_id: str, agent_name: str,
                           agent_slug: str, description: str, tools_spec: str = None,
                           skill_tags: str = None, packages: str = None,
                           origin: str = "manual", source_chat_id: str = None,
                           gap_fingerprint: str = None, revises_agent_id: str = None) -> None:
        """Create a new draft agent record.

        Feature 027: ``origin`` records the entry point (``manual`` |
        ``auto_chat`` | ``revision``); ``source_chat_id`` + ``gap_fingerprint``
        scope capability-gap dedup; ``revises_agent_id`` links a revision
        draft to the live agent it stages changes for.
        """
        import time
        now = int(time.time() * 1000)
        try:
            draft_uuid = str(uuid.UUID(str(draft_id)))
        except (TypeError, ValueError, AttributeError):
            # Compatibility for exceptional pre-060/non-UUID callers. The
            # public draft id remains untouched, while every new transition
            # receives a canonical immutable UUID alias.
            draft_uuid = str(uuid.uuid4())
        target_agent_id = revises_agent_id or str(uuid.uuid4())
        self.execute(
            """INSERT INTO draft_agents (id, user_id, agent_name, agent_slug, description,
               tools_spec, skill_tags, packages, status, origin, source_chat_id,
               gap_fingerprint, revises_agent_id, draft_uuid, target_agent_id,
               state_revision, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (draft_id, user_id, agent_name, agent_slug, description,
             tools_spec, skill_tags, packages, origin, source_chat_id,
             gap_fingerprint, revises_agent_id, draft_uuid, target_agent_id,
             now, now)
        )

    def find_gap_draft(self, user_id: str, source_chat_id: str,
                       gap_fingerprint: str) -> Optional[Dict]:
        """Feature 027 (FR-007): the non-terminal draft already staged for a
        capability gap, or None. Terminal = live (resolved) or deleted
        (discarded); rejected drafts stay editable so they still count."""
        row = self.fetch_one(
            """SELECT * FROM draft_agents
               WHERE user_id = ? AND source_chat_id = ? AND gap_fingerprint = ?
                 AND status != 'live'
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, source_chat_id, gap_fingerprint)
        )
        return dict(row) if row else None

    def get_draft_agent(self, draft_id: str) -> Optional[Dict]:
        """Get a draft agent by ID."""
        row = self.fetch_one("SELECT * FROM draft_agents WHERE id = ?", (draft_id,))
        return dict(row) if row else None

    def get_user_draft_agents(self, user_id: str) -> List[Dict]:
        """Get all draft agents for a user (excludes live/rejected agents)."""
        rows = self.fetch_all(
            "SELECT * FROM draft_agents WHERE user_id = ? AND status NOT IN ('live', 'rejected') ORDER BY created_at DESC",
            (user_id,)
        )
        return [dict(r) for r in rows]

    def get_draft_agent_by_slug(self, slug: str) -> Optional[Dict]:
        """Get a draft agent by its slug."""
        row = self.fetch_one(
            "SELECT * FROM draft_agents WHERE agent_slug = ?", (slug,)
        )
        return dict(row) if row else None

    def get_pending_review_drafts(self) -> List[Dict]:
        """Get all draft agents awaiting admin review."""
        rows = self.fetch_all(
            "SELECT * FROM draft_agents WHERE status = 'pending_review' ORDER BY updated_at ASC"
        )
        return [dict(r) for r in rows]

    def update_draft_agent(self, draft_id: str, **kwargs) -> bool:
        """Update draft agent fields. Pass any column as keyword argument."""
        import time
        kwargs['updated_at'] = int(time.time() * 1000)
        set_clauses = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [draft_id]
        cursor = self.execute(
            f"UPDATE draft_agents SET {set_clauses} WHERE id = ?",
            tuple(values)
        )
        return cursor.rowcount > 0

    def claim_draft_generation(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        expected_revision: int,
        claim_id: str,
        lease_seconds: int = 300,
    ) -> Optional[Dict]:
        """Claim one draft generation using database time and revision CAS."""

        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 1800:
            raise ValueError("lease_seconds must be between 1 and 1800")
        parsed_claim_id = uuid.UUID(str(claim_id))
        if parsed_claim_id.version != 4:
            raise ValueError("claim_id must be a UUID4")
        claim_id = str(parsed_claim_id)
        connection = self._get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE draft_agents
                SET generation_claim_id = %s,
                    generation_claim_expires_at =
                        clock_timestamp() + (%s * interval '1 second'),
                    status = 'generating',
                    error_message = NULL,
                    state_revision = state_revision + 1,
                    updated_at =
                        (extract(epoch from clock_timestamp()) * 1000)::bigint
                WHERE id = %s AND user_id = %s AND state_revision = %s
                  AND (
                    generation_claim_id IS NULL
                    OR generation_claim_expires_at <= clock_timestamp()
                    OR generation_claim_id = %s
                  )
                RETURNING *
                """,
                (
                    claim_id,
                    lease_seconds,
                    draft_id,
                    owner_user_id,
                    expected_revision,
                    claim_id,
                ),
            )
            row = cursor.fetchone()
            connection.commit()
            return dict(row) if row else None
        except BaseException:
            connection.rollback()
            raise
        finally:
            try:
                cursor.close()
            finally:
                connection.close()

    def finish_draft_generation(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        expected_revision: int,
        claim_id: str,
        status: str,
        error_message: Optional[str] = None,
        security_report: Optional[str] = None,
        validation_report: Optional[str] = None,
        required_credentials: Optional[str] = None,
    ) -> Optional[Dict]:
        """Release the exact live generation claim and publish its draft state."""

        if status not in {"generated", "error"}:
            raise ValueError("generation terminal status is invalid")
        parsed_claim_id = uuid.UUID(str(claim_id))
        if parsed_claim_id.version != 4:
            raise ValueError("claim_id must be a UUID4")
        claim_id = str(parsed_claim_id)
        connection = self._get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE draft_agents
                SET generation_claim_id = NULL,
                    generation_claim_expires_at = NULL,
                    status = %s,
                    error_message = %s,
                    security_report = %s,
                    validation_report = %s,
                    required_credentials = %s,
                    state_revision = state_revision + 1,
                    updated_at =
                        (extract(epoch from clock_timestamp()) * 1000)::bigint
                WHERE id = %s AND user_id = %s AND state_revision = %s
                  AND generation_claim_id = %s
                  AND generation_claim_expires_at > clock_timestamp()
                RETURNING *
                """,
                (
                    status,
                    error_message,
                    security_report,
                    validation_report,
                    required_credentials,
                    draft_id,
                    owner_user_id,
                    expected_revision,
                    claim_id,
                ),
            )
            row = cursor.fetchone()
            connection.commit()
            return dict(row) if row else None
        except BaseException:
            connection.rollback()
            raise
        finally:
            try:
                cursor.close()
            finally:
                connection.close()

    def delete_draft_agent(self, draft_id: str) -> bool:
        """Delete a draft agent record."""
        cursor = self.execute("DELETE FROM draft_agents WHERE id = ?", (draft_id,))
        return cursor.rowcount > 0

    # ── Interaction Log (Knowledge Synthesis) ──────────────────────────────

    def log_interaction(self, agent_id: str, tool_name: str, success: bool,
                        error_message: str = None, response_time_ms: int = None,
                        chat_id: str = None) -> None:
        """Record a tool interaction for knowledge synthesis."""
        import time
        now = int(time.time() * 1000)
        self.execute(
            """INSERT INTO interaction_log (agent_id, tool_name, success, error_message,
               response_time_ms, chat_id, synthesized, created_at)
               VALUES (?, ?, ?, ?, ?, ?, FALSE, ?)""",
            (agent_id, tool_name, success, error_message, response_time_ms, chat_id, now)
        )

    def get_unsynthesized_interactions(self, limit: int = 500) -> List[Dict]:
        """Fetch interactions not yet processed by the knowledge synthesizer."""
        rows = self.fetch_all(
            "SELECT * FROM interaction_log WHERE synthesized = FALSE ORDER BY created_at ASC LIMIT ?",
            (limit,)
        )
        return [dict(r) for r in rows]

    def mark_interactions_synthesized(self, ids: List[int]) -> None:
        """Mark interaction rows as synthesized."""
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        self.execute(
            f"UPDATE interaction_log SET synthesized = TRUE WHERE id IN ({placeholders})",
            tuple(ids)
        )

    def get_interaction_stats(self, agent_id: str = None) -> List[Dict]:
        """Get aggregated interaction stats, optionally filtered by agent."""
        if agent_id:
            rows = self.fetch_all(
                """SELECT agent_id, tool_name, COUNT(*) as total_calls,
                   SUM(CASE WHEN success THEN 1 ELSE 0 END) as success_count,
                   AVG(response_time_ms) as avg_response_ms
                   FROM interaction_log WHERE agent_id = ?
                   GROUP BY agent_id, tool_name""",
                (agent_id,)
            )
        else:
            rows = self.fetch_all(
                """SELECT agent_id, tool_name, COUNT(*) as total_calls,
                   SUM(CASE WHEN success THEN 1 ELSE 0 END) as success_count,
                   AVG(response_time_ms) as avg_response_ms
                   FROM interaction_log
                   GROUP BY agent_id, tool_name"""
            )
        return [dict(r) for r in rows]
