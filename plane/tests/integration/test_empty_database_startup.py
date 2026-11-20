"""Tests for astralplane.database.migrations and baseline.py: a fresh empty PostgreSQL
database reaches the current schema revision idempotently, with index, constraint,
and host/session contract checks along the way. The 074.004 attachment cases clone a
074.003 lineage template from tests/fixtures/migrated_template.py and apply that edge alone.
"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from astralplane.api import (
    create_durable_purge_executor,
    create_repository_catalog,
    create_streaming_blob_store,
)
from astralplane.contracts import ReconciliationHookIdentity, ReconciliationMarkerState
from astralplane.database.baseline import (
    BASELINE_MIGRATION_NAME,
    BASELINE_REQUIRED_TABLES,
    BaselineCompatibilityState,
    BaselineMigrationRunner,
    initialize_empty_database,
    inspect_baseline_compatibility,
)
from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_REGISTRY,
    PLANE_SCHEMA_067_MIGRATION,
    PLANE_SCHEMA_074_002_MIGRATION,
    PLANE_SCHEMA_074_002_REGISTRY_DIGEST,
    PLANE_SCHEMA_074_003_MIGRATION,
    PLANE_SCHEMA_074_003_REGISTRY_DIGEST,
    PLANE_SCHEMA_074_003_SCHEMA_VERIFIER_CHECKSUM,
    PLANE_SCHEMA_074_004_MIGRATION,
    PLANE_SCHEMA_074_004_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    PLANE_SCHEMA_074_004_REGISTRY_DIGEST,
    PLANE_SCHEMA_074_004_SCHEMA_VERIFIER_CHECKSUM,
    PLANE_SCHEMA_074_MIGRATION,
    MigrationRegistry,
    MigrationReport,
    MigrationRunner,
    _verify_predecessor_plane_schema,
)
from astralplane.database.pool import ConnectionPool
from astralplane.database.revision import DataPlaneRevision
from astralplane.database.transaction import PlaneDatabase
from astralplane.errors import PlaneError, SchemaRevisionError
from astralplane.purge import PurgeAttemptState, PurgeTargetScope
from astralplane.reconciliation import RECONCILIATION_ADVISORY_LOCK
from astralplane.reconciliation_store import PostgresReconciliationCoordinator
from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.agents import AgentRepository
from astralplane.repositories.drafts import DraftAgentRepository
from astralplane.repositories.history import HistoryRepository
from astralplane.repositories.quality_audit import (
    QualityAuditRepository,
    QualityTestCaseRecord,
    QualityTestRunRecord,
)
from astralplane.repositories.work_admission import (
    AcceptedAdmission,
    AdmissionClass,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    WorkAdmissionNotFoundError,
    WorkAdmissionRepository,
)
from tests.fixtures.migrated_template import LineageTemplates, MigratedDatabase, bound_clone
from tests.fixtures.pre_split.loader import (
    TEST_DATABASE_ENV,
    FixtureLoadError,
    connect_fixture_database,
    drop_postgres_fixture,
)


class _DedicatedDriverPool:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.borrowed = False

    def getconn(self) -> Any:
        if self.borrowed:
            raise RuntimeError("integration connection is already borrowed")
        self.borrowed = True
        return self.connection

    def putconn(self, connection: Any, *, close: bool = False) -> None:
        if connection is not self.connection or not self.borrowed or close:
            raise RuntimeError("integration connection was returned in an invalid state")
        self.borrowed = False

    def closeall(self) -> None:
        return None


@dataclass(slots=True)
class _EmptySchema:
    connection: Any
    schema: str
    pool: ConnectionPool
    database: PlaneDatabase


@dataclass(frozen=True, slots=True)
class _FreshBoot:
    schema: _EmptySchema
    decoy_schema: str
    first: MigrationReport
    second: MigrationReport


@dataclass(slots=True)
class _EmptyDatabase:
    administrator: Any
    connection: Any
    database_name: str
    hostile_owner_role: str
    pool: ConnectionPool
    database: PlaneDatabase


def _quoted_schema(schema: str) -> str:
    assert schema.startswith("astralplane_fixture_")
    assert schema.removeprefix("astralplane_fixture_").isalnum()
    return f'"{schema}"'


def _quoted_database_identity(value: str, *, prefix: str) -> str:
    assert value.startswith(prefix)
    assert value.removeprefix(prefix).isalnum()
    assert len(value) <= 63
    return f'"{value}"'


def historical_074_003_runner(database: PlaneDatabase) -> MigrationRunner:
    registry = MigrationRegistry(
        (
            PLANE_SCHEMA_067_MIGRATION,
            PLANE_SCHEMA_074_MIGRATION,
            PLANE_SCHEMA_074_002_MIGRATION,
            PLANE_SCHEMA_074_003_MIGRATION,
        ),
        current_schema_verifier=lambda _transaction: None,
        current_schema_verifier_checksum=PLANE_SCHEMA_074_003_SCHEMA_VERIFIER_CHECKSUM,
    )
    assert registry.digest == PLANE_SCHEMA_074_003_REGISTRY_DIGEST
    revision = DataPlaneRevision(
        schema_revision="074.003",
        read_compatible_from=("066.001", "067.001", "074.001", "074.002"),
        migration_digest=registry.digest,
    )
    return MigrationRunner(database, revision=revision, registry=registry)


def lineage_074_004_runner(database: PlaneDatabase) -> MigrationRunner:
    registry = MigrationRegistry(
        (
            PLANE_SCHEMA_067_MIGRATION,
            PLANE_SCHEMA_074_MIGRATION,
            PLANE_SCHEMA_074_002_MIGRATION,
            PLANE_SCHEMA_074_003_MIGRATION,
            PLANE_SCHEMA_074_004_MIGRATION,
        ),
        current_schema_verifier=lambda transaction: _verify_predecessor_plane_schema(
            transaction, "074.004"
        ),
        current_schema_verifier_checksum=PLANE_SCHEMA_074_004_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_074_004_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == PLANE_SCHEMA_074_004_REGISTRY_DIGEST
    revision = DataPlaneRevision(
        schema_revision="074.004",
        read_compatible_from=("066.001", "067.001", "074.001", "074.002", "074.003"),
        migration_digest=registry.digest,
        accepted_predecessor_digests=(("074.003", PLANE_SCHEMA_074_003_REGISTRY_DIGEST),),
    )
    return MigrationRunner(database, revision=revision, registry=registry)


@contextmanager
def _empty_schema() -> Iterator[_EmptySchema]:
    database_url = os.environ.get(TEST_DATABASE_ENV)
    if database_url is None:
        pytest.skip(f"{TEST_DATABASE_ENV} is required for PostgreSQL integration tests")
    try:
        connection = connect_fixture_database(database_url)
    except FixtureLoadError as exc:
        pytest.fail(str(exc))
    schema = f"astralplane_fixture_{uuid.uuid4().hex}"
    cursor = connection.cursor()
    try:
        cursor.execute(f"CREATE SCHEMA {_quoted_schema(schema)}")
        cursor.execute(f"SET search_path TO {_quoted_schema(schema)}, pg_catalog")
        connection.commit()
    finally:
        cursor.close()

    try:
        pool = ConnectionPool(_DedicatedDriverPool(connection))
        yield _EmptySchema(
            connection=connection,
            schema=schema,
            pool=pool,
            database=PlaneDatabase(pool),
        )
    finally:
        drop_postgres_fixture(connection, schema=schema)
        connection.close()


@pytest.fixture
def empty_postgres_schema() -> Iterator[_EmptySchema]:
    with _empty_schema() as fixture:
        yield fixture


@pytest.fixture(scope="module")
def fresh_schema_boot() -> Iterator[_FreshBoot]:
    with _empty_schema() as fixture:
        decoy_schema = f"astralplane_fixture_{uuid.uuid4().hex}"
        quoted_decoy = _quoted_schema(decoy_schema)
        cursor = fixture.connection.cursor()
        try:
            cursor.execute(f"CREATE SCHEMA {quoted_decoy}")
            cursor.execute(f"CREATE TABLE {quoted_decoy}.saved_components (component_id TEXT)")
            cursor.execute(
                f"CREATE TABLE {quoted_decoy}.workspace_snapshot "
                "(turn_message_id INTEGER, "
                "CONSTRAINT fk_workspace_snapshot_turn_message CHECK (TRUE))"
            )
            fixture.connection.commit()
            runner = BaselineMigrationRunner(
                fixture.database,
                MigrationRunner(
                    fixture.database,
                    revision=CURRENT_DATA_PLANE_REVISION,
                    registry=MIGRATION_REGISTRY,
                ),
            )
            first = runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
            second = runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
            yield _FreshBoot(fixture, decoy_schema, first, second)
        finally:
            fixture.connection.rollback()
            cursor.execute(f"DROP SCHEMA IF EXISTS {quoted_decoy} CASCADE")
            fixture.connection.commit()
            cursor.close()


@pytest.fixture
def historical_074_003(lineage_templates: LineageTemplates) -> Iterator[MigratedDatabase]:
    with bound_clone(lineage_templates.template(historical_074_003_runner)) as clone:
        yield clone


@pytest.fixture
def empty_postgres_database() -> Iterator[_EmptyDatabase]:
    database_url = os.environ.get(TEST_DATABASE_ENV)
    if database_url is None:
        pytest.skip(f"{TEST_DATABASE_ENV} is required for PostgreSQL integration tests")
    psycopg2 = pytest.importorskip("psycopg2")
    database_name = f"astralplane_db_{uuid.uuid4().hex}"
    hostile_owner_role = f"astralplane_role_{uuid.uuid4().hex}"
    quoted_database = _quoted_database_identity(
        database_name,
        prefix="astralplane_db_",
    )
    quoted_role = _quoted_database_identity(
        hostile_owner_role,
        prefix="astralplane_role_",
    )
    administrator = psycopg2.connect(database_url)
    administrator.autocommit = True
    connection = None
    pool = None
    try:
        with administrator.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {quoted_database} TEMPLATE template0")
        connection = psycopg2.connect(database_url, dbname=database_name)
        pool = ConnectionPool(_DedicatedDriverPool(connection))
        yield _EmptyDatabase(
            administrator=administrator,
            connection=connection,
            database_name=database_name,
            hostile_owner_role=hostile_owner_role,
            pool=pool,
            database=PlaneDatabase(pool),
        )
    finally:
        if pool is not None:
            pool.close()
        if connection is not None:
            connection.close()
        try:
            with administrator.cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS {quoted_database} WITH (FORCE)")
                cursor.execute(f"DROP ROLE IF EXISTS {quoted_role}")
        finally:
            administrator.close()


def test_template0_default_public_schema_is_exactly_qualified_then_hardened(
    empty_postgres_database: _EmptyDatabase,
) -> None:
    fixture = empty_postgres_database
    migration = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    runner = BaselineMigrationRunner(fixture.database, migration)

    first = runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
    second = runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)

    assert first.source_revision is None
    assert first.target_revision == CURRENT_DATA_PLANE_REVISION.schema_revision
    assert second.already_current
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            SELECT
                current_schema(),
                pg_get_userbyid(namespace_record.nspowner) = current_user,
                has_schema_privilege('public', 'public', 'CREATE'),
                has_schema_privilege('public', 'public', 'USAGE')
            FROM pg_namespace AS namespace_record
            WHERE namespace_record.nspname = 'public'
            """
        )
        assert cursor.fetchone() == ("public", True, False, False)
        cursor.execute("SELECT value FROM schema_meta WHERE key = 'revision'")
        assert cursor.fetchone() == (CURRENT_DATA_PLANE_REVISION.schema_revision,)
    finally:
        cursor.close()
        fixture.connection.rollback()


