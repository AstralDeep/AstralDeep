"""Real-PostgreSQL test for astralplane.repositories.scheduler: cadence materialization
never regresses a job definition's time even when the host clock skews.
"""

from __future__ import annotations

import uuid

import pytest

from astralplane.repositories.scheduler import ScheduledJob
from tests.integration.test_catalog_caller_rollback import catalog_database as _catalog_database

catalog_database = _catalog_database


@pytest.mark.parametrize("clock_skew_ms", [-30_000, 30_000])
@pytest.mark.parametrize("schedule_kind", ["interval", "one_shot"])
def test_materialization_never_regresses_definition_time(
    catalog_database, clock_skew_ms, schedule_kind,
):
    database = catalog_database.database
    repository = catalog_database.catalog.scheduler
    with database.transaction() as transaction:
        observed = transaction.fetch_one("SELECT clock_timestamp() AS now")["now"]
        now_ms = int(observed.timestamp() * 1000)
        definition = ScheduledJob(
            job_id=str(uuid.uuid4()), owner_id="clock-owner", name="Clock skew check",
            instruction="Produce an in-app result", schedule_kind=schedule_kind,
            schedule_expression="1h", timezone="UTC", status="active",
            next_run_at=now_ms - 1000,
            created_at=now_ms + clock_skew_ms, updated_at=now_ms + clock_skew_ms,
        )
        repository.create_job_definition(transaction, job=definition)

    def claim(transaction):
        return repository.materialize_and_claim_due_for_administration(
            transaction, instance_id="clock-check", limit=1000, lease_seconds=15,
            eligible=lambda job: job.job_id == definition.job_id,
            next_run=lambda job, due: due + 3_600_000 if schedule_kind == "interval" else None,
        )

    with database.transaction() as transaction:
        batch = claim(transaction)
        assert len(batch.claims) == 1
        claimed = batch.claims[0]
        assert claimed.job.created_at == definition.created_at
        assert claimed.job.updated_at >= definition.updated_at
        assert claimed.job.updated_at >= now_ms
        assert claimed.occurrence.scheduled_for.timestamp() * 1000 == definition.next_run_at
        assert claimed.job.status == ("active" if schedule_kind == "interval" else "completed")
    with database.transaction() as transaction:
        assert claim(transaction).claims == ()
        stored = repository.get_job(
            transaction, owner_id=definition.owner_id, job_id=definition.job_id,
        )
        assert stored == claimed.job
