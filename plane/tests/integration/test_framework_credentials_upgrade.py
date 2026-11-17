"""Tests for astralplane.database.migrations: the framework-credentials schema upgrade
adds no rows to existing offline-grant or session data, and a fresh post-upgrade
owner can issue and execute a credential.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.framework_credentials import FrameworkCredentialRepository
from astralplane.repositories.history import (
    FrameworkCredentialFence,
    FrameworkCredentialObservation,
)
from astralplane.repositories.offline_grants import OfflineGrantRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_session_issuer_upgrade import load_liabilities, retained_rows

OWNER = "framework-credential-upgrade-owner"


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(e for e in m.MIGRATION_REGISTRY.migrations if e.target_revision <= "088.007"),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "088.007"),
        current_schema_verifier_checksum="155427334f10cae9a4fb0103b8ada8916762d7dfcb61baadf607bc2431c6fba1",
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum="a91bdcd9592cb719168e81e08b046c90068d26771d68eed5df72647170e3b5ad",
    )
    assert registry.digest == m.PLANE_SCHEMA_088_007_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="088.007",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            r for r in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if r < "088.007"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "088.007"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=registry)


def current_runner(database):
    return m.MigrationRunner(
        database, revision=m.CURRENT_DATA_PLANE_REVISION, registry=m.MIGRATION_REGISTRY
    )


def populated(tx):
    tables = load_liabilities(tx)
    # Raw SQL only: repo helpers assume columns not yet added
    tx.execute(
        "INSERT INTO user_offline_grant (id, user_id, agent_id, refresh_token_enc, "
        "issued_at, expires_at, revoked_at, created_at, updated_at) "
        "VALUES (%s, %s, NULL, %s, %s, %s, NULL, %s, %s)",
        (str(uuid.uuid4()), OWNER, b"pre-upgrade-refresh-token", 100, 2_000_000_000, 100, 100),
    )
    return tuple(dict.fromkeys((*tables, "user_offline_grant")))


@pytest.fixture
def predecessor(lineage_templates):
    with bound_clone(lineage_templates.template(prior_runner)) as clone:
        yield clone


def test_populated007_upgrade_keeps_exact_rows_and_adds_no_credential(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = populated(tx)
        before = retained_rows(tx, tables)
    report = current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    assert report.applied_steps == (
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
        unaffected = tuple(table for table in tables if table != "user_offline_grant")
        assert {k: after[k] for k in unaffected} == {k: before[k] for k in unaffected}
        assert len(after["user_offline_grant"]) == len(before["user_offline_grant"])
        for old_row, new_row in zip(
            before["user_offline_grant"], after["user_offline_grant"], strict=True
        ):
            assert new_row["max_admissions"] is None
            assert new_row["consumed_admissions"] is None
            added_keys = ("max_admissions", "consumed_admissions")
            trimmed = {k: v for k, v in new_row.items() if k not in added_keys}
            assert trimmed == old_row
        assert tx.fetch_all("SELECT * FROM framework_credential") == ()
        grant = OfflineGrantRepository().get_grant(
            tx, owner_id=OWNER, grant_id=before["user_offline_grant"][0]["id"]
        )
        assert grant.max_admissions is None
        assert grant.consumed_admissions is None
        assert grant.admissions_remaining is None
    assert current_runner(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).already_current
    with pytest.raises(SchemaRevisionError):
        prior_runner(db).run(expected_revision="088.007")


def test_after_088_008_a_fresh_owner_session_can_issue_and_execute_a_credential(migrated_clone):
    db = migrated_clone.database
    incarnation = str(uuid.uuid4())
    sid = uuid.uuid4().hex
    with db.transaction() as tx:
        now_s = tx.fetch_one(
            "SELECT floor(extract(epoch FROM clock_timestamp()))::bigint AS now"
        )["now"]
        tx.execute(
            "INSERT INTO web_session (sid, user_id, access_token_enc, refresh_token_enc, "
            "interactive_anchor, hard_expires_at, last_refresh_at, resumed, created_at, "
            "incarnation_id) VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s)",
            (sid, OWNER, "access", "refresh", now_s, now_s + 3_600, now_s, now_s, incarnation),
        )
    repository = FrameworkCredentialRepository()
    credential_id = str(uuid.uuid4())
    with db.transaction() as tx:
        record = repository.issue(
            tx,
            owner_id=OWNER,
            credential_id=credential_id,
            name="My SDK",
            scopes=("operations.submit", "operations.read"),
            token_hash="a" * 64,
            token_prefix="afk_ab12",
            issuer_kind="session_incarnation",
            issuer_reference=incarnation,
            max_admissions=5,
            ttl_seconds=3600,
        )
    assert record.credential_id == credential_id
    assert record.consumed_admissions == 0
    with db.transaction() as tx:
        started = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
        observation = FrameworkCredentialObservation(
            credential=FrameworkCredentialFence(
                owner_id=OWNER,
                credential_id=credential_id,
                token_hash="a" * 64,
                scopes=record.scopes,
                max_admissions=record.max_admissions,
                consumed_admissions=record.consumed_admissions,
                created_at=record.created_at,
                expires_at=record.expires_at,
                revoked_at=None,
            ),
            started_at=started,
            valid_until=started + timedelta(seconds=15),
        )
        state = repository.assert_current_execution(tx, observation=observation)
        assert state.credential.credential_id == credential_id
        consumed = repository.consume_admission(tx, owner_id=OWNER, credential_id=credential_id)
        assert consumed.consumed_admissions == 1
        revoked = repository.revoke(tx, owner_id=OWNER, credential_id=credential_id)
        assert revoked.revoked_at is not None
    with db.transaction() as tx, pytest.raises(RepositoryConflictError):
        repository.assert_current_execution(tx, observation=observation)