def test_template0_public_schema_with_arbitrary_owner_is_not_normalized(
    empty_postgres_database: _EmptyDatabase,
) -> None:
    fixture = empty_postgres_database
    initialize_empty_database(fixture.database)
    quoted_role = _quoted_database_identity(
        fixture.hostile_owner_role,
        prefix="astralplane_role_",
    )
    with fixture.administrator.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {quoted_role} NOLOGIN")
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(f"ALTER SCHEMA public OWNER TO {quoted_role}")
        cursor.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        cursor.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
        fixture.connection.commit()
    finally:
        cursor.close()

    migration = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    with pytest.raises(
        SchemaRevisionError,
        match="predecessor schema canonical structure",
    ):
        migration.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)

    cursor = fixture.connection.cursor()
    try:
        cursor.execute("SELECT value FROM schema_meta WHERE key = 'revision'")
        assert cursor.fetchone() == ("066.001",)
        cursor.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='public'")
        assert cursor.fetchone() == (fixture.hostile_owner_role,)
        cursor.execute("SELECT to_regclass('test_runs')")
        assert cursor.fetchone() == (None,)
    finally:
        cursor.close()
        fixture.connection.rollback()


def test_empty_database_reaches_current_revision_and_repeats_safely(
    fresh_schema_boot: _FreshBoot,
) -> None:
    fixture = fresh_schema_boot.schema
    first = fresh_schema_boot.first
    second = fresh_schema_boot.second
    compatibility = inspect_baseline_compatibility(fixture.database)

    assert first.source_revision is None
    assert first.applied_steps == (
        BASELINE_MIGRATION_NAME,
        "astralplane-067-transactional-recovery",
        "astralplane-074-lets-authority",
        "astralplane-074-quality-audit-ownership",
        "astralplane-074-current-runtime-contract",
        "astralplane-074-pending-attachment-materialization",
        "astralplane-075-client-local-speech",
        "astralplane-079-persistent-assignments",
        "astralplane-088-one-shot-operations",
        "astralplane-088-session-incarnation",
        "astralplane-088-session-issuer",
        "astralplane-088-declarative-agents",
        "astralplane-088-owner-guidance",
        "astralplane-088-selected-input",
        "astralplane-088-scheduler-policy",
        "astralplane-088-framework-credentials",
        "astralplane-089-typesafe-credentials",
        "astralplane-089-mesh-enrollment-records",
        "astralplane-owner-stop-epochs",
        "astralplane-089-completion-wake",
        "astralplane-089-notification-records",
        "astralplane-089-atlas-pages",
        "astralplane-089-atlas-revisions",
        "astralplane-089-atlas-links",
        "astralplane-089-capability-publications",
        "astralplane-089-operation-harness-bindings",
        "astralplane-089-push-deliveries",
        "astralplane-089-observation-records",
        "astralplane-089-projects",
        "astralplane-089-harness-captures",
        "astralplane-089-audit-conversation-index",
        "astralplane-089-chat-queue",
        "astralplane-089-conversation-branches",
        "astralplane-089-computer-use",
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )
    assert second.already_current
    assert second.applied_steps == ()
    assert compatibility.state is BaselineCompatibilityState.COMPATIBLE
    assert compatibility.observed_revision == CURRENT_DATA_PLANE_REVISION.schema_revision
    assert not compatibility.missing_required_tables

    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
            """
        )
        tables = {str(row[0]) for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT function_record.proname, function_record.proconfig
            FROM pg_proc AS function_record
            JOIN pg_namespace AS namespace_record
              ON namespace_record.oid = function_record.pronamespace
             AND namespace_record.nspname = current_schema()
            ORDER BY function_record.proname
            """
        )
        function_configurations = dict(cursor.fetchall())
    finally:
        cursor.close()
    assert tables >= BASELINE_REQUIRED_TABLES
    assert tables >= {
        "astralplane_authority_binding",
        "astralplane_authority_lifecycle_operation",
        "astralplane_outbox",
        "astralplane_receipt_claim",
        "atlas_page",
        "atlas_page_command",
        "atlas_page_revision",
        "atlas_page_link",
        "atlas_page_link_head",
        "atlas_page_link_command",
        "operation_harness_binding",
        "tool_observation",
        "tool_observation_page",
        "test_runs",
        "test_case_results",
        "test_evidence",
        "audit_entries",
        "latex_artifacts",
        "astralplane_blob_owner_state",
    }
    expected_search_path = [f"search_path=pg_catalog, {fixture.schema}, pg_temp"]
    assert function_configurations.pop("reject_guidance_snapshot_update") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("valid_assignment_selected_input") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("reject_selected_agent_identity_update") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("enforce_atlas_page_lifecycle") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("reject_atlas_page_command_mutation") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("reject_atlas_page_revision_mutation") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("verify_atlas_page_head_revision") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("reject_atlas_page_link_mutation") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("enforce_atlas_page_link_head_advance") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("reject_atlas_page_link_command_mutation") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("reject_assignment_harness_capture_mutation") == [
        "search_path=pg_catalog, pg_temp"
    ]
    assert function_configurations.pop("reject_operation_harness_binding_mutation") == [
        "search_path=pg_catalog, pg_temp"
    ]
    assert function_configurations.pop("enforce_tool_observation_lifecycle") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("reject_tool_observation_delete") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("reject_tool_observation_page_update") == [
        "search_path=pg_catalog"
    ]
    assert function_configurations.pop("enforce_tool_observation_page_purge") == [
        "search_path=pg_catalog"
    ]
    assert set(function_configurations) == {
        "astraldeep_positive_unique_int_array",
        "astralplane_attachment_id_is_canonical",
        "astralplane_blob_owner_is_canonical",
        "astralplane_blob_storage_key_is_canonical",
        "astralplane_capabilities_are_canonical",
        "astralplane_identifier_is_canonical",
        "astralplane_receipt_watermark_require_advance",
        "audit_events_assign_chain_sequence",
        "audit_events_protect",
    }
    assert all(
        configuration == expected_search_path for configuration in function_configurations.values()
    )
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'voice_session' "
            "AND column_name = 'speech_backend'"
        )
        assert cursor.fetchone() == ("NO",)
    finally:
        cursor.close()
        fixture.connection.rollback()


def test_message_repository_round_trips_json_looking_strings_as_strings(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    history = HistoryRepository()

    with fixture.database.transaction() as transaction:
        history.conversations.create(
            transaction,
            conversation_id="chat-json-looking-string",
            owner_id="owner-json-looking-string",
            title="Unambiguous message strings",
            agent_id=None,
            created_at=1,
        )
        for timestamp, content in enumerate(("[]", "null", "7"), start=2):
            created = history.messages.append(
                transaction,
                owner_id="owner-json-looking-string",
                conversation_id="chat-json-looking-string",
                role="user",
                content=content,
                timestamp=timestamp,
            )
            assert created.content == content

    with fixture.database.transaction() as transaction:
        reloaded = history.messages.list_visible(
            transaction,
            owner_id="owner-json-looking-string",
            conversation_id="chat-json-looking-string",
            through_render_revision=None,
            limit=10,
        )

    assert tuple(message.content for message in reloaded) == ("[]", "null", "7")


def test_voice_backend_constraint_accepts_only_exact_remote_and_local_rows(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    insert_sql = """
        INSERT INTO voice_session (
            session_id, user_id, activation_id, device_id, device_kind,
            speech_backend, transport, room_name, participant_identity,
            visible_chat_id, owner_connection_generation, control_binding_id,
            control_binding_expires_at, lease_expires_at, started_at, updated_at,
            media_grant_nonce_hash, media_grant_issued_at,
            media_grant_expires_at, worker_rtc_grant_revision
        ) VALUES (
            %s, %s, %s, %s, 'web', %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s
        )
    """
    started = datetime(2026, 8, 28, 12, tzinfo=UTC)
    remote_session = str(uuid.uuid4())
    local_session = str(uuid.uuid4())

    with fixture.database.transaction() as transaction:
        transaction.execute(
            insert_sql,
            (
                remote_session,
                "remote-owner",
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                "llm_factory",
                "livekit",
                "remote-room",
                "remote-participant",
                "remote-chat",
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                started + timedelta(minutes=2),
                started + timedelta(minutes=1),
                started,
                started,
                b"r" * 32,
                started,
                started + timedelta(seconds=30),
                1,
            ),
        )
        transaction.execute(
            insert_sql,
            (
                local_session,
                "local-owner",
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                "client_local",
                "client_local",
                None,
                None,
                "local-chat",
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                started + timedelta(minutes=2),
                started + timedelta(minutes=1),
                started,
                started,
                None,
                None,
                None,
                None,
            ),
        )

    remote_only_values = {
        "room_name": "mixed-room",
        "participant_identity": "mixed-participant",
        "worker_identity": "mixed-worker",
        "media_grant_nonce_hash": b"m" * 32,
        "media_grant_issued_at": started,
        "media_grant_expires_at": started + timedelta(seconds=30),
        "media_grant_consumed_at": started + timedelta(seconds=1),
        "last_media_refresh_id": str(uuid.uuid4()),
        "worker_assignment_id": str(uuid.uuid4()),
        "worker_rtc_grant_revision": 1,
        "worker_rtc_grant_issued_at": started,
        "worker_rtc_grant_expires_at": started + timedelta(seconds=30),
    }
    for field, value in remote_only_values.items():
        with (
            pytest.raises(Exception, match="voice_session_speech_backend_075_check"),
            fixture.database.transaction() as transaction,
        ):
            transaction.execute(
                f"UPDATE voice_session SET {field} = %s WHERE session_id = %s",
                (value, local_session),
            )

    for field in (
        "room_name",
        "participant_identity",
        "media_grant_nonce_hash",
        "media_grant_issued_at",
        "media_grant_expires_at",
        "worker_rtc_grant_revision",
    ):
        with (
            pytest.raises(Exception, match="voice_session_speech_backend_075_check"),
            fixture.database.transaction() as transaction,
        ):
            transaction.execute(
                f"UPDATE voice_session SET {field} = NULL WHERE session_id = %s",
                (remote_session,),
            )

    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            "SELECT speech_backend, transport, room_name, participant_identity "
            "FROM voice_session ORDER BY speech_backend"
        )
        assert cursor.fetchall() == [
            ("client_local", "client_local", None, None),
            ("llm_factory", "livekit", "remote-room", "remote-participant"),
        ]
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'voice_session'"
        )
        columns = {str(row[0]) for row in cursor.fetchall()}
        assert columns.isdisjoint(
            {
                "audio",
                "audio_bytes",
                "transcript",
                "transcript_digest",
                "capability",
                "capabilities",
                "speech_engine",
            }
        )
    finally:
        cursor.close()
        fixture.connection.rollback()


def test_two_starter_migration_race_converges_once(
    empty_postgres_schema: _EmptySchema,
) -> None:
    fixture = empty_postgres_schema
    database_url = os.environ[TEST_DATABASE_ENV]
    expected_steps = (
        BASELINE_MIGRATION_NAME,
        "astralplane-067-transactional-recovery",
        "astralplane-074-lets-authority",
        "astralplane-074-quality-audit-ownership",
        "astralplane-074-current-runtime-contract",
        "astralplane-074-pending-attachment-materialization",
        "astralplane-075-client-local-speech",
        "astralplane-079-persistent-assignments",
        "astralplane-088-one-shot-operations",
        "astralplane-088-session-incarnation",
        "astralplane-088-session-issuer",
        "astralplane-088-declarative-agents",
        "astralplane-088-owner-guidance",
        "astralplane-088-selected-input",
        "astralplane-088-scheduler-policy",
        "astralplane-088-framework-credentials",
        "astralplane-089-typesafe-credentials",
        "astralplane-089-mesh-enrollment-records",
        "astralplane-owner-stop-epochs",
    )
    connections = [
        connect_fixture_database(database_url),
        connect_fixture_database(database_url),
    ]
    databases: list[PlaneDatabase] = []
    for connection in connections:
        cursor = connection.cursor()
        try:
            cursor.execute(f"SET search_path TO {_quoted_schema(fixture.schema)}, pg_catalog")
            connection.commit()
        finally:
            cursor.close()
        databases.append(PlaneDatabase(ConnectionPool(_DedicatedDriverPool(connection))))

    reports: list[Any] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()
    start = threading.Barrier(2)

    def boot(database: PlaneDatabase) -> None:
        try:
            runner = BaselineMigrationRunner(
                database,
                MigrationRunner(
                    database,
                    revision=CURRENT_DATA_PLANE_REVISION,
                    registry=MIGRATION_REGISTRY,
                ),
            )
            start.wait(timeout=10)
            report = runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
            with result_lock:
                reports.append(report)
        except BaseException as exc:
            with result_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=boot, args=(database,), daemon=True) for database in databases
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        if any(thread.is_alive() for thread in threads):
            raise AssertionError("two-starter migration race exceeded its deadline")
        if errors:
            raise AssertionError("two-starter migration race failed") from errors[0]
    finally:
        for connection in connections:
            connection.close()

    observed_steps = [step for report in reports for step in report.applied_steps]
    assert len(reports) == 2
    assert all(observed_steps.count(step) == 1 for step in expected_steps)
    compatibility = inspect_baseline_compatibility(fixture.database)
    assert compatibility.state is BaselineCompatibilityState.COMPATIBLE
    assert compatibility.observed_revision == CURRENT_DATA_PLANE_REVISION.schema_revision
    assert not compatibility.missing_required_tables


