"""Tests for astralplane.database.migrations: the scheduler-policy schema upgrade keeps
every scheduled-job row and imposes no policy by default, with rollback-and-retry and
predecessor checks.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.scheduler import ScheduledJob, SchedulerRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_session_issuer_upgrade import load_liabilities, retained_rows

OWNER = "scheduler-upgrade-owner"
SCHEDULER_TABLES = ("scheduled_job", "scheduled_occurrence", "job_run")


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(e for e in m.MIGRATION_REGISTRY.migrations if e.target_revision <= "088.006"),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "088.006"),
        current_schema_verifier_checksum="13766ff2448cfa38b8e8be4586a17c3becb54be7bbcccead71cfb1494b1ca346",
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum="5788f9df6985a4a97e85ca1077f9a654ecf179240690d70675f6e9625783f1e7",
    )
    assert registry.digest == m.PLANE_SCHEMA_088_006_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="088.006",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            r for r in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if r < "088.006"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "088.006"
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


HEAD_REVISION = m.CURRENT_DATA_PLANE_REVISION.schema_revision


def _known_columns(rows_by_table):
    return {table: set().union(*(set(row) for row in rows)) if rows else set()
            for table, rows in rows_by_table.items()}


def _restricted_to(rows_by_table, known_columns):
    restricted = {}
    for table, rows in rows_by_table.items():
        columns = known_columns.get(table)
        filtered = [
            {key: value for key, value in row.items() if columns is None or key in columns}
            for row in rows
        ]
        restricted[table] = sorted(filtered, key=lambda row: json.dumps(row, sort_keys=True))
    return restricted


def populated(tx):
    tables = load_liabilities(tx)
    repository = SchedulerRepository()
    base = datetime(2026, 9, 1, 12, tzinfo=UTC)
    for index, (kind, expression) in enumerate(
        (("cron", "0 9 * * *"), ("interval", "PT1H"), ("one_shot", "at"))
    ):
        job = repository.create_job_definition(
            tx,
            ScheduledJob(
                job_id=str(uuid.uuid4()),
                owner_id=OWNER,
                name=f"Pre-upgrade {kind}",
                instruction="Read a synthetic source",
                schedule_kind=kind,
                schedule_expression=expression,
                timezone="UTC",
                status=("active", "paused", "completed")[index],
                next_run_at=1_000 + index,
                created_at=1,
                updated_at=2,
                last_run_at=None if index == 0 else 500,
            ),
        )
        pending = repository.create_occurrence(
            tx,
            occurrence_id=str(uuid.uuid4()),
            job_id=job.job_id,
            owner_id=OWNER,
            scheduled_for=base + timedelta(minutes=index),
        )
        assert pending.claim_generation == 0
        claimed = repository.create_occurrence(
            tx,
            occurrence_id=str(uuid.uuid4()),
            job_id=job.job_id,
            owner_id=OWNER,
            scheduled_for=base - timedelta(hours=1, minutes=index),
        )
        repository.claim_occurrence(
            tx,
            owner_id=OWNER,
            occurrence_id=claimed.occurrence_id,
            worker_id="pre-upgrade-worker",
            lease_token=str(uuid.uuid4()),
            now=base,
            lease_expires_at=base + timedelta(seconds=30),
        )
        run = repository.start_legacy_run(
            tx,
            run_id=str(uuid.uuid4()),
            job_id=job.job_id,
            owner_id=OWNER,
            correlation_id=str(uuid.uuid4()),
            started_at=10 + index,
        )
        assert repository.finish_run_for_administration(
            tx,
            run_id=run.run_id,
            outcome="success",
            summary="pre-upgrade run",
            auth_ref=None,
            ended_at=20 + index,
        )
    return tuple(dict.fromkeys((*tables, *SCHEDULER_TABLES)))


def test_populated006_upgrade_keeps_exact_rows_and_imposes_no_policy(predecessor):
    db = predecessor.database
    repository = SchedulerRepository()
    with db.transaction() as tx:
        tables = populated(tx)
        before = retained_rows(tx, tables)
        due_before = repository.list_due_jobs_for_administration(tx, due_at_ms=5_000)
    report = current_runner(db).run(expected_revision=HEAD_REVISION)
    assert "astralplane-088-scheduler-policy" in report.applied_steps
    known_columns = _known_columns(before)
    with db.transaction() as tx:
        assert _restricted_to(retained_rows(tx, tables), known_columns) == before
        assert tx.fetch_all("SELECT * FROM scheduled_job_policy") == ()
        assert tx.fetch_all("SELECT * FROM scheduled_occurrence_assignment") == ()
        assert repository.list_due_jobs_for_administration(tx, due_at_ms=5_000) == due_before
        assert len(due_before) == 1
        for row in before["scheduled_job"]:
            assert repository.get_job_policy(tx, owner_id=OWNER, job_id=row["id"]) is None
            assert (
                repository.list_outstanding_episodes(tx, owner_id=OWNER, job_id=row["id"]) == ()
            )
        active = due_before[0]
        now = datetime.now(UTC)
        occurrence = repository.claim_occurrence(
            tx,
            owner_id=OWNER,
            occurrence_id=next(
                row["occurrence_id"]
                for row in before["scheduled_occurrence"]
                if row["job_id"] == active.job_id and row["state"] == "pending"
            ),
            worker_id="post-upgrade-worker",
            lease_token=str(uuid.uuid4()),
            now=now + timedelta(days=365),
            lease_expires_at=now + timedelta(days=365, seconds=30),
        )
        admission = repository.admit_assignment_episode(
            tx,
            owner_id=OWNER,
            job_id=active.job_id,
            occurrence_id=occurrence.occurrence_id,
            claim_generation=occurrence.claim_generation,
            lease_token=occurrence.lease_token,
            lease_owner=occurrence.lease_owner,
            assignment_id=str(uuid.uuid4()),
            admitted_at=1,
        )
        assert not admission.admitted and admission.reason == "policy_missing"
        assert tx.fetch_all("SELECT * FROM scheduled_occurrence_assignment") == ()
        assert retained_rows(tx, ("scheduled_job", "job_run"))["job_run"] == before["job_run"]
        assert {r["state"] for r in retained_rows(tx, tables)["persistent_assignment_action"]} == {
            "started",
            "uncertain",
        }
    assert current_runner(db).run(expected_revision=HEAD_REVISION).already_current
    with pytest.raises(SchemaRevisionError):
        prior_runner(db).run(expected_revision="088.006")


def test_007_interruption_rolls_back_populated006_and_recovery_repeats(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = populated(tx)
        before = retained_rows(tx, tables)
    with pytest.raises(RuntimeError, match="after full DDL"), db.transaction() as tx:
        m.PLANE_SCHEMA_088_007_MIGRATION.apply(tx)
        assert tx.fetch_one("SELECT to_regclass('scheduled_job_policy') AS t")["t"] is not None
        raise RuntimeError("after full DDL")
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        m._verify_predecessor_plane_schema(tx, "088.006")
        assert tx.fetch_one("SELECT to_regclass('scheduled_job_policy') AS t")["t"] is None
        assert (
            tx.fetch_one("SELECT to_regclass('scheduled_occurrence_assignment') AS t")["t"] is None
        )
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "088.006"
        )
    from tests.integration.test_framework_credentials_upgrade import prior_runner as at_088_007

    assert at_088_007(db).run(expected_revision="088.007").applied_steps == (
        "astralplane-088-scheduler-policy",
    )
    assert at_088_007(db).run(expected_revision="088.007").already_current
    with db.transaction() as tx:
        assert _restricted_to(retained_rows(tx, tables), _known_columns(before)) == before


@pytest.mark.parametrize(
    "corrupt",
    [
        "CREATE TABLE scheduled_job_policy (owner_id TEXT)",
        "ALTER TABLE scheduled_occurrence DROP CONSTRAINT scheduled_occurrence_pkey CASCADE",
        "ALTER TABLE scheduled_job ADD COLUMN max_runs INTEGER",
    ],
)
def test_wrong006_predecessor_refuses_before_mutation(predecessor, corrupt):
    db = predecessor.database
    with db.transaction() as tx:
        tx.execute(corrupt)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=HEAD_REVISION)
    with db.transaction() as tx:
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "088.006"
        )
        assert (
            tx.fetch_one("SELECT to_regclass('scheduled_occurrence_assignment') AS t")["t"] is None
        )


@pytest.mark.parametrize(
    "corrupt",
    [
        "DROP INDEX scheduled_job_policy_owner",
        "DROP INDEX scheduled_occurrence_assignment_job",
        "ALTER TABLE scheduled_job_policy DROP CONSTRAINT scheduled_job_policy_allowance",
        "ALTER TABLE scheduled_occurrence_assignment "
        "DROP CONSTRAINT scheduled_occurrence_assignment_unique",
        "ALTER TABLE scheduled_occurrence_assignment "
        "DROP CONSTRAINT scheduled_occurrence_assignment_assignment_id_owner_id_fkey",
        "ALTER TABLE scheduled_job_policy ADD COLUMN instruction_text TEXT",
    ],
)
def test_current_policy_catalog_refuses_removed_guards_or_new_text(migrated_clone, corrupt):
    db = migrated_clone.database
    with db.transaction() as tx:
        tx.execute(corrupt)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=HEAD_REVISION)
