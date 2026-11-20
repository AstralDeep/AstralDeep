"""Tests for astralplane.database.migrations: the session-incarnation schema upgrade
issues identities once, preserves every prior session field, and rolls back cleanly
on a failed identity edge.
"""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from astralplane.database import migrations as m
from astralplane.database.baseline import BaselineMigrationRunner
from astralplane.errors import SchemaRevisionError
from tests.fixtures.migrated_template import bound_clone


def old_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "088.001"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "088.001"),
        current_schema_verifier_checksum="35bd630d2be86b48988d2fdbe16da54faea293aca68e80d6363db8fb41ded1de",
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum="f87fa1fba779b03a64feec13821d5426c28159117e6c0aafb47756847706f885",
    )
    assert registry.digest == m.PLANE_SCHEMA_088_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="088.001",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            r for r in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if r < "088.001"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "088.001"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=registry)


def current_runner(database):
    return m.MigrationRunner(
        database, revision=m.CURRENT_DATA_PLANE_REVISION, registry=m.MIGRATION_REGISTRY
    )


@pytest.fixture(scope="module")
def predecessor_template(lineage_templates):
    return lineage_templates.template(old_runner)


@pytest.fixture
def predecessor(predecessor_template):
    with bound_clone(predecessor_template) as clone:
        yield clone


def test_populated_upgrade_preserves_all_prior_session_fields_and_repeats(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        for index in range(4):
            tx.execute(
                "INSERT INTO web_session(sid,user_id,access_token_enc,refresh_token_enc,"
                "interactive_anchor,hard_expires_at,last_refresh_at,resumed,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    f"session-{index}",
                    f"owner-{index % 2}",
                    f"opaque-{index}",
                    "opaque-refresh",
                    10,
                    100 if index else 11,
                    20,
                    bool(index % 2),
                    5,
                ),
            )
        before = tuple(dict(row) for row in tx.fetch_all("SELECT * FROM web_session ORDER BY sid"))
        tx.execute(
            "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
            "VALUES ('retained-chat', 'owner-0', 'Synthetic history', 10, 10)"
        )
        tx.execute(
            "INSERT INTO user_offline_grant (id, user_id, agent_id, refresh_token_enc, "
            "issued_at, expires_at, revoked_at, created_at, updated_at) "
            "VALUES (%s, %s, NULL, %s, %s, %s, NULL, %s, %s)",
            (str(uuid4()), "owner-0", b"legacy-opaque-reference", 10, 100, 10, 10),
        )
        tables = ("persistent_assignment", "user_offline_grant", "chats")
        unchanged = {
            table: tuple(
                dict(row)
                for row in tx.fetch_all(
                    "SELECT id, user_id, title, created_at, updated_at, "
                    "has_saved_components, agent_id, render_revision, "
                    "snapshot_committed_at, conversation_commit_id FROM " + table
                    if table == "chats"
                    else "SELECT * FROM " + table
                )
            )
            for table in tables
        }
    runner = current_runner(db)
    assert runner.run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).applied_steps == (
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
    with db.transaction() as tx:
        after = tuple(dict(row) for row in tx.fetch_all("SELECT * FROM web_session ORDER BY sid"))
        assert before == tuple(
            {
                key: value
                for key, value in row.items()
                if key not in {"incarnation_id", "issuing_issuer", "issuing_client_id"}
            }
            for row in after
        )
        identities = {str(row["incarnation_id"]) for row in after}
        assert len(identities) == 4
        assert all(UUID(value).version == 4 and str(UUID(value)) == value for value in identities)
        after_unchanged = {
            table: tuple(
                dict(row)
                for row in tx.fetch_all(
                    "SELECT id, user_id, title, created_at, updated_at, "
                    "has_saved_components, agent_id, render_revision, "
                    "snapshot_committed_at, conversation_commit_id FROM " + table
                    if table == "chats"
                    else "SELECT * FROM " + table
                )
            )
            for table in tables
        }
        # Later migration adds nullable cols here; excluded from diff
        grants_after = tuple(dict(row) for row in after_unchanged["user_offline_grant"])
        for row in grants_after:
            assert row.pop("max_admissions") is None
            assert row.pop("consumed_admissions") is None
        assert grants_after == unchanged["user_offline_grant"]
        assert {k: v for k, v in after_unchanged.items() if k != "user_offline_grant"} == {
            k: v for k, v in unchanged.items() if k != "user_offline_grant"
        }
    assert (
        BaselineMigrationRunner(db, runner)
        .run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
        .already_current
    )
    with db.transaction() as tx:
        assert after == tuple(
            dict(row) for row in tx.fetch_all("SELECT * FROM web_session ORDER BY sid")
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "ALTER TABLE web_session ALTER COLUMN incarnation_id DROP DEFAULT",
        "ALTER TABLE web_session ALTER COLUMN incarnation_id DROP NOT NULL",
        "ALTER TABLE web_session DROP CONSTRAINT web_session_incarnation_unique",
        "ALTER TABLE web_session DROP CONSTRAINT web_session_incarnation_uuid4",
    ],
)
def test_current_verifier_refuses_weakened_identity_catalog(migrated_clone, corruption):
    db = migrated_clone.database
    runner = current_runner(db)
    with db.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        runner.run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)