def test_extracted_legacy_contracts_have_exact_live_indexes_and_foreign_keys(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone

    required_indexes = {
        "idx_attachment_parser_status": ("attachment_parser", "(status)"),
        "idx_background_task_user": ("background_task", "(user_id, created_at DESC)"),
        "idx_chat_steps_chat_id": ("chat_steps", "(chat_id, started_at)"),
        "idx_chat_steps_turn": ("chat_steps", "(turn_message_id)"),
        "idx_component_version_lookup": (
            "component_version",
            "(chat_id, component_id, version_no DESC)",
        ),
        "idx_machine_credential_owner": ("machine_credential", "(owner_user_id)"),
        "idx_message_attachment_chat": (
            "message_attachment",
            "(chat_id, created_at)",
        ),
        "idx_message_attachment_message": (
            "message_attachment",
            "(message_id, user_id)",
        ),
        "idx_messages_chat_user_ts": (
            "messages",
            '(chat_id, user_id, "timestamp", id)',
        ),
        "idx_remote_machine_owner": ("remote_machine", "(owner_user_id)"),
        "idx_share_grant_owner": ("share_grant", "(user_id, created_at DESC)"),
        "uq_attachment_parser_gap": ("attachment_parser", "(gap_fingerprint)"),
    }
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema()"
        )
        observed = {
            str(index_name): (str(table_name), str(index_definition))
            for table_name, index_name, index_definition in cursor.fetchall()
        }
        for index_name, (table_name, definition_suffix) in required_indexes.items():
            assert index_name in observed
            observed_table, definition = observed[index_name]
            assert observed_table == table_name
            assert definition.endswith(definition_suffix), definition
        assert (
            "CREATE UNIQUE INDEX uq_attachment_parser_gap"
            in observed["uq_attachment_parser_gap"][1]
        )

        cursor.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND ("
            "(table_name = 'draft_agents' AND column_name = 'source_attachment_id') OR "
            "(table_name = 'messages' AND column_name = 'step_count'))"
        )
        assert set(cursor.fetchall()) == {
            ("draft_agents", "source_attachment_id"),
            ("messages", "step_count"),
        }

        cursor.execute(
            "SELECT referenced.relname, constraint_row.confdeltype "
            "FROM pg_constraint AS constraint_row "
            "JOIN pg_class AS local ON local.oid = constraint_row.conrelid "
            "JOIN pg_class AS referenced ON referenced.oid = constraint_row.confrelid "
            "JOIN pg_namespace AS namespace ON namespace.oid = local.relnamespace "
            "WHERE namespace.nspname = current_schema() "
            "AND local.relname = 'chat_steps' AND constraint_row.contype = 'f'"
        )
        assert dict(cursor.fetchall()) == {"chats": "c", "messages": "n"}
        fixture.connection.commit()
    finally:
        cursor.close()


def test_hot_message_queries_use_the_declared_composite_index(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone

    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
            "SELECT 'index-chat-' || n, 'index-owner-' || (n % 5), 't', n, n "
            "FROM generate_series(1, 400) AS n"
        )
        cursor.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
            "SELECT 'index-chat-' || n, 'index-owner-' || (n % 5), 'user', 'c', t "
            "FROM generate_series(1, 400) AS n, generate_series(1, 20) AS t"
        )
        cursor.execute("ANALYZE messages")

        for statement in (
            "SELECT COUNT(*) FROM messages WHERE chat_id = %s AND user_id = %s",
            "SELECT * FROM messages WHERE chat_id = %s AND user_id = %s "
            "ORDER BY timestamp ASC, id ASC",
            "SELECT id FROM messages WHERE chat_id = %s AND user_id = %s ORDER BY id DESC LIMIT 1",
        ):
            cursor.execute(
                f"EXPLAIN {statement}",
                ("index-chat-7", "index-owner-2"),
            )
            plan = "\n".join(str(row[0]) for row in cursor.fetchall())
            assert "idx_messages_chat_user_ts" in plan, plan
        fixture.connection.commit()
    finally:
        cursor.close()


def test_fresh_baseline_ignores_same_table_columns_in_another_schema(
    fresh_schema_boot: _FreshBoot,
) -> None:
    fixture = fresh_schema_boot.schema
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name IN ('saved_components', 'workspace_snapshot')",
            (fresh_schema_boot.decoy_schema,),
        )
        assert cursor.fetchone() == (2,)
        cursor.execute(
            "SELECT is_nullable, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'saved_components' "
            "AND column_name = 'component_id'"
        )
        assert cursor.fetchone() == ("YES", "text")
        cursor.execute(
            "SELECT 1 FROM pg_constraint "
            "WHERE conrelid = 'workspace_snapshot'::regclass "
            "AND conname = 'fk_workspace_snapshot_turn_message'"
        )
        assert cursor.fetchone() == (1,)
    finally:
        fixture.connection.rollback()
        cursor.close()


def _host_session_kwargs(runtime_contract_version: int) -> dict[str, object]:
    observed_at = datetime(2026, 8, 14, 18, 0, tzinfo=UTC)
    return {
        "host_session_id": str(uuid.uuid4()),
        "host_id": str(uuid.uuid4()),
        "owner_id": f"runtime-owner-{uuid.uuid4().hex}",
        "connection_scope_id": str(uuid.uuid4()),
        "platform": "windows",
        "client_version": "1.2.3",
        "host_generation": 1,
        "supported_runtime_contract_versions": (runtime_contract_version,),
        "runtime_contract_version": runtime_contract_version,
        "release_lock_digest": "a" * 64,
        "eligible_since": observed_at,
        "accepted_at": observed_at,
        "last_seen_at": observed_at,
    }


def test_fresh_host_registration_accepts_only_current_runtime_contract(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    repository = AgentRepository()

    current = _host_session_kwargs(3)
    with fixture.database.transaction() as transaction:
        accepted = repository.create_host_session(transaction, **current)
    assert accepted.runtime_contract_version == 3

    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            "SELECT legacy_runtime_contract FROM agent_host_session WHERE host_session_id = %s",
            (current["host_session_id"],),
        )
        assert cursor.fetchone() == (False,)
    finally:
        fixture.connection.rollback()
        cursor.close()

    for unsupported_version in (2, 4):
        with (
            pytest.raises(
                Exception,
                match="agent_host_session_runtime_contract_version_check",
            ),
            fixture.database.transaction() as transaction,
        ):
            repository.create_host_session(
                transaction,
                **_host_session_kwargs(unsupported_version),
            )


def test_runtime_contract_upgrade_preserves_bounded_legacy_host_history(
    empty_postgres_schema: _EmptySchema,
) -> None:
    fixture = empty_postgres_schema
    initialize_empty_database(fixture.database)
    with fixture.database.transaction() as transaction:
        for migration in (
            PLANE_SCHEMA_067_MIGRATION,
            PLANE_SCHEMA_074_MIGRATION,
            PLANE_SCHEMA_074_002_MIGRATION,
        ):
            migration.apply(transaction)
        transaction.execute(
            "INSERT INTO schema_meta (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("revision", "074.002"),
        )
        transaction.execute(
            "INSERT INTO schema_meta (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("astralplane_migration_digest", PLANE_SCHEMA_074_002_REGISTRY_DIGEST),
        )

    repository = AgentRepository()
    legacy = _host_session_kwargs(2)
    with fixture.database.transaction() as transaction:
        accepted = repository.create_host_session(transaction, **legacy)
    assert accepted.runtime_contract_version == 2

    report = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    ).run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
    assert report.source_revision == "074.002"
    assert report.applied_steps == (
        "astralplane-074-current-runtime-contract",
        "astralplane-074-pending-attachment-materialization",
        "astralplane-075-client-local-speech",
        "astralplane-079-persistent-assignments",
        "astralplane-088-one-shot-operations",
        "astralplane-088-session-incarnation",
        "astralplane-088-session-issuer",
        "astralplane-088-declarative-agents",
        "astralplane-088-owner-guidance",
        "astralplane-088-selected-input",
        "astralplane-088-scheduler-policy",
        "astralplane-088-framework-credentials",
        "astralplane-089-typesafe-credentials",
        "astralplane-089-mesh-enrollment-records",
        "astralplane-owner-stop-epochs",
        "astralplane-089-completion-wake",
        "astralplane-089-notification-records",
        "astralplane-089-atlas-pages",
        "astralplane-089-atlas-revisions",
        "astralplane-089-atlas-links",
        "astralplane-089-capability-publications",
        "astralplane-089-operation-harness-bindings",
        "astralplane-089-push-deliveries",
        "astralplane-089-observation-records",
        "astralplane-089-projects",
        "astralplane-089-harness-captures",
        "astralplane-089-audit-conversation-index",
        "astralplane-089-chat-queue",
        "astralplane-089-conversation-branches",
        "astralplane-089-computer-use",
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )

    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            "SELECT runtime_contract_version, legacy_runtime_contract "
            "FROM agent_host_session WHERE host_session_id = %s",
            (legacy["host_session_id"],),
        )
        assert cursor.fetchone() == (2, True)
    finally:
        fixture.connection.rollback()
        cursor.close()


def test_current_metadata_rejects_same_name_runtime_contract_tampering(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    runner = BaselineMigrationRunner(
        fixture.database,
        MigrationRunner(
            fixture.database,
            revision=CURRENT_DATA_PLANE_REVISION,
            registry=MIGRATION_REGISTRY,
        ),
    )

    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            "ALTER TABLE agent_host_session DROP CONSTRAINT "
            "agent_host_session_runtime_contract_version_check"
        )
        cursor.execute(
            "ALTER TABLE agent_host_session ADD CONSTRAINT "
            "agent_host_session_runtime_contract_version_check "
            "CHECK (runtime_contract_version > 0)"
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    with pytest.raises(Exception, match="runtime contract constraint is incompatible"):
        runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)


def test_current_metadata_cannot_admit_a_dropped_current_index(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    migration = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    runner = BaselineMigrationRunner(fixture.database, migration)

    cursor = fixture.connection.cursor()
    try:
        cursor.execute("DROP INDEX idx_latex_artifacts_owner_run")
        fixture.connection.commit()
    finally:
        cursor.close()

    with pytest.raises(Exception, match="qualification audit indexes"):
        runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)

    fixture.connection.rollback()
    cursor = fixture.connection.cursor()
    try:
        cursor.execute("SELECT value FROM schema_meta WHERE key = 'revision'")
        assert cursor.fetchone() == (CURRENT_DATA_PLANE_REVISION.schema_revision,)
    finally:
        cursor.close()


