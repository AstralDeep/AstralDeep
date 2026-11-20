"""Real-PostgreSQL tests for astralplane.repositories.offline_grants and work_admission:
execution and grant reads use the database's own clock under lock, never a mocked
one, and never mutate or renew authority.
"""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import independent_database

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.offline_grants import OfflineGrantRepository
from astralplane.repositories.work_admission import (
    AcceptedAdmission,
    AdmissionClass,
    ExecutionFence,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    StaleWorkExecutionFenceError,
    WorkAdmissionIntegrityError,
    WorkAdmissionRepository,
)


@pytest.fixture
def work(database):
    repo = WorkAdmissionRepository()
    with database.transaction() as tx:
        tx.execute("DELETE FROM operation_submission_result")
        tx.execute(
            "UPDATE operation_admission_slot SET operation_id=NULL,lease_token=NULL,"
            "lease_expires_at=NULL"
        )
        tx.execute("DELETE FROM operation_record")
        configs = repo.load_existing_configs(tx)
        repo.configure(tx, configs)
        repo.bind_configs(configs)
    return repo


def running(database, work, *, admission_class=AdmissionClass.INTERACTIVE):
    with database.transaction() as tx:
        now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
        request = OperationRequest(
            operation_kind="guidance_read",
            admission_class=admission_class,
            owner=OperationOwner(OwnerScope.USER, "clock-owner", None),
            submission_id=uuid.uuid4(),
            idempotency_namespace=None,
            idempotency_key=None,
            normalized_input_digest=None,
            chat_id=None,
            parent_operation_id=None,
            connection_generation=None,
            request_generation=None,
        )
        accepted = work.submit(
            tx, request, now=now, retention=timedelta(days=1), slot_lease=timedelta(minutes=2)
        )
        assert isinstance(accepted, AcceptedAdmission)
        claimed = work.claim_operation(
            tx,
            admission_class,
            accepted.operation_id,
            now=now,
            retention=timedelta(days=1),
            slot_lease=timedelta(minutes=2),
        )
        assert claimed is not None
        return claimed.fence


def grant(database, **changes):
    repo = OfflineGrantRepository()
    owner = "clock-grant-" + uuid.uuid4().hex
    with database.transaction() as tx:
        now = tx.fetch_one("SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS n")[
            "n"
        ]
        args = dict(
            grant_id=str(uuid.uuid4()),
            owner_id=owner,
            agent_id=None,
            encrypted_refresh_token=b"synthetic-encrypted-reference",
            issued_at=now - 1000,
            expires_at=now + 120000,
        )
        args.update(changes)
        return repo, repo.create_grant(tx, **args)


def slot_snapshot(tx, fence):
    return tx.fetch_all(
        "SELECT * FROM operation_admission_slot WHERE operation_id=%s "
        "ORDER BY class_name,slot_number",
        (str(fence.operation_id),),
    )


@pytest.mark.parametrize(
    "admission_class",
    [AdmissionClass.INTERACTIVE, AdmissionClass.VOICE_INTERACTIVE, AdmissionClass.SCHEDULED],
)
def test_execution_exact_complete_chain_is_read_only(database, work, admission_class):
    fence = running(database, work, admission_class=admission_class)
    with database.transaction() as tx:
        before = work.assert_current_execution(tx, fence)
        slots = slot_snapshot(tx, fence)
        assert work.assert_current_execution_lease(tx, fence) == before
        assert work.assert_current_execution_lease(tx, fence) == before
        assert slot_snapshot(tx, fence) == slots


@pytest.mark.parametrize(
    "damage", ["expired", "missing", "wrong_class", "extra", "mixed_token", "zero_generation"]
)
def test_execution_incomplete_or_expired_chain_refuses_without_repair(database, work, damage):
    fence = running(database, work)
    with database.transaction() as tx:
        params = (str(fence.operation_id),)
        if damage == "expired":
            tx.execute(
                "UPDATE operation_admission_slot SET lease_expires_at=clock_timestamp() "
                "WHERE operation_id=%s",
                params,
            )
        elif damage in {"missing", "wrong_class"}:
            tx.execute(
                "DELETE FROM operation_admission_slot WHERE operation_id=%s "
                "AND class_name='interactive'",
                params,
            )
        if damage in {"wrong_class", "extra"}:
            tx.execute(
                "UPDATE operation_admission_slot SET operation_id=%s,lease_token=%s,"
                "claim_generation=1,lease_expires_at=clock_timestamp()+interval '1 minute' "
                "WHERE class_name='mcp' AND slot_number=1",
                (*params, str(uuid.uuid4())),
            )
        elif damage == "mixed_token":
            tx.execute(
                "UPDATE operation_admission_slot SET lease_token=%s "
                "WHERE operation_id=%s AND class_name='interactive'",
                (str(uuid.uuid4()), *params),
            )
        elif damage == "zero_generation":
            tx.execute(
                "UPDATE operation_admission_slot SET claim_generation=0 WHERE operation_id=%s",
                params,
            )
        before = slot_snapshot(tx, fence)
        work.assert_current_execution(tx, fence)
        with pytest.raises(StaleWorkExecutionFenceError):
            work.assert_current_execution_lease(tx, fence)
        assert slot_snapshot(tx, fence) == before


