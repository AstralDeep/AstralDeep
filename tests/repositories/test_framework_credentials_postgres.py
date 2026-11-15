"""Real-PostgreSQL tests proving astralplane.repositories.framework_credentials.issue()
re-checks validity and computes expiry inside the same owner lock create_operation()
uses, closing a revoke-during-mint race.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import timedelta
from threading import Event

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import (
    independent_database,
    session_observation,
    standalone_database,
    uid,
)

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.framework_credentials import (
    FrameworkCredentialRecord,
    FrameworkCredentialRepository,
)
from astralplane.repositories.history import (
    FrameworkCredentialFence,
    FrameworkCredentialObservation,
    SessionRecord,
    SessionRepository,
)

TOKEN_HASH = "b" * 64


def test_database_capture_returns_a_frozen_current_observation(database):
    repository = FrameworkCredentialRepository()
    with database.transaction() as tx:
        owner = uid()
        record, token_hash = issue(tx, owner, seed_session(tx, owner))
        observation = repository.capture_execution_observation(
            tx, owner_id=owner, credential_id=record.credential_id, token_hash=token_hash)
        state = repository.assert_current_execution(tx, observation=observation)
    assert isinstance(observation, FrameworkCredentialObservation)
    assert observation.credential.owner_id == owner
    assert observation.credential.scopes == record.scopes
    assert observation.valid_until - observation.started_at == timedelta(seconds=15)
    assert observation.started_at <= state.observed_at < observation.valid_until
    assert token_hash not in repr(observation)
    with pytest.raises(FrozenInstanceError):
        observation.started_at = state.observed_at


@pytest.mark.parametrize("change", ["owner", "credential", "hash", "revoked", "expired"])
def test_database_capture_refuses_noncurrent_authority(database, change):
    repository = FrameworkCredentialRepository()
    with database.transaction() as tx:
        owner = uid()
        record, token_hash = issue(tx, owner, seed_session(tx, owner))
        if change == "revoked":
            repository.revoke(tx, owner_id=owner, credential_id=record.credential_id)
        elif change == "expired":
            tx.execute(
                "UPDATE framework_credential SET created_at=clock_timestamp()-interval '2 hours', "
                "expires_at=clock_timestamp()-interval '1 hour' WHERE id=%s",
                (record.credential_id,),
            )
        with pytest.raises(RepositoryConflictError):
            repository.capture_execution_observation(
                tx, owner_id=uid() if change == "owner" else owner,
                credential_id=uid() if change == "credential" else record.credential_id,
                token_hash=fresh_token_hash() if change == "hash" else token_hash)


@pytest.mark.parametrize("requested, expected", [
    (("operations.read",), ("operations.read",)),
    (("operations.read", "artifacts.read"), ("operations.read",)),
])
def test_database_capture_preserves_attenuated_scopes(database, requested, expected):
    with database.transaction() as tx:
        owner = uid()
        record, token_hash = issue(tx, owner, seed_session(tx, owner))
        observation = FrameworkCredentialRepository().capture_execution_observation(
            tx, owner_id=owner, credential_id=record.credential_id,
            token_hash=token_hash, scopes=requested)
    assert observation.credential.scopes == expected


@pytest.mark.parametrize("scopes", [(), ("sudo",), ("artifacts.read",)])
def test_database_capture_never_returns_an_empty_or_unknown_scope_grant(database, scopes):
    with database.transaction() as tx:
        owner = uid()
        record, token_hash = issue(tx, owner, seed_session(tx, owner))
        with pytest.raises((RepositoryConflictError, RepositoryValidationError)):
            FrameworkCredentialRepository().capture_execution_observation(
                tx, owner_id=owner, credential_id=record.credential_id,
                token_hash=token_hash, scopes=scopes)


@pytest.mark.parametrize("offset", [timedelta(seconds=-16), timedelta(seconds=1)])
def test_database_capture_cannot_refresh_an_overlong_or_backwards_clock_window(database, offset):
    with database.transaction() as tx:
        owner = uid()
        record, token_hash = issue(tx, owner, seed_session(tx, owner))

        class CaptureClock:
            sampled = False

            def __getattr__(self, name):
                return getattr(tx, name)

            def fetch_one(self, sql, *args, **kwargs):
                value = tx.fetch_one(sql, *args, **kwargs)
                if sql == "SELECT clock_timestamp() AS now" and not self.sampled:
                    self.sampled = True
                    return {"now": value["now"] + offset}
                return value

        with pytest.raises(RepositoryConflictError):
            FrameworkCredentialRepository().capture_execution_observation(
                CaptureClock(), owner_id=owner, credential_id=record.credential_id,
                token_hash=token_hash)


def test_database_capture_does_not_authorize_a_later_revoked_key(database):
    repository = FrameworkCredentialRepository()
    with database.transaction() as tx:
        owner = uid()
        record, token_hash = issue(tx, owner, seed_session(tx, owner))
        observation = repository.capture_execution_observation(
            tx, owner_id=owner, credential_id=record.credential_id, token_hash=token_hash)
    with database.transaction() as tx:
        repository.revoke(tx, owner_id=owner, credential_id=record.credential_id)
    with database.transaction() as tx, pytest.raises(RepositoryConflictError):
        repository.assert_current_execution(tx, observation=observation)


def fresh_token_hash():
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def seed_session(transaction, owner_id, *, session_id=None):
    sessions = SessionRepository()
    now = int(transaction.fetch_one("SELECT clock_timestamp() AS now")["now"].timestamp())
    record = sessions.put(
        transaction,
        SessionRecord(
            session_id or uid(),
            owner_id,
            "synthetic-encrypted-access",
            "synthetic-encrypted-refresh",
            now,
            now + 3600,
            now,
            False,
            now,
        ),
    )
    return record.incarnation_id


def issue(
    transaction,
    owner_id,
    incarnation_id,
    *,
    credential_id=None,
    max_admissions=5,
    ttl_seconds=3600,
    token_hash=None,
):
    token_hash = token_hash or fresh_token_hash()
    record = FrameworkCredentialRepository().issue(
        transaction,
        owner_id=owner_id,
        credential_id=credential_id or uid(),
        name="test credential",
        scopes=("operations.submit", "operations.read"),
        token_hash=token_hash,
        token_prefix="afk_test",
        issuer_kind="session_incarnation",
        issuer_reference=incarnation_id,
        max_admissions=max_admissions,
        ttl_seconds=ttl_seconds,
    )
    return record, token_hash


def observation_for(transaction, record: FrameworkCredentialRecord, *, token_hash):
    started = transaction.fetch_one("SELECT clock_timestamp() AS now")["now"]
    fence = FrameworkCredentialFence(
        owner_id=record.owner_id,
        credential_id=record.credential_id,
        token_hash=token_hash,
        scopes=record.scopes,
        max_admissions=record.max_admissions,
        consumed_admissions=record.consumed_admissions,
        created_at=record.created_at,
        expires_at=record.expires_at,
        revoked_at=record.revoked_at,
    )
    return FrameworkCredentialObservation(fence, started, started + timedelta(seconds=15))


def wait_for_lock(transaction, waiting_pid, blocking_pid):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        if (
            blocking_pid
            in transaction.fetch_one("SELECT pg_blocking_pids(%s) AS pids", (waiting_pid,))["pids"]
        ):
            return
        time.sleep(0.01)
    pytest.fail("issue() did not wait on the exact owner lock holder")


def test_public_framework_credential_contract_behaviors():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with db.transaction() as tx:
            owner = uid()
            incarnation = seed_session(tx, owner)
            record, _ = issue(tx, owner, incarnation, max_admissions=1)
            other = uid()
            assert FrameworkCredentialRepository().list_for_owner(tx, owner_id=other) == ()
            assert FrameworkCredentialRepository().list_for_owner(tx, owner_id=owner) == (record,)
        with db.transaction() as tx:
            schema = tx.fetch_one("SELECT current_schema() AS name")["name"]

        def consume():
            with independent_database(schema) as worker_db, worker_db.transaction() as tx:
                return FrameworkCredentialRepository().consume_admission(
                    tx, owner_id=owner, credential_id=record.credential_id
                )

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: _safe(consume), range(2)))
        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, Exception)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], RepositoryConflictError)
        with db.transaction() as tx, pytest.raises(RepositoryNotFoundError):
            FrameworkCredentialRepository().revoke(
                tx, owner_id="nobody", credential_id=record.credential_id
            )
    finally:
        fixture.close()


def test_issue_takes_the_same_owner_lock_domain_as_assignment_creation(database):
    owner = uid()
    with database.transaction() as tx:
        incarnation = seed_session(tx, owner)
        record, token_hash = issue(tx, owner, incarnation)
    assert record.owner_id == owner
    assert record.consumed_admissions == 0
    assert record.revoked_at is None
    assert not hasattr(record, "token_hash")
    assert token_hash not in repr(record)


@pytest.mark.postgresql_lock_catalog
def test_issue_refuses_when_the_issuing_session_is_revoked_while_it_waits_on_the_owner_lock(
    database,
):
    owner = uid()
    with database.transaction() as tx:
        incarnation = seed_session(tx, owner)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]

    holder_ready, release_holder, waiter_ready, identities = Event(), Event(), Event(), {}

    def holder():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout='5s'")
            identities["holder"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner,))
            holder_ready.set()
            assert release_holder.wait(5)
            # Revokes while issue() already waits on this same lock
            tx.execute("DELETE FROM web_session WHERE user_id=%s", (owner,))

    def waiter():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout='5s'")
            identities["waiter"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            waiter_ready.set()
            return issue(tx, owner, incarnation)

    with ThreadPoolExecutor(max_workers=2) as workers:
        holder_future = workers.submit(holder)
        assert holder_ready.wait(3)
        waiter_future = workers.submit(waiter)
        assert waiter_ready.wait(3)
        with database.transaction() as observer:
            wait_for_lock(observer, identities["waiter"], identities["holder"])
        release_holder.set()
        holder_future.result(timeout=5)
        with pytest.raises(RepositoryConflictError, match="credential_authority_unavailable"):
            waiter_future.result(timeout=5)
    with database.transaction() as tx:
        assert tx.fetch_all("SELECT * FROM framework_credential WHERE owner_id=%s", (owner,)) == ()


@pytest.mark.postgresql_lock_catalog
def test_issue_refuses_when_the_owner_retires_while_it_waits_on_the_lock(database):
    owner = uid()
    with database.transaction() as tx:
        incarnation = seed_session(tx, owner)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]

    holder_ready, release_holder, waiter_ready, identities = Event(), Event(), Event(), {}

    def holder():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout='5s'")
            identities["holder"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner,))
            holder_ready.set()
            assert release_holder.wait(5)
            tx.execute(
                "INSERT INTO astralplane_blob_owner_state (owner_id, state, retired_at) "
                "VALUES (%s, 'retired', clock_timestamp()) "
                "ON CONFLICT (owner_id) DO UPDATE SET "
                "state='retired', retired_at=clock_timestamp()",
                (owner,),
            )

    def waiter():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout='5s'")
            identities["waiter"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            waiter_ready.set()
            return issue(tx, owner, incarnation)

    with ThreadPoolExecutor(max_workers=2) as workers:
        holder_future = workers.submit(holder)
        assert holder_ready.wait(3)
        waiter_future = workers.submit(waiter)
        assert waiter_ready.wait(3)
        with database.transaction() as observer:
            wait_for_lock(observer, identities["waiter"], identities["holder"])
        release_holder.set()
        holder_future.result(timeout=5)
        with pytest.raises(RepositoryConflictError, match="credential_authority_unavailable"):
            waiter_future.result(timeout=5)
    with database.transaction() as tx:
        assert tx.fetch_all("SELECT * FROM framework_credential WHERE owner_id=%s", (owner,)) == ()


def test_expiry_is_computed_from_the_database_clock_inside_the_lock(database):
    owner = uid()
    with database.transaction() as tx:
        incarnation = seed_session(tx, owner)
        before = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
        record, _ = issue(tx, owner, incarnation, ttl_seconds=100)
        after = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    assert int(before.timestamp()) + 99 <= record.expires_at <= int(after.timestamp()) + 101


def test_revoke_and_relist_are_owner_scoped(database):
    owner, other = uid(), uid()
    with database.transaction() as tx:
        incarnation = seed_session(tx, owner)
        record, _ = issue(tx, owner, incarnation)
    with database.transaction() as tx, pytest.raises(RepositoryNotFoundError):
        FrameworkCredentialRepository().revoke(
            tx, owner_id=other, credential_id=record.credential_id
        )
    with database.transaction() as tx:
        revoked = FrameworkCredentialRepository().revoke(
            tx, owner_id=owner, credential_id=record.credential_id
        )
        assert revoked.revoked_at is not None
        again = FrameworkCredentialRepository().revoke(
            tx, owner_id=owner, credential_id=record.credential_id
        )
        assert again.revoked_at == revoked.revoked_at
        assert FrameworkCredentialRepository().list_for_owner(tx, owner_id=owner) == (revoked,)
        assert FrameworkCredentialRepository().list_for_owner(tx, owner_id=other) == ()


def test_consume_admission_two_workers_one_unit_exactly_one_succeeds(database):
    owner = uid()
    with database.transaction() as tx:
        incarnation = seed_session(tx, owner)
        record, _ = issue(tx, owner, incarnation, max_admissions=1)
    schema_holder = {}
    with database.transaction() as tx:
        schema_holder["schema"] = tx.fetch_one("SELECT current_schema() AS name")["name"]
    schema = schema_holder["schema"]

    def consume():
        with independent_database(schema) as db, db.transaction() as tx:
            return FrameworkCredentialRepository().consume_admission(
                tx, owner_id=owner, credential_id=record.credential_id
            )

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: _safe(consume), range(2)))
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], RepositoryConflictError)
    with database.transaction() as tx:
        row = tx.fetch_one(
            "SELECT consumed_admissions FROM framework_credential WHERE id=%s",
            (record.credential_id,),
        )
        assert row["consumed_admissions"] == 1


def _safe(fn):
    try:
        return fn()
    except Exception as exc:
        return exc


def test_consume_admission_refuses_exhausted_allowance(database):
    owner = uid()
    with database.transaction() as tx:
        incarnation = seed_session(tx, owner)
        record, _ = issue(tx, owner, incarnation, max_admissions=1)
        FrameworkCredentialRepository().consume_admission(
            tx, owner_id=owner, credential_id=record.credential_id
        )
    with database.transaction() as tx, pytest.raises(
        RepositoryConflictError, match="credential_allowance_exhausted"
    ):
        FrameworkCredentialRepository().consume_admission(
            tx, owner_id=owner, credential_id=record.credential_id
        )


def test_assert_current_execution_locks_the_row_and_refuses_revoked_expired_or_mismatched(
    database,
):
    owner = uid()
    with database.transaction() as tx:
        incarnation = seed_session(tx, owner)
        live, live_hash = issue(tx, owner, incarnation)
        expired, expired_hash = issue(tx, owner, incarnation, ttl_seconds=1)
    until = time.monotonic() + 15
    while time.monotonic() < until:
        with database.transaction() as tx:
            now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
            row = tx.fetch_one(
                "SELECT expires_at FROM framework_credential WHERE id=%s", (expired.credential_id,)
            )
        if now.timestamp() >= row["expires_at"].timestamp() + 1:
            break
        time.sleep(0.2)
    else:
        pytest.fail("framework_credential never became expired by the database's own clock")
    with database.transaction() as tx:
        state = FrameworkCredentialRepository().assert_current_execution(
            tx, observation=observation_for(tx, live, token_hash=live_hash)
        )
        assert state.credential.credential_id == live.credential_id
    with database.transaction() as tx, pytest.raises(RepositoryConflictError):
        FrameworkCredentialRepository().assert_current_execution(
            tx, observation=observation_for(tx, expired, token_hash=expired_hash)
        )
    with database.transaction() as tx:
        FrameworkCredentialRepository().revoke(tx, owner_id=owner, credential_id=live.credential_id)
    with database.transaction() as tx, pytest.raises(RepositoryConflictError):
        FrameworkCredentialRepository().assert_current_execution(
            tx, observation=observation_for(tx, live, token_hash=live_hash)
        )
    with database.transaction() as tx:
        fresh, fresh_hash = issue(tx, owner, incarnation)
    with database.transaction() as tx, pytest.raises(RepositoryConflictError):
        assert fresh_hash != "c" * 64
        FrameworkCredentialRepository().assert_current_execution(
            tx, observation=observation_for(tx, fresh, token_hash="c" * 64)
        )


def test_issue_validation_refuses_bad_inputs_before_any_lock(database):
    owner = uid()
    with database.transaction() as tx:
        incarnation = seed_session(tx, owner)
        with pytest.raises(RepositoryValidationError):
            issue(tx, owner, incarnation, max_admissions=0)
        with pytest.raises(RepositoryValidationError):
            issue(tx, owner, incarnation, ttl_seconds=0)
        with pytest.raises(RepositoryValidationError):
            FrameworkCredentialRepository().issue(
                tx,
                owner_id=owner,
                credential_id=uid(),
                name="x",
                scopes=("not-a-real-scope",),
                token_hash=TOKEN_HASH,
                token_prefix="afk_x",
                issuer_kind="session_incarnation",
                issuer_reference=incarnation,
                max_admissions=1,
                ttl_seconds=60,
            )
        with pytest.raises(RepositoryValidationError):
            FrameworkCredentialRepository().issue(
                tx,
                owner_id=owner,
                credential_id=uid(),
                name="x",
                scopes=("operations.submit",),
                token_hash="not-a-hash",
                token_prefix="afk_x",
                issuer_kind="session_incarnation",
                issuer_reference=incarnation,
                max_admissions=1,
                ttl_seconds=60,
            )
        with pytest.raises(RepositoryValidationError):
            FrameworkCredentialRepository().issue(
                tx,
                owner_id=owner,
                credential_id=uid(),
                name="x",
                scopes=("operations.submit",),
                token_hash=TOKEN_HASH,
                token_prefix="afk_x",
                issuer_kind="not-a-real-kind",
                issuer_reference=incarnation,
                max_admissions=1,
                ttl_seconds=60,
            )
    with database.transaction() as tx:
        assert tx.fetch_all("SELECT * FROM framework_credential WHERE owner_id=%s", (owner,)) == ()


def test_framework_one_shot_operation_can_be_claimed_and_executed_via_the_adapter(database):
    from dataclasses import replace

    from astralplane.repositories.assignment_models import (
        AssignmentOperationAuthority,
        AssignmentOperationSpec,
    )
    from astralplane.repositories.assignments import AssignmentRepository

    owner = uid()
    with database.transaction() as tx:
        tx.execute("DELETE FROM persistent_assignment")
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id=%s", (owner,))
        incarnation = seed_session(tx, owner)
        credential, token_hash = issue(tx, owner, incarnation)

    from test_assignments_postgres import definition as build_definition

    repo = AssignmentRepository()
    assignment_id = uid()
    with database.transaction() as tx:
        now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
        values = build_definition(tx)
        limits = {
            k: v
            for k, v in values.limits.items()
            if not k.startswith("daily_") and k != "cadence_seconds"
        }
        observation = observation_for(tx, credential, token_hash=token_hash)
        record = repo.create_operation(
            tx,
            authority=observation,
            owner_id=owner,
            assignment_id=assignment_id,
            origin_namespace="framework",
            caller_key="framework-caller",
            command_digest="0" * 64,
            definition=replace(
                values, source={}, allowed_tools=(), offline_grant_id=None, limits=limits
            ),
            operation=AssignmentOperationSpec(
                kind="chat",
                authority=AssignmentOperationAuthority(
                    owner_id=owner,
                    origin="framework",
                    reference_kind="credential",
                    reference_id=credential.credential_id,
                    expires_at=now + timedelta(minutes=10),
                ),
                deadline_at=now + timedelta(minutes=5),
                source_retention="none",
            ),
            credential_id=credential.credential_id,
        )
    assert record.operation["authority"]["origin"] == "framework"
    assert record.operation["authority"]["reference_kind"] == "credential"
    assert record.assignment_id == assignment_id
    with database.transaction() as tx:
        fetched = repo.get_assignment(tx, owner_id=owner, assignment_id=assignment_id)
        data = {
            "owner_id": owner,
            "execution_profile": "one_shot",
            "operation": fetched.operation,
            "definition": {"offline_grant_id": fetched.definition.offline_grant_id},
            "checkpoint": {},
        }
        fresh_observation = observation_for(tx, credential, token_hash=token_hash)
        assert repo._lock_execution_authority(tx, data, fresh_observation) is True
        assert (
            repo._lock_execution_authority(tx, data, session_observation(tx, owner_id=owner))
            is False
        )