@pytest.mark.parametrize("tamper_kind", ["index", "constraint"])
def test_current_metadata_cannot_admit_same_name_structural_tampering(
    migrated_clone: MigratedDatabase,
    tamper_kind: str,
) -> None:
    fixture = migrated_clone
    migration = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    runner = BaselineMigrationRunner(fixture.database, migration)

    cursor = fixture.connection.cursor()
    try:
        if tamper_kind == "index":
            cursor.execute("DROP INDEX uq_audit_entries_owner_id")
            cursor.execute("CREATE INDEX uq_audit_entries_owner_id ON audit_entries (id)")
        else:
            cursor.execute("ALTER TABLE audit_entries DROP CONSTRAINT audit_entries_values_check")
            cursor.execute(
                "ALTER TABLE audit_entries ADD CONSTRAINT audit_entries_values_check CHECK (TRUE)"
            )
        fixture.connection.commit()
    finally:
        cursor.close()

    with pytest.raises(Exception, match="canonical structure"):
        runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)


def test_current_metadata_rejects_same_name_voice_backend_constraint_drift(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    runner = BaselineMigrationRunner(
        fixture.database,
        MigrationRunner(
            fixture.database,
            revision=CURRENT_DATA_PLANE_REVISION,
            registry=MIGRATION_REGISTRY,
        ),
    )

    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            "ALTER TABLE voice_session DROP CONSTRAINT "
            "voice_session_speech_backend_075_check"
        )
        cursor.execute(
            "ALTER TABLE voice_session ADD CONSTRAINT "
            "voice_session_speech_backend_075_check CHECK (TRUE)"
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    with pytest.raises(Exception, match="voice speech backend constraint is incompatible"):
        runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)


def test_failed_reconciliation_marker_retries_against_real_postgresql(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    coordinator = PostgresReconciliationCoordinator(fixture.pool)
    hook = ReconciliationHookIdentity(name="deep-contract", version="1.0.0")

    with coordinator.coordinate(
        advisory_lock=RECONCILIATION_ADVISORY_LOCK,
        schema_revision=CURRENT_DATA_PLANE_REVISION.schema_revision,
        plan_digest="b" * 64,
    ) as session:
        first = session.mark_started(hook)
        failed = session.mark_failed(hook, error_type="RuntimeError")
        retried = session.mark_started(hook)

    assert first.state is ReconciliationMarkerState.STARTED
    assert failed.state is ReconciliationMarkerState.FAILED
    assert retried.state is ReconciliationMarkerState.STARTED
    assert retried.attempt == 2
    assert retried.error_type is None


def test_work_admission_real_postgresql_owner_replay_fence_and_rollback(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    repository = WorkAdmissionRepository()
    with fixture.database.transaction() as transaction:
        configs = repository.load_existing_configs(transaction)
    repository.bind_configs(configs)

    owner = OperationOwner(OwnerScope.SCHEDULE, "owner-admission", None)
    request = OperationRequest(
        operation_kind="scheduled_occurrence",
        admission_class=AdmissionClass.SCHEDULED,
        owner=owner,
        submission_id=uuid.uuid4(),
        idempotency_namespace="integration",
        idempotency_key="stable-work",
        normalized_input_digest=hashlib.sha256(b"stable-work").hexdigest(),
        chat_id="chat-admission",
        parent_operation_id=None,
        connection_generation=None,
        request_generation=uuid.uuid4(),
    )
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    with fixture.database.transaction() as transaction:
        accepted = repository.submit(
            transaction,
            request,
            now=now,
            retention=timedelta(days=1),
            slot_lease=timedelta(seconds=30),
        )
    assert isinstance(accepted, AcceptedAdmission)
    assert accepted.state is OperationState.RUNNING

    with fixture.database.transaction() as transaction:
        replay = repository.submit(
            transaction,
            request,
            now=now,
            retention=timedelta(days=1),
            slot_lease=timedelta(seconds=30),
        )
    assert accepted.created is True and replay.created is False
    assert replay == replace(accepted, created=False)

    with (
        fixture.database.transaction() as transaction,
        pytest.raises(WorkAdmissionNotFoundError),
    ):
        repository.query_operation(
            transaction,
            OperationOwner(OwnerScope.SCHEDULE, "other-owner", None),
            accepted.operation_id,
        )

    with fixture.database.transaction() as transaction:
        claim = repository.claim_operation(
            transaction,
            AdmissionClass.SCHEDULED,
            accepted.operation_id,
            now=now,
            retention=timedelta(days=1),
            slot_lease=timedelta(seconds=30),
        )
    assert claim is not None
    with fixture.database.transaction() as transaction:
        current = repository.assert_current_execution(transaction, claim.fence)
        terminal = repository.terminalize(
            transaction,
            claim.fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary="completed",
            retry_after_ms=None,
            now=now,
            retention=timedelta(days=1),
        )
    assert current.state is OperationState.RUNNING
    assert terminal.state is OperationState.COMPLETED

    rolled_back = OperationRequest(
        operation_kind="scheduled_occurrence",
        admission_class=AdmissionClass.SCHEDULED,
        owner=owner,
        submission_id=uuid.uuid4(),
        idempotency_namespace="integration",
        idempotency_key="rolled-back-work",
        normalized_input_digest=hashlib.sha256(b"rolled-back-work").hexdigest(),
        chat_id=None,
        parent_operation_id=None,
        connection_generation=None,
        request_generation=uuid.uuid4(),
    )
    rolled_back_id: uuid.UUID | None = None
    with (
        pytest.raises(RuntimeError, match="force rollback"),
        fixture.database.transaction() as transaction,
    ):
        result = repository.submit(
            transaction,
            rolled_back,
            now=now,
            retention=timedelta(days=1),
            slot_lease=timedelta(seconds=30),
        )
        assert isinstance(result, AcceptedAdmission)
        rolled_back_id = result.operation_id
        raise RuntimeError("force rollback")
    assert rolled_back_id is not None
    with (
        fixture.database.transaction() as transaction,
        pytest.raises(WorkAdmissionNotFoundError),
    ):
        repository.query_operation(transaction, owner, rolled_back_id)


def test_concurrent_quality_reviews_serialize_chain_and_case_transition(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    repository = QualityAuditRepository()
    observed_at = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    with fixture.database.transaction() as transaction:
        repository.create_run(
            transaction,
            QualityTestRunRecord(
                owner_id="system:quality-audit",
                run_id="concurrent-run",
                started_at=observed_at - timedelta(minutes=1),
                finished_at=None,
                system_state={},
                categories=("concurrency",),
                status="running",
            ),
        )
        repository.create_case(
            transaction,
            QualityTestCaseRecord(
                owner_id="system:quality-audit",
                case_id="concurrent-case",
                run_id="concurrent-run",
                suite="plane",
                test_name="serializes_reviews",
                outcome="passed",
                duration_ms=1.0,
                metrics={},
                qualitative="",
                evidence_hash="",
                verification_status="pending",
            ),
        )

    database_url = os.environ[TEST_DATABASE_ENV]
    second_connection = connect_fixture_database(database_url)
    cursor = second_connection.cursor()
    try:
        cursor.execute(f"SET search_path TO {_quoted_schema(fixture.schema)}, pg_catalog")
        second_connection.commit()
    finally:
        cursor.close()
    second_database = PlaneDatabase(ConnectionPool(_DedicatedDriverPool(second_connection)))
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []
    outcome_lock = threading.Lock()

    def review(database: PlaneDatabase, *, entry_id: str, action: str) -> None:
        barrier.wait(timeout=5)
        try:
            with database.transaction() as transaction:
                result = repository.append_review_and_transition(
                    transaction,
                    owner_id="system:quality-audit",
                    entry_id=entry_id,
                    case_id="concurrent-case",
                    action=action,
                    reviewer=entry_id,
                    rationale="concurrency proof",
                    timestamp=observed_at,
                    expected_verification_status="pending",
                )
            value = "missing" if result is None else result.test_case.verification_status
            observed = ("success", value)
        except RepositoryConflictError:
            observed = ("conflict", action)
        with outcome_lock:
            outcomes.append(observed)

    first = threading.Thread(
        target=review,
        kwargs={"database": fixture.database, "entry_id": "review-a", "action": "verified"},
    )
    second = threading.Thread(
        target=review,
        kwargs={"database": second_database, "entry_id": "review-b", "action": "disputed"},
    )
    try:
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        assert not first.is_alive() and not second.is_alive()
        assert sorted(kind for kind, _ in outcomes) == ["conflict", "success"]

        cursor = fixture.connection.cursor()
        try:
            cursor.execute(
                "SELECT verification_status FROM test_case_results WHERE owner_id = %s AND id = %s",
                ("system:quality-audit", "concurrent-case"),
            )
            status = cursor.fetchone()[0]
            cursor.execute(
                "SELECT id, action FROM audit_entries WHERE owner_id = %s ORDER BY id",
                ("system:quality-audit",),
            )
            audits = tuple(cursor.fetchall())
        finally:
            cursor.close()
            fixture.connection.rollback()
        assert status in {"verified", "disputed"}
        assert len(audits) == 1
        assert audits[0][1] == status
    finally:
        second_connection.close()


@pytest.mark.parametrize(
    "owner_id,attachment_id,filename,storage_path",
    [
        ("tenant:owner", "attachment-1", "file.txt", "tenant:owner/attachment-1/file.txt"),
        ("owner-1", "attachment:1", "file.txt", "owner-1/attachment:1/file.txt"),
        ("owner-1", "attachment-1", "CON.txt", "owner-1/attachment-1/CON.txt"),
        ("owner-1", "attachment-1", "trailing.", "owner-1/attachment-1/trailing."),
        ("owner-1", "attachment-1", "trailing ", "owner-1/attachment-1/trailing "),
        (
            "owner-1",
            "attachment-1",
            ".astralplane-stage",
            "owner-1/attachment-1/.astralplane-stage",
        ),
        ("owner-1", "attachment-1", "report?.txt", "owner-1/attachment-1/report?.txt"),
        ("owner-1", "attachment-1", "bad\nname.txt", "owner-1/attachment-1/bad\nname.txt"),
        (
            "owner-1",
            "attachment-1",
            "x" * 256,
            "owner-1/attachment-1/" + "x" * 256,
        ),
        ("owner-1", "attachment-1", "file.txt", "owner-1/other/file.txt"),
        ("owner-1", "attachment-1", "file.txt", "file.txt"),
    ],
    ids=(
        "new-invalid-owner",
        "new-invalid-attachment",
        "windows-reserved-filename",
        "trailing-dot-filename",
        "trailing-space-filename",
        "reserved-staging-filename",
        "platform-metachar-filename",
        "control-character-filename",
        "overlong-filename",
        "identity-mismatched-locator",
        "root-level-locator",
    ),
)
def test_074_004_refuses_unaddressable_legacy_ready_attachment_before_mutation(
    historical_074_003: MigratedDatabase,
    owner_id: str,
    attachment_id: str,
    filename: str,
    storage_path: str,
) -> None:
    fixture = historical_074_003
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO user_attachments (
                attachment_id, user_id, filename, content_type, category, extension,
                size_bytes, sha256, storage_path, created_at
            ) VALUES (%s, %s, %s, 'text/plain', 'document', 'txt', 7, %s, %s, 1)
            """,
            (
                attachment_id,
                owner_id,
                filename,
                hashlib.sha256(b"fixture").hexdigest(),
                storage_path,
            ),
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    current = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    with pytest.raises(Exception, match="cannot represent legacy attachment"):
        current.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)

    cursor = fixture.connection.cursor()
    try:
        cursor.execute("SELECT value FROM schema_meta WHERE key = 'revision'")
        assert cursor.fetchone() == ("074.003",)
        cursor.execute(
            """
            SELECT user_id, attachment_id, filename, storage_path
            FROM user_attachments WHERE attachment_id = %s
            """,
            (attachment_id,),
        )
        assert cursor.fetchone() == (owner_id, attachment_id, filename, storage_path)
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'user_attachments'
                  AND column_name = 'materialization_state'
            )
            """
        )
        assert cursor.fetchone() == (False,)
    finally:
        cursor.close()
        fixture.connection.rollback()


@pytest.mark.parametrize(
    "storage_path",
    [
        r"owner-1\attachment-1\file.txt",
        r"owner-1\attachment-1/file.txt",
    ],
    ids=("windows-separators", "mixed-separators"),
)
def test_074_004_accepts_canonical_windows_legacy_attachment_locator_without_rewriting(
    historical_074_003: MigratedDatabase,
    storage_path: str,
) -> None:
    fixture = historical_074_003
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO user_attachments (
                attachment_id, user_id, filename, content_type, category, extension,
                size_bytes, sha256, storage_path, created_at
            ) VALUES (
                'attachment-1', 'owner-1', 'file.txt', 'text/plain', 'document', 'txt',
                7, %s, %s, 1
            )
            """,
            (hashlib.sha256(b"fixture").hexdigest(), storage_path),
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    report = lineage_074_004_runner(fixture.database).run(expected_revision="074.004")
    assert report.applied_steps == ("astralplane-074-pending-attachment-materialization",)
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            SELECT storage_path, materialization_state, materialization_lease_id
            FROM user_attachments WHERE attachment_id = 'attachment-1'
            """
        )
        assert cursor.fetchone() == (storage_path, "ready", None)
    finally:
        cursor.close()
        fixture.connection.rollback()


def test_074_004_backfills_predecessor_ready_without_admitting_stale_shaped_inserts(
    historical_074_003: MigratedDatabase,
) -> None:
    fixture = historical_074_003
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO user_attachments (
                attachment_id, user_id, filename, content_type, category, extension,
                size_bytes, sha256, storage_path, created_at
            ) VALUES (
                'legacy-attachment', 'legacy-owner', 'file.bin',
                'application/octet-stream', 'data', 'bin', 1, %s,
                'legacy-owner/legacy-attachment/file.bin', 1
            )
            """,
            (hashlib.sha256(b"x").hexdigest(),),
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    report = lineage_074_004_runner(fixture.database).run(expected_revision="074.004")
    assert report.applied_steps == ("astralplane-074-pending-attachment-materialization",)
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            SELECT materialization_state
            FROM user_attachments
            WHERE attachment_id = 'legacy-attachment'
            """
        )
        assert cursor.fetchone() == ("ready",)
        with pytest.raises(Exception, match="materialization_state"):
            cursor.execute(
                """
                INSERT INTO user_attachments (
                    attachment_id, user_id, filename, content_type, category, extension,
                    size_bytes, sha256, storage_path, created_at
                ) VALUES (
                    'stale-writer-attachment', 'legacy-owner', 'stale.bin',
                    'application/octet-stream', 'data', 'bin', 1, %s,
                    'legacy-owner/stale-writer-attachment/stale.bin', 2
                )
                """,
                (hashlib.sha256(b"y").hexdigest(),),
            )
        fixture.connection.rollback()
        cursor.execute(
            "SELECT count(*) FROM user_attachments WHERE attachment_id = %s",
            ("stale-writer-attachment",),
        )
        assert cursor.fetchone() == (0,)
        with pytest.raises(Exception, match="user_attachments_materialization_state_check"):
            cursor.execute(
                """
                INSERT INTO user_attachments (
                    attachment_id, user_id, filename, content_type, category, extension,
                    size_bytes, sha256, storage_path, created_at,
                    materialization_state
                ) VALUES (
                    'forged-ready-attachment', 'legacy-owner', 'stale.bin',
                    'application/octet-stream', 'data', 'bin', 1, %s,
                    'wrong-owner/wrong-prefix/stale.bin', 2, 'ready'
                )
                """,
                (hashlib.sha256(b"z").hexdigest(),),
            )
        fixture.connection.rollback()
        cursor.execute(
            "SELECT count(*) FROM user_attachments WHERE attachment_id = %s",
            ("forged-ready-attachment",),
        )
        assert cursor.fetchone() == (0,)
    finally:
        cursor.close()
        fixture.connection.rollback()