def test_predecessor_extra_identity_column_is_refused_without_adoption(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tx.execute("ALTER TABLE web_session ADD COLUMN incarnation_id UUID")
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM web_session")["n"] == 0


def test_failed_identity_edge_rolls_back_issuance_and_can_retry(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tx.execute(
            "INSERT INTO web_session(sid,user_id,access_token_enc,refresh_token_enc,"
            "interactive_anchor,hard_expires_at,last_refresh_at,resumed,created_at) "
            "VALUES('rollback','owner','opaque','opaque',1,100,1,false,1)"
        )

    def fail(tx):
        tx.execute(m.PLANE_SCHEMA_088_002_STATEMENTS[0])
        raise RuntimeError("injected identity edge failure")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=fail) if edge.target_revision == "088.002" else edge
            for edge in m.MIGRATION_REGISTRY.migrations
        ),
        current_schema_verifier=m._verify_current_plane_schema,
        current_schema_verifier_checksum=m.MIGRATION_REGISTRY.current_schema_verifier_checksum,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.MIGRATION_REGISTRY.predecessor_schema_verifier_checksum,
    )
    assert registry.digest == m.MIGRATION_REGISTRY.digest
    runner = m.MigrationRunner(db, revision=m.CURRENT_DATA_PLANE_REVISION, registry=registry)
    with pytest.raises(Exception, match=r"injected|migration"):
        runner.run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        m._verify_predecessor_plane_schema(tx, "088.001")
        assert tx.fetch_one("SELECT count(*) AS n FROM web_session")["n"] == 1
    from tests.integration.test_session_issuer_upgrade import prior_runner as at_088_002

    assert at_088_002(db).run(expected_revision="088.002").applied_steps == (
        "astralplane-088-session-incarnation",
    )
    assert at_088_002(db).run(expected_revision="088.002").already_current
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM web_session")["n"] == 1


def test_populated_088001_issued_and_uncertain_liabilities_survive_upgrade(predecessor):
    import json
    from collections.abc import Mapping
    from pathlib import Path

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

    def rows(tx, table):
        values = [normalize(dict(row)) for row in tx.fetch_all("SELECT * FROM " + table)]
        if table == "web_session":
            for row in values:
                row.pop("incarnation_id", None)
                row.pop("issuing_issuer", None)
                row.pop("issuing_client_id", None)
        if table == "user_offline_grant":
            # Legacy grants predate that migration; both fields stay unset
            for row in values:
                assert row.pop("max_admissions", None) is None
                assert row.pop("consumed_admissions", None) is None
        return sorted(values, key=lambda row: json.dumps(row, sort_keys=True))

    db = predecessor.database
    with db.transaction() as tx:
        for table in tables:
            for row in fixture["tables"][table]:
                columns = list(row)
                assert all(column.replace("_", "").isalnum() for column in columns)
                values = []
                placeholders = []
                for column in columns:
                    value = row[column]
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
        before = {table: rows(tx, table) for table in tables}
        assert {row["state"] for row in before["persistent_assignment_action"]} == {
            "started",
            "uncertain",
        }
        assert all(
            row["data"]["operation"]["version"] == 1 for row in before["persistent_assignment"]
        )
        assert all(
            row["data"]["usage"]["outstanding"]["tool_calls"] == 1
            for row in before["persistent_assignment"]
        )
        assert len(before["assignment_operation_receipt"]) == 2
        assert any(
            row["data"]["operation"]["authority"]["reference_kind"] == "offline_grant"
            for row in before["persistent_assignment"]
        )
        for permit in fixture["permits"]:
            row = next(
                row
                for row in before["persistent_assignment_action"]
                if row["id"] == permit["action_id"]
            )
            attempt = next(
                attempt
                for attempt in row["data"]["attempts"]
                if attempt["attempt_id"] == permit["attempt_id"]
            )
            assert attempt["dispatch_token"] == permit["dispatch_token"]
            assert row["data"]["intent"]["request_digest"] == permit["request_digest"]
    runner = current_runner(db)
    assert runner.run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).applied_steps == (
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
    for _ in range(2):
        with db.transaction() as tx:
            assert {table: rows(tx, table) for table in tables} == before
            identities = [
                str(row["incarnation_id"])
                for row in tx.fetch_all("SELECT incarnation_id FROM web_session")
            ]
            assert all(UUID(value).version == 4 for value in identities)
        assert runner.run(
            expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
        ).already_current