@pytest.mark.parametrize(
    "change",
    [
        {"execution_generation": 2},
        {"execution_lease_token": uuid.uuid4()},
        {"operation_id": uuid.uuid4()},
    ],
)
def test_wrong_execution_refuses(database, work, change):
    fence = running(database, work)
    with database.transaction() as tx, pytest.raises(StaleWorkExecutionFenceError):
        work.assert_current_execution_lease(tx, replace(fence, **change))


def test_cancellation_stops_guidance_but_preserves_old_settlement_assertion(database, work):
    fence = running(database, work)
    with database.transaction() as tx:
        cancelled = work.cancel(
            tx,
            OperationOwner(OwnerScope.USER, "clock-owner", None),
            fence.operation_id,
            "owner_cancelled",
            now=None,
            retention=timedelta(days=1),
        )
        assert cancelled.cancel_requested_at is not None
        assert work.assert_current_execution(tx, fence) == cancelled
        with pytest.raises(StaleWorkExecutionFenceError):
            work.assert_current_execution_lease(tx, fence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_generation", True),
        ("execution_generation", 0),
        ("execution_generation", 2**63),
        ("execution_generation", "1"),
        ("operation_id", "wrong"),
        ("execution_lease_token", None),
    ],
)
def test_forged_execution_fence_refused_before_sql(field, value):
    class NoSQL:
        def execute(self, *args):
            pytest.fail("malformed fence reached SQL")

    fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())
    object.__setattr__(fence, field, value)
    with pytest.raises(RepositoryValidationError):
        WorkAdmissionRepository().assert_current_execution_lease(NoSQL(), fence)
    with pytest.raises(RepositoryValidationError):
        WorkAdmissionRepository().assert_current_execution_lease(NoSQL(), object())


def test_grant_canonical_owner_path_read_only_and_retired_refusal(database):
    repo, original = grant(database)
    with database.transaction() as tx:
        tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (original.owner_id,))
        assert (
            repo.assert_current_grant(tx, owner_id=original.owner_id, grant_id=original.grant_id)
            == original
        )
        assert (
            repo.get_grant(tx, owner_id=original.owner_id, grant_id=original.grant_id) == original
        )
    with database.transaction() as tx:
        tx.execute(
            "INSERT INTO astralplane_blob_owner_state(owner_id,state,retired_at) "
            "VALUES(%s,'retired',clock_timestamp())",
            (original.owner_id,),
        )
    with database.transaction() as tx:
        with pytest.raises(RepositoryConflictError, match="offline grant is unavailable"):
            repo.assert_current_grant(tx, owner_id=original.owner_id, grant_id=original.grant_id)
        assert tx.fetch_one("SELECT 1 AS usable")["usable"] == 1


@pytest.mark.parametrize("damage", ["missing", "foreign", "revoked", "expired", "future"])
def test_grant_exact_owner_and_db_lifetime(database, damage):
    repo, original = grant(database)
    with database.transaction() as tx:
        if damage == "revoked":
            repo.revoke_grant(
                tx,
                owner_id=original.owner_id,
                grant_id=original.grant_id,
                revoked_at=original.issued_at + 1,
            )
        elif damage == "expired":
            tx.execute(
                "UPDATE user_offline_grant SET expires_at="
                "floor(extract(epoch FROM clock_timestamp())*1000)::bigint WHERE id=%s",
                (original.grant_id,),
            )
        elif damage == "future":
            tx.execute(
                "UPDATE user_offline_grant SET issued_at=expires_at-1 WHERE id=%s",
                (original.grant_id,),
            )
        args = dict(owner_id=original.owner_id, grant_id=original.grant_id)
        if damage == "missing":
            args["grant_id"] = str(uuid.uuid4())
        if damage == "foreign":
            args["owner_id"] = "foreign"
        with pytest.raises(RepositoryConflictError):
            repo.assert_current_grant(tx, **args)
        assert tx.fetch_one("SELECT 1 AS usable")["usable"] == 1


def schema_of(database):
    with database.transaction() as tx:
        return tx.fetch_one("SELECT current_schema() AS s")["s"]