def test_074_004_seeds_legacy_owner_fence_before_casefold_alias_admission(
    historical_074_003: MigratedDatabase,
    tmp_path,
) -> None:
    fixture = historical_074_003
    owner_id = "LegacyOwner"
    alias_owner_id = owner_id.casefold()
    attachment_id = "legacy-owner-attachment"
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO user_attachments (
                attachment_id, user_id, filename, content_type, category, extension,
                size_bytes, sha256, storage_path, created_at
            ) VALUES (
                %s, %s, 'file.bin', 'application/octet-stream', 'data', 'bin',
                1, %s, %s, 1
            )
            """,
            (
                attachment_id,
                owner_id,
                hashlib.sha256(b"x").hexdigest(),
                f"{owner_id}/{attachment_id}/file.bin",
            ),
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    report = lineage_074_004_runner(fixture.database).run(expected_revision="074.004")
    assert report.applied_steps == ("astralplane-074-pending-attachment-materialization",)
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            SELECT owner_id, state, version, retired_at
            FROM astralplane_blob_owner_state
            WHERE lower(owner_id) = lower(%s)
            """,
            (owner_id,),
        )
        assert cursor.fetchall() == [(owner_id, "active", 0, None)]
    finally:
        cursor.close()
        fixture.connection.rollback()

    catalog = create_repository_catalog()
    with (
        pytest.raises(RepositoryConflictError),
        fixture.database.transaction() as transaction,
    ):
        catalog.artifacts.materializations.begin_pending_materialization(
            transaction,
            attachment_id="alias-owner-attachment",
            owner_id=alias_owner_id,
            filename="alias.bin",
            category="data",
            extension="bin",
            storage_locator=f"{alias_owner_id}/alias-owner-attachment/alias.bin",
            storage_key="alias-owner-attachment/alias.bin",
            max_bytes=1,
            created_at=2,
            lease_id="alias-owner-lease",
            lease_seconds=300,
        )

    with (
        pytest.raises(PlaneError) as alias_retirement,
        fixture.database.transaction() as transaction,
    ):
        catalog.purge.schedule_owner_namespace(
            transaction,
            owner_id=alias_owner_id,
            requested_at=datetime(2026, 8, 14, tzinfo=UTC),
            deleted_at=2,
        )
    assert alias_retirement.value.code == "purge_write_invalid"

    with fixture.database.transaction() as transaction:
        assert (
            catalog.artifacts.attachments.get(
                transaction,
                owner_id=owner_id,
                attachment_id=attachment_id,
            )
            is not None
        )
        scheduled = catalog.purge.schedule_owner_namespace(
            transaction,
            owner_id=owner_id,
            requested_at=datetime(2026, 8, 14, 0, 1, tzinfo=UTC),
            deleted_at=3,
        )

    blobs = create_streaming_blob_store(root=(tmp_path / "blobs").resolve())
    executor = create_durable_purge_executor(
        database=fixture.database,
        purge_store=catalog.purge,
        blobs=blobs,
    )
    try:
        result = executor.execute(
            owner_id=owner_id,
            tombstone_id=scheduled.tombstone.tombstone_id,
            now=datetime(2026, 8, 14, 0, 2, tzinfo=UTC),
            retry_at=datetime(2026, 8, 14, 0, 3, tzinfo=UTC),
        )
        assert result.state is PurgeAttemptState.PURGED
        assert blobs.is_owner_absent(owner_id=owner_id)
    finally:
        blobs.close()


