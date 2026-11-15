"""Real-PostgreSQL tests for astralplane.repositories.assignments, history, and
offline_grants: the execution guard locks the exact session first, refuses on
authority loss, and holds it until commit before logout or rotation.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Event

import pytest
from test_assignment_execution_guard_postgres import ObservedTransaction, _wait_for_lock
from test_assignments_postgres import (
    action,
    create_operation,
    independent_database,
    reserve,
    start,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_control_postgres import control, current, operation_claim
from test_operation_payload_postgres import admission, settle_args

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.assignments import (
    AssignmentOperationAuthority,
    AssignmentOperationSpec,
    AssignmentRepository,
)
from astralplane.repositories.history import (
    SessionExecutionObservation,
    SessionRecord,
    SessionRepository,
)


def seed(tx, *, owner="owner", sid=None):
    sessions = SessionRepository()
    now = int(tx.fetch_one("SELECT clock_timestamp() AS now")["now"].timestamp())
    record = SessionRecord(
        sid or uid(),
        owner,
        "encrypted-access-" + uid(),
        "encrypted-refresh-" + uid(),
        now,
        now + 3600,
        now,
        False,
        now,
    )
    record = sessions.put(tx, replace(record, incarnation_id=None))
    state = sessions.get_execution_state(tx, owner_id=owner, session_id=record.session_id)
    observation = SessionExecutionObservation(
        state.credential, state.observed_at, state.observed_at + timedelta(seconds=15)
    )
    return sessions, record, observation


def operation(tx, repo, *, issued=False):
    sessions, session, observation = seed(tx)
    now = observation.started_at
    record = create_operation(
        repo,
        tx,
        authority=observation,
        operation=AssignmentOperationSpec(
            "chat",
            AssignmentOperationAuthority(
                "owner",
                "interactive",
                "session_incarnation",
                session.incarnation_id,
                now + timedelta(minutes=5),
            ),
            now + timedelta(minutes=5),
            "none",
        ),
    )
    claim = operation_claim(repo, tx)
    work, selected, binding = admission(repo, tx, claim)
    permit = None
    if issued:
        intent = action(repo, tx, claim.fence)
        repo.assert_current_assignment_execution(
            tx, fence=claim.fence, binding=binding, authority=observation
        )
        permit = start(repo, tx, claim.fence, reserve(repo, tx, claim.fence, intent), binding)
    return sessions, session, observation, record, claim, work, selected, binding, permit


def guard(tx, repo, values, observation=None):
    return repo.assert_current_assignment_execution(
        tx,
        fence=values[4].fence,
        binding=values[7],
        authority=values[2] if observation is None else observation,
    )


def wrapper_arguments(tx, repo, values, method):
    intent = action(repo, tx, values[4].fence)
    arguments = dict(fence=values[4].fence, binding=values[7], authority=values[2])
    if method == "put":
        arguments["intent"] = replace(intent.intent, action_key=uid())
    else:
        arguments.update(
            action_id=intent.action_id,
            attempt_id=uid(),
            expected_request_digest=intent.intent.request_digest,
        )
        if method == "reserve":
            arguments["maximum"] = intent.intent.maximum
        else:
            reservation = reserve(repo, tx, values[4].fence, intent)
            arguments.update(
                attempt_id=reservation.attempt_id,
                current_permission_digest=intent.intent.permission_digest,
                current_precondition_digest=intent.intent.precondition_digest,
            )
    return arguments


def stored_rows(tx, assignment_id):
    return (
        tx.fetch_all("SELECT * FROM persistent_assignment WHERE id=%s", (assignment_id,)),
        tx.fetch_all(
            "SELECT * FROM persistent_assignment_action WHERE assignment_id=%s ORDER BY id",
            (assignment_id,),
        ),
        tx.fetch_all(
            "SELECT * FROM persistent_assignment_activity WHERE assignment_id=%s ORDER BY id",
            (assignment_id,),
        ),
    )


@pytest.mark.parametrize("method", ["put", "reserve", "start"])
def test_named_guarded_action_operations_commit_their_expected_transition(tx, repo, method):
    values = operation(tx, repo)
    arguments = wrapper_arguments(tx, repo, values, method)
    result = getattr(repo, method + "_action_for_execution")(tx, **arguments)
    if method == "put":
        assert result.state == "ready"
        assert result.intent == arguments["intent"]
    elif method == "reserve":
        assert result.action.state == "reserved"
        assert result.attempt_id == arguments["attempt_id"]
        assert current(repo, tx, values[3]).usage["outstanding"]["tool_calls"] == 1
    else:
        assert result.action_id == arguments["action_id"]
        assert result.attempt_id == arguments["attempt_id"]
        assert result.dispatch_token
        settled = repo.record_action_outcome(
            tx,
            **settle_args(tx, values[3], values[4], values[7], result, result_authority=values[2]),
        )
        assert settled.result["result_available"] is True
        assert current(repo, tx, values[3]).usage["spent"]["tool_calls"] == 1


@pytest.mark.parametrize("method", ["put", "reserve", "start"])
def test_caught_final_authority_refusal_rolls_back_only_the_guarded_operation(
    database, repo, method
):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        values = operation(tx, repo)
        arguments = wrapper_arguments(tx, repo, values, method)
        before = stored_rows(tx, values[3].assignment_id)

    class RefuseFinalObservation(AssignmentRepository):
        calls = 0

        def assert_current_assignment_execution(self, transaction, **kwargs):
            self.calls += 1
            if self.calls == 2:
                authority = kwargs["authority"]
                kwargs["authority"] = replace(
                    authority,
                    started_at=authority.started_at - timedelta(seconds=16),
                    valid_until=authority.valid_until - timedelta(seconds=16),
                )
            return super().assert_current_assignment_execution(transaction, **kwargs)

    guarded = RefuseFinalObservation()
    with database.transaction() as tx:
        values[0].mark_resumed(
            tx,
            owner_id="owner",
            session_id=values[1].session_id,
            expected_incarnation_id=values[1].incarnation_id,
            expected_resumed=False,
            resumed=True,
        )
        with pytest.raises(RepositoryConflictError):
            getattr(guarded, method + "_action_for_execution")(tx, **arguments)
        assert guarded.calls == 2
        assert stored_rows(tx, values[3].assignment_id) == before
    with database.transaction() as tx:
        assert stored_rows(tx, values[3].assignment_id) == before
        assert values[0].get(tx, owner_id="owner", session_id=values[1].session_id).resumed


def test_exact_owner_scoped_observation_is_ephemeral_and_does_not_modify_session(tx):
    sessions, record, observation = seed(tx)
    assert sessions.get_execution_state(tx, owner_id="other", session_id=record.session_id) is None
    assert sessions.get_execution_state(tx, owner_id="owner", session_id=uid()) is None
    checked = sessions.assert_current_execution(tx, observation=observation)
    assert checked.credential == observation.credential
    assert sessions.get(tx, owner_id="owner", session_id=record.session_id) == record
    for secret in (
        record.session_id,
        record.access_token_ciphertext,
        record.refresh_token_ciphertext,
        observation.credential.encrypted_state_binding,
    ):
        assert secret not in repr(observation)
        assert secret not in repr(checked)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 3),
        ("version", True),
        ("owner_id", ""),
        ("session_id", ""),
        ("created_at", True),
        ("interactive_anchor", -1),
        ("hard_expires_at", 2**53),
        ("refresh_generation", 0),
        ("encrypted_state_binding", "PRIVATE_SECRET"),
        ("encrypted_state_binding", None),
    ],
)
def test_malformed_credential_fence_refuses_before_database_use(tx, field, value):
    sessions, _, observation = seed(tx)
    malformed = replace(observation, credential=replace(observation.credential, **{field: value}))
    with pytest.raises(RepositoryValidationError) as failure:
        sessions.assert_current_execution(object(), observation=malformed)
    assert "PRIVATE_SECRET" not in str(failure.value)


@pytest.mark.parametrize(
    "change",
    [
        "mapping",
        "future_version",
        "boolean_version",
        "credential_mapping",
        "naive_start",
        "naive_end",
        "zero_window",
        "long_window",
        "backwards_window",
    ],
)
def test_malformed_observation_refuses_before_database_use(tx, change):
    sessions, _, observation = seed(tx)
    changes = {
        "mapping": None,
        "future_version": {"version": 2},
        "boolean_version": {"version": True},
        "credential_mapping": {"credential": {}},
        "naive_start": {"started_at": datetime(2026, 1, 1)},
        "naive_end": {"valid_until": None},
        "zero_window": {"valid_until": observation.started_at},
        "long_window": {"valid_until": observation.started_at + timedelta(seconds=16)},
        "backwards_window": {"valid_until": observation.started_at - timedelta(seconds=1)},
    }
    value = {} if change == "mapping" else replace(observation, **changes[change])
    with pytest.raises(RepositoryValidationError):
        sessions.assert_current_execution(object(), observation=value)


@pytest.mark.parametrize("offset", [-16, 1])
def test_old_or_future_database_observation_is_unavailable(tx, offset):
    sessions, _, observed = seed(tx)
    observed = replace(
        observed,
        started_at=observed.started_at + timedelta(seconds=offset),
        valid_until=observed.valid_until + timedelta(seconds=offset),
    )
    with pytest.raises(RepositoryConflictError, match="session authority unavailable"):
        sessions.assert_current_execution(tx, observation=observed)


@pytest.mark.parametrize(
    "field",
    [
        "access_token_ciphertext",
        "refresh_token_ciphertext",
        "created_at",
        "interactive_anchor",
        "hard_expires_at",
        "last_refresh_at",
    ],
)
@pytest.mark.parametrize("operation_name", ["guard", "refresh"])
def test_deleted_and_recreated_same_sid_refuses_previous_generation(tx, field, operation_name):
    sessions, record, observation = seed(tx)
    assert (
        sessions.delete_and_return(
            tx,
            owner_id="owner",
            session_id=record.session_id,
            expected_incarnation_id=record.incarnation_id,
        )
        == record
    )
    value = getattr(record, field)
    replacement = replace(
        record, **{field: value + "-replacement" if isinstance(value, str) else value + 1}
    )
    if field in {"created_at", "interactive_anchor"}:
        replacement = replace(
            replacement,
            created_at=record.created_at + 1,
            interactive_anchor=record.interactive_anchor + 1,
            last_refresh_at=record.last_refresh_at + 1,
        )
    replacement = sessions.put(tx, replace(replacement, incarnation_id=None))
    with pytest.raises(RepositoryConflictError):
        if operation_name == "guard":
            sessions.assert_current_execution(tx, observation=observation)
        else:
            sessions.compare_and_set_refresh(
                tx,
                replace(record, last_refresh_at=record.last_refresh_at + 1),
                expected_last_refresh_at=record.last_refresh_at,
                expected_credential=observation.credential,
            )
    assert sessions.get(tx, owner_id="owner", session_id=record.session_id) == replacement


@pytest.mark.parametrize("lifetime", ["expired", "future", "generation_ahead", "resumed"])
def test_session_lifetime_uses_db_time_but_generation_is_not_a_wall_clock(tx, lifetime):
    sessions, record, _ = seed(tx)
    sessions.delete(
        tx,
        owner_id="owner",
        session_id=record.session_id,
        expected_incarnation_id=record.incarnation_id,
    )
    if lifetime == "expired":
        record = replace(
            record,
            created_at=record.created_at - 10,
            interactive_anchor=record.interactive_anchor - 10,
            hard_expires_at=record.interactive_anchor,
        )
    elif lifetime == "future":
        record = replace(
            record,
            created_at=record.created_at + 10,
            interactive_anchor=record.interactive_anchor + 10,
            last_refresh_at=record.last_refresh_at + 10,
        )
    elif lifetime == "generation_ahead":
        record = replace(record, last_refresh_at=record.last_refresh_at + 1000)
    else:
        record = replace(record, resumed=True)
    record = sessions.put(tx, replace(record, incarnation_id=None))
    state = sessions.get_execution_state(tx, owner_id="owner", session_id=record.session_id)
    observation = SessionExecutionObservation(
        state.credential, state.observed_at, state.observed_at + timedelta(seconds=15)
    )
    if lifetime in {"expired", "future"}:
        with pytest.raises(RepositoryConflictError):
            sessions.assert_current_execution(tx, observation=observation)
    else:
        assert (
            sessions.assert_current_execution(tx, observation=observation).credential
            == state.credential
        )


def test_new_observation_after_same_sid_replacement_requires_host_incarnation_binding(tx, repo):
    values = operation(tx, repo)
    sessions, record, old_observation = values[:3]
    sessions.delete(
        tx,
        owner_id="owner",
        session_id=record.session_id,
        expected_incarnation_id=record.incarnation_id,
    )
    replacement = replace(record, access_token_ciphertext="encrypted-new-incarnation")
    replacement = sessions.put(tx, replace(replacement, incarnation_id=None))
    with pytest.raises(RepositoryConflictError):
        guard(tx, repo, values, old_observation)
    current = sessions.get_execution_state(tx, owner_id="owner", session_id=record.session_id)
    new_observation = SessionExecutionObservation(
        current.credential, current.observed_at, current.observed_at + timedelta(seconds=15)
    )
    with pytest.raises(RepositoryConflictError):
        guard(tx, repo, values, new_observation)


@pytest.mark.parametrize("operation_name", ["guard", "refresh"])
def test_direct_session_authority_and_bound_refresh_refuse_retired_owner(tx, repo, operation_name):
    sessions, record, observation = seed(tx)
    repo.retire_operations_for_owner(tx, owner_id="owner")
    with pytest.raises(RepositoryConflictError):
        if operation_name == "guard":
            sessions.assert_current_execution(tx, observation=observation)
        else:
            sessions.compare_and_set_refresh(
                tx,
                replace(record, last_refresh_at=record.last_refresh_at + 1),
                expected_last_refresh_at=record.last_refresh_at,
                expected_credential=observation.credential,
            )
    assert sessions.get(tx, owner_id="owner", session_id=record.session_id) == record


def test_refresh_cas_binds_both_encrypted_credentials_and_immutable_identity(tx):
    sessions, record, observation = seed(tx)
    replacement = replace(
        record,
        last_refresh_at=record.last_refresh_at + 1,
        refresh_token_ciphertext="encrypted-rotated",
    )
    stored = sessions.compare_and_set_refresh(
        tx,
        replacement,
        expected_last_refresh_at=record.last_refresh_at,
        expected_credential=observation.credential,
    )
    assert stored == replacement
    with pytest.raises(RepositoryConflictError):
        sessions.compare_and_set_refresh(
            tx,
            replacement,
            expected_last_refresh_at=record.last_refresh_at,
            expected_credential=observation.credential,
        )
    with pytest.raises(RepositoryValidationError):
        sessions.compare_and_set_refresh(
            tx,
            replace(replacement, interactive_anchor=0),
            expected_last_refresh_at=record.last_refresh_at,
            expected_credential=observation.credential,
        )
    with pytest.raises(RepositoryValidationError):
        sessions.execution_fence({"access_token": "PRIVATE"})


def test_guard_locks_exact_session_before_assignment_and_admission(tx, repo):
    values = operation(tx, repo)
    observed = ObservedTransaction(tx)
    assert guard(observed, repo, values).assignment_id == values[3].assignment_id
    locked = [sql for sql in observed.statements if "FOR UPDATE" in sql]
    session = next(i for i, sql in enumerate(locked) if "FROM web_session" in sql)
    assignment = next(i for i, sql in enumerate(locked) if "persistent_assignment" in sql)
    admission_index = next(i for i, sql in enumerate(locked) if "operation_record" in sql)
    assert session < assignment < admission_index


@pytest.mark.parametrize(
    "loss",
    [
        "missing",
        "other_session",
        "other_owner",
        "unknown",
        "malformed",
        "deleted",
        "rotated",
        "retired",
        "expired",
        "future",
        "delegation",
        "grant_revoked",
    ],
)
def test_guard_refuses_session_authority_loss_before_mutation(tx, repo, loss):
    values = operation(tx, repo)
    sessions, session, observed, record, claim, _, _, binding, _ = values
    if loss == "missing":
        observed = None
    elif loss in {"other_session", "other_owner"}:
        _, _, observed = seed(tx, owner="other" if loss == "other_owner" else "owner")
    elif loss == "unknown":
        observed = replace(observed, version=2)
    elif loss == "malformed":
        observed = replace(observed, credential={"token": "PRIVATE"})
    elif loss == "deleted":
        sessions.delete(
            tx,
            owner_id="owner",
            session_id=session.session_id,
            expected_incarnation_id=session.incarnation_id,
        )
    elif loss == "rotated":
        sessions.compare_and_set_refresh(
            tx,
            replace(session, last_refresh_at=session.last_refresh_at + 1),
            expected_last_refresh_at=session.last_refresh_at,
        )
    elif loss == "retired":
        repo.retire_operations_for_owner(tx, owner_id="owner")
    elif loss in {"expired", "future"}:
        offset = timedelta(seconds=-16 if loss == "expired" else 1)
        observed = replace(
            observed,
            started_at=observed.started_at + offset,
            valid_until=observed.valid_until + offset,
        )
    elif loss == "delegation":
        from test_operation_control_postgres import mutate

        mutate(
            tx,
            record,
            lambda data: data["operation"]["authority"].update(reference_kind="delegation"),
        )
    else:
        from test_assignments_postgres import definition
        from test_operation_control_postgres import mutate

        from astralplane.repositories.offline_grants import OfflineGrantRepository

        grant = definition(tx).offline_grant_id
        mutate(tx, record, lambda data: data["definition"].update(offline_grant_id=grant))
        OfflineGrantRepository().revoke_grant(tx, owner_id="owner", grant_id=grant, revoked_at=1)
    with pytest.raises(RepositoryConflictError):
        repo.assert_current_assignment_execution(
            tx, fence=claim.fence, binding=binding, authority=observed
        )


@pytest.mark.parametrize(
    "loss", ["none", "missing", "deleted", "rotated", "retired", "paused", "admission", "unknown"]
)
@pytest.mark.parametrize("outcome", ["succeeded", "failed", "uncertain"])
def test_authentic_late_consumption_charged_once_without_stale_result_or_wake(
    tx, repo, loss, outcome
):
    values = operation(tx, repo, issued=True)
    sessions, session, observation, record, claim, work, selected, binding, permit = values
    if loss == "missing":
        observation = None
    elif loss == "deleted":
        sessions.delete(
            tx,
            owner_id="owner",
            session_id=session.session_id,
            expected_incarnation_id=session.incarnation_id,
        )
    elif loss == "rotated":
        sessions.compare_and_set_refresh(
            tx,
            replace(session, last_refresh_at=session.last_refresh_at + 1),
            expected_last_refresh_at=session.last_refresh_at,
        )
    elif loss == "retired":
        repo.retire_operations_for_owner(tx, owner_id="owner")
    elif loss == "paused":
        control(repo, tx, current(repo, tx, record), "pause")
    elif loss == "admission":
        work.reselect_execution(tx, selected.fence, now=None, slot_lease=timedelta(minutes=1))
    elif loss == "unknown":
        observation = replace(observation, version=2)
    args = settle_args(tx, record, claim, binding, permit, result_authority=observation)
    args["outcome"] = replace(args["outcome"], outcome=outcome)
    before = current(repo, tx, record)
    settled = repo.record_action_outcome(tx, **args)
    after = current(repo, tx, record)
    charge = int(outcome != "uncertain")
    assert (
        after.usage["spent"].get("tool_calls", 0)
        == before.usage["spent"].get("tool_calls", 0) + charge
    )
    assert after.usage["outstanding"]["tool_calls"] == 1 - charge
    if loss != "none":
        assert settled.result["result_available"] is False
        assert settled.result["result"] == {}
        assert after.wake_generation == before.wake_generation
        assert after.checkpoint == before.checkpoint
    else:
        assert settled.result["result"]["text"] == "safe result"
    repo.record_action_outcome(tx, **args)
    assert current(repo, tx, record).usage == after.usage
    if outcome == "uncertain":
        args["outcome"] = replace(args["outcome"], outcome="failed")
        repo.record_action_outcome(tx, **args)
        final_usage = current(repo, tx, record).usage
        assert final_usage["spent"]["tool_calls"] == 1
        assert final_usage["outstanding"]["tool_calls"] == 0
        repo.record_action_outcome(tx, **args)
        assert current(repo, tx, record).usage == final_usage


@pytest.mark.parametrize("blocker", ["logout", "session", "owner", "admission", "action"])
@pytest.mark.postgresql_lock_catalog
def test_database_time_and_logout_rechecked_after_real_lock_wait(database, repo, blocker):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        values = operation(tx, repo, issued=blocker == "action")
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
        observed = replace(values[2], valid_until=values[2].started_at + timedelta(seconds=1.5))
    waiting, identities = Event(), {}

    def attempt():
        with independent_database(schema) as db, db.transaction() as tx:
            identities["waiting"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            waiting.set()
            try:
                if blocker == "action":
                    return repo.record_action_outcome(
                        tx,
                        **settle_args(
                            tx,
                            values[3],
                            values[4],
                            values[7],
                            values[8],
                            result_authority=observed,
                        ),
                    )
                return guard(tx, repo, values, observed)
            except RepositoryConflictError as error:
                return error

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            if blocker == "logout":
                values[0].delete_and_return(
                    tx,
                    owner_id="owner",
                    session_id=values[1].session_id,
                    expected_incarnation_id=values[1].incarnation_id,
                )
            elif blocker == "session":
                tx.fetch_one(
                    "SELECT sid FROM web_session WHERE sid=%s FOR UPDATE", (values[1].session_id,)
                )
            elif blocker == "owner":
                tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", ("owner",))
            elif blocker == "admission":
                tx.fetch_one(
                    "SELECT operation_id FROM operation_record WHERE operation_id=%s FOR UPDATE",
                    (values[7].operation_id,),
                )
            else:
                tx.fetch_one(
                    "SELECT id FROM persistent_assignment_action WHERE id=%s FOR UPDATE",
                    (values[8].action_id,),
                )
            future = pool.submit(attempt)
            assert waiting.wait(3)
            _wait_for_lock(tx, identities["waiting"], pid)
            tx.fetch_one("SELECT pg_sleep(1.55)")
        result = future.result(timeout=5)
    if blocker == "action":
        assert result.result["result_available"] is False
        with database.transaction() as tx:
            assert current(repo, tx, values[3]).usage["spent"]["tool_calls"] == 1
    else:
        assert isinstance(result, RepositoryConflictError)


@pytest.mark.parametrize("method", ["reserve", "start"])
@pytest.mark.postgresql_lock_catalog
def test_action_lock_wait_expiry_rolls_back_guarded_writes_even_when_caught(database, repo, method):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        values = operation(tx, repo)
        arguments = wrapper_arguments(tx, repo, values, method)
        arguments["authority"] = replace(
            values[2], valid_until=values[2].started_at + timedelta(seconds=1.5)
        )
        before = stored_rows(tx, values[3].assignment_id)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    waiting, identities = Event(), {}

    def attempt():
        with independent_database(schema) as db, db.transaction() as tx:
            identities["waiting"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            waiting.set()
            with pytest.raises(RepositoryConflictError):
                getattr(repo, method + "_action_for_execution")(tx, **arguments)
            assert stored_rows(tx, values[3].assignment_id) == before
            values[0].mark_resumed(
                tx,
                owner_id="owner",
                session_id=values[1].session_id,
                expected_incarnation_id=values[1].incarnation_id,
                expected_resumed=False,
                resumed=True,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            tx.fetch_one(
                "SELECT id FROM persistent_assignment_action WHERE id=%s FOR UPDATE",
                (arguments["action_id"],),
            )
            future = pool.submit(attempt)
            assert waiting.wait(3)
            _wait_for_lock(tx, identities["waiting"], pid)
            tx.fetch_one("SELECT pg_sleep(1.55)")
        future.result(timeout=5)
    with database.transaction() as tx:
        assert stored_rows(tx, values[3].assignment_id) == before
        assert values[0].get(tx, owner_id="owner", session_id=values[1].session_id).resumed


@pytest.mark.parametrize("operation_name", ["logout", "refresh"])
@pytest.mark.postgresql_lock_catalog
def test_guard_holds_exact_session_until_commit_before_logout_or_rotation(
    database, repo, operation_name
):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        values = operation(tx, repo)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    waiting, identities = Event(), {}

    def mutate_session():
        with independent_database(schema) as db, db.transaction() as tx:
            identities["waiting"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            waiting.set()
            if operation_name == "logout":
                return values[0].delete_and_return(
                    tx,
                    owner_id="owner",
                    session_id=values[1].session_id,
                    expected_incarnation_id=values[1].incarnation_id,
                )
            return values[0].compare_and_set_refresh(
                tx,
                replace(values[1], last_refresh_at=values[1].last_refresh_at + 1),
                expected_last_refresh_at=values[1].last_refresh_at,
                expected_credential=values[2].credential,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            guard(tx, repo, values)
            future = pool.submit(mutate_session)
            assert waiting.wait(3)
            _wait_for_lock(tx, identities["waiting"], pid)
            assert values[0].get(tx, owner_id="owner", session_id=values[1].session_id) == values[1]
        assert future.result(timeout=5) is not None
    with database.transaction() as tx, pytest.raises(RepositoryConflictError):
        guard(tx, repo, values)
