"""Real-PostgreSQL behavior for the computer-use repository: owner denial, one-way intent
transitions under concurrency, revision fences, retention cleanup, and rollback safety
against the migrated schema template.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest
from test_assignments_postgres import database as database

from astralplane.database.pool import ConnectionPool
from astralplane.database.transaction import PlaneDatabase
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.computer_use import (
    ApprovalFence,
    ComputerSession,
    ComputerTaskGrant,
    ComputerUseRepository,
    HostEffectIntent,
    HostEffectIntentState,
)

OWNER = "computer-pg-owner"
OTHER = "computer-pg-other"
ARGUMENT = hashlib.sha256(b"synthetic-argument-bytes").hexdigest()
BINDING = hashlib.sha256(b"synthetic-binding-bytes").hexdigest()


class Pool:
    def __init__(self, connection):
        self.connection = connection

    def getconn(self):
        return self.connection

    def putconn(self, connection, *, close=False):
        assert connection is self.connection

    def closeall(self):
        pass


@contextmanager
def independent_database(schema):
    import psycopg2

    if os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"].startswith("sqlite:"):
        from test_assignments_postgres import independent_database as sqlite_database

        with sqlite_database(schema) as database:
            yield database
        return
    connection = psycopg2.connect(os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"])
    with connection.cursor() as cursor:
        cursor.execute(f'SET search_path TO "{schema}",pg_catalog')
    connection.commit()
    pool = ConnectionPool(Pool(connection))
    try:
        yield PlaneDatabase(pool)
    finally:
        pool.close()
        connection.close()


def parallel_transactions(database, operations):
    with database.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    barrier = Barrier(len(operations))

    def run(operation):
        with independent_database(schema) as db:
            barrier.wait(timeout=10)
            try:
                with db.transaction() as tx:
                    return operation(tx)
            except RepositoryConflictError as error:
                return error

    with ThreadPoolExecutor(max_workers=len(operations)) as workers:
        return tuple(workers.map(run, operations))


@pytest.fixture
def tx(database):
    with database.transaction() as transaction:
        transaction.execute("DELETE FROM computer_approval_fence")
        transaction.execute("DELETE FROM computer_host_effect_intent")
        transaction.execute("DELETE FROM computer_task_grant")
        transaction.execute("DELETE FROM computer_session")
        yield transaction


@pytest.fixture
def repo():
    return ComputerUseRepository()


def uid() -> str:
    return str(uuid.uuid4())


def new_session(**changes) -> ComputerSession:
    values = dict(
        session_id=uid(),
        owner_id=OWNER,
        host_id="mac-studio-pg",
        host_incarnation="incarnation-3",
        state="active",
        revision=1,
        created_at=100,
        updated_at=100,
    )
    values.update(changes)
    return ComputerSession(**values)


def new_grant(**changes) -> ComputerTaskGrant:
    values = dict(
        grant_id=uid(),
        owner_id=OWNER,
        binding_digest=BINDING,
        expires_at=100_000,
        hard_cap=2,
        use_count=0,
        revision=1,
        created_at=100,
    )
    values.update(changes)
    return ComputerTaskGrant(**values)


def new_intent(**changes) -> HostEffectIntent:
    values = dict(
        operation_id=uid(),
        owner_id=OWNER,
        verb="screenshot",
        argument_digest=ARGUMENT,
        state=HostEffectIntentState.PENDING,
        created_at=100,
        updated_at=100,
    )
    values.update(changes)
    return HostEffectIntent(**values)


def new_fence(**changes) -> ApprovalFence:
    values = dict(
        fence_id=uid(),
        owner_id=OWNER,
        continuation_digest=ARGUMENT,
        state="open",
        created_at=100,
        updated_at=100,
    )
    values.update(changes)
    return ApprovalFence(**values)


def test_session_lifecycle_pause_resume_end_roundtrip(tx, repo):
    session = repo.open_session(tx, new_session())
    with pytest.raises(RepositoryConflictError, match="conflicting semantics"):
        repo.open_session(tx, new_session(session_id=session.session_id, host_id="different"))
    paused = repo.pause_session(
        tx,
        owner_id=OWNER,
        session_id=session.session_id,
        expected_revision=1,
        pause_reason="user_requested",
        updated_at=110,
    )
    assert (paused.state, paused.pause_reason, paused.revision) == ("paused", "user_requested", 2)
    resumed = repo.resume_session(
        tx,
        owner_id=OWNER,
        session_id=session.session_id,
        expected_revision=2,
        updated_at=120,
    )
    assert (resumed.state, resumed.pause_reason, resumed.revision) == ("active", None, 3)
    assert repo.get_session(tx, owner_id=OWNER, session_id=session.session_id) == resumed
    assert repo.list_open_sessions(tx, owner_id=OWNER, limit=10) == (resumed,)
    ended = repo.end_session(
        tx,
        owner_id=OWNER,
        session_id=session.session_id,
        expected_revision=3,
        ended_at=130,
    )
    assert (ended.state, ended.ended_at) == ("ended", 130)
    assert repo.list_open_sessions(tx, owner_id=OWNER, limit=10) == ()
    with pytest.raises(RepositoryConflictError, match="revision fence"):
        repo.pause_session(
            tx,
            owner_id=OWNER,
            session_id=session.session_id,
            expected_revision=4,
            pause_reason="user_requested",
            updated_at=140,
        )


def test_session_pause_requires_current_revision_and_live_row(tx, repo):
    session = repo.open_session(tx, new_session())
    with pytest.raises(RepositoryConflictError, match="revision fence"):
        repo.pause_session(
            tx,
            owner_id=OWNER,
            session_id=session.session_id,
            expected_revision=2,
            pause_reason="user_requested",
            updated_at=110,
        )
    repo.pause_session(
        tx,
        owner_id=OWNER,
        session_id=session.session_id,
        expected_revision=1,
        pause_reason="user_requested",
        updated_at=110,
    )
    with pytest.raises(RepositoryConflictError, match="revision fence"):
        repo.resume_session(
            tx,
            owner_id=OWNER,
            session_id=session.session_id,
            expected_revision=1,
            updated_at=120,
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.resume_session(
            tx,
            owner_id=OWNER,
            session_id=uid(),
            expected_revision=1,
            updated_at=120,
        )


def test_session_denial_across_owners(tx, repo):
    session = repo.open_session(tx, new_session())
    assert repo.get_session(tx, owner_id=OTHER, session_id=session.session_id) is None
    with pytest.raises(RepositoryNotFoundError):
        repo.pause_session(
            tx,
            owner_id=OTHER,
            session_id=session.session_id,
            expected_revision=1,
            pause_reason="user_requested",
            updated_at=110,
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.end_session(
            tx,
            owner_id=OTHER,
            session_id=session.session_id,
            expected_revision=1,
            ended_at=110,
        )
    assert repo.list_open_sessions(tx, owner_id=OTHER, limit=10) == ()
    assert repo.get_session(tx, owner_id=OWNER, session_id=session.session_id) is not None


def test_task_grant_consume_up_to_hard_cap_then_fail_closed(tx, repo):
    grant = repo.create_task_grant(tx, new_grant())
    first = repo.consume_task_grant(
        tx, owner_id=OWNER, grant_id=grant.grant_id, expected_revision=1, now=200
    )
    assert (first.use_count, first.revision) == (1, 2)
    second = repo.consume_task_grant(
        tx, owner_id=OWNER, grant_id=grant.grant_id, expected_revision=2, now=210
    )
    assert (second.use_count, second.revision) == (2, 3)
    with pytest.raises(RepositoryConflictError):
        repo.consume_task_grant(
            tx, owner_id=OWNER, grant_id=grant.grant_id, expected_revision=3, now=220
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.consume_task_grant(
            tx, owner_id=OTHER, grant_id=grant.grant_id, expected_revision=3, now=220
        )
    assert repo.get_task_grant(tx, owner_id=OTHER, grant_id=grant.grant_id) is None


def test_task_grant_expiry_revocation_and_listing(tx, repo):
    grant = repo.create_task_grant(tx, new_grant(expires_at=150))
    assert repo.list_active_task_grants(tx, owner_id=OWNER, now=100) == (grant,)
    assert repo.list_active_task_grants(tx, owner_id=OWNER, now=150) == ()
    with pytest.raises(RepositoryConflictError):
        repo.consume_task_grant(
            tx, owner_id=OWNER, grant_id=grant.grant_id, expected_revision=1, now=150
        )
    fresh = repo.create_task_grant(tx, new_grant())
    revoked = repo.revoke_task_grant(
        tx, owner_id=OWNER, grant_id=fresh.grant_id, expected_revision=1, revoked_at=300
    )
    assert revoked.revoked_at == 300
    assert repo.list_active_task_grants(tx, owner_id=OWNER, now=100) == (grant,)
    assert repo.list_active_task_grants(tx, owner_id=OWNER, now=301) == ()
    with pytest.raises(RepositoryConflictError):
        repo.revoke_task_grant(
            tx, owner_id=OWNER, grant_id=fresh.grant_id, expected_revision=2, revoked_at=310
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.revoke_task_grant(
            tx, owner_id=OTHER, grant_id=fresh.grant_id, expected_revision=2, revoked_at=310
        )


def test_task_grant_concurrent_consumption_is_fenced(database, repo):
    with database.transaction() as tx:
        tx.execute("DELETE FROM computer_task_grant")
        grant = repo.create_task_grant(tx, new_grant(hard_cap=8))
    outcomes = parallel_transactions(
        database,
        tuple(
            lambda tx, revision=1: repo.consume_task_grant(
                tx, owner_id=OWNER, grant_id=grant.grant_id, expected_revision=revision, now=400
            )
            for _ in range(4)
        ),
    )
    successes = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, RepositoryConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 3
    with database.transaction() as tx:
        final = repo.get_task_grant(tx, owner_id=OWNER, grant_id=grant.grant_id)
    assert (final.use_count, final.revision) == (1, 2)


def test_host_effect_intent_one_way_transitions(tx, repo):
    intent = repo.record_host_effect_intent(tx, new_intent())
    assert repo.list_open_host_effect_intents(tx, owner_id=OWNER, limit=10) == (intent,)
    replayed = repo.record_host_effect_intent(tx, new_intent(operation_id=intent.operation_id))
    assert replayed == intent
    with pytest.raises(RepositoryConflictError, match="conflicting semantics"):
        repo.record_host_effect_intent(
            tx, new_intent(operation_id=intent.operation_id, verb="key_tap")
        )
    committed = repo.commit_host_effect_intent(
        tx, owner_id=OWNER, operation_id=intent.operation_id, resolved_at=200
    )
    assert committed.state is HostEffectIntentState.COMMITTED
    with pytest.raises(RepositoryConflictError, match="cannot become unknown"):
        repo.mark_host_effect_intent_unknown(
            tx, owner_id=OWNER, operation_id=intent.operation_id, resolved_at=210
        )
    acknowledged = repo.acknowledge_host_effect_intent(
        tx, owner_id=OWNER, operation_id=intent.operation_id, acknowledged_at=300
    )
    assert acknowledged.state is HostEffectIntentState.ACKNOWLEDGED
    assert repo.list_open_host_effect_intents(tx, owner_id=OWNER, limit=10) == ()
    assert (
        repo.acknowledge_host_effect_intent(
            tx, owner_id=OWNER, operation_id=intent.operation_id, acknowledged_at=310
        )
        == acknowledged
    )


def test_unknown_intent_refuses_commit_and_acknowledges_once(tx, repo):
    intent = repo.record_host_effect_intent(tx, new_intent())
    unknown = repo.mark_host_effect_intent_unknown(
        tx, owner_id=OWNER, operation_id=intent.operation_id, resolved_at=200
    )
    assert unknown.state is HostEffectIntentState.UNKNOWN
    with pytest.raises(RepositoryConflictError, match="cannot commit"):
        repo.commit_host_effect_intent(
            tx, owner_id=OWNER, operation_id=intent.operation_id, resolved_at=210
        )
    acknowledged = repo.acknowledge_host_effect_intent(
        tx, owner_id=OWNER, operation_id=intent.operation_id, acknowledged_at=300
    )
    assert acknowledged.state is HostEffectIntentState.ACKNOWLEDGED
    with pytest.raises(RepositoryConflictError):
        repo.mark_host_effect_intent_unknown(
            tx, owner_id=OWNER, operation_id=intent.operation_id, resolved_at=310
        )


def test_pending_intent_cannot_be_acknowledged(tx, repo):
    intent = repo.record_host_effect_intent(tx, new_intent())
    with pytest.raises(RepositoryConflictError, match="must resolve"):
        repo.acknowledge_host_effect_intent(
            tx, owner_id=OWNER, operation_id=intent.operation_id, acknowledged_at=300
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.acknowledge_host_effect_intent(
            tx, owner_id=OTHER, operation_id=intent.operation_id, acknowledged_at=300
        )


def test_bulk_unknown_marks_only_pending_and_returns_records(tx, repo):
    first = repo.record_host_effect_intent(tx, new_intent())
    second = repo.record_host_effect_intent(
        tx, new_intent(operation_id=uid(), verb="key_tap", created_at=101, updated_at=101)
    )
    committed = repo.record_host_effect_intent(
        tx, new_intent(operation_id=uid(), verb="scroll", created_at=102, updated_at=102)
    )
    repo.commit_host_effect_intent(
        tx, owner_id=OWNER, operation_id=committed.operation_id, resolved_at=150
    )
    marked = repo.mark_host_effect_intents_unknown(tx, owner_id=OWNER, resolved_at=200, limit=10)
    assert {intent.operation_id for intent in marked} == {first.operation_id, second.operation_id}
    assert all(intent.state is HostEffectIntentState.UNKNOWN for intent in marked)
    assert repo.list_open_host_effect_intents(tx, owner_id=OWNER, limit=10) == ()
    assert repo.mark_host_effect_intents_unknown(tx, owner_id=OWNER, resolved_at=210) == ()
    other = repo.record_host_effect_intent(tx, new_intent(owner_id=OTHER))
    marked_other = repo.mark_host_effect_intents_unknown(tx, owner_id=OTHER, resolved_at=220)
    assert [intent.operation_id for intent in marked_other] == [other.operation_id]
    assert all(intent.owner_id == OTHER for intent in marked_other)


def test_intent_denial_across_owners(tx, repo):
    intent = repo.record_host_effect_intent(tx, new_intent())
    assert repo.get_host_effect_intent(tx, owner_id=OTHER, operation_id=intent.operation_id) is None
    with pytest.raises(RepositoryNotFoundError):
        repo.commit_host_effect_intent(
            tx, owner_id=OTHER, operation_id=intent.operation_id, resolved_at=200
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.acknowledge_host_effect_intent(
            tx, owner_id=OTHER, operation_id=intent.operation_id, acknowledged_at=200
        )
    assert repo.list_open_host_effect_intents(tx, owner_id=OTHER, limit=10) == ()


def test_intent_write_failure_rolls_back_to_a_clean_state(tx, repo):
    intent = repo.record_host_effect_intent(tx, new_intent())
    with pytest.raises(RepositoryConflictError):
        repo.record_host_effect_intent(tx, new_intent(operation_id=intent.operation_id, verb="x"))
    tx.execute("SELECT 1")
    assert repo.get_host_effect_intent(
        tx, owner_id=OWNER, operation_id=intent.operation_id
    ) is not None


def test_intent_transactions_do_not_commit_inside_the_repository(database, repo):
    intent = new_intent()
    with pytest.raises(RuntimeError, match="caller abort"), database.transaction() as transaction:
        repo.record_host_effect_intent(transaction, intent)
        raise RuntimeError("caller abort")
    with database.transaction() as fresh:
        assert repo.get_host_effect_intent(
            fresh, owner_id=OWNER, operation_id=intent.operation_id
        ) is None


def test_approval_fence_single_use_consumption(tx, repo):
    fence = repo.create_approval_fence(tx, new_fence())
    assert repo.list_open_approval_fences(tx, owner_id=OWNER, limit=10) == (fence,)
    consumed = repo.consume_approval_fence(
        tx, owner_id=OWNER, fence_id=fence.fence_id, closed_at=200
    )
    assert (consumed.state, consumed.closed_at) == ("consumed", 200)
    with pytest.raises(RepositoryConflictError, match="already closed"):
        repo.release_approval_fence(tx, owner_id=OWNER, fence_id=fence.fence_id, closed_at=210)
    assert repo.list_open_approval_fences(tx, owner_id=OWNER, limit=10) == ()
    second = repo.create_approval_fence(tx, new_fence())
    released = repo.release_approval_fence(
        tx, owner_id=OWNER, fence_id=second.fence_id, closed_at=220
    )
    assert (released.state, released.closed_at) == ("released", 220)
    assert repo.get_approval_fence(tx, owner_id=OTHER, fence_id=fence.fence_id) is None
    with pytest.raises(RepositoryNotFoundError):
        repo.consume_approval_fence(tx, owner_id=OTHER, fence_id=fence.fence_id, closed_at=230)


def test_ended_session_and_expired_grant_cleanup_bounds(tx, repo):
    kept_active = repo.open_session(tx, new_session())
    ended_old = repo.open_session(tx, new_session(state="ended", ended_at=150, updated_at=150))
    ended_new = repo.open_session(tx, new_session(state="ended", ended_at=500, updated_at=500))
    deleted = repo.delete_ended_sessions_before(tx, owner_id=OWNER, ended_before=200, limit=10)
    assert deleted == (ended_old.session_id,)
    assert repo.get_session(tx, owner_id=OWNER, session_id=ended_new.session_id) is not None
    assert repo.get_session(tx, owner_id=OWNER, session_id=kept_active.session_id) is not None
    bounded = repo.delete_ended_sessions_before(tx, owner_id=OWNER, ended_before=600, limit=1)
    assert bounded == (ended_new.session_id,)
    grant_old = repo.create_task_grant(tx, new_grant(expires_at=150))
    grant_revoked = repo.create_task_grant(tx, new_grant(revoked_at=120))
    grant_live = repo.create_task_grant(tx, new_grant(expires_at=90_000))
    removed = repo.delete_expired_task_grants_before(
        tx, owner_id=OWNER, expired_before=300, limit=10
    )
    assert set(removed) == {grant_old.grant_id, grant_revoked.grant_id}
    assert repo.get_task_grant(tx, owner_id=OWNER, grant_id=grant_live.grant_id) is not None


def test_cleanup_denial_across_owners(tx, repo):
    session = repo.open_session(tx, new_session(state="ended", ended_at=150, updated_at=150))
    grant = repo.create_task_grant(tx, new_grant(expires_at=150))
    assert repo.delete_ended_sessions_before(tx, owner_id=OTHER, ended_before=500) == ()
    assert repo.delete_expired_task_grants_before(tx, owner_id=OTHER, expired_before=500) == ()
    assert repo.get_session(tx, owner_id=OWNER, session_id=session.session_id) is not None
    assert repo.get_task_grant(tx, owner_id=OWNER, grant_id=grant.grant_id) is not None


def test_limit_validation_rejects_out_of_range_values(tx, repo):
    with pytest.raises(RepositoryValidationError):
        repo.list_open_sessions(tx, owner_id=OWNER, limit=0)
    with pytest.raises(RepositoryValidationError):
        repo.list_open_host_effect_intents(tx, owner_id=OWNER, limit=1001)
    with pytest.raises(RepositoryValidationError):
        repo.delete_ended_sessions_before(tx, owner_id=OWNER, ended_before=10, limit=10**9)
    with pytest.raises(RepositoryValidationError):
        repo.mark_host_effect_intents_unknown(tx, owner_id=OWNER, resolved_at=10, limit="x")


def test_public_computer_use_contract_behaviors():
    from test_assignments_postgres import standalone_database

    fixture = standalone_database()
    database = next(fixture)
    repository = ComputerUseRepository()
    try:
        with database.transaction() as tx:
            tx.execute("DELETE FROM computer_approval_fence")
            tx.execute("DELETE FROM computer_host_effect_intent")
            tx.execute("DELETE FROM computer_task_grant")
            tx.execute("DELETE FROM computer_session")
            session = repository.open_session(tx, new_session())
            grant = repository.create_task_grant(tx, new_grant(hard_cap=2))
            intent = repository.record_host_effect_intent(tx, new_intent())
            fence = repository.create_approval_fence(tx, new_fence())
            assert repository.get_session(
                tx, owner_id=OTHER, session_id=session.session_id
            ) is None
            assert repository.get_task_grant(tx, owner_id=OTHER, grant_id=grant.grant_id) is None
            assert (
                repository.get_host_effect_intent(
                    tx, owner_id=OTHER, operation_id=intent.operation_id
                )
                is None
            )
            assert repository.get_approval_fence(
                tx, owner_id=OTHER, fence_id=fence.fence_id
            ) is None
            replayed = repository.record_host_effect_intent(
                tx, new_intent(operation_id=intent.operation_id)
            )
            assert replayed == intent
            with pytest.raises(RepositoryConflictError):
                repository.record_host_effect_intent(
                    tx, new_intent(operation_id=intent.operation_id, verb="key_tap")
                )
            consumed = repository.consume_task_grant(
                tx, owner_id=OWNER, grant_id=grant.grant_id, expected_revision=1, now=200
            )
            assert consumed.use_count == 1
            with pytest.raises(RepositoryConflictError):
                repository.consume_task_grant(
                    tx, owner_id=OWNER, grant_id=grant.grant_id, expected_revision=1, now=210
                )
        outcomes = parallel_transactions(
            database,
            (
                lambda tx: repository.consume_task_grant(
                    tx,
                    owner_id=OWNER,
                    grant_id=grant.grant_id,
                    expected_revision=2,
                    now=300,
                ),
                lambda tx: repository.pause_session(
                    tx,
                    owner_id=OWNER,
                    session_id=session.session_id,
                    expected_revision=1,
                    pause_reason="user_requested",
                    updated_at=300,
                ),
            ),
        )
        assert sum(not isinstance(result, BaseException) for result in outcomes) == 2
        with database.transaction() as tx:
            raced = repository.create_task_grant(tx, new_grant(hard_cap=8))
        outcomes = parallel_transactions(
            database,
            (
                lambda tx: repository.consume_task_grant(
                    tx,
                    owner_id=OWNER,
                    grant_id=raced.grant_id,
                    expected_revision=1,
                    now=310,
                ),
                lambda tx: repository.consume_task_grant(
                    tx,
                    owner_id=OWNER,
                    grant_id=raced.grant_id,
                    expected_revision=1,
                    now=310,
                ),
            ),
        )
        assert sum(isinstance(result, RepositoryConflictError) for result in outcomes) == 1
        with database.transaction() as tx:
            final = repository.get_task_grant(tx, owner_id=OWNER, grant_id=raced.grant_id)
        assert (final.use_count, final.revision) == (1, 2)
        with database.transaction() as tx:
            committed = repository.commit_host_effect_intent(
                tx, owner_id=OWNER, operation_id=intent.operation_id, resolved_at=400
            )
            assert committed.state is HostEffectIntentState.COMMITTED
            with pytest.raises(RepositoryConflictError, match="cannot become unknown"):
                repository.mark_host_effect_intent_unknown(
                    tx, owner_id=OWNER, operation_id=intent.operation_id, resolved_at=410
                )
            closed = repository.consume_approval_fence(
                tx, owner_id=OWNER, fence_id=fence.fence_id, closed_at=420
            )
            assert closed.state == "consumed"
            with pytest.raises(RepositoryConflictError, match="already closed"):
                repository.release_approval_fence(
                    tx, owner_id=OWNER, fence_id=fence.fence_id, closed_at=430
                )
            with pytest.raises(RepositoryValidationError):
                repository.pause_session(
                    tx,
                    owner_id=OWNER,
                    session_id=session.session_id,
                    expected_revision=0,
                    pause_reason="user_requested",
                    updated_at=440,
                )
            with pytest.raises(RepositoryNotFoundError):
                repository.acknowledge_host_effect_intent(
                    tx, owner_id=OWNER, operation_id=uid(), acknowledged_at=440
                )
    finally:
        fixture.close()
