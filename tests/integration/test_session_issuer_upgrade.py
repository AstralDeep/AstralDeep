"""Tests for astralplane.database.migrations: the session-issuer schema upgrade adds
issuing metadata without adopting identity, preserving incarnations and issued
liabilities across the edge.
"""

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from tests.fixtures.migrated_template import bound_clone


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "088.002"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "088.002"),
        current_schema_verifier_checksum="c9321dc01e0196b626c68c7be653f7c89329cd779f90cbd86776c9843730aae0",
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum="ff709a4f975d23dcf10cf27ebd4d7e66d97f213b9f028409cb54cefa0d5fc291",
    )
    assert registry.digest == m.PLANE_SCHEMA_088_002_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="088.002",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            r for r in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if r < "088.002"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "088.002"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=registry)


def current_runner(database):
    return m.MigrationRunner(
        database, revision=m.CURRENT_DATA_PLANE_REVISION, registry=m.MIGRATION_REGISTRY
    )


@pytest.fixture(scope="module")
def predecessor_template(lineage_templates):
    return lineage_templates.template(prior_runner)


@pytest.fixture
def predecessor(predecessor_template):
    with bound_clone(predecessor_template) as clone:
        yield clone


def normalize(value):
    if isinstance(value, (bytes, memoryview)):
        return {"$bytea": bytes(value).hex()}
    if isinstance(value, Mapping):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def retained_rows(tx, tables):
    return {
        table: sorted(
            [normalize(dict(row)) for row in tx.fetch_all("SELECT * FROM " + table)],
            key=lambda row: json.dumps(row, sort_keys=True),
        )
        for table in tables
    }


def load_liabilities(tx):
    fixture = json.loads(
        Path(__file__).parents[1].joinpath("fixtures/session_incarnation_088001.json").read_text()
    )
    assert fixture["source_commit"] == "718021ba019abcd3a04ffbd6b80b88e51395bda6"
    assert fixture["synthetic"] is True
    tables = (
        "web_session",
        "user_offline_grant",
        "persistent_assignment",
        "assignment_operation_receipt",
        "persistent_assignment_action",
        "persistent_assignment_activity",
    )
    assert set(fixture["tables"]) == set(tables)
    for table in tables:
        for row in fixture["tables"][table]:
            columns = list(row)
            assert all(column.replace("_", "").isalnum() for column in columns)
            values, placeholders = [], []
            for value in row.values():
                if isinstance(value, dict) and set(value) == {"$bytea"}:
                    value = bytes.fromhex(value["$bytea"])
                    placeholders.append("%s")
                elif isinstance(value, (dict, list)):
                    value = json.dumps(value)
                    placeholders.append("%s::jsonb")
                else:
                    placeholders.append("%s")
                values.append(value)
            tx.execute(
                "INSERT INTO "
                + table
                + " ("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join(placeholders)
                + ")",
                tuple(values),
            )
    for client in (None, "legacy-web-client"):
        tx.execute(
            "INSERT INTO auth_revocation_queue "
            "(user_id,refresh_token_enc,enqueued_at,attempts,client_id) "
            "VALUES (%s,%s,%s,%s,%s)",
            ("synthetic-queue-owner", "opaque-ciphertext", 100, 3, client),
        )
    return (*tables, "auth_revocation_queue")


def test_populated_upgrade_preserves_exact_incarnations_and_issued_liabilities(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = load_liabilities(tx)
        before = retained_rows(tx, tables)
        assert {row["state"] for row in before["persistent_assignment_action"]} == {
            "started",
            "uncertain",
        }
        assert len(before["assignment_operation_receipt"]) == 2
        assert before["user_offline_grant"]
    runner = current_runner(db)
    assert runner.run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).applied_steps == (
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
    with db.transaction() as tx:
        after = retained_rows(tx, tables)
        sessions = after["web_session"]
        assert all(
            row["issuing_issuer"] is None and row["issuing_client_id"] is None for row in sessions
        )
        for row in sessions:
            del row["issuing_issuer"], row["issuing_client_id"]
        for row in after["auth_revocation_queue"]:
            assert row.pop("issuing_issuer") is None
        # Later migration adds nullable cols here; excluded from diff
        for row in after["user_offline_grant"]:
            assert row.pop("max_admissions") is None
            assert row.pop("consumed_admissions") is None
        assert after == before
    assert runner.run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).already_current
    with db.transaction() as tx:
        assert all(
            row["issuing_issuer"] is None and row["issuing_client_id"] is None
            for row in tx.fetch_all("SELECT * FROM web_session")
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "ALTER TABLE web_session ADD COLUMN issuing_issuer TEXT",
        "ALTER TABLE web_session ADD COLUMN issuing_client_id TEXT",
        "ALTER TABLE auth_revocation_queue ADD COLUMN issuing_issuer TEXT",
        "ALTER TABLE web_session DROP CONSTRAINT web_session_incarnation_uuid4",
    ],
)
def test_wrong_predecessor_catalog_is_refused_before_any_new_edge(predecessor, corruption):
    db = predecessor.database
    with db.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "088.002"
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "ALTER TABLE web_session DROP CONSTRAINT web_session_issuing_metadata",
        "ALTER TABLE web_session ALTER COLUMN issuing_client_id SET DEFAULT 'guessed-client'",
        "ALTER TABLE web_session ALTER COLUMN issuing_issuer TYPE VARCHAR(2048)",
        "ALTER TABLE auth_revocation_queue DROP CONSTRAINT auth_revocation_queue_issuing_metadata",
        "ALTER TABLE auth_revocation_queue ALTER COLUMN issuing_issuer "
        "SET DEFAULT 'guessed-issuer'",
    ],
)
def test_current_issuing_catalog_drift_is_refused(migrated_clone, corruption):
    db = migrated_clone.database
    runner = current_runner(db)
    with db.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        runner.run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)


def test_interrupted_upgrade_rolls_back_both_metadata_columns_and_retries(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = load_liabilities(tx)
        before = retained_rows(tx, tables)

    def interrupt(tx):
        tx.execute(m.PLANE_SCHEMA_088_003_STATEMENTS[0])
        raise RuntimeError("controlled interrupted issuing edge")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=interrupt) if edge.target_revision == "088.003" else edge
            for edge in m.MIGRATION_REGISTRY.migrations
        ),
        current_schema_verifier=m._verify_current_plane_schema,
        current_schema_verifier_checksum=m.CURRENT_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    assert registry.digest == m.MIGRATION_DIGEST
    with pytest.raises(Exception, match=r"controlled|migration"):
        m.MigrationRunner(db, revision=m.CURRENT_DATA_PLANE_REVISION, registry=registry).run(
            expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
        )
    with db.transaction() as tx:
        m._verify_predecessor_plane_schema(tx, "088.002")
        assert retained_rows(tx, tables) == before
    from tests.integration.test_declarative_agents_upgrade import prior_runner as at_088_003

    assert at_088_003(db).run(expected_revision="088.003").applied_steps == (
        "astralplane-088-session-issuer",
    )
    assert at_088_003(db).run(expected_revision="088.003").already_current
