"""Real-PostgreSQL tests for astralplane.repositories.assignments, audit, and history:
explicit session restore retires every owner completely, never migrates damaged
metadata, and survives an interrupted retry.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import replace
from datetime import timedelta

import pytest
from test_assignments_postgres import database as database
from test_audit import authenticate, event
from test_operation_payload_postgres import settle_args
from test_session_execution_postgres import guard, operation, seed, stored_rows
from test_session_incarnation_postgres import issued

from astralplane import (
    JointRestoreError,
    RestoreBlobEntry,
    RestoredSessionRetirement,
    SessionRetirementError,
    recover_joint_restore,
    retire_restored_sessions,
)
from astralplane.database.migrations import MIGRATION_DIGEST
from astralplane.database.revision import SCHEMA_REVISION
from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.assignments import AssignmentRepository
from astralplane.repositories.audit import AuditRepository
from astralplane.repositories.history import SessionRepository


class RecoveryTarget(dict):
    def __repr__(self):
        return "RecoveryTarget(<private test connection>)"


@pytest.fixture
def target(database):
    from psycopg2.extensions import make_dsn

    database_url = os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"]
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        tx.execute("DELETE FROM web_session")
        tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
    if database_url.startswith("sqlite:"):
        from astralplane.database.sqlite import sqlite_path_from_url
        from astralplane.database.sqlite_migrations import SQLITE_MIGRATION_DIGEST

        return RecoveryTarget(
            database_url=database_url,
            expected_database=str(sqlite_path_from_url(database_url).resolve()),
            expected_schema="main",
            expected_schema_revision=SCHEMA_REVISION,
            expected_migration_digest=SQLITE_MIGRATION_DIGEST,
        )

    with database.transaction() as tx:
        row = tx.fetch_one("SELECT current_database() AS database, current_schema() AS schema")
    return RecoveryTarget(
        database_url=make_dsn(
            os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"],
            options=f"-csearch_path={row['schema']},pg_catalog",
        ),
        expected_database=row["database"],
        expected_schema=row["schema"],
        expected_schema_revision=SCHEMA_REVISION,
        expected_migration_digest=MIGRATION_DIGEST,
    )


def sessions(database):
    with database.transaction() as tx:
        return tx.fetch_all("SELECT * FROM web_session ORDER BY sid")


def joint_restore_arguments(target, tmp_path):
    from astralplane.blob_store import (
        ExplicitRootStreamingBlobStore,
        _create_blob_publish_authority,
    )

    root = tmp_path / "restored-durable-root"
    root.mkdir()
    content = b"synthetic preserved bytes"
    storage = ExplicitRootStreamingBlobStore(root, create_root=False)
    authority = _create_blob_publish_authority(
        owner_id="owner",
        storage_key="recovery/preserved.bin",
        max_bytes=len(content),
        lease_id="restore-test-stage",
    )
    reservation = storage.reserve_materialization_staging(owner_id="owner")
    staged = storage._begin_staged_materialization(
        authority=authority,
        reservation=reservation,
    ).write_chunks((content,))
    storage._publish_staged_materialization(staged, authority=authority)
    storage.close()
    blob = root / "owner" / "recovery" / "preserved.bin"
    blob_entry = RestoreBlobEntry(
        root="attachments",
        storage_key="owner/recovery/preserved.bin",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    staging_digest = hashlib.sha256(b"owner\0recovery/preserved.bin").hexdigest()
    marker_key = (
        "owner/recovery/.astralplane-stage-"
        f"{staging_digest}.lock"
    )
    marker_entry = RestoreBlobEntry(
        root="attachments",
        storage_key=marker_key,
        size_bytes=0,
        sha256=hashlib.sha256(b"").hexdigest(),
    )
    return blob, {
        **target,
        "blob_roots": {"attachments": root},
        "blob_inventory": (blob_entry, marker_entry),
        "expected_blob_roots": ("attachments",),
    }


def test_public_entry_retires_all_owners_without_decoding_or_initializing(
    database, target, monkeypatch
):
    from astralplane import PlaneRuntime
    from astralplane.database.migrations import MigrationRunner
    from astralplane.reconciliation import ReconciliationRunner

    def forbidden(*args, **kwargs):
        pytest.fail("recovery must not initialize, migrate, reconcile or decode credentials")

    monkeypatch.setattr(PlaneRuntime, "initialize", forbidden)
    monkeypatch.setattr(MigrationRunner, "run", forbidden)
    monkeypatch.setattr(ReconciliationRunner, "run", forbidden)
    monkeypatch.setattr(SessionRepository, "get_latest_live_for_owner", forbidden)
    with database.transaction() as tx:
        active = issued(tx, owner_id="active-owner")
        issued(tx, owner_id="missing-identity-owner", resumed=True)
        issued(tx, owner_id="retired-owner")
        issued(tx, owner_id="expired-owner", hard_expires_at=1)
        tx.execute(
            "UPDATE web_session SET user_id='',access_token_enc='' WHERE sid=%s",
            (active.session_id,),
        )
        tx.execute(
            "INSERT INTO astralplane_blob_owner_state(owner_id,state,retired_at) "
            "VALUES('retired-owner','retired',clock_timestamp())"
        )
    result = retire_restored_sessions(**target)
    assert result == RestoredSessionRetirement(4) and sessions(database) == ()
    assert retire_restored_sessions(**target) == RestoredSessionRetirement(0)
    assert "owner" not in repr(result) and "session_id" not in repr(result)
    with pytest.raises(AttributeError):
        result.retired_sessions = 5


@pytest.mark.parametrize("change", ["database", "schema", "revision", "digest", "system-schema"])
def test_wrong_expected_target_refuses_without_any_session_mutation(database, target, change):
    with database.transaction() as tx:
        issued(tx)
    before = sessions(database)
    changed = RecoveryTarget(target)
    key = {
        "database": "expected_database",
        "schema": "expected_schema",
        "revision": "expected_schema_revision",
        "digest": "expected_migration_digest",
        "system-schema": "expected_schema",
    }[change]
    changed[key] = {"revision": "088.001", "digest": "0" * 64, "system-schema": "pg_catalog"}.get(
        change, "unselected"
    )
    with pytest.raises(SessionRetirementError, match=r"^restored session retirement unavailable$"):
        retire_restored_sessions(**changed)
    assert sessions(database) == before


@pytest.mark.parametrize(
    "damage",
    [
        "wrong-database",
        "wrong-schema",
        "wrong-revision",
        "wrong-digest",
        "missing-blob",
        "extra-blob",
        "truncated-blob",
        "changed-content",
        "wrong-inventory-digest",
    ],
)
def test_joint_restore_refuses_unqualified_unit_without_session_mutation(
    database, target, tmp_path, damage
):
    with database.transaction() as tx:
        issued(tx)
    before = sessions(database)
    blob, restore = joint_restore_arguments(target, tmp_path)
    root = blob.parent
    if damage == "wrong-database":
        restore["expected_database"] = "unselected-database"
    elif damage == "wrong-schema":
        restore["expected_schema"] = "unselected_schema"
    elif damage == "wrong-revision":
        restore["expected_schema_revision"] = "088.008"
    elif damage == "wrong-digest":
        restore["expected_migration_digest"] = "0" * 64
    elif damage == "missing-blob":
        blob.unlink()
    elif damage == "extra-blob":
        (root / "unexpected.bin").write_bytes(b"unexpected")
    elif damage == "truncated-blob":
        blob.write_bytes(b"synthetic")
    elif damage == "changed-content":
        blob.write_bytes(b"synthetic preserued bytes")
    else:
        restore["expected_blob_inventory_digest"] = "0" * 64
    with pytest.raises(JointRestoreError):
        recover_joint_restore(**restore)
    assert sessions(database) == before


@pytest.mark.postgresql_schema_catalog
def test_joint_restore_retirement_failure_returns_no_completion_evidence(
    database, target, tmp_path, monkeypatch
):
    with database.transaction() as tx:
        issued(tx)
    before = sessions(database)
    _, restore = joint_restore_arguments(target, tmp_path)
    original = SessionRepository.retire_all_for_recovery

    def interrupted(repository, transaction):
        original(repository, transaction)
        raise RuntimeError("synthetic interruption")

    with monkeypatch.context() as patch:
        patch.setattr(SessionRepository, "retire_all_for_recovery", interrupted)
        with pytest.raises(SessionRetirementError):
            recover_joint_restore(**restore)
    assert sessions(database) == before


@pytest.mark.parametrize(
    "damage", ["old-revision", "future-revision", "wrong-digest", "missing-marker", "catalog"]
)
def test_restored_metadata_or_catalog_damage_is_not_migrated_or_repaired(database, target, damage):
    with database.transaction() as tx:
        issued(tx)
        original = tx.fetch_all("SELECT key,value FROM schema_meta ORDER BY key")
        if damage == "catalog":
            tx.execute("ALTER TABLE web_session ADD COLUMN unexpected_recovery_field TEXT")
        elif damage == "missing-marker":
            tx.execute("DELETE FROM schema_meta WHERE key='astralplane_migration_digest'")
        else:
            key = "astralplane_migration_digest" if damage == "wrong-digest" else "revision"
            value = {
                "old-revision": "088.001",
                "future-revision": "999.001",
                "wrong-digest": "0" * 64,
            }[damage]
            tx.execute("UPDATE schema_meta SET value=%s WHERE key=%s", (value, key))
        damaged = tx.fetch_all("SELECT key,value FROM schema_meta ORDER BY key")
    before = sessions(database)
    try:
        with pytest.raises(SessionRetirementError):
            retire_restored_sessions(**target)
        assert sessions(database) == before
        with database.transaction() as tx:
            assert tx.fetch_all("SELECT key,value FROM schema_meta ORDER BY key") == damaged
    finally:
        with database.transaction() as tx:
            if damage == "catalog":
                tx.execute("ALTER TABLE web_session DROP COLUMN unexpected_recovery_field")
            for row in original:
                tx.execute(
                    "INSERT INTO schema_meta(key,value) VALUES(%s,%s) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (row["key"], row["value"]),
                )


@pytest.mark.parametrize("interruption", [RuntimeError, KeyboardInterrupt])
def test_interruption_after_deletion_rolls_back_and_retry_retires_complete_set(
    database, target, monkeypatch, interruption
):
    with database.transaction() as tx:
        issued(tx, owner_id="first")
        issued(tx, owner_id="second")
    before = sessions(database)
    original = SessionRepository.retire_all_for_recovery
    reached = []

    def interrupted(repository, transaction):
        reached.append(original(repository, transaction))
        raise interruption("synthetic interruption")

    with monkeypatch.context() as patch:
        patch.setattr(SessionRepository, "retire_all_for_recovery", interrupted)
        with pytest.raises(
            SessionRetirementError if interruption is RuntimeError else interruption
        ):
            retire_restored_sessions(**target)
    assert reached == [2] and sessions(database) == before
    assert retire_restored_sessions(**target).retired_sessions == 2


@pytest.mark.parametrize("lock", ["table", "migration"])
def test_held_lock_refuses_with_bounded_wait_and_no_partial_deletion(database, target, lock):
    from astralplane.database.migrations import CURRENT_DATA_PLANE_REVISION

    with database.transaction() as tx:
        issued(tx)
    before = sessions(database)
    with database.transaction() as blocker:
        if lock == "table":
            blocker.execute(
                "UPDATE web_session SET sid=sid WHERE 0"
                if blocker.dialect.is_sqlite
                else "LOCK TABLE web_session IN ACCESS EXCLUSIVE MODE"
            )
        else:
            blocker.fetch_one(
                "SELECT pg_advisory_xact_lock(%s,%s)", CURRENT_DATA_PLANE_REVISION.migration_lock
            )
        started = time.monotonic()
        with pytest.raises(SessionRetirementError):
            retire_restored_sessions(**target)
        assert time.monotonic() - started < 3
    assert sessions(database) == before
    assert retire_restored_sessions(**target).retired_sessions == 1


def test_same_snapshot_restored_again_is_retired_again_without_marker_skip(database, target):
    with database.transaction() as tx:
        original = issued(tx)
        row = dict(tx.fetch_one("SELECT * FROM web_session WHERE sid=%s", (original.session_id,)))
    assert retire_restored_sessions(**target).retired_sessions == 1
    with database.transaction() as tx:
        columns = tuple(row)
        tx.execute(
            "INSERT INTO web_session("
            + ",".join(columns)
            + ") VALUES("
            + ",".join(["%s"] * len(columns))
            + ")",
            tuple(row.values()),
        )
    assert retire_restored_sessions(**target).retired_sessions == 1
    assert retire_restored_sessions(**target).retired_sessions == 0


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "uncertain"])
@pytest.mark.postgresql_schema_catalog
def test_restore_retirement_preserves_liabilities_and_authentic_settlement(
    database, target, tmp_path, outcome
):
    repository = AssignmentRepository()
    blob, restore = joint_restore_arguments(target, tmp_path)
    with database.transaction() as tx:
        values = operation(tx, repository, issued=True)
        record = values[3]
        AuditRepository().append(
            tx,
            event(
                event_id=record.assignment_id,
                correlation_id=record.assignment_id,
                chain_id="recovery-fixture-owner",
                conversation_id=None,
            ),
            authenticate,
        )
        before = stored_rows(tx, record.assignment_id)
        grants = tx.fetch_all("SELECT * FROM user_offline_grant ORDER BY id")
        receipts = tx.fetch_all("SELECT * FROM assignment_operation_receipt ORDER BY assignment_id")
        audit = tx.fetch_all("SELECT * FROM audit_events")
        assert grants and receipts and audit
        session_count = tx.fetch_one("SELECT count(*) AS count FROM web_session")["count"]
    result = recover_joint_restore(**restore)
    assert result.retired_sessions == session_count and session_count > 0
    repeated = recover_joint_restore(**restore)
    assert repeated.retired_sessions == 0
    assert repeated.evidence == result.evidence
    with database.transaction() as tx:
        assert stored_rows(tx, record.assignment_id) == before
        assert tx.fetch_all("SELECT * FROM user_offline_grant ORDER BY id") == grants
        assert (
            tx.fetch_all("SELECT * FROM assignment_operation_receipt ORDER BY assignment_id")
            == receipts
        )
        assert tx.fetch_all("SELECT * FROM audit_events") == audit
        with pytest.raises(RepositoryConflictError):
            guard(tx, repository, values)
        replacement = SessionRepository().put(tx, replace(values[1], incarnation_id=None))
        assert replacement.incarnation_id != values[1].incarnation_id
        with pytest.raises(RepositoryConflictError):
            SessionRepository().assert_current_execution(tx, observation=values[2])
        args = settle_args(tx, record, values[4], values[7], values[8], result_authority=values[2])
        args["outcome"] = replace(args["outcome"], outcome=outcome)
        settled = repository.record_action_outcome(tx, **args)
        current = repository.get_operation(
            tx, owner_id="owner", assignment_id=record.assignment_id
        ).assignment
        assert settled.result["result_available"] is False and settled.result["result"] == {}
        assert (
            current.checkpoint == record.checkpoint
            and current.wake_generation == record.wake_generation
        )
        charged = int(outcome != "uncertain")
        assert current.usage["spent"].get("tool_calls", 0) == charged
        assert current.usage["outstanding"]["tool_calls"] == 1 - charged
        repository.record_action_outcome(tx, **args)
        assert (
            repository.get_operation(
                tx, owner_id="owner", assignment_id=record.assignment_id
            ).assignment.usage
            == current.usage
        )
        if outcome == "uncertain":
            args["outcome"] = replace(args["outcome"], outcome="failed")
            repository.record_action_outcome(tx, **args)
            repository.record_action_outcome(tx, **args)
            final = repository.get_operation(
                tx, owner_id="owner", assignment_id=record.assignment_id
            ).assignment
            assert final.usage["outstanding"]["tool_calls"] == 0
            assert final.usage["spent"]["tool_calls"] == 1
    assert blob.read_bytes() == b"synthetic preserved bytes"


def test_typed_consent_and_execution_are_both_refused_after_retirement(database, target):
    from astralplane.repositories.history import SessionConsentObservation

    with database.transaction() as tx:
        repository, _, execution = seed(tx)
        consent = SessionConsentObservation(
            execution.credential, execution.started_at, execution.started_at + timedelta(seconds=15)
        )
    retire_restored_sessions(**target)
    with database.transaction() as tx:
        for method, value in (
            (repository.assert_current_execution, execution),
            (repository.assert_current_consent, consent),
        ):
            with pytest.raises(RepositoryConflictError):
                method(tx, observation=value)


@pytest.mark.parametrize(
    "corruption", ["set_config", "current_schema", "pg_settings", "schema_meta"]
)
@pytest.mark.postgresql_schema_catalog
def test_restored_shadow_functions_and_metadata_view_are_never_executed(
    database, target, corruption
):
    with database.transaction() as tx:
        issued(tx)
        tx.execute("CREATE SEQUENCE recovery_side_effect")
        if corruption in {"set_config", "current_schema"}:
            signature = (
                "set_config(text,text,boolean)"
                if corruption == "set_config"
                else "current_schema()"
            )
            returns = "text" if corruption == "set_config" else "name"
            tx.execute(
                f"CREATE FUNCTION {signature} RETURNS {returns} LANGUAGE plpgsql AS $$ "
                "BEGIN PERFORM nextval('recovery_side_effect'); RETURN 'PRIVATE'; END $$"
            )
        else:
            tx.execute(
                "CREATE FUNCTION recovery_poison() RETURNS text LANGUAGE plpgsql AS $$ "
                "BEGIN PERFORM nextval('recovery_side_effect'); RETURN 'PRIVATE'; END $$"
            )
            if corruption == "pg_settings":
                tx.execute(
                    "CREATE VIEW pg_settings AS SELECT recovery_poison() AS name,"
                    "'1'::text AS setting"
                )
            else:
                tx.execute("ALTER TABLE schema_meta RENAME TO restore_metadata_original")
                tx.execute(
                    "CREATE VIEW schema_meta AS SELECT key,recovery_poison() AS value "
                    "FROM restore_metadata_original"
                )
    before = sessions(database)
    try:
        with pytest.raises(SessionRetirementError):
            retire_restored_sessions(**target)
        assert sessions(database) == before
        with database.transaction() as tx:
            assert tx.fetch_one("SELECT is_called FROM recovery_side_effect")["is_called"] is False
    finally:
        with database.transaction() as tx:
            if corruption in {"set_config", "current_schema"}:
                tx.execute(f"DROP FUNCTION {signature}")
            else:
                tx.execute(f"DROP VIEW {corruption}")
                tx.execute("DROP FUNCTION recovery_poison()")
                if corruption == "schema_meta":
                    tx.execute("ALTER TABLE restore_metadata_original RENAME TO schema_meta")
            tx.execute("DROP SEQUENCE recovery_side_effect")


def test_lost_commit_acknowledgement_returns_no_completion_and_retry_checks_actual_rows(
    database, target, monkeypatch
):
    from astralplane.database.transaction import Transaction

    with database.transaction() as tx:
        issued(tx)
    finish = Transaction._finish
    committed = []

    def uncertain(transaction, *, failed):
        result = finish(transaction, failed=failed)
        if not failed:
            committed.append(True)
            raise RuntimeError("synthetic lost acknowledgement")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, "_finish", uncertain)
        with pytest.raises(SessionRetirementError):
            retire_restored_sessions(**target)
    assert committed == [True] and sessions(database) == ()
    assert retire_restored_sessions(**target).retired_sessions == 0


@pytest.mark.postgresql_lock_catalog
@pytest.mark.postgresql_schema_catalog
def test_statement_timeout_after_delete_rolls_back_complete_retirement(
    database, target, monkeypatch
):
    with database.transaction() as tx:
        issued(tx)
    before = sessions(database)
    original = SessionRepository.retire_all_for_recovery
    deleted = []

    def slow(repository, transaction):
        deleted.append(original(repository, transaction))
        transaction.execute("SELECT pg_catalog.pg_sleep(3)")

    with monkeypatch.context() as patch:
        patch.setattr(SessionRepository, "retire_all_for_recovery", slow)
        started = time.monotonic()
        with pytest.raises(SessionRetirementError):
            retire_restored_sessions(**target)
        assert time.monotonic() - started < 3
    assert deleted == [1] and sessions(database) == before
