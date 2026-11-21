"""Real-PostgreSQL tests for astralplane.repositories.scheduler: bounded fairness
scanning rotates past held or refused owners without starving later eligible work,
and malformed scan continuations are rejected.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from astralplane.repositories.scheduler import DueScanContinuation, ScheduledJob
from tests.integration.test_catalog_caller_rollback import catalog_database as _catalog_database

catalog_database = _catalog_database


@pytest.fixture(autouse=True)
def empty_scheduler(catalog_database):
    with catalog_database.database.transaction() as transaction:
        transaction.execute("DELETE FROM scheduled_occurrence")
        transaction.execute("DELETE FROM scheduled_job")


def seed_jobs(catalog_database, *, occurrences=False, held_count=10, owners=None):
    database = catalog_database.database
    repository = catalog_database.catalog.scheduler
    prefix = uuid.uuid4().hex
    with database.transaction() as transaction:
        now = transaction.fetch_one("SELECT clock_timestamp() AS now")["now"]
        now_ms = int(now.timestamp() * 1000)
        jobs = []
        for index in range(held_count + 1):
            due_ms = now_ms - 20_000 + index
            job = ScheduledJob(
                job_id=str(uuid.uuid4()),
                owner_id=(
                    owners[index]
                    if owners
                    else prefix + ("-held" if index < held_count else "-eligible")
                ),
                name="Synthetic fairness",
                instruction="Read a synthetic source",
                schedule_kind="one_shot",
                schedule_expression="at",
                timezone="UTC",
                status="active",
                next_run_at=now_ms + 60_000 if occurrences else due_ms,
                created_at=1,
                updated_at=1,
            )
            repository.create_job_definition(transaction, job=job)
            if occurrences:
                repository.create_occurrence(
                    transaction,
                    occurrence_id=str(uuid.uuid4()),
                    job_id=job.job_id,
                    owner_id=job.owner_id,
                    scheduled_for=datetime.fromtimestamp(due_ms / 1000, tz=UTC),
                )
            jobs.append(job)
    return jobs


@pytest.mark.parametrize("occurrences", [False, True])
def test_ten_held_older_jobs_do_not_exclude_later_eligible_owner(catalog_database, occurrences):
    jobs = seed_jobs(catalog_database, occurrences=occurrences)
    repository = catalog_database.catalog.scheduler
    with catalog_database.database.transaction() as transaction:
        batch = repository.materialize_and_claim_due_for_administration(
            transaction,
            instance_id="fairness-check",
            limit=5,
            lease_seconds=15,
            eligible=lambda job: job.job_id == jobs[-1].job_id,
            next_run=lambda job, due: None,
        )
        assert [claim.job.job_id for claim in batch.claims] == [jobs[-1].job_id]
        for job in jobs[:-1]:
            assert repository.get_job(transaction, owner_id=job.owner_id, job_id=job.job_id) == job


def claim_page(fixture, *, continuation=None, eligible=lambda job: True, limit=1, scan_limit=3):
    with fixture.database.transaction() as transaction:
        return fixture.catalog.scheduler.materialize_and_claim_due_for_administration(
            transaction,
            instance_id="fairness-check",
            limit=limit,
            lease_seconds=60,
            eligible=eligible,
            next_run=lambda job, due: None,
            continuation=continuation,
            scan_limit=scan_limit,
        )


@pytest.mark.parametrize("occurrences", [False, True])
def test_refused_pages_advance_to_later_owner_and_wrap(catalog_database, occurrences):
    jobs = seed_jobs(catalog_database, occurrences=occurrences)
    hint = None
    seen = []
    for _ in range(4):
        batch = claim_page(
            catalog_database,
            continuation=hint,
            eligible=lambda job: job.job_id == jobs[-1].job_id,
        )
        seen.extend(batch.ineligible_job_ids)
        hint = batch.continuation
    assert [item.job.job_id for item in batch.claims] == [jobs[-1].job_id]
    assert {job.job_id for job in jobs[:-1]} <= set(seen)
    for _ in range(4):
        batch = claim_page(
            catalog_database,
            continuation=hint,
            eligible=lambda job: job.job_id == jobs[0].job_id,
        )
        hint = batch.continuation
        if batch.claims:
            break
    assert [item.job.job_id for item in batch.claims] == [jobs[0].job_id]
    assert (
        claim_page(
            catalog_database,
            eligible=lambda job: (
                job.job_id
                in {
                    jobs[0].job_id,
                    jobs[-1].job_id,
                }
            ),
            scan_limit=32,
        ).claims
        == ()
    )


@pytest.mark.parametrize("occurrences", [False, True])
def test_owner_rotation_precedes_dispatch_limit_and_preserves_due_order(
    catalog_database, occurrences
):
    jobs = seed_jobs(
        catalog_database,
        occurrences=occurrences,
        held_count=5,
        owners=["owner-a"] * 4 + ["owner-b", "owner-c"],
    )
    first = claim_page(catalog_database, limit=2, scan_limit=10)
    assert [item.job.job_id for item in first.claims] == [jobs[0].job_id, jobs[4].job_id]
    second = claim_page(catalog_database, continuation=first.continuation, limit=1, scan_limit=10)
    assert [item.job.job_id for item in second.claims] == [jobs[5].job_id]
    third = claim_page(catalog_database, continuation=second.continuation, limit=3, scan_limit=10)
    assert [item.job.job_id for item in third.claims] == [job.job_id for job in jobs[1:4]]
    assert (
        len(
            {
                item.occurrence.occurrence_id
                for page in [first, second, third]
                for item in page.claims
            }
        )
        == 6
    )


@pytest.mark.parametrize("occurrences", [False, True])
def test_deleted_cursor_row_and_empty_scan_are_resettable_hints(catalog_database, occurrences):
    jobs = seed_jobs(catalog_database, occurrences=occurrences, held_count=2)
    first = claim_page(catalog_database, eligible=lambda job: False, scan_limit=2)
    with catalog_database.database.transaction() as transaction:
        transaction.execute("DELETE FROM scheduled_occurrence WHERE job_id = %s", (jobs[1].job_id,))
        transaction.execute("DELETE FROM scheduled_job WHERE id = %s", (jobs[1].job_id,))
    next_page = claim_page(
        catalog_database,
        continuation=first.continuation,
        eligible=lambda job: job.job_id == jobs[2].job_id,
        scan_limit=1,
    )
    assert [item.job.job_id for item in next_page.claims] == [jobs[2].job_id]
    future = DueScanContinuation(
        definition=(2**63 - 1, str(uuid.uuid4())),
        occurrence=(datetime(9999, 1, 1, tzinfo=UTC), str(uuid.uuid4())),
        definition_owner="absent-owner",
        occurrence_owner="absent-owner",
    )
    last = claim_page(catalog_database, continuation=future, scan_limit=2)
    assert [item.job.job_id for item in last.claims] == [jobs[0].job_id]
    empty = claim_page(catalog_database, continuation=last.continuation)
    assert empty.claims == ()
    assert empty.continuation == last.continuation


@pytest.mark.parametrize("occurrences", [False, True])
def test_callback_and_query_work_remain_bounded_and_rollback_is_complete(
    catalog_database, occurrences
):
    seed_jobs(catalog_database, occurrences=occurrences, held_count=40)
    examined = []
    first = claim_page(catalog_database, eligible=lambda job: examined.append(job.job_id) or False)
    assert len(examined) == 3
    examined.clear()
    second = claim_page(
        catalog_database,
        continuation=first.continuation,
        eligible=lambda job: examined.append(job.job_id) or False,
    )
    assert len(examined) == 3
    assert second.continuation != first.continuation
    with pytest.raises(RuntimeError, match="policy failed"):
        claim_page(
            catalog_database,
            continuation=second.continuation,
            eligible=lambda job: (_ for _ in ()).throw(RuntimeError("policy failed")),
        )
    replay = claim_page(
        catalog_database, continuation=second.continuation, eligible=lambda job: False
    )
    assert replay.continuation != second.continuation
    with catalog_database.database.transaction() as transaction:
        assert (
            transaction.fetch_one(
                "SELECT count(*) AS count FROM scheduled_occurrence WHERE state = 'claimed'"
            )["count"]
            == 0
        )
        assert (
            transaction.fetch_one(
                "SELECT count(*) AS count FROM scheduled_job WHERE status != 'active'"
            )["count"]
            == 0
        )


def test_owner_hint_rotates_across_separate_pages(catalog_database):
    jobs = seed_jobs(
        catalog_database,
        occurrences=True,
        held_count=5,
        owners=["owner-a", "owner-b", "owner-a", "owner-b", "owner-a", "owner-b"],
    )
    hint = None
    selected = []
    for _ in range(6):
        batch = claim_page(catalog_database, continuation=hint, limit=1, scan_limit=2)
        hint = batch.continuation
        selected.extend(item.job for item in batch.claims)
    assert [job.owner_id for job in selected] == ["owner-a", "owner-b"] * 3
    assert {job.job_id for job in selected} == {job.job_id for job in jobs}


def test_retry_and_expired_claim_keep_original_occurrence_timestamps(catalog_database):
    jobs = seed_jobs(catalog_database, occurrences=True, held_count=1)
    with catalog_database.database.transaction() as transaction:
        before = transaction.fetch_all("SELECT * FROM scheduled_occurrence ORDER BY scheduled_for")
        transaction.execute(
            "UPDATE scheduled_occurrence SET state='retryable', "
            "next_attempt_at=clock_timestamp()-interval '1 second' WHERE job_id=%s",
            (jobs[0].job_id,),
        )
    first = claim_page(catalog_database, limit=2, scan_limit=2)
    with catalog_database.database.transaction() as transaction:
        transaction.execute(
            "UPDATE scheduled_occurrence SET lease_expires_at=clock_timestamp()-interval '1 second'"
        )
    second = claim_page(catalog_database, continuation=first.continuation, limit=2, scan_limit=2)
    expected = {str(row["occurrence_id"]): row["scheduled_for"] for row in before}
    assert {
        claim.occurrence.occurrence_id: claim.occurrence.scheduled_for for claim in second.claims
    } == expected
    assert all(claim.occurrence.claim_generation == 2 for claim in second.claims)


@pytest.mark.parametrize(
    "changes",
    [
        {"definition": [1, str(uuid.uuid4())]},
        {"definition": (1,)},
        {"definition": (True, str(uuid.uuid4()))},
        {"definition": (-1, str(uuid.uuid4()))},
        {"definition": (2**63, str(uuid.uuid4()))},
        {"definition": (1, "not-a-uuid")},
        {"occurrence": (1, str(uuid.uuid4()))},
        {"occurrence": (datetime(2026, 1, 1), str(uuid.uuid4()))},
        {"occurrence": (datetime(2026, 1, 1, tzinfo=UTC), "not-a-uuid")},
        {"definition_owner": " "},
        {"occurrence_owner": "a" * 513},
    ],
)
def test_malformed_continuations_are_refused(changes):
    with pytest.raises(ValueError):
        DueScanContinuation(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"limit": True},
        {"limit": 1.5},
        {"limit": "1"},
        {"scan_limit": 0},
        {"scan_limit": 1001},
        {"scan_limit": True},
        {"scan_limit": 1.5},
        {"scan_limit": "2"},
        {"continuation": {}},
    ],
)
def test_invalid_scan_inputs_are_refused_before_database_access(catalog_database, changes):
    with pytest.raises(ValueError):
        claim_page(catalog_database, **changes)


@pytest.mark.parametrize("occurrences", [False, True])
@pytest.mark.postgresql_lock_catalog
def test_locked_rows_and_same_cursor_workers_do_not_duplicate_claims(catalog_database, occurrences):
    import os

    from astralplane.database.pool import ConnectionPool
    from astralplane.database.transaction import PlaneDatabase
    from tests.fixtures.pre_split.loader import connect_fixture_database
    from tests.integration.test_catalog_caller_rollback import _DedicatedDriverPool

    jobs = seed_jobs(catalog_database, occurrences=occurrences, held_count=3)
    connection = connect_fixture_database(os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"])
    with connection.cursor() as cursor:
        assert catalog_database.schema.removeprefix("astralplane_fixture_").isalnum()
        cursor.execute(f'SET search_path TO "{catalog_database.schema}", pg_catalog')
        cursor.execute("SET statement_timeout = '1500ms'")
    connection.commit()
    pool = ConnectionPool(_DedicatedDriverPool(connection))
    other = PlaneDatabase(pool)
    repository = catalog_database.catalog.scheduler
    try:
        with catalog_database.database.transaction() as transaction:
            table = "scheduled_occurrence" if occurrences else "scheduled_job"
            column = "job_id" if occurrences else "id"
            transaction.fetch_all(
                f"SELECT * FROM {table} WHERE {column} = %s FOR UPDATE", (jobs[0].job_id,)
            )
            with other.transaction() as second_transaction:
                second = repository.materialize_and_claim_due_for_administration(
                    second_transaction,
                    instance_id="second",
                    limit=2,
                    lease_seconds=60,
                    eligible=lambda job: True,
                    next_run=lambda job, due: None,
                    scan_limit=4,
                )
                assert len(second.claims) == 2
                assert jobs[0].job_id not in {claim.job.job_id for claim in second.claims}
            first = repository.materialize_and_claim_due_for_administration(
                transaction,
                instance_id="first",
                limit=2,
                lease_seconds=60,
                eligible=lambda job: True,
                next_run=lambda job, due: None,
                scan_limit=4,
            )
        assert len(first.claims) == 2
        assert not {claim.occurrence.occurrence_id for claim in first.claims} & {
            claim.occurrence.occurrence_id for claim in second.claims
        }
        assert claim_page(catalog_database, scan_limit=4).claims == ()
    finally:
        pool.close()
        connection.close()