@pytest.mark.parametrize("physical_shape", ["hidden_temp", "published_final"])
def test_074_004_refuses_partial_pending_predecessor_without_exposing_or_clearing_it(
    historical_074_003: MigratedDatabase,
    tmp_path,
    physical_shape: str,
) -> None:
    fixture = historical_074_003
    blob = tmp_path / "owner-1" / "attachment-1"
    blob.mkdir(parents=True)
    physical = blob / (
        ".astralplane-stage-crash.tmp" if physical_shape == "hidden_temp" else "fixture.bin"
    )
    physical.write_bytes(b"unqualified partial rollout bytes")
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            ALTER TABLE user_attachments
                ADD COLUMN materialization_state TEXT,
                ADD COLUMN materialization_lease_id TEXT,
                ADD COLUMN materialization_lease_version BIGINT,
                ADD COLUMN materialization_lease_expires_at TIMESTAMPTZ,
                ADD COLUMN materialization_max_bytes BIGINT,
                ADD COLUMN materialization_storage_key TEXT
            """
        )
        cursor.execute(
            """
            INSERT INTO user_attachments (
                attachment_id, user_id, filename, content_type, category, extension,
                size_bytes, sha256, storage_path, created_at, deleted_at,
                materialization_state, materialization_lease_id,
                materialization_lease_version, materialization_lease_expires_at,
                materialization_max_bytes, materialization_storage_key
            ) VALUES (
                'attachment-1', 'owner-1', 'fixture.bin',
                'application/x-astralplane-pending-materialization', 'data', 'bin',
                0, repeat('0', 64), 'owner-1/attachment-1/fixture.bin', 1, NULL,
                'pending', 'lease-1', 0, clock_timestamp() - INTERVAL '1 second',
                4096, 'attachment-1/fixture.bin'
            )
            """
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    current = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    with pytest.raises(
        Exception,
        match=r"predecessor schema canonical structure|clean 074[.]003 predecessor",
    ):
        current.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)

    cursor = fixture.connection.cursor()
    try:
        cursor.execute("SELECT value FROM schema_meta WHERE key = 'revision'")
        assert cursor.fetchone() == ("074.003",)
        cursor.execute(
            """
            SELECT materialization_state, materialization_lease_id,
                   materialization_storage_key
            FROM user_attachments WHERE attachment_id = 'attachment-1'
            """
        )
        assert cursor.fetchone() == (
            "pending",
            "lease-1",
            "attachment-1/fixture.bin",
        )
    finally:
        cursor.close()
    assert physical.read_bytes() == b"unqualified partial rollout bytes"


@pytest.mark.parametrize(
    "scope,object_id,storage_key",
    [
        (
            "owner_namespace",
            "owner-namespace:" + "a" * 64,
            "owner-namespace",
        ),
        ("attachment_prefix", "attachment-1", "attachment-1"),
    ],
)
def test_074_004_refuses_partial_typed_purge_scope_without_retargeting_bytes(
    historical_074_003: MigratedDatabase,
    tmp_path,
    scope: str,
    object_id: str,
    storage_key: str,
) -> None:
    fixture = historical_074_003
    residual = tmp_path / "owner-1" / "attachment-1" / "fixture.bin"
    residual.parent.mkdir(parents=True)
    residual.write_bytes(b"must remain behind failed startup")
    cursor = fixture.connection.cursor()
    try:
        cursor.execute("ALTER TABLE astralplane_purge_tombstone ADD COLUMN target_scope TEXT")
        cursor.execute(
            """
            INSERT INTO astralplane_purge_tombstone (
                tombstone_id, owner_id, object_kind, object_id, storage_key,
                storage_locator_sha256, requested_at, status, attempt_count,
                version, available_at, verified_absent_at, last_error_code,
                target_scope
            ) VALUES (
                %s, 'owner-1', 'attachment', %s, %s,
                repeat('0', 64), clock_timestamp(), 'purged', 1, 1,
                clock_timestamp(), clock_timestamp(), NULL, %s
            )
            """,
            (f"hostile-{scope}", object_id, storage_key, scope),
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    current = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    with pytest.raises(
        Exception,
        match=r"predecessor schema canonical structure|clean 074[.]003 predecessor",
    ):
        current.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)

    cursor = fixture.connection.cursor()
    try:
        cursor.execute("SELECT value FROM schema_meta WHERE key = 'revision'")
        assert cursor.fetchone() == ("074.003",)
        cursor.execute("SELECT target_scope, status FROM astralplane_purge_tombstone")
        assert cursor.fetchone() == (scope, "purged")
    finally:
        cursor.close()
    assert residual.read_bytes() == b"must remain behind failed startup"


@pytest.mark.parametrize(
    "hostile_object",
    [
        "owner_table",
        "owner_function",
        "pending_index",
        "attachment_casefold_index",
    ],
)
def test_074_004_refuses_hostile_precreated_namesakes_without_stamping(
    historical_074_003: MigratedDatabase,
    hostile_object: str,
) -> None:
    fixture = historical_074_003
    statement = {
        "owner_table": "CREATE TABLE astralplane_blob_owner_state (unsafe TEXT)",
        "owner_function": (
            "CREATE FUNCTION astralplane_blob_owner_is_canonical(TEXT) "
            "RETURNS BOOLEAN LANGUAGE SQL IMMUTABLE STRICT AS 'SELECT TRUE'"
        ),
        "pending_index": (
            "CREATE INDEX idx_user_attachments_pending_materialization_expiry "
            "ON user_attachments (attachment_id)"
        ),
        "attachment_casefold_index": (
            "CREATE INDEX uq_user_attachments_attachment_id_casefold "
            "ON user_attachments (attachment_id)"
        ),
    }[hostile_object]
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(statement)
        fixture.connection.commit()
    finally:
        cursor.close()

    current = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    with pytest.raises(
        Exception,
        match=r"predecessor schema canonical structure|clean 074[.]003 predecessor",
    ):
        current.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)

    cursor = fixture.connection.cursor()
    try:
        cursor.execute("SELECT value FROM schema_meta WHERE key = 'revision'")
        assert cursor.fetchone() == ("074.003",)
    finally:
        cursor.close()


@pytest.mark.parametrize(
    "tamper_kind",
    [
        "audit_function",
        "positive_array_function",
        "predecessor_function_config",
        "missing_legacy_index",
        "wrong_legacy_index",
        "legacy_foreign_key",
    ],
)
def test_074_004_refuses_tampered_predecessor_before_canonicalization(
    historical_074_003: MigratedDatabase,
    tamper_kind: str,
) -> None:
    fixture = historical_074_003
    cursor = fixture.connection.cursor()
    try:
        if tamper_kind == "audit_function":
            cursor.execute("ALTER FUNCTION audit_events_protect() SECURITY DEFINER")
        elif tamper_kind == "positive_array_function":
            cursor.execute(
                "ALTER FUNCTION astraldeep_positive_unique_int_array(integer[]) SECURITY DEFINER"
            )
        elif tamper_kind == "predecessor_function_config":
            cursor.execute(
                "ALTER FUNCTION audit_events_assign_chain_sequence() "
                "SET search_path TO hostile, pg_catalog"
            )
        elif tamper_kind == "missing_legacy_index":
            cursor.execute("DROP INDEX idx_audit_correlation")
        elif tamper_kind == "wrong_legacy_index":
            cursor.execute("DROP INDEX idx_audit_correlation")
            cursor.execute("CREATE INDEX idx_audit_correlation ON audit_events (event_id)")
        else:
            cursor.execute(
                "ALTER TABLE test_case_results "
                "DROP CONSTRAINT IF EXISTS test_case_results_run_id_fkey"
            )
            cursor.execute(
                "ALTER TABLE test_case_results ADD CONSTRAINT "
                "test_case_results_run_id_fkey CHECK (TRUE)"
            )
        fixture.connection.commit()
    finally:
        cursor.close()

    current = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    with pytest.raises(
        Exception,
        match=(
                r"predecessor schema canonical structure|audit protection predecessor|"
                r"positive integer-array predecessor|"
            r"clean 074[.]003 predecessor|legacy foreign key"
        ),
    ):
        current.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)

    cursor = fixture.connection.cursor()
    try:
        cursor.execute("SELECT value FROM schema_meta WHERE key = 'revision'")
        assert cursor.fetchone() == ("074.003",)
        if tamper_kind in {"audit_function", "positive_array_function"}:
            function_name = (
                "audit_events_protect"
                if tamper_kind == "audit_function"
                else "astraldeep_positive_unique_int_array"
            )
            cursor.execute(
                f"""
                SELECT function_record.prosecdef
                FROM pg_proc AS function_record
                JOIN pg_namespace AS namespace_record
                  ON namespace_record.oid = function_record.pronamespace
                 AND namespace_record.nspname = current_schema()
                WHERE function_record.proname = '{function_name}'
                """
            )
            assert cursor.fetchone() == (True,)
        elif tamper_kind == "predecessor_function_config":
            cursor.execute(
                """
                SELECT function_record.proconfig
                FROM pg_proc AS function_record
                JOIN pg_namespace AS namespace_record
                  ON namespace_record.oid = function_record.pronamespace
                 AND namespace_record.nspname = current_schema()
                WHERE function_record.proname =
                    'audit_events_assign_chain_sequence'
                """
            )
            configuration = cursor.fetchone()[0]
            assert configuration is not None
            assert any("hostile" in setting for setting in configuration)
        elif tamper_kind in {"missing_legacy_index", "wrong_legacy_index"}:
            cursor.execute(
                "SELECT to_regclass('idx_audit_correlation'), "
                "CASE WHEN to_regclass('idx_audit_correlation') IS NULL THEN NULL "
                "ELSE pg_get_indexdef(to_regclass('idx_audit_correlation'), 1, TRUE) END"
            )
            index_record, first_key = cursor.fetchone()
            if tamper_kind == "missing_legacy_index":
                assert index_record is None
                assert first_key is None
            else:
                assert index_record is not None
                assert first_key == "event_id"
        else:
            cursor.execute(
                """
                SELECT constraint_record.contype
                FROM pg_constraint AS constraint_record
                WHERE constraint_record.conrelid = 'test_case_results'::regclass
                  AND constraint_record.conname = 'test_case_results_run_id_fkey'
                """
            )
            assert cursor.fetchone() == ("c",)
    finally:
        cursor.close()
        fixture.connection.rollback()


@pytest.mark.parametrize(
    "tamper_kind",
    [
        "scope_constraint",
        "owner_function",
        "attachment_casefold_index",
        "audit_function_attributes",
        "unexpected_index",
        "unexpected_trigger",
        "table_persistence",
        "table_row_security",
        "function_search_path",
        "foreign_key_match_type",
        "table_acl",
        "column_acl",
        "function_acl",
        "unexpected_function",
        "unexpected_rule",
        "unexpected_inheritance",
        "incoming_inheritance",
        "disabled_internal_trigger",
        "column_type_namespace",
        "index_include_column",
        "omitted_table_column",
        "schema_acl",
        "sequence_state",
        "rls_policy",
    ],
)
def test_current_074_004_rejects_same_name_blob_lifecycle_tampering(
    migrated_clone: MigratedDatabase,
    tamper_kind: str,
) -> None:
    fixture = migrated_clone
    runner = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    cursor = fixture.connection.cursor()
    try:
        if tamper_kind == "scope_constraint":
            cursor.execute(
                "ALTER TABLE astralplane_purge_tombstone DROP CONSTRAINT "
                "astralplane_purge_tombstone_target_shape_check"
            )
            cursor.execute(
                "ALTER TABLE astralplane_purge_tombstone ADD CONSTRAINT "
                "astralplane_purge_tombstone_target_shape_check CHECK (TRUE)"
            )
        elif tamper_kind == "owner_function":
            cursor.execute(
                """
                CREATE OR REPLACE FUNCTION astralplane_blob_owner_is_canonical(candidate TEXT)
                RETURNS BOOLEAN LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
                AS 'SELECT TRUE'
                """
            )
        elif tamper_kind == "attachment_casefold_index":
            cursor.execute("DROP INDEX uq_user_attachments_attachment_id_casefold")
            cursor.execute(
                "CREATE INDEX uq_user_attachments_attachment_id_casefold "
                "ON user_attachments (attachment_id)"
            )
        elif tamper_kind == "unexpected_index":
            cursor.execute(
                "CREATE INDEX hostile_user_attachments_created_at ON user_attachments (created_at)"
            )
        elif tamper_kind == "unexpected_trigger":
            cursor.execute(
                """
                CREATE FUNCTION hostile_user_attachments_trigger()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    RETURN NEW;
                END;
                $$
                """
            )
            cursor.execute(
                """
                CREATE TRIGGER hostile_user_attachments_before_write
                BEFORE INSERT OR UPDATE ON user_attachments
                FOR EACH ROW EXECUTE FUNCTION hostile_user_attachments_trigger()
                """
            )
        elif tamper_kind == "table_persistence":
            cursor.execute("ALTER TABLE astralplane_blob_owner_state SET UNLOGGED")
        elif tamper_kind == "table_row_security":
            cursor.execute("ALTER TABLE user_attachments ENABLE ROW LEVEL SECURITY")
        elif tamper_kind == "function_search_path":
            cursor.execute(
                "ALTER FUNCTION audit_events_assign_chain_sequence() "
                "SET search_path TO hostile, pg_catalog"
            )
        elif tamper_kind == "foreign_key_match_type":
            cursor.execute(
                "ALTER TABLE test_case_results DROP CONSTRAINT test_case_results_owner_run_fk"
            )
            cursor.execute(
                "ALTER TABLE test_case_results ADD CONSTRAINT "
                "test_case_results_owner_run_fk "
                "FOREIGN KEY (owner_id, run_id) "
                "REFERENCES test_runs(owner_id, id) MATCH FULL"
            )
        elif tamper_kind == "table_acl":
            cursor.execute("GRANT SELECT ON user_attachments TO PUBLIC")
        elif tamper_kind == "column_acl":
            cursor.execute("GRANT UPDATE (filename) ON user_attachments TO PUBLIC")
        elif tamper_kind == "function_acl":
            cursor.execute(
                "REVOKE EXECUTE ON FUNCTION astralplane_blob_owner_is_canonical(text) FROM PUBLIC"
            )
        elif tamper_kind == "unexpected_function":
            cursor.execute(
                """
                CREATE FUNCTION hostile_shadow_length(candidate text)
                RETURNS integer LANGUAGE SQL IMMUTABLE
                AS 'SELECT 0'
                """
            )
        elif tamper_kind == "unexpected_rule":
            cursor.execute(
                "CREATE RULE hostile_user_attachments_delete AS "
                "ON DELETE TO user_attachments DO INSTEAD NOTHING"
            )
        elif tamper_kind == "unexpected_inheritance":
            cursor.execute("CREATE TABLE hostile_attachment_parent ()")
            cursor.execute("ALTER TABLE user_attachments INHERIT hostile_attachment_parent")
        elif tamper_kind == "incoming_inheritance":
            cursor.execute("CREATE TABLE hostile_attachment_child () INHERITS (user_attachments)")
        elif tamper_kind == "disabled_internal_trigger":
            cursor.execute("ALTER TABLE test_case_results DISABLE TRIGGER ALL")
        elif tamper_kind == "column_type_namespace":
            cursor.execute("CREATE DOMAIN text AS pg_catalog.text")
            cursor.execute(
                "ALTER TABLE user_attachments ALTER COLUMN category TYPE text USING category::text"
            )
        elif tamper_kind == "index_include_column":
            cursor.execute("DROP INDEX idx_user_attachments_user")
            cursor.execute(
                "CREATE INDEX idx_user_attachments_user ON user_attachments "
                "(user_id, created_at) INCLUDE (filename)"
            )
        elif tamper_kind == "omitted_table_column":
            cursor.execute(
                "ALTER TABLE user_credentials ALTER COLUMN user_id DROP NOT NULL"
            )
        elif tamper_kind == "schema_acl":
            cursor.execute(
                f"GRANT CREATE ON SCHEMA {_quoted_schema(fixture.schema)} TO PUBLIC"
            )
        elif tamper_kind == "sequence_state":
            cursor.execute("ALTER SEQUENCE messages_id_seq INCREMENT BY 2")
        elif tamper_kind == "rls_policy":
            cursor.execute(
                "CREATE POLICY hostile_attachment_visibility ON user_attachments "
                "USING (TRUE)"
            )
        else:
            cursor.execute("ALTER FUNCTION audit_events_protect() SECURITY DEFINER")
        fixture.connection.commit()
    finally:
        cursor.close()

    with pytest.raises(
        Exception,
        match=(
            r"canonical structure|attachment case-fold identity index is incompatible|"
            r"authority validation functions are missing"
        ),
    ):
        runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)


def test_current_074_004_rejects_cross_schema_legacy_foreign_key_rebind(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    runner = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    hostile_schema = f"astralplane_hostile_{uuid.uuid4().hex}"
    quoted_hostile = f'"{hostile_schema}"'
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(f"CREATE SCHEMA {quoted_hostile}")
        cursor.execute(f"CREATE TABLE {quoted_hostile}.test_runs (LIKE test_runs INCLUDING ALL)")
        cursor.execute(
            "ALTER TABLE test_case_results ADD CONSTRAINT "
            "test_case_results_run_id_fkey FOREIGN KEY (run_id) REFERENCES "
            f"{quoted_hostile}.test_runs(id)"
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    try:
        with pytest.raises(Exception, match=r"canonical structure|index is incompatible"):
            runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
    finally:
        cursor = fixture.connection.cursor()
        try:
            cursor.execute(f"DROP SCHEMA {quoted_hostile} CASCADE")
            fixture.connection.commit()
        finally:
            cursor.close()


def test_current_verifier_never_resolves_missing_owned_table_from_later_search_path(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    runner = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    hostile_schema = f"astralplane_hostile_{uuid.uuid4().hex}"
    quoted_hostile = f'"{hostile_schema}"'
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(f"CREATE SCHEMA {quoted_hostile}")
        cursor.execute(
            f"CREATE TABLE {quoted_hostile}.user_attachments "
            "(LIKE user_attachments INCLUDING ALL)"
        )
        cursor.execute("DROP TABLE user_attachments CASCADE")
        cursor.execute(
            f"SET search_path TO {_quoted_schema(fixture.schema)}, "
            f"{quoted_hostile}, pg_catalog"
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    try:
        with pytest.raises(Exception, match=r"canonical structure|index is incompatible"):
            runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
    finally:
        cursor = fixture.connection.cursor()
        try:
            cursor.execute(f"DROP SCHEMA {quoted_hostile} CASCADE")
            fixture.connection.commit()
        finally:
            cursor.close()


def test_074_004_legacy_exact_manual_review_has_evidence_bound_operator_recovery(
    historical_074_003: MigratedDatabase,
    tmp_path,
) -> None:
    fixture = historical_074_003
    owner_id = "legacy:owner-1"
    key = ".astralplane-legacy/CON?.bin"
    locator_digest = hashlib.sha256(f"{owner_id}\0{key}".encode()).hexdigest()
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO astralplane_purge_tombstone (
                tombstone_id, owner_id, object_kind, object_id, storage_key,
                storage_locator_sha256, requested_at, status, attempt_count,
                version, available_at, verified_absent_at, last_error_code
            ) VALUES (
                'legacy-purge-1', %s, 'artifact', 'legacy-object-1', %s, %s,
                clock_timestamp(), 'purged', 1, 3, clock_timestamp(),
                clock_timestamp(), NULL
            )
            """,
            (owner_id, key, locator_digest),
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    edge = lineage_074_004_runner(fixture.database)
    assert edge.run(expected_revision="074.004").applied_steps == (
        "astralplane-074-pending-attachment-materialization",
    )
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            SELECT target_scope, status, version, verified_absent_at,
                   manual_resolution_evidence_sha256, manual_resolved_at,
                   last_error_code
            FROM astralplane_purge_tombstone
            WHERE tombstone_id = 'legacy-purge-1'
            """
        )
        assert cursor.fetchone() == (
            "exact_key",
            "manual_review",
            4,
            None,
            None,
            None,
            "legacy_scope_unqualified",
        )
    finally:
        cursor.close()
        fixture.connection.commit()

    blobs = create_streaming_blob_store(root=(tmp_path / "blobs").resolve())
    executor = create_durable_purge_executor(
        database=fixture.database,
        purge_store=create_repository_catalog().purge,
        blobs=blobs,
    )
    evidence = hashlib.sha256(b"retained operator quiescence record").hexdigest()
    resolved = executor.resolve_legacy_exact_for_administration(
        tombstone_id="legacy-purge-1",
        expected_owner_id=owner_id,
        expected_storage_locator_sha256=locator_digest,
        observed_at=datetime(2026, 8, 14, tzinfo=UTC),
        resolution_evidence_sha256=evidence,
    )
    assert resolved.state is PurgeAttemptState.PURGED
    assert executor.has_incomplete_for_administration() is False
    with pytest.raises(PlaneError) as changed_evidence:
        executor.resolve_legacy_exact_for_administration(
            tombstone_id="legacy-purge-1",
            expected_owner_id=owner_id,
            expected_storage_locator_sha256=locator_digest,
            observed_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=UTC),
            resolution_evidence_sha256=hashlib.sha256(b"changed").hexdigest(),
        )
    assert changed_evidence.value.code == "purge_resolution_evidence_conflict"


def test_074_004_quarantines_live_attachment_legacy_tombstone_before_later_typed_delete(
    historical_074_003: MigratedDatabase,
    tmp_path,
) -> None:
    fixture = historical_074_003
    owner_id = "legacy-live-owner"
    attachment_id = "legacy-live-attachment"
    legacy_key = "legacy/file.bin"
    locator_digest = hashlib.sha256(f"{owner_id}\0{legacy_key}".encode()).hexdigest()
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO user_attachments (
                attachment_id, user_id, filename, content_type, category, extension,
                size_bytes, sha256, storage_path, created_at, deleted_at
            ) VALUES (
                %s, %s, 'file.bin', 'application/octet-stream', 'data', 'bin',
                1, %s, %s, 1, NULL
            )
            """,
            (
                attachment_id,
                owner_id,
                hashlib.sha256(b"x").hexdigest(),
                f"{owner_id}/{attachment_id}/file.bin",
            ),
        )
        cursor.execute(
            """
            INSERT INTO astralplane_purge_tombstone (
                tombstone_id, owner_id, object_kind, object_id, storage_key,
                storage_locator_sha256, requested_at, status, attempt_count,
                version, available_at, verified_absent_at, last_error_code
            ) VALUES (
                'legacy-live-conflict', %s, 'attachment', %s, %s, %s,
                clock_timestamp(), 'pending', 0, 0, clock_timestamp(), NULL, NULL
            )
            """,
            (owner_id, attachment_id, legacy_key, locator_digest),
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    report = lineage_074_004_runner(fixture.database).run(expected_revision="074.004")
    assert report.applied_steps == ("astralplane-074-pending-attachment-materialization",)
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            SELECT object_id, target_scope, status
            FROM astralplane_purge_tombstone
            WHERE tombstone_id = 'legacy-live-conflict'
            """
        )
        quarantined = cursor.fetchone()
    finally:
        cursor.close()
        fixture.connection.rollback()
    assert quarantined is not None
    assert quarantined[0].startswith("legacy-unqualified-")
    assert quarantined[1:] == ("exact_key", "manual_review")

    blobs = create_streaming_blob_store(root=(tmp_path / "blobs").resolve())
    executor = create_durable_purge_executor(
        database=fixture.database,
        purge_store=create_repository_catalog().purge,
        blobs=blobs,
    )
    executor.resolve_legacy_exact_for_administration(
        tombstone_id="legacy-live-conflict",
        expected_owner_id=owner_id,
        expected_storage_locator_sha256=locator_digest,
        observed_at=datetime(2026, 8, 14, tzinfo=UTC),
        resolution_evidence_sha256=hashlib.sha256(b"operator resolution").hexdigest(),
    )
    catalog = create_repository_catalog()
    with fixture.database.transaction() as transaction:
        scheduled = catalog.purge.schedule_attachment_prefix(
            transaction,
            owner_id=owner_id,
            attachment_id=attachment_id,
            requested_at=datetime(2026, 8, 14, 0, 1, tzinfo=UTC),
            deleted_at=2,
        )
    assert scheduled.tombstone.target_scope is PurgeTargetScope.ATTACHMENT_PREFIX
    result = executor.execute(
        owner_id=owner_id,
        tombstone_id=scheduled.tombstone.tombstone_id,
        now=datetime(2026, 8, 14, 0, 2, tzinfo=UTC),
        retry_at=datetime(2026, 8, 14, 0, 3, tzinfo=UTC),
    )
    assert result.state is PurgeAttemptState.PURGED
    assert executor.has_incomplete_for_administration() is False
    blobs.close()


@pytest.mark.parametrize(
    "physical_present,legacy_conflict",
    [(True, False), (False, False), (True, True)],
    ids=("extant-bytes", "already-absent", "coexisting-legacy-manual"),
)
def test_074_004_schedules_every_legacy_deleted_attachment_for_typed_cleanup(
    historical_074_003: MigratedDatabase,
    tmp_path,
    physical_present: bool,
    legacy_conflict: bool,
) -> None:
    fixture = historical_074_003
    owner_id = "legacy-owner-1"
    attachment_id = "legacy-attachment-1"
    filename = "file.bin"
    payload = b"legacy soft-deleted bytes"
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO user_attachments (
                attachment_id, user_id, filename, content_type, category, extension,
                size_bytes, sha256, storage_path, created_at, deleted_at
            ) VALUES (%s, %s, %s, 'application/octet-stream', 'data', 'bin',
                      %s, %s, %s, 1, 1000)
            """,
            (
                attachment_id,
                owner_id,
                filename,
                len(payload),
                hashlib.sha256(payload).hexdigest(),
                f"{owner_id}/{attachment_id}/{filename}",
            ),
        )
        if legacy_conflict:
            legacy_key = "legacy/specific.bin"
            cursor.execute(
                """
                INSERT INTO astralplane_purge_tombstone (
                    tombstone_id, owner_id, object_kind, object_id, storage_key,
                    storage_locator_sha256, requested_at, status, attempt_count,
                    version, available_at, verified_absent_at, last_error_code
                ) VALUES (
                    'legacy-delete-conflict', %s, 'attachment', %s, %s, %s,
                    clock_timestamp(), 'pending', 0, 0, clock_timestamp(), NULL, NULL
                )
                """,
                (
                    owner_id,
                    attachment_id,
                    legacy_key,
                    hashlib.sha256(f"{owner_id}\0{legacy_key}".encode()).hexdigest(),
                ),
            )
        fixture.connection.commit()
    finally:
        cursor.close()

    blob_root = (tmp_path / "blobs").resolve()
    blobs = create_streaming_blob_store(root=blob_root)
    if physical_present:
        fixture_blob = blob_root / owner_id / attachment_id / filename
        fixture_blob.parent.mkdir(parents=True)
        fixture_blob.write_bytes(payload)

    edge = lineage_074_004_runner(fixture.database)
    assert edge.run(expected_revision="074.004").applied_steps == (
        "astralplane-074-pending-attachment-materialization",
    )
    assert edge.run(expected_revision="074.004").already_current
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            SELECT tombstone_id, target_scope, status, object_id, storage_key
            FROM astralplane_purge_tombstone
            WHERE owner_id = %s
            ORDER BY target_scope, tombstone_id
            """,
            (owner_id,),
        )
        rows = tuple(cursor.fetchall())
    finally:
        cursor.close()
        fixture.connection.rollback()
    typed = tuple(row for row in rows if row[1] == "attachment_prefix")
    assert len(typed) == 1
    assert typed[0][2:] == ("pending", attachment_id, attachment_id)
    if legacy_conflict:
        manual = tuple(row for row in rows if row[1] == "exact_key")
        assert len(manual) == 1
        assert manual[0][2] == "manual_review"
        assert manual[0][3].startswith("legacy-unqualified-")

    executor = create_durable_purge_executor(
        database=fixture.database,
        purge_store=create_repository_catalog().purge,
        blobs=blobs,
    )
    assert executor.has_incomplete_for_administration() is True
    result = executor.execute(
        owner_id=owner_id,
        tombstone_id=typed[0][0],
        now=datetime(2026, 8, 14, tzinfo=UTC),
        retry_at=datetime(2026, 8, 14, 0, 5, tzinfo=UTC),
    )
    assert result.state is PurgeAttemptState.PURGED
    assert blobs.is_prefix_absent(owner_id=owner_id, prefix=attachment_id)
    assert executor.has_incomplete_for_administration() is legacy_conflict


def test_074_004_deleted_attachment_schedule_failure_rolls_back_every_schema_and_data_write(
    historical_074_003: MigratedDatabase,
) -> None:
    fixture = historical_074_003
    owner_id = "legacy-owner-1"
    attachment_id = "legacy-attachment-1"
    typed_digest = hashlib.sha256(
        f"attachment_prefix\0{owner_id}\0{attachment_id}".encode()
    ).hexdigest()
    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO user_attachments (
                attachment_id, user_id, filename, content_type, category, extension,
                size_bytes, sha256, storage_path, created_at, deleted_at
            ) VALUES (%s, %s, 'file.bin', 'application/octet-stream', 'data', 'bin',
                      1, %s, %s, 1, 1000)
            """,
            (
                attachment_id,
                owner_id,
                hashlib.sha256(b"x").hexdigest(),
                f"{owner_id}/{attachment_id}/file.bin",
            ),
        )
        cursor.execute(
            """
            INSERT INTO astralplane_purge_tombstone (
                tombstone_id, owner_id, object_kind, object_id, storage_key,
                storage_locator_sha256, requested_at, status, attempt_count,
                version, available_at, verified_absent_at, last_error_code
            ) VALUES (%s, 'other-owner', 'artifact', 'other-object', 'other/file.bin', %s,
                      clock_timestamp(), 'pending', 0, 0, clock_timestamp(), NULL, NULL)
            """,
            (
                f"purge-attachment_prefix-{typed_digest}",
                hashlib.sha256(b"other-owner\0other/file.bin").hexdigest(),
            ),
        )
        fixture.connection.commit()
    finally:
        cursor.close()

    current = MigrationRunner(
        fixture.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )
    with pytest.raises(Exception, match=r"duplicate key|unique constraint"):
        current.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
    cursor = fixture.connection.cursor()
    try:
        cursor.execute("SELECT value FROM schema_meta WHERE key = 'revision'")
        assert cursor.fetchone() == ("074.003",)
        cursor.execute(
            """
            SELECT status, object_id FROM astralplane_purge_tombstone
            WHERE tombstone_id = %s
            """,
            (f"purge-attachment_prefix-{typed_digest}",),
        )
        assert cursor.fetchone() == ("pending", "other-object")
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'user_attachments'
                  AND column_name = 'materialization_state'
            )
            """
        )
        assert cursor.fetchone() == (False,)
    finally:
        cursor.close()
        fixture.connection.rollback()


def test_generation_log_write_preserves_claim_revision_and_finish_fence(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    repository = DraftAgentRepository()
    active_claim = str(uuid.uuid4())

    with fixture.database.transaction() as transaction:
        created = repository.create_draft(
            transaction,
            draft_id="generation-log-draft",
            owner_id="generation-log-owner",
            agent_name="Generation log agent",
            agent_slug="generation-log-agent",
            description="claim-fenced progress proof",
            observed_at=1,
        )
    with fixture.database.transaction() as transaction:
        claimed = repository.claim_generation(
            transaction,
            owner_id=created.owner_id,
            draft_id=created.draft_id,
            expected_revision=created.state_revision,
            claim_id=active_claim,
            lease_seconds=300,
        )
    with fixture.database.transaction() as transaction:
        logged = repository.replace_generation_log_for_claim(
            transaction,
            owner_id=created.owner_id,
            draft_id=created.draft_id,
            expected_revision=claimed.state_revision,
            claim_id=active_claim,
            generation_log='[{"message":"progress"}]',
        )

    assert logged.state_revision == claimed.state_revision
    assert logged.generation_log == '[{"message":"progress"}]'
    with fixture.database.transaction() as transaction:
        finished = repository.finish_generation(
            transaction,
            owner_id=created.owner_id,
            draft_id=created.draft_id,
            expected_revision=claimed.state_revision,
            claim_id=active_claim,
            status="generated",
        )
    assert finished.state_revision == claimed.state_revision + 1
    assert finished.generation_claim_id is None
    assert finished.generation_log == logged.generation_log

    denied_claim = str(uuid.uuid4())
    with fixture.database.transaction() as transaction:
        denied = repository.create_draft(
            transaction,
            draft_id="generation-log-denial",
            owner_id="generation-log-owner",
            agent_name="Generation log denial agent",
            agent_slug="generation-log-denial-agent",
            description="stale claim denial proof",
            observed_at=2,
        )
    with fixture.database.transaction() as transaction:
        denied = repository.claim_generation(
            transaction,
            owner_id=denied.owner_id,
            draft_id=denied.draft_id,
            expected_revision=denied.state_revision,
            claim_id=denied_claim,
            lease_seconds=300,
        )

    with (
        pytest.raises(RepositoryConflictError, match="log claim fence"),
        fixture.database.transaction() as transaction,
    ):
        repository.replace_generation_log_for_claim(
            transaction,
            owner_id=denied.owner_id,
            draft_id=denied.draft_id,
            expected_revision=denied.state_revision,
            claim_id=str(uuid.uuid4()),
            generation_log="wrong claim",
        )
    with (
        pytest.raises(RepositoryConflictError, match="log claim fence"),
        fixture.database.transaction() as transaction,
    ):
        repository.replace_generation_log_for_claim(
            transaction,
            owner_id=denied.owner_id,
            draft_id=denied.draft_id,
            expected_revision=denied.state_revision - 1,
            claim_id=denied_claim,
            generation_log="stale revision",
        )

    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            "UPDATE draft_agents SET generation_claim_expires_at = "
            "clock_timestamp() - interval '1 second' "
            "WHERE id = %s AND user_id = %s",
            (denied.draft_id, denied.owner_id),
        )
        fixture.connection.commit()
    finally:
        cursor.close()
    with (
        pytest.raises(RepositoryConflictError, match="log claim fence"),
        fixture.database.transaction() as transaction,
    ):
        repository.replace_generation_log_for_claim(
            transaction,
            owner_id=denied.owner_id,
            draft_id=denied.draft_id,
            expected_revision=denied.state_revision,
            claim_id=denied_claim,
            generation_log="expired claim",
        )


def test_draft_creation_round_trips_initial_candidate_provenance(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    repository = DraftAgentRepository()
    tools_spec = '[{"description":"","name":"search","scope":"records:read"}]'
    plan_json = (
        '{"declared_egress":[],"declared_scopes":["records:read"],'
        '"tasks":["persist atomically"],"tool_scopes":{"search":"records:read"},'
        '"tools":[{"name":"search"}]}'
    )

    with fixture.database.transaction() as transaction:
        created = repository.create_draft(
            transaction,
            draft_id="initial-provenance-draft",
            owner_id="initial-provenance-owner",
            agent_name="Initial provenance agent",
            agent_slug="initial-provenance-agent",
            description="initial candidate provenance round-trip proof",
            observed_at=1,
            tools_spec=tools_spec,
            plan_json=plan_json,
            constitution_version="0.1.0",
        )
    with fixture.database.transaction() as transaction:
        persisted = repository.get_draft(
            transaction,
            owner_id=created.owner_id,
            draft_id=created.draft_id,
        )

    assert persisted is not None
    assert persisted.target_agent_id == created.target_agent_id
    assert uuid.UUID(str(persisted.target_agent_id)).version == 4
    assert persisted.state_revision == 0
    assert persisted.plan_json == plan_json
    assert persisted.constitution_version == "0.1.0"
    assert persisted.tools_spec == tools_spec
    assert persisted.revises_agent_id is None


def test_user_agent_policy_reconciliation_is_concurrent_idempotent_and_rollback_safe(
    migrated_clone: MigratedDatabase,
) -> None:
    fixture = migrated_clone
    repository = AgentRepository()
    with fixture.database.transaction() as transaction:
        for index in range(5):
            repository.create_agent(
                transaction,
                agent_id=f"policy-agent-{index}",
                owner_id=f"policy-owner-{index}",
                display_name=f"Policy agent {index}",
                observed_at=index + 1,
            )

    cursor = fixture.connection.cursor()
    try:
        cursor.execute(
            "UPDATE user_agent SET validated_policy_revision = 'policy-v1' "
            "WHERE agent_id IN ('policy-agent-0', 'policy-agent-2', 'policy-agent-4')"
        )
        cursor.execute(
            "UPDATE user_agent SET revalidation_required = TRUE WHERE agent_id = 'policy-agent-2'"
        )
        cursor.execute(
            "UPDATE user_agent SET validated_policy_revision = 'policy-v2' "
            "WHERE agent_id = 'policy-agent-3'"
        )
        cursor.execute("UPDATE user_agent SET deleted_at = 99 WHERE agent_id = 'policy-agent-4'")
        fixture.connection.commit()
    finally:
        cursor.close()

    database_url = os.environ[TEST_DATABASE_ENV]
    second_connection = connect_fixture_database(database_url)
    cursor = second_connection.cursor()
    try:
        cursor.execute(f"SET search_path TO {_quoted_schema(fixture.schema)}, pg_catalog")
        second_connection.commit()
    finally:
        cursor.close()
    second_database = PlaneDatabase(ConnectionPool(_DedicatedDriverPool(second_connection)))
    barrier = threading.Barrier(2)
    results: list[tuple[bool, int]] = []
    failures: list[BaseException] = []
    outcome_lock = threading.Lock()

    def reconcile(database: PlaneDatabase) -> None:
        try:
            with database.transaction() as transaction:
                barrier.wait(timeout=5)
                result = repository.reconcile_validation_policy_for_administration(
                    transaction,
                    policy_revision="policy-v2",
                )
            with outcome_lock:
                results.append((result.marker_changed, result.agents_marked_for_revalidation))
        except BaseException as exc:  # pragma: no cover
            with outcome_lock:
                failures.append(exc)

    try:
        first = threading.Thread(target=reconcile, args=(fixture.database,))
        second = threading.Thread(target=reconcile, args=(second_database,))
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        assert not first.is_alive() and not second.is_alive()
        assert failures == []
        assert sorted(results) == [(False, 0), (True, 2)]

        cursor = fixture.connection.cursor()
        try:
            cursor.execute("SELECT value FROM schema_meta WHERE key = 'user_agent_policy_revision'")
            assert cursor.fetchone()[0] == "policy-v2"
            cursor.execute(
                "SELECT agent_id, revalidation_required FROM user_agent "
                "WHERE agent_id LIKE 'policy-agent-%' ORDER BY agent_id"
            )
            assert tuple(cursor.fetchall()) == (
                ("policy-agent-0", True),
                ("policy-agent-1", True),
                ("policy-agent-2", True),
                ("policy-agent-3", False),
                ("policy-agent-4", False),
            )
            fixture.connection.commit()
        finally:
            cursor.close()

        with (
            pytest.raises(RuntimeError, match="rollback proof"),
            fixture.database.transaction() as transaction,
        ):
            repository.create_agent(
                transaction,
                agent_id="policy-agent-rollback",
                owner_id="policy-owner-rollback",
                display_name="Rollback agent",
                observed_at=10,
            )
            result = repository.reconcile_validation_policy_for_administration(
                transaction,
                policy_revision="policy-v3",
            )
            assert result.marker_changed
            raise RuntimeError("rollback proof")

        cursor = fixture.connection.cursor()
        try:
            cursor.execute("SELECT value FROM schema_meta WHERE key = 'user_agent_policy_revision'")
            assert cursor.fetchone()[0] == "policy-v2"
            cursor.execute(
                "SELECT COUNT(*) FROM user_agent WHERE agent_id = 'policy-agent-rollback'"
            )
            assert cursor.fetchone()[0] == 0
            fixture.connection.commit()
        finally:
            cursor.close()
    finally:
        second_connection.close()
