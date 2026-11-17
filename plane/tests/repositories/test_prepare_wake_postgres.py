"""Real-PostgreSQL tests for astralplane.repositories.assignments and audit: wake
preparation is read-only until an exact receipt is accepted, never substitutes for
original session authority, and rechecks prerequisites after every real lock wait.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from threading import Event

import pytest
from test_assignment_execution_guard_postgres import _reset, _wait_for_lock
from test_assignments_postgres import (
    action,
    create,
    create_operation,
    independent_database,
    parallel_transactions,
    reserve,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_control_postgres import (
    control,
    current,
    mutate,
    operation_claim,
    wait,
    wake_args,
)
from test_operation_terminal_postgres import change_action, command_args, expire_authority
from test_owner_event_wait_postgres import arguments as wait_args
from test_reconciliation_authority_postgres import append_audit, uncertain
from test_session_execution_postgres import operation, stored_rows

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.assignments import (
    AssignmentTask,
    AssignmentWakePreparation,
    digest,
    plain,
)
from astralplane.repositories.audit import AuditRepository


def waiting(tx, repo):
    create_operation(repo, tx)
    return wait(repo, tx, operation_claim(repo, tx))


def clear_args(record, **changes):
    args = dict(
        owner_id=record.owner_id,
        assignment_id=record.assignment_id,
        expected_state_version=record.state_version,
        expected_instruction_revision=record.instruction_revision,
        expected_control_epoch=record.control_epoch,
    )
    args.update(changes)
    return args


def test_continuation_clear_is_read_only_and_never_claims(tx, repo):
    record = waiting(tx, repo)
    before = stored_rows(tx, record.assignment_id)
    check = getattr(repo, "assert_operation_continuation_clear", None)
    assert callable(check), "host needs factual liability check before accepting a new wake"
    assert check(tx, **clear_args(record)) == record
    assert stored_rows(tx, record.assignment_id) == before


def test_prepare_is_read_only_and_exact_receipt_precedes_stale_counters(tx, repo):
    record = waiting(tx, repo)
    args = wake_args(record)
    rows = stored_rows(tx, record.assignment_id)
    prepare = getattr(repo, "prepare_wake", None)
    assert callable(prepare), "wake requires public read-only receipt preparation"
    prepared = prepare(tx, **args)
    assert prepared.assignment == record and prepared.replayed is False
    assert stored_rows(tx, record.assignment_id) == rows
    accepted = repo.accept_wake(tx, **args)
    assert accepted.applied and accepted.assignment.phase == "waiting"
    stopped = control(repo, tx, accepted.assignment, "stop").assignment
    rows = stored_rows(tx, record.assignment_id)
    replay = prepare(tx, **args)
    assert replay.replayed is True and replay.assignment == stopped
    assert repo.accept_wake(tx, **args).applied is False
    assert current(repo, tx, record) == stopped
    assert stored_rows(tx, record.assignment_id) == rows


def test_prepared_facts_are_detached_immutable_and_hidden_in_repr(tx, repo):
    record = waiting(tx, repo)
    prepared = repo.prepare_wake(tx, **wake_args(record))
    assert type(prepared) is AssignmentWakePreparation
    assert repr(prepared) == "AssignmentWakePreparation(replayed=False)"
    with pytest.raises(FrozenInstanceError):
        prepared.replayed = True
    with pytest.raises(TypeError):
        prepared.assignment.operation["control"]["wait"]["event_key"] = "other"
    assert current(repo, tx, record) == record


@pytest.mark.parametrize("method", ["prepare_wake", "accept_wake"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_state_version", True),
        ("expected_instruction_revision", 1.0),
        ("expected_control_epoch", 0),
        ("control_version", 2),
        ("control_version", False),
        ("event_id", ""),
        ("event_id", "x" * 129),
        ("event_key", None),
        ("event_key", "x" * 129),
        ("source_revision", -1),
        ("source_revision", True),
        ("event_digest", "not-a-digest"),
        ("assignment_id", "not-a-uuid"),
        ("owner_id", ""),
    ],
)
def test_even_an_accepted_receipt_requires_well_formed_arguments(tx, repo, method, field, value):
    record = waiting(tx, repo)
    args = wake_args(record)
    repo.accept_wake(tx, **args)
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryValidationError):
        getattr(repo, method)(tx, **dict(args, **{field: value}))
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize("loss", ["stop", "authority", "deadline", "retired", "capacity"])
def test_exact_accepted_replay_precedes_new_continuation_prerequisites(tx, repo, loss):
    record = waiting(tx, repo)
    args = wake_args(record)
    accepted = repo.accept_wake(tx, **args).assignment
    if loss == "stop":
        control(repo, tx, accepted, "stop")
    elif loss == "capacity":
        mutate(
            tx,
            record,
            lambda d: d["operation"]["control"]["wake_receipts"].update(
                {f"other-{i}": digest(i) for i in range(127)}
            ),
        )
    else:
        expire_authority(tx, record, loss)
    before = stored_rows(tx, record.assignment_id)
    changed_counters = dict(
        args, expected_instruction_revision=77, expected_control_epoch=88, expected_state_version=99
    )
    prepared = repo.prepare_wake(tx, **changed_counters)
    assert prepared.replayed is True
    assert prepared.assignment == current(repo, tx, record)
    assert repo.accept_wake(tx, **changed_counters).applied is False
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize(
    "change",
    [
        {"event_key": "other"},
        {"source_revision": 3},
        {"event_digest": digest("other")},
    ],
)
def test_same_event_identity_with_different_content_is_conflict(tx, repo, change):
    record = waiting(tx, repo)
    args = wake_args(record)
    repo.accept_wake(tx, **args)
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryConflictError, match="assignment_idempotency_conflict"):
        repo.prepare_wake(tx, **dict(args, **change))
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize(
    "change,code",
    [
        ({"expected_instruction_revision": 77}, "assignment_revision_conflict"),
        ({"expected_control_epoch": 77}, "assignment_revision_conflict"),
        ({"expected_state_version": 77}, "assignment_revision_conflict"),
        ({"event_key": "other"}, "assignment_event_key_conflict"),
        ({"source_revision": 1}, "assignment_event_revision_conflict"),
    ],
)
def test_new_event_observation_must_match_current_wait(tx, repo, change, code):
    record = waiting(tx, repo)
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryConflictError, match=code):
        repo.prepare_wake(tx, **wake_args(record, **change))
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize("owner,missing", [("other", False), ("owner", True)])
def test_private_receipt_never_crosses_owner_or_absent_identity(tx, repo, owner, missing):
    record = waiting(tx, repo)
    repo.accept_wake(tx, **wake_args(record))
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryNotFoundError, match="assignment_not_found"):
        repo.prepare_wake(
            tx,
            **wake_args(
                record, owner_id=owner, assignment_id=uid() if missing else record.assignment_id
            ),
        )
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize(
    "loss,code",
    [
        ("authority", "assignment_authorization_unavailable"),
        ("deadline", "assignment_deadline_exceeded"),
        ("retired", "assignment_owner_retired"),
        ("paused", "assignment_not_waiting"),
        ("watermark", "assignment_event_revision_conflict"),
        ("capacity", "assignment_history_capacity_exhausted"),
        ("no_wait", "assignment_event_key_conflict"),
    ],
)
def test_final_accept_rechecks_prerequisites_after_preparation(tx, repo, loss, code):
    record = waiting(tx, repo)
    args = wake_args(record)
    assert repo.prepare_wake(tx, **args).replayed is False
    if loss in {"authority", "deadline", "retired"}:
        expire_authority(tx, record, loss)
    else:

        def change(data):
            if loss == "paused":
                data["phase"] = "waiting_authorization"
            elif loss == "watermark":
                data["operation"]["control"]["watermarks"]["source-observation"] = 2
            elif loss == "no_wait":
                data["operation"]["control"]["wait"] = None
            else:
                data["operation"]["control"]["wake_receipts"] = {
                    str(i): digest(i) for i in range(128)
                }

        mutate(tx, record, change)
    before = stored_rows(tx, record.assignment_id)
    for method in (repo.prepare_wake, repo.accept_wake):
        with pytest.raises(RepositoryConflictError, match=code):
            method(tx, **args)
        assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize("version", ["v1", "operation", "control", "checkpoint", "malformed"])
@pytest.mark.parametrize("accepted", [False, True])
def test_unknown_or_corrupt_rows_are_not_reinterpreted_even_for_replay(tx, repo, version, accepted):
    record = waiting(tx, repo)
    args = wake_args(record)
    if accepted:
        repo.accept_wake(tx, **args)

    def change(data):
        if version in {"v1", "operation", "malformed"}:
            data["operation"]["version"] = {"v1": 1, "operation": 3, "malformed": True}[version]
            if version == "v1":
                data["operation"]["authority"].update(reference_kind="session", reference_id="old")
        elif version == "control":
            data["operation"]["control"] = {"version": 2}
        else:
            data["checkpoint"] = {"schema_version": 2}

    mutate(tx, record, change)
    before = stored_rows(tx, record.assignment_id)
    if version == "v1" and accepted:
        assert repo.prepare_wake(tx, **args).replayed is True
    else:
        with pytest.raises(
            RepositoryDataError if version == "malformed" else RepositoryConflictError
        ):
            repo.prepare_wake(tx, **args)
    assert stored_rows(tx, record.assignment_id) == before


def test_persistent_work_is_not_a_wake_operation(tx, repo):
    record = create(repo, tx)
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryConflictError, match="assignment_operation_required"):
        repo.prepare_wake(tx, **wake_args(record))
    assert stored_rows(tx, record.assignment_id) == before


def test_unresolved_issued_liability_refuses_normal_event_wake(tx, repo):
    values, record, _, _ = uncertain(tx, repo)
    held = repo.set_owner_event_wait(tx, **wait_args(record)).assignment
    before = stored_rows(tx, record.assignment_id)
    assert held.phase == "reconciliation"
    with pytest.raises(RepositoryConflictError, match="assignment_not_waiting"):
        repo.prepare_wake(tx, **wake_args(held, source_revision=4))
    assert stored_rows(tx, record.assignment_id) == before
    assert held.usage["outstanding"]["tool_calls"] == 1
    assert values[8].attempt_id


def test_read_preparation_does_not_substitute_for_original_session_authority(tx, repo):
    values = operation(tx, repo)
    sessions, session, observation, record = values[:4]
    held = repo.set_owner_event_wait(tx, **wait_args(current(repo, tx, record))).assignment
    args = wake_args(held, source_revision=4)
    sessions.delete(
        tx,
        owner_id="owner",
        session_id=session.session_id,
        expected_incarnation_id=session.incarnation_id,
    )
    before = stored_rows(tx, record.assignment_id)
    assert repo.prepare_wake(tx, **args).replayed is False
    assert stored_rows(tx, record.assignment_id) == before
    with pytest.raises(RepositoryConflictError):
        sessions.assert_current_execution(tx, observation=observation)
    repo.accept_wake(tx, **args)
    after = current(repo, tx, record)
    with pytest.raises(RepositoryConflictError, match="assignment_authorization_unavailable"):
        repo.claim_operation_for_administration(
            tx,
            owner_id="owner",
            assignment_id=record.assignment_id,
            expected_state_version=after.state_version,
            worker_id="no-authority",
            authority=observation,
        )


@pytest.mark.parametrize("conflicting", [False, True])
def test_concurrent_preparation_audits_and_accepts_one_event(database, repo, conflicting):
    with database.transaction() as tx:
        _reset(tx)
        record = waiting(tx, repo)
    args = wake_args(record)

    def accept(tx, changes=None):
        selected = dict(args, **(changes or {}))
        prepared = repo.prepare_wake(tx, **selected)
        audit = None if prepared.replayed else append_audit(tx, record.assignment_id)
        return audit, repo.accept_wake(tx, **selected)

    results = parallel_transactions(
        database,
        (accept, lambda tx: accept(tx, {"event_digest": digest("other")} if conflicting else {})),
    )
    successful = [r for r in results if not isinstance(r, RepositoryConflictError)]
    assert sum(audit is not None for audit, _ in successful) == 1
    assert sum(result.applied for _, result in successful) == 1
    if conflicting:
        assert len(successful) == 1
        assert any(isinstance(r, RepositoryConflictError) for r in results)
    else:
        assert successful[0][1].assignment == successful[1][1].assignment
    with database.transaction() as tx:
        after = current(repo, tx, record)
        assert after.state_version == record.state_version + 1
        assert after.wake_generation == record.wake_generation + 1
        assert len(after.operation["control"]["wake_receipts"]) == 1


@pytest.mark.parametrize("boundary", ["audit_failure", "final_state", "final_session"])
def test_audit_and_wake_roll_back_with_required_host_failure(database, repo, boundary):
    with database.transaction() as tx:
        _reset(tx)
        values = operation(tx, repo)
        sessions, session, observation, original = values[:4]
        record = repo.set_owner_event_wait(tx, **wait_args(current(repo, tx, original))).assignment
        before = stored_rows(tx, record.assignment_id)
    args = wake_args(record, source_revision=4)
    with pytest.raises((RuntimeError, RepositoryConflictError)), database.transaction() as tx:
        sessions.assert_current_execution(tx, observation=observation)
        prepared = repo.prepare_wake(tx, **args)
        assert prepared.replayed is False
        audit = append_audit(tx, record.assignment_id)
        if boundary == "audit_failure":
            raise RuntimeError("required host audit delivery failed")
        if boundary == "final_state":
            control(repo, tx, record)
        repo.accept_wake(tx, **args)
        sessions.delete(
            tx,
            owner_id="owner",
            session_id=session.session_id,
            expected_incarnation_id=session.incarnation_id,
        )
        sessions.assert_current_execution(tx, observation=observation)
    with database.transaction() as tx:
        assert stored_rows(tx, record.assignment_id) == before
        assert (
            AuditRepository().get(
                tx,
                chain_id=audit.event.chain_id,
                event_id=audit.event.event_id,
            )
            is None
        )
        assert sessions.get(tx, owner_id="owner", session_id=session.session_id) is not None


@pytest.mark.parametrize(
    "blocker,loss",
    [
        ("owner", "retired"),
        ("owner", "deadline"),
        ("owner", "authority"),
        ("assignment", "deadline"),
        ("assignment", "authority"),
    ],
)
@pytest.mark.postgresql_lock_catalog
def test_new_wake_rechecks_committed_loss_after_real_lock_wait(database, repo, blocker, loss):
    with database.transaction() as tx:
        _reset(tx)
        record = waiting(tx, repo)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    args = wake_args(record)
    ready, identities = Event(), {}

    def prepare():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout=3000")
            identities["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            return repo.prepare_wake(tx, **args)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            if blocker == "owner":
                tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", ("owner",))
            else:
                tx.fetch_one(
                    "SELECT id FROM persistent_assignment WHERE id=%s FOR UPDATE",
                    (record.assignment_id,),
                )
            future = pool.submit(prepare)
            assert ready.wait(3)
            _wait_for_lock(tx, identities["pid"], pid)
            expire_authority(tx, record, loss)
        with pytest.raises(RepositoryConflictError):
            future.result(timeout=6)
    with database.transaction() as tx:
        assert current(repo, tx, record).operation["control"]["wake_receipts"] == {}


@pytest.mark.parametrize(
    "hold",
    ["reserved", "started", "uncertain", "opaque", "proposed", "approved", "outstanding", "task"],
)
def test_continuation_clear_refuses_real_ledger_holds_without_changing_them(tx, repo, hold):
    if hold == "uncertain":
        values, record, _, _ = uncertain(tx, repo)
    else:
        values = operation(tx, repo, issued=hold in {"started", "opaque"})
        if hold in {"reserved", "proposed", "approved"}:
            item = action(repo, tx, values[4].fence)
            if hold == "reserved":
                reserve(repo, tx, values[4].fence, item)
            else:
                change_action(tx, item.action_id, lambda d: d.update(state=hold))
        elif hold == "opaque":
            change_action(tx, values[8].action_id, lambda d: d.update(future_receipt_version=2))
        elif hold == "outstanding":
            mutate(tx, values[3], lambda d: d["usage"]["outstanding"].update(tool_calls=1))
        elif hold == "task":
            mutate(
                tx,
                values[3],
                lambda d: d.update(
                    tasks=[
                        plain(
                            AssignmentTask(
                                "unresolved-task",
                                "plan",
                                1,
                                "Task",
                                "Read",
                                (),
                                state="reconciliation",
                            )
                        )
                    ]
                ),
            )
        record = current(repo, tx, values[3])
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryConflictError, match="assignment_action_uncertain"):
        repo.assert_operation_continuation_clear(tx, **clear_args(record))
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_state_version", True),
        ("expected_control_epoch", 1.0),
        ("expected_instruction_revision", 0),
        ("owner_id", ""),
        ("assignment_id", "invalid"),
    ],
)
def test_continuation_clear_refuses_malformed_identity_and_counters(tx, repo, field, value):
    record = waiting(tx, repo)
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryValidationError):
        repo.assert_operation_continuation_clear(tx, **clear_args(record, **{field: value}))
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize(
    "field", ["expected_state_version", "expected_control_epoch", "expected_instruction_revision"]
)
def test_continuation_clear_requires_exact_current_counters(tx, repo, field):
    record = waiting(tx, repo)
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryConflictError, match="assignment_revision_conflict"):
        repo.assert_operation_continuation_clear(tx, **clear_args(record, **{field: 99}))
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize("owner,missing", [("other", False), ("owner", True)])
def test_continuation_clear_keeps_missing_and_foreign_ownership_private(tx, repo, owner, missing):
    record = waiting(tx, repo)
    with pytest.raises(RepositoryNotFoundError, match="assignment_not_found"):
        repo.assert_operation_continuation_clear(
            tx,
            **clear_args(
                record, owner_id=owner, assignment_id=uid() if missing else record.assignment_id
            ),
        )


@pytest.mark.parametrize("loss", ["persistent", "v1", "future", "retired", "authority", "deadline"])
def test_continuation_clear_checks_profile_version_owner_and_stored_deadlines(tx, repo, loss):
    record = create(repo, tx) if loss == "persistent" else waiting(tx, repo)
    if loss in {"retired", "authority", "deadline"}:
        expire_authority(tx, record, loss)
    elif loss in {"v1", "future"}:

        def change(data):
            data["operation"]["version"] = 1 if loss == "v1" else 3
            if loss == "v1":
                data["operation"]["authority"].update(reference_kind="session", reference_id="old")

        mutate(tx, record, change)
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryConflictError):
        repo.assert_operation_continuation_clear(tx, **clear_args(record))
    assert stored_rows(tx, record.assignment_id) == before


def test_factual_reconciliation_clears_liability_without_granting_a_wake(tx, repo):
    values, record, item, decision = uncertain(tx, repo)
    with pytest.raises(RepositoryConflictError, match="assignment_action_uncertain"):
        repo.assert_operation_continuation_clear(tx, **clear_args(record))
    repo.reconcile_action(tx, **command_args(record, item, decision), authority=None)
    settled = current(repo, tx, record)
    before = stored_rows(tx, record.assignment_id)
    assert repo.assert_operation_continuation_clear(tx, **clear_args(settled)) == settled
    assert settled.next_wake_at is None and settled.phase == "waiting_authorization"
    assert settled.usage["spent"]["tool_calls"] == 1
    assert settled.usage["outstanding"]["tool_calls"] == 0
    assert stored_rows(tx, record.assignment_id) == before
    assert values[8].dispatch_token


@pytest.mark.postgresql_lock_catalog
def test_continuation_clear_locks_actions_in_sorted_order(database, repo):
    with database.transaction() as tx:
        _reset(tx)
        values = operation(tx, repo)
        first = action(repo, tx, values[4].fence)
        second = repo.put_action(
            tx, fence=values[4].fence, intent=replace(first.intent, action_key="second")
        )
        lower, higher = sorted([first, second], key=lambda a: a.action_id)
        record = current(repo, tx, values[3])
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    ready, state = Event(), {}

    def check():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout=3000")
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            return repo.assert_operation_continuation_clear(tx, **clear_args(record))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout=1000")
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            repo.get_action(
                tx, owner_id="owner", assignment_id=record.assignment_id, action_id=lower.action_id
            )
            future = pool.submit(check)
            assert ready.wait(3)
            _wait_for_lock(tx, state["pid"], pid)
            assert (
                repo.get_action(
                    tx,
                    owner_id="owner",
                    assignment_id=record.assignment_id,
                    action_id=higher.action_id,
                ).state
                == "ready"
            )
        assert future.result(timeout=6) == record


@pytest.mark.parametrize("expiry", ["authority", "deadline"])
@pytest.mark.postgresql_lock_catalog
def test_continuation_clear_checks_db_deadline_after_action_lock_wait(database, repo, expiry):
    with database.transaction() as tx:
        _reset(tx)
        values = operation(tx, repo)
        item = action(repo, tx, values[4].fence)
        now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
        expires = now + timedelta(seconds=0.6)

        def bound(data):
            if expiry == "authority":
                data["operation"]["authority"]["expires_at"] = plain(expires)
            else:
                data["operation"]["deadline_at"] = plain(expires)

        mutate(tx, values[3], bound)
        record = current(repo, tx, values[3])
        before = stored_rows(tx, record.assignment_id)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    ready, state = Event(), {}

    def check():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout=15000")
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            return repo.assert_operation_continuation_clear(tx, **clear_args(record))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            repo.get_action(
                tx, owner_id="owner", assignment_id=record.assignment_id, action_id=item.action_id
            )
            future = pool.submit(check)
            assert ready.wait(10)
            _wait_for_lock(tx, state["pid"], pid)
            tx.fetch_one(
                "SELECT pg_sleep(GREATEST(0,extract(epoch FROM "
                "(%s::timestamptz-clock_timestamp())))+0.02)",
                (expires,),
            )
        with pytest.raises(
            RepositoryConflictError,
            match="assignment_authorization_unavailable"
            if expiry == "authority"
            else "assignment_deadline_exceeded",
        ):
            future.result(timeout=20)
    with database.transaction() as tx:
        assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.postgresql_lock_catalog
def test_concurrent_settlement_invalidates_original_preparation_counters(database, repo):
    with database.transaction() as tx:
        _reset(tx)
        _, record, item, decision = uncertain(tx, repo)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    ready, state = Event(), {}

    def check():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout=3000")
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            return repo.assert_operation_continuation_clear(tx, **clear_args(record))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            repo.prepare_action_reconciliation(tx, **command_args(record, item, decision))
            future = pool.submit(check)
            assert ready.wait(3)
            _wait_for_lock(tx, state["pid"], pid)
            repo.reconcile_action(tx, **command_args(record, item, decision))
        with pytest.raises(RepositoryConflictError, match="assignment_revision_conflict"):
            future.result(timeout=6)
    with database.transaction() as tx:
        settled = current(repo, tx, record)
        assert repo.assert_operation_continuation_clear(tx, **clear_args(settled)) == settled
        assert settled.usage["spent"]["tool_calls"] == 1 and settled.next_wake_at is None
