"""Real-PostgreSQL tests for astralplane.repositories and history: a database-issued
session identity survives refresh and resume but never a delete/recreate cycle, and
consent grant/clock checks share one transaction.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from uuid import UUID

import pytest
from test_assignment_execution_guard_postgres import _wait_for_lock
from test_assignments_postgres import database as database
from test_assignments_postgres import independent_database, parallel_transactions, uid
from test_assignments_postgres import tx as tx

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.history import (
    SessionConsentObservation,
    SessionExecutionObservation,
    SessionRecord,
    SessionRepository,
)


def input_record(tx, **changes):
    now = int(tx.fetch_one("SELECT clock_timestamp() AS now")["now"].timestamp())
    return replace(
        SessionRecord(
            uid(), "owner", "opaque-access", "opaque-refresh", now, now + 3600, now, False, now
        ),
        **changes,
    )


def issued(tx, **changes):
    return SessionRepository().put(tx, input_record(tx, **changes))


def recreate(tx, record):
    repo = SessionRepository()
    assert repo.delete(
        tx,
        owner_id=record.owner_id,
        session_id=record.session_id,
        expected_incarnation_id=record.incarnation_id,
    )
    return repo.put(tx, replace(record, incarnation_id=None))


def test_database_issues_identity_and_exact_issuance_replay_preserves_it(tx):
    repo = SessionRepository()
    original = input_record(tx)
    stored = repo.put(tx, original)
    assert UUID(stored.incarnation_id).version == 4
    assert stored == replace(original, incarnation_id=stored.incarnation_id)
    assert repo.put(tx, original) == stored
    assert repo.put(tx, stored) == stored
    for lookup in (
        repo.get(tx, owner_id=stored.owner_id, session_id=stored.session_id),
        repo.get_by_session_id_for_administration(tx, session_id=stored.session_id),
        repo.get_by_incarnation(tx, owner_id=stored.owner_id, incarnation_id=stored.incarnation_id),
    ):
        assert lookup == stored
    assert (
        repo.get_by_incarnation(tx, owner_id="other", incarnation_id=stored.incarnation_id) is None
    )
    assert repo.get_by_incarnation(tx, owner_id="owner", incarnation_id=uid()) is None


def test_explicit_identity_can_never_insert_or_rebind(tx):
    repo = SessionRepository()
    original = input_record(tx, incarnation_id=uid())
    with pytest.raises(RepositoryConflictError):
        repo.put(tx, original)
    assert repo.get(tx, owner_id="owner", session_id=original.session_id) is None
    stored = repo.put(tx, replace(original, incarnation_id=None))
    for replacement in (
        original,
        replace(stored, refresh_token_ciphertext="other"),
        replace(stored, owner_id="other"),
    ):
        with pytest.raises(RepositoryConflictError):
            repo.put(tx, replacement)
    newer = recreate(tx, stored)
    assert newer.incarnation_id != stored.incarnation_id
    assert replace(newer, incarnation_id=stored.incarnation_id) == stored
    with pytest.raises(RepositoryConflictError):
        repo.put(tx, stored)
    assert (
        repo.get_by_incarnation(tx, owner_id="owner", incarnation_id=stored.incarnation_id) is None
    )
    assert (
        repo.get_by_incarnation(tx, owner_id="owner", incarnation_id=newer.incarnation_id) == newer
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        1,
        "",
        "PRIVATE-invalid",
        "0" * 36,
        "00000000-0000-1000-8000-000000000000",
        "AAAAAAAA-AAAA-4AAA-AAAA-AAAAAAAAAAAA",
    ],
)
def test_malformed_or_missing_identity_cannot_mutate_or_authorize(tx, value):
    repo = SessionRepository()
    stored = issued(tx)
    actions = (
        lambda: repo.get_by_incarnation(tx, owner_id="owner", incarnation_id=value),
        lambda: repo.delete(
            tx, owner_id="owner", session_id=stored.session_id, expected_incarnation_id=value
        ),
        lambda: repo.delete_and_return(
            tx, owner_id="owner", session_id=stored.session_id, expected_incarnation_id=value
        ),
        lambda: repo.mark_resumed(
            tx,
            owner_id="owner",
            session_id=stored.session_id,
            expected_incarnation_id=value,
            expected_resumed=False,
            resumed=True,
        ),
        lambda: repo.compare_and_set_refresh(
            tx,
            replace(stored, incarnation_id=value, last_refresh_at=stored.last_refresh_at + 1),
            expected_last_refresh_at=stored.last_refresh_at,
        ),
        lambda: repo.execution_fence(replace(stored, incarnation_id=value)),
    )
    for action in actions:
        with pytest.raises(RepositoryValidationError) as error:
            action()
        assert "PRIVATE" not in str(error.value)
    assert repo.get(tx, owner_id="owner", session_id=stored.session_id) == stored


def test_malformed_stored_identity_refuses_decode_without_echoing_data(tx):
    stored = issued(tx)
    repo = SessionRepository()
    row = dict(tx.fetch_one("SELECT * FROM web_session WHERE sid=%s", (stored.session_id,)))

    class Query:
        def fetch_one(self, *_args):
            return row

    for value in (None, "PRIVATE-invalid", 7):
        row["incarnation_id"] = value
        with pytest.raises(RepositoryDataError, match="stored session identity is invalid"):
            repo.get(Query(), owner_id="owner", session_id=stored.session_id)
    del row["incarnation_id"]
    with pytest.raises(RepositoryDataError):
        repo.get(Query(), owner_id="owner", session_id=stored.session_id)


@pytest.mark.parametrize("exact_fence", [False, True])
def test_refresh_and_resume_preserve_identity_then_stale_replacement_is_refused(tx, exact_fence):
    repo = SessionRepository()
    original = issued(tx)
    kwargs = {"expected_credential": repo.execution_fence(original)} if exact_fence else {}
    refreshed = repo.compare_and_set_refresh(
        tx,
        replace(
            original,
            access_token_ciphertext="rotated",
            last_refresh_at=original.last_refresh_at + 1,
        ),
        expected_last_refresh_at=original.last_refresh_at,
        **kwargs,
    )
    assert refreshed.incarnation_id == original.incarnation_id
    resumed = repo.mark_resumed(
        tx,
        owner_id="owner",
        session_id=original.session_id,
        expected_resumed=False,
        resumed=True,
        expected_incarnation_id=original.incarnation_id,
    )
    assert resumed.incarnation_id == original.incarnation_id
    assert (
        repo.mark_resumed(
            tx,
            owner_id="owner",
            session_id=original.session_id,
            expected_resumed=False,
            resumed=True,
            expected_incarnation_id=original.incarnation_id,
        )
        == resumed
    )
    newer = recreate(tx, resumed)
    with pytest.raises(RepositoryConflictError):
        repo.compare_and_set_refresh(
            tx,
            replace(resumed, last_refresh_at=resumed.last_refresh_at + 1),
            expected_last_refresh_at=resumed.last_refresh_at,
            **({"expected_credential": repo.execution_fence(resumed)} if exact_fence else {}),
        )
    with pytest.raises(RepositoryConflictError):
        repo.mark_resumed(
            tx,
            owner_id="owner",
            session_id=original.session_id,
            expected_resumed=False,
            resumed=True,
            expected_incarnation_id=original.incarnation_id,
        )
    assert not repo.delete(
        tx,
        owner_id="owner",
        session_id=original.session_id,
        expected_incarnation_id=original.incarnation_id,
    )
    assert (
        repo.delete_and_return(
            tx,
            owner_id="owner",
            session_id=original.session_id,
            expected_incarnation_id=original.incarnation_id,
        )
        is None
    )
    assert repo.get(tx, owner_id="owner", session_id=original.session_id) == newer


def test_exact_refresh_fence_cannot_name_a_different_incarnation(tx):
    repo = SessionRepository()
    stored = issued(tx)
    with pytest.raises(RepositoryValidationError, match="bound session identity"):
        repo.compare_and_set_refresh(
            tx,
            replace(stored, last_refresh_at=stored.last_refresh_at + 1),
            expected_last_refresh_at=stored.last_refresh_at,
            expected_credential=replace(repo.execution_fence(stored), incarnation_id=uid()),
        )


def test_v2_observation_refuses_v1_and_same_bytes_recreated_session(tx):
    repo = SessionRepository()
    stored = issued(tx)
    state = repo.get_execution_state(tx, owner_id="owner", session_id=stored.session_id)
    observation = SessionExecutionObservation(
        state.credential, state.observed_at, state.observed_at + timedelta(seconds=15)
    )
    assert observation.credential.version == 2
    assert repo.assert_current_execution(tx, observation=observation).credential == state.credential
    with pytest.raises(RepositoryValidationError, match="unsupported session credential"):
        repo.assert_current_execution(
            tx, observation=replace(observation, credential=replace(state.credential, version=1))
        )
    recreate(tx, stored)
    with pytest.raises(RepositoryConflictError, match="session authority unavailable"):
        repo.assert_current_execution(tx, observation=observation)


def test_concurrent_same_issuance_retains_one_database_identity(database):
    repo = SessionRepository()
    with database.transaction() as tx:
        original = input_record(tx, owner_id="concurrent-" + uid())
    results = parallel_transactions(database, [lambda tx: repo.put(tx, original)] * 2)
    assert results[0] == results[1]
    assert UUID(results[0].incarnation_id).version == 4


@pytest.mark.parametrize("operation", ["refresh", "resume", "delete", "delete_and_return"])
@pytest.mark.postgresql_lock_catalog
def test_replacement_commits_while_stale_mutation_waits_on_row(database, operation):
    repo = SessionRepository()
    with database.transaction() as tx:
        stored = issued(tx, owner_id="race-" + uid())
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    waiting, pids = Event(), {}

    def mutate():
        with independent_database(schema) as db, db.transaction() as tx:
            pids["waiter"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            waiting.set()
            try:
                if operation == "refresh":
                    return repo.compare_and_set_refresh(
                        tx,
                        replace(stored, last_refresh_at=stored.last_refresh_at + 1),
                        expected_last_refresh_at=stored.last_refresh_at,
                    )
                kwargs = dict(
                    owner_id=stored.owner_id,
                    session_id=stored.session_id,
                    expected_incarnation_id=stored.incarnation_id,
                )
                if operation == "resume":
                    kwargs.update(expected_resumed=False, resumed=True)
                return getattr(repo, "mark_resumed" if operation == "resume" else operation)(
                    tx, **kwargs
                )
            except (RepositoryConflictError, RepositoryNotFoundError) as error:
                return error

    with ThreadPoolExecutor(max_workers=1) as workers:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            tx.fetch_one(
                "SELECT sid FROM web_session WHERE sid=%s FOR UPDATE", (stored.session_id,)
            )
            future = workers.submit(mutate)
            assert waiting.wait(3)
            _wait_for_lock(tx, pids["waiter"], pid)
            newer = recreate(tx, stored)
        result = future.result(timeout=5)
    assert result is None or result is False or isinstance(result, RepositoryConflictError)
    with database.transaction() as tx:
        assert repo.get(tx, owner_id=stored.owner_id, session_id=stored.session_id) == newer


@pytest.mark.postgresql_lock_catalog
def test_retirement_lock_prevents_new_session_resurrection(database):
    repo = SessionRepository()
    with database.transaction() as tx:
        original = input_record(tx, owner_id="retired-" + uid())
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    waiting, pids = Event(), {}

    def issue():
        with independent_database(schema) as db, db.transaction() as tx:
            pids["waiter"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            waiting.set()
            with pytest.raises(RepositoryConflictError):
                repo.put(tx, original)

    with ThreadPoolExecutor(max_workers=1) as workers:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            tx.fetch_one(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (original.owner_id,)
            )
            future = workers.submit(issue)
            assert waiting.wait(3)
            _wait_for_lock(tx, pids["waiter"], pid)
            tx.execute(
                "INSERT INTO astralplane_blob_owner_state(owner_id,state,retired_at) "
                "VALUES(%s,'retired',clock_timestamp())",
                (original.owner_id,),
            )
        future.result(timeout=5)
    with database.transaction() as tx:
        assert repo.get(tx, owner_id=original.owner_id, session_id=original.session_id) is None


def consent(tx, record):
    repo = SessionRepository()
    state = repo.get_execution_state(tx, owner_id=record.owner_id, session_id=record.session_id)
    return SessionConsentObservation(
        state.credential, state.observed_at, state.observed_at + timedelta(seconds=15)
    )


def test_consent_has_distinct_type_and_exact_v2_session_fence(tx):
    repo = SessionRepository()
    original = issued(tx)
    observation = consent(tx, original)
    assert (
        repo.assert_current_consent(tx, observation=observation).credential
        == observation.credential
    )
    execution = SessionExecutionObservation(
        observation.credential, observation.started_at, observation.valid_until
    )
    with pytest.raises(RepositoryValidationError, match="typed session consent"):
        repo.assert_current_consent(tx, observation=execution)
    with pytest.raises(RepositoryValidationError, match="typed session execution"):
        repo.assert_current_execution(tx, observation=observation)
    assert original.incarnation_id not in repr(observation)
    recreate(tx, original)
    with pytest.raises(RepositoryConflictError):
        repo.assert_current_consent(tx, observation=observation)


@pytest.mark.parametrize(
    "change", ["version", "boolean", "naive", "zero", "long", "future", "expired", "old_fence"]
)
def test_consent_rejects_malformed_and_stale_observations(tx, change):
    repo = SessionRepository()
    original = issued(tx)
    observation = consent(tx, original)
    if change == "version":
        observation = replace(observation, version=2)
    elif change == "boolean":
        observation = replace(observation, version=True)
    elif change == "naive":
        observation = replace(observation, started_at=observation.started_at.replace(tzinfo=None))
    elif change == "zero":
        observation = replace(observation, valid_until=observation.started_at)
    elif change == "long":
        observation = replace(
            observation, valid_until=observation.started_at + timedelta(seconds=16)
        )
    elif change == "future":
        observation = replace(observation, started_at=observation.started_at + timedelta(seconds=5))
    elif change == "expired":
        observation = replace(
            observation,
            started_at=observation.started_at - timedelta(seconds=16),
            valid_until=observation.started_at,
        )
    else:
        observation = replace(observation, credential=replace(observation.credential, version=1))
    with pytest.raises((RepositoryValidationError, RepositoryConflictError)):
        repo.assert_current_consent(tx, observation=observation)


@pytest.mark.parametrize("expire", [False, True])
@pytest.mark.postgresql_lock_catalog
def test_consent_grant_write_and_final_clock_check_are_one_transaction(database, expire):
    from astralplane.api import create_repository_catalog

    catalog = create_repository_catalog()
    repo = catalog.history.sessions
    grant_id = uid()
    with database.transaction() as tx:
        original = issued(tx, owner_id="consent-" + uid())
        observation = consent(tx, original)
    if expire:
        observation = replace(
            observation, valid_until=observation.started_at + timedelta(milliseconds=150)
        )

    def create():
        with database.transaction() as tx:
            repo.assert_current_consent(tx, observation=observation)
            catalog.offline_grants.create_grant(
                tx,
                grant_id=grant_id,
                owner_id=original.owner_id,
                agent_id=None,
                encrypted_refresh_token=b"opaque-reference",
                issued_at=original.created_at,
                expires_at=original.hard_expires_at,
            )
            if expire:
                tx.fetch_one("SELECT pg_sleep(0.2)")
            repo.assert_current_consent(tx, observation=observation)

    if expire:
        with pytest.raises(RepositoryConflictError):
            create()
    else:
        create()
    with database.transaction() as tx:
        found = catalog.offline_grants.get_grant(tx, owner_id=original.owner_id, grant_id=grant_id)
        assert (found is None) == expire


def test_deliberate_owner_session_delete_preserves_issued_liabilities_and_startup(database):
    from test_session_execution_postgres import operation, stored_rows

    from astralplane.database.migrations import (
        CURRENT_DATA_PLANE_REVISION,
        MIGRATION_REGISTRY,
        MigrationRunner,
    )
    from astralplane.repositories.assignments import AssignmentRepository

    with database.transaction() as tx:
        tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        values = operation(tx, AssignmentRepository(), issued=True)
        repo, session = values[:2]
        before = stored_rows(tx, values[3].assignment_id)
        other = issued(tx, owner_id="other-" + uid())
        assert repo.delete_owner(tx, owner_id=session.owner_id) >= 1
        after = stored_rows(tx, values[3].assignment_id)
        assert before == after
    if tx.dialect.is_sqlite:
        from astralplane.database.sqlite_migrations import (
            SQLITE_DATA_PLANE_REVISION,
            SQLITE_MIGRATION_REGISTRY,
            SQLiteBootRunner,
        )

        report = SQLiteBootRunner(
            database,
            MigrationRunner(
                database, revision=SQLITE_DATA_PLANE_REVISION, registry=SQLITE_MIGRATION_REGISTRY
            ),
        ).run(expected_revision=SQLITE_DATA_PLANE_REVISION.schema_revision)
    else:
        report = MigrationRunner(
            database, revision=CURRENT_DATA_PLANE_REVISION, registry=MIGRATION_REGISTRY
        ).run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
    assert report.already_current
    with database.transaction() as tx:
        assert repo.get(tx, owner_id=session.owner_id, session_id=session.session_id) is None
        assert repo.get(tx, owner_id=other.owner_id, session_id=other.session_id) == other


@pytest.mark.postgresql_lock_catalog
def test_consent_checks_original_deadline_after_session_lock_wait(database):
    repo = SessionRepository()
    with database.transaction() as tx:
        original = issued(tx, owner_id="consent-wait-" + uid())
        observation = consent(tx, original)
        observation = replace(
            observation, valid_until=observation.started_at + timedelta(seconds=1)
        )
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    waiting, pids = Event(), {}

    def check():
        with independent_database(schema) as db, db.transaction() as tx:
            pids["waiter"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            waiting.set()
            with pytest.raises(RepositoryConflictError):
                repo.assert_current_consent(tx, observation=observation)

    with ThreadPoolExecutor(max_workers=1) as workers:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            tx.fetch_one(
                "SELECT sid FROM web_session WHERE sid=%s FOR UPDATE", (original.session_id,)
            )
            future = workers.submit(check)
            assert waiting.wait(3)
            _wait_for_lock(tx, pids["waiter"], pid)
            tx.fetch_one(
                "SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM (%s - clock_timestamp()))) + 0.05)",
                (observation.valid_until,),
            )
        future.result(timeout=5)


@pytest.mark.parametrize("field", ["created_at", "interactive_anchor", "hard_expires_at"])
@pytest.mark.parametrize("exact_fence", [False, True])
def test_refresh_cannot_rewrite_issued_family_lifetime(tx, field, exact_fence):
    repo = SessionRepository()
    original = issued(tx)
    changed = replace(
        original,
        last_refresh_at=original.last_refresh_at + 1,
        **{field: getattr(original, field) + 1},
    )
    kwargs = {"expected_credential": repo.execution_fence(original)} if exact_fence else {}
    with pytest.raises((RepositoryConflictError, RepositoryValidationError)):
        repo.compare_and_set_refresh(
            tx, changed, expected_last_refresh_at=original.last_refresh_at, **kwargs
        )
    assert repo.get(tx, owner_id=original.owner_id, session_id=original.session_id) == original