def watch_sql(tx, entered):
    execute, fetch_one = tx.execute, tx.fetch_one

    def observed_execute(sql, params=()):
        if "FROM operation_record" in sql and "FOR UPDATE" in sql:
            entered.set()
        return execute(sql, params)

    def observed_fetch_one(sql, params=()):
        if "FROM user_offline_grant" in sql and "FOR UPDATE" in sql:
            entered.set()
        return fetch_one(sql, params)

    tx.execute, tx.fetch_one = observed_execute, observed_fetch_one
    return tx


@pytest.mark.parametrize("resource", ["operation", "slot", "grant"])
@pytest.mark.postgresql_lock_catalog
def test_expiry_during_real_lock_wait_uses_clock_not_transaction_start(database, work, resource):
    fence = running(database, work)
    repo, original = grant(database)
    schema = schema_of(database)
    entered = Event()
    started = Event()
    proceed = Event()

    def reader():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.fetch_one("SELECT CURRENT_TIMESTAMP AS old_time")
            started.set()
            assert proceed.wait(5)
            with pytest.raises(RepositoryConflictError):
                if resource == "grant":
                    repo.assert_current_grant(
                        watch_sql(tx, entered),
                        owner_id=original.owner_id,
                        grant_id=original.grant_id,
                    )
                else:
                    work.assert_current_execution_lease(watch_sql(tx, entered), fence)
            assert tx.fetch_one("SELECT 1 AS usable")["usable"] == 1

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(reader)
        assert started.wait(5)
        with database.transaction() as tx:
            if resource == "grant":
                tx.execute(
                    "UPDATE user_offline_grant SET expires_at="
                    "floor(extract(epoch FROM clock_timestamp())*1000)::bigint+150 "
                    "WHERE id=%s",
                    (original.grant_id,),
                )
            else:
                if resource == "operation":
                    tx.fetch_one(
                        "SELECT operation_id FROM operation_record "
                        "WHERE operation_id=%s FOR UPDATE",
                        (str(fence.operation_id),),
                    )
                tx.execute(
                    "UPDATE operation_admission_slot SET lease_expires_at="
                    "clock_timestamp()+interval '150 milliseconds' WHERE operation_id=%s",
                    (str(fence.operation_id),),
                )
            proceed.set()
            assert entered.wait(5)
            time.sleep(0.25)
            assert not future.done()
        future.result(timeout=5)


@pytest.mark.parametrize("lock", ["owner", "state"])
@pytest.mark.postgresql_lock_catalog
def test_grant_late_upstream_contention_refuses_without_aborting_caller(database, work, lock):
    fence = running(database, work)
    repo, original = grant(database)
    schema = schema_of(database)
    with database.transaction() as tx:
        tx.execute(
            "INSERT INTO astralplane_blob_owner_state(owner_id,state) VALUES(%s,'active')",
            (original.owner_id,),
        )
    with database.transaction() as holder:
        if lock == "owner":
            holder.fetch_one(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (original.owner_id,)
            )
        else:
            holder.fetch_one(
                "SELECT state FROM astralplane_blob_owner_state WHERE owner_id=%s FOR UPDATE",
                (original.owner_id,),
            )
        with independent_database(schema) as db, db.transaction() as tx:
            work.assert_current_execution_lease(tx, fence)
            with pytest.raises(RepositoryConflictError, match="offline grant is unavailable"):
                repo.assert_current_grant(
                    tx, owner_id=original.owner_id, grant_id=original.grant_id
                )
            assert tx.fetch_one("SELECT 1 AS usable")["usable"] == 1


def test_execution_captures_original_fence_before_owner_wait(database, work):
    fence = running(database, work)
    other = running(database, work)
    original = replace(fence)
    entered = Event()
    schema = schema_of(database)

    def reader():
        with independent_database(schema) as db, db.transaction() as tx:
            return work.assert_current_execution_lease(watch_sql(tx, entered), fence)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with database.transaction() as tx:
            tx.fetch_one(
                "SELECT operation_id FROM operation_record WHERE operation_id=%s FOR UPDATE",
                (str(fence.operation_id),),
            )
            future = executor.submit(reader)
            assert entered.wait(5)
            for name in ("operation_id", "execution_generation", "execution_lease_token"):
                object.__setattr__(fence, name, getattr(other, name))
        observed = future.result(timeout=5)
    assert observed.operation_id == original.operation_id


def test_grant_observation_does_not_adopt_replacement_and_outer_rollback(database):
    repo, original = grant(database)
    with pytest.raises(RuntimeError), database.transaction() as tx:
        assert (
            repo.assert_current_grant(tx, owner_id=original.owner_id, grant_id=original.grant_id)
            == original
        )
        repo.revoke_grant(
            tx,
            owner_id=original.owner_id,
            grant_id=original.grant_id,
            revoked_at=original.issued_at + 1,
        )
        raise RuntimeError("caller audit failed")
    with database.transaction() as tx:
        assert (
            repo.assert_current_grant(tx, owner_id=original.owner_id, grant_id=original.grant_id)
            == original
        )
        now = int(datetime.now(UTC).timestamp() * 1000)
        changed = repo.replace_refresh_token_if_current(
            tx,
            owner_id=original.owner_id,
            grant_id=original.grant_id,
            expected_encrypted_refresh_token=original.encrypted_refresh_token,
            encrypted_refresh_token=b"synthetic-successor",
            as_of=now,
        )
        current = repo.assert_current_grant(
            tx, owner_id=original.owner_id, grant_id=original.grant_id
        )
        assert current == changed and current != original


@pytest.mark.parametrize("mutation", ["cancel", "reselect", "terminal", "recover", "revoke"])
@pytest.mark.postgresql_lock_catalog
def test_locked_mutation_is_seen_by_new_read_before_delivery(database, work, mutation):
    fence = running(database, work)
    repo, original = grant(database)
    entered = Event()
    schema = schema_of(database)

    def reader():
        with independent_database(schema) as db, db.transaction() as tx:
            with pytest.raises(RepositoryConflictError):
                if mutation == "revoke":
                    repo.assert_current_grant(
                        watch_sql(tx, entered),
                        owner_id=original.owner_id,
                        grant_id=original.grant_id,
                    )
                else:
                    work.assert_current_execution_lease(watch_sql(tx, entered), fence)
            assert tx.fetch_one("SELECT 1 AS usable")["usable"] == 1

    with ThreadPoolExecutor(max_workers=1) as executor:
        with database.transaction() as tx:
            if mutation == "cancel":
                work.cancel(
                    tx,
                    OperationOwner(OwnerScope.USER, "clock-owner", None),
                    fence.operation_id,
                    "owner_cancelled",
                    now=None,
                    retention=timedelta(days=1),
                )
            elif mutation == "reselect":
                work.reselect_execution(tx, fence, now=None, slot_lease=timedelta(minutes=1))
            elif mutation == "terminal":
                work.terminalize(
                    tx,
                    fence,
                    state=OperationState.COMPLETED,
                    terminal_code=None,
                    safe_summary=None,
                    retry_after_ms=None,
                    now=None,
                    retention=timedelta(days=1),
                )
            elif mutation == "recover":
                tx.execute(
                    "UPDATE operation_admission_slot SET lease_expires_at="
                    "clock_timestamp()-interval '1 second' WHERE operation_id=%s",
                    (str(fence.operation_id),),
                )
                assert (
                    len(work.expire_execution_leases(tx, now=None, retention=timedelta(days=1)))
                    == 1
                )
            else:
                repo.revoke_grant(
                    tx,
                    owner_id=original.owner_id,
                    grant_id=original.grant_id,
                    revoked_at=original.issued_at + 1,
                )
            future = executor.submit(reader)
            assert entered.wait(5)
            assert not future.done()
        future.result(timeout=5)


@pytest.mark.parametrize("damaged", ["slot", "operation_clock", "grant_clock"])
def test_invalid_database_observation_cannot_be_authority(database, work, damaged):
    from astralplane.repositories import RepositoryDataError

    fence = running(database, work)
    repo, original = grant(database)

    class FaultedRead:
        def __init__(self, tx):
            self.tx = tx

        def __getattr__(self, name):
            return getattr(self.tx, name)

        def execute(self, sql, params=()):
            result = self.tx.execute(sql, params)
            if damaged == "slot" and "ORDER BY class_name, slot_number FOR UPDATE" in sql:
                rows = [dict(row) for row in result.returned_records]
                rows[0]["lease_expires_at"] = "invalid-database-timestamp"
                return replace(result, returned_records=tuple(rows))
            if damaged == "operation_clock" and sql == "SELECT clock_timestamp() AS current_time":
                return replace(result, returned_records=())
            return result

        def fetch_one(self, sql, params=()):
            result = self.tx.fetch_one(sql, params)
            return None if damaged == "grant_clock" and "AS now_ms" in sql else result

    with database.transaction() as tx:
        error = (
            StaleWorkExecutionFenceError
            if damaged == "slot"
            else (RepositoryDataError if damaged == "grant_clock" else WorkAdmissionIntegrityError)
        )
        with pytest.raises(error):
            if damaged == "grant_clock":
                repo.assert_current_grant(
                    FaultedRead(tx), owner_id=original.owner_id, grant_id=original.grant_id
                )
            else:
                work.assert_current_execution_lease(FaultedRead(tx), fence)
        assert tx.fetch_one("SELECT 1 AS usable")["usable"] == 1
