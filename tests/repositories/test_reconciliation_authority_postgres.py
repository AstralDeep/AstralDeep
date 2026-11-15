"""Real-PostgreSQL tests for astralplane.repositories.assignments, audit, and history:
reconciliation settles liabilities using only the original session's authority, with
sorted action locks that avoid deadlocking a concurrent result reader.
"""

import hmac
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest
from test_assignment_execution_guard_postgres import _reset, _wait_for_lock
from test_assignments_postgres import action as make_action
from test_assignments_postgres import (
    claim_operations,
    independent_database,
    parallel_transactions,
    reserve,
    start,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_control_postgres import current, mutate
from test_operation_terminal_postgres import command_args, expire_authority
from test_session_execution_postgres import operation

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.assignments import (
    AssignmentActionDecision,
    AssignmentActionOutcome,
    AssignmentActionReconciliation,
    AssignmentTask,
    canonical,
    digest,
    plain,
)
from astralplane.repositories.audit import AuditEvent, AuditRepository
from astralplane.repositories.history import (
    SessionConsentObservation,
    SessionExecutionObservation,
    SessionRepository,
)


def uncertain(tx, repo):
    values = operation(tx, repo, issued=True)
    _, _, observation, original, claim, _, _, binding, permit = values
    action = repo.get_action(
        tx, owner_id="owner", assignment_id=original.assignment_id, action_id=permit.action_id
    )
    observed = AssignmentActionOutcome("uncertain", digest("unresolved transport"), {})
    repo.record_action_outcome(
        tx,
        owner_id="owner",
        assignment_id=original.assignment_id,
        action_id=action.action_id,
        attempt_id=permit.attempt_id,
        dispatch_token=permit.dispatch_token,
        expected_request_digest=permit.request_digest,
        outcome=observed,
        result_fence=claim.fence,
        result_binding=binding,
        result_authority=observation,
    )
    record = current(repo, tx, original)
    decision = AssignmentActionReconciliation(
        observed.result_digest,
        "confirmed_applied",
        "verified:external-receipt",
        uid(),
        digest("owner-reconciliation"),
    )
    return values, record, action, decision


def test_missing_original_session_settles_without_waking(tx, repo):
    values, record, action, decision = uncertain(tx, repo)
    sessions, session = values[:2]
    sessions.delete(
        tx,
        session_id=session.session_id,
        owner_id="owner",
        expected_incarnation_id=session.incarnation_id,
    )
    args = command_args(record, action, decision)
    result = repo.reconcile_action(tx, **args)
    after = current(repo, tx, record)
    assert result.state == "succeeded" and result.result["result"] == {}
    assert after.usage["outstanding"]["tool_calls"] == 0
    assert after.usage["spent"]["tool_calls"] == 1
    assert after.next_wake_at is None
    assert after.wake_generation == record.wake_generation
    assert after.phase == "waiting_authorization"
    assert repo.reconcile_action(tx, **args) == result
    assert current(repo, tx, record) == after


def test_prepare_is_read_only_and_final_reconciliation_uses_original_observation(tx, repo):
    values, record, action, decision = uncertain(tx, repo)
    args = dict(command_args(record, action, decision), authority=values[2])
    prepare = getattr(repo, "prepare_action_reconciliation", None)
    assert callable(prepare), "owner reconciliation needs a public read/lock-only preparation"
    prepared = prepare(tx, **args)
    assert prepared.assignment == record and prepared.action.state == "uncertain"
    assert prepared.replayed is False
    assert current(repo, tx, record) == record
    result = repo.reconcile_action(tx, **args)
    after = current(repo, tx, record)
    assert result.result["result_available"] is False
    assert after.phase == "waiting" and after.next_wake_at is not None
    assert after.wake_generation == record.wake_generation + 1
    assert prepare(tx, **args).replayed is True
    assert repo.reconcile_action(tx, **args) == result
    assert current(repo, tx, record) == after


@pytest.mark.parametrize(
    "loss",
    [
        "absent",
        "expired",
        "wrong_type",
        "consent",
        "deleted",
        "replacement",
        "rotated",
        "other_owner",
        "owner_retired",
        "authority_deadline",
        "task_deadline",
        "revoked",
    ],
)
@pytest.mark.parametrize("verdict", ["confirmed_applied", "confirmed_not_applied"])
def test_authority_loss_preserves_once_only_charge_and_payload_free_receipt(
    tx, repo, loss, verdict
):
    values, record, action, decision = uncertain(tx, repo)
    sessions, session, observation = values[:3]
    decision = replace(decision, decision=verdict)
    if loss == "absent":
        observation = None
    elif loss == "expired":
        observation = replace(
            observation,
            started_at=observation.started_at - timedelta(seconds=30),
            valid_until=observation.started_at - timedelta(seconds=15),
        )
    elif loss == "wrong_type":
        observation = {"credential": observation.credential}
    elif loss == "consent":
        observation = SessionConsentObservation(
            observation.credential, observation.started_at, observation.valid_until
        )
    elif loss in {"deleted", "replacement"}:
        sessions.delete(
            tx,
            owner_id="owner",
            session_id=session.session_id,
            expected_incarnation_id=session.incarnation_id,
        )
        if loss == "replacement":
            replacement = sessions.put(tx, replace(session, incarnation_id=None))
            state = sessions.get_execution_state(
                tx, owner_id="owner", session_id=session.session_id
            )
            assert replacement.incarnation_id != session.incarnation_id
            observation = SessionExecutionObservation(
                state.credential, state.observed_at, state.observed_at + timedelta(seconds=15)
            )
    elif loss == "rotated":
        sessions.compare_and_set_refresh(
            tx,
            replace(
                session,
                access_token_ciphertext="new-access",
                refresh_token_ciphertext="new-refresh",
                last_refresh_at=session.last_refresh_at + 1,
            ),
            expected_last_refresh_at=session.last_refresh_at,
        )
    elif loss == "other_owner":
        observation = replace(
            observation, credential=replace(observation.credential, owner_id="other")
        )
    elif loss == "owner_retired":
        expire_authority(tx, record, "retired")
    elif loss in {"authority_deadline", "task_deadline"}:
        expire_authority(tx, record, "authority" if loss == "authority_deadline" else "deadline")
    else:
        expire_authority(tx, record, "revoked")
    before = current(repo, tx, record)
    args = dict(command_args(before, action, decision), authority=observation)
    prepared = repo.prepare_action_reconciliation(tx, **args)
    assert not prepared.replayed
    assert current(repo, tx, record) == before
    settled = repo.reconcile_action(tx, **args)
    after = current(repo, tx, record)
    assert after.usage["spent"]["tool_calls"] == 1
    assert after.usage["outstanding"]["tool_calls"] == 0
    assert after.wake_generation == before.wake_generation and after.next_wake_at is None
    assert after.checkpoint == before.checkpoint
    assert settled.result["result"] == {} and settled.result["result_available"] is False
    assert repo.reconcile_action(tx, **dict(args, authority=values[2])) == settled
    assert current(repo, tx, record) == after
    assert claim_operations(repo, tx, worker_id="must-not-wake") == ()


@pytest.mark.parametrize(
    "hold", ["reserved", "started", "uncertain", "proposed", "approved", "task", "event", "budget"]
)
def test_another_hold_cannot_be_erased_by_reconciling_one_action(tx, repo, hold):
    values, record, target, decision = uncertain(tx, repo)
    if hold in {"event", "budget", "task"}:

        def change(data):
            if hold == "event":
                data["operation"]["control"] = {
                    "version": 1,
                    "wait": {"event_key": "new-source", "source_revision": 4},
                    "watermarks": {},
                    "wake_receipts": {},
                }
            elif hold == "budget":
                data["phase"] = "budget_exhausted"
            else:
                data["tasks"] = [
                    plain(
                        AssignmentTask(
                            "item", "plan", 1, "Title", "Inspect", (), state="reconciliation"
                        )
                    )
                ]

        mutate(tx, record, change)
    else:
        sensitive = hold in {"proposed", "approved"}
        extra = make_action(
            repo,
            tx,
            values[4].fence,
            sensitivity="sensitive" if sensitive else "ordinary",
            approval_expires_at=values[2].started_at + timedelta(minutes=1) if sensitive else None,
        )
        if hold == "approved":
            observed = current(repo, tx, record)
            repo.decide_action(
                tx,
                owner_id="owner",
                assignment_id=record.assignment_id,
                action_id=extra.action_id,
                expected_instruction_revision=observed.instruction_revision,
                expected_control_epoch=observed.control_epoch,
                expected_state_version=observed.state_version,
                decision=AssignmentActionDecision(
                    extra.intent.request_digest,
                    "approve",
                    uid(),
                    digest("approval"),
                    extra.intent.permission_digest,
                    extra.intent.precondition_digest,
                ),
            )
        elif not sensitive:
            reservation = reserve(repo, tx, values[4].fence, extra)
            if hold in {"started", "uncertain"}:
                permit = start(repo, tx, values[4].fence, reservation, values[7])
                if hold == "uncertain":
                    repo.record_action_outcome(
                        tx,
                        owner_id="owner",
                        assignment_id=record.assignment_id,
                        action_id=extra.action_id,
                        attempt_id=permit.attempt_id,
                        dispatch_token=permit.dispatch_token,
                        expected_request_digest=permit.request_digest,
                        outcome=AssignmentActionOutcome("uncertain", digest("second-unknown"), {}),
                        result_authority=values[2],
                        result_fence=values[4].fence,
                        result_binding=values[7],
                    )
        extra_before = repo.get_action(
            tx, owner_id="owner", assignment_id=record.assignment_id, action_id=extra.action_id
        )
    before = current(repo, tx, record)
    repo.reconcile_action(tx, **command_args(before, target, decision), authority=values[2])
    after = current(repo, tx, record)
    assert after.next_wake_at is None and after.wake_generation == before.wake_generation
    assert after.usage["spent"]["tool_calls"] == 1
    assert after.usage["outstanding"]["tool_calls"] == before.usage["outstanding"]["tool_calls"] - 1
    if hold not in {"event", "budget", "task"}:
        assert (
            repo.get_action(
                tx, owner_id="owner", assignment_id=record.assignment_id, action_id=extra.action_id
            )
            == extra_before
        )


@pytest.mark.parametrize("method", ["prepare_action_reconciliation", "reconcile_action"])
@pytest.mark.parametrize(
    "change",
    [
        "decision_type",
        "decision",
        "prior",
        "evidence",
        "digest",
        "submission",
        "revision",
        "epoch",
        "state",
        "owner",
        "action",
    ],
)
def test_invalid_or_stale_decision_has_no_partial_writes(tx, repo, method, change):
    values, record, action, decision = uncertain(tx, repo)
    args = dict(command_args(record, action, decision), authority=values[2])
    if change == "decision_type":
        args["decision"] = plain(decision)
    elif change in {"decision", "prior", "evidence", "digest", "submission"}:
        key, value = {
            "decision": ("decision", "uncertain"),
            "prior": ("prior_result_digest", digest("wrong")),
            "evidence": ("evidence_reference", ""),
            "digest": ("submission_digest", "bad"),
            "submission": ("submission_id", "not-uuid"),
        }[change]
        args["decision"] = replace(decision, **{key: value})
    else:
        key, value = {
            "revision": ("expected_instruction_revision", True),
            "epoch": ("expected_control_epoch", 0),
            "state": ("expected_state_version", record.state_version - 1),
            "owner": ("owner_id", "other"),
            "action": ("action_id", uid()),
        }[change]
        args[key] = value
    before = snapshot(tx)
    with pytest.raises(
        (RepositoryValidationError, RepositoryConflictError, RepositoryNotFoundError)
    ):
        getattr(repo, method)(tx, **args)
    assert snapshot(tx) == before


def snapshot(tx):
    return tuple(
        plain(tx.fetch_all("SELECT * FROM " + name + " ORDER BY id"))
        for name in ["persistent_assignment", "persistent_assignment_action"]
    )


def audit_event(tx, identity):
    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    return AuditEvent(
        uid(),
        "reconciliation-tests",
        "owner",
        None,
        "settings",
        "reconcile",
        "Owner reconciliation",
        None,
        identity,
        "success",
        None,
        "{}",
        "{}",
        "[]",
        now,
        now,
        "test-key",
    )


def append_audit(tx, identity):
    return AuditRepository().append(
        tx,
        audit_event(tx, identity),
        lambda key, payload: hmac.digest(b"synthetic-audit-key", payload, "sha256"),
    )


def test_preparation_audit_and_settlement_commit_or_rollback_together(database, repo):
    with database.transaction() as tx:
        _reset(tx)
        values, record, action, decision = uncertain(tx, repo)
        before = snapshot(tx)
    args = dict(command_args(record, action, decision), authority=values[2])
    with pytest.raises(RuntimeError, match="after-audit"), database.transaction() as tx:
        prepared = repo.prepare_action_reconciliation(tx, **args)
        assert snapshot(tx) == before
        audit = append_audit(tx, record.assignment_id)
        repo.reconcile_action(tx, **args)
        raise RuntimeError("after-audit")
    with database.transaction() as tx:
        assert snapshot(tx) == before
        assert (
            AuditRepository().get(tx, chain_id=audit.event.chain_id, event_id=audit.event.event_id)
            is None
        )
        prepared = repo.prepare_action_reconciliation(tx, **args)
        assert not prepared.replayed
        receipt = append_audit(tx, record.assignment_id)
        repo.reconcile_action(tx, **args)
    with database.transaction() as tx:
        after = current(repo, tx, record)
        assert after.usage["spent"]["tool_calls"] == 1
        assert (
            AuditRepository().get(
                tx, chain_id=receipt.event.chain_id, event_id=receipt.event.event_id
            )
            == receipt
        )
        assert repo.prepare_action_reconciliation(tx, **args).replayed


@pytest.mark.parametrize("blocker", ["owner", "session", "assignment", "action", "audit", "logout"])
@pytest.mark.postgresql_lock_catalog
def test_real_lock_wait_crosses_observation_expiry_but_charge_still_commits(
    database, repo, blocker
):
    with database.transaction() as tx:
        _reset(tx)
        values, record, action, decision = uncertain(tx, repo)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
        observation = replace(values[2], valid_until=values[2].started_at + timedelta(seconds=0.5))
    args = dict(command_args(record, action, decision), authority=observation)
    ready, identities = Event(), {}

    def run():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout=3000")
            identities["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            repo.prepare_action_reconciliation(tx, **args)
            receipt = append_audit(tx, record.assignment_id)
            result = repo.reconcile_action(tx, **args)
            return receipt, result

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            if blocker == "owner":
                tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", ("owner",))
            elif blocker == "session":
                tx.fetch_one(
                    "SELECT sid FROM web_session WHERE sid=%s FOR UPDATE", (values[1].session_id,)
                )
            elif blocker == "logout":
                values[0].delete(
                    tx,
                    owner_id="owner",
                    session_id=values[1].session_id,
                    expected_incarnation_id=values[1].incarnation_id,
                )
            elif blocker == "audit":
                tx.fetch_one(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("audit_events:reconciliation-tests",),
                )
            else:
                table, identity = (
                    ("persistent_assignment", record.assignment_id)
                    if blocker == "assignment"
                    else ("persistent_assignment_action", action.action_id)
                )
                tx.fetch_one("SELECT id FROM " + table + " WHERE id=%s FOR UPDATE", (identity,))
            future = pool.submit(run)
            assert ready.wait(3)
            _wait_for_lock(tx, identities["pid"], pid)
            tx.fetch_one("SELECT pg_sleep(0.55)")
        receipt, result = future.result(timeout=8)
    with database.transaction() as tx:
        after = current(repo, tx, record)
        assert (
            after.usage["outstanding"]["tool_calls"] == 0
            and after.usage["spent"]["tool_calls"] == 1
        )
        assert after.next_wake_at is None and after.wake_generation == record.wake_generation
        assert result.result["result_available"] is False
        assert (
            AuditRepository().get(
                tx, chain_id=receipt.event.chain_id, event_id=receipt.event.event_id
            )
            == receipt
        )


def test_caught_storage_failure_rolls_back_final_settlement_savepoint(tx, repo):
    values, record, action, decision = uncertain(tx, repo)
    args = dict(command_args(record, action, decision), authority=values[2])
    repo.prepare_action_reconciliation(tx, **args)
    before = snapshot(tx)

    class FailingTransaction:
        def __getattr__(self, name):
            return getattr(tx, name)

        def execute(self, statement, parameters=()):
            result = tx.execute(statement, parameters)
            if statement.startswith("UPDATE persistent_assignment_action SET"):
                raise RuntimeError("storage-failed-after-write")
            return result

    with pytest.raises(RuntimeError, match="storage-failed-after-write"):
        repo.reconcile_action(FailingTransaction(), **args)
    assert snapshot(tx) == before
    assert repo.reconcile_action(tx, **args).state == "succeeded"


@pytest.mark.postgresql_lock_catalog
def test_final_hold_inventory_delay_cannot_extend_original_observation(tx, repo):
    values, record, action, decision = uncertain(tx, repo)
    observation = replace(values[2], valid_until=values[2].started_at + timedelta(seconds=0.2))

    class DelayedInventory:
        def __init__(self):
            self.settled = False

        def __getattr__(self, name):
            return getattr(tx, name)

        def execute(self, statement, parameters=()):
            result = tx.execute(statement, parameters)
            if statement.startswith("UPDATE persistent_assignment_action SET"):
                self.settled = True
            return result

        def fetch_all(self, statement, parameters=()):
            if self.settled and "persistent_assignment_action" in statement:
                self.settled = False
                tx.fetch_one("SELECT pg_sleep(0.25)")
            return tx.fetch_all(statement, parameters)

    repo.reconcile_action(
        DelayedInventory(), **command_args(record, action, decision), authority=observation
    )
    after = current(repo, tx, record)
    assert after.usage["spent"]["tool_calls"] == 1
    assert after.next_wake_at is None and after.wake_generation == record.wake_generation


@pytest.mark.postgresql_lock_catalog
def test_sorted_action_locks_do_not_deadlock_with_an_existing_result_reader(database, repo):
    from unittest.mock import patch
    from uuid import UUID

    from astralplane.repositories import assignments

    with database.transaction() as tx:
        _reset(tx)
        values, record, action, decision = uncertain(tx, repo)
        lower_id = "00000000-0000-4000-8000-000000000001"
        with patch.object(assignments.uuid, "uuid4", return_value=UUID(lower_id)):
            lower = repo.put_action(
                tx,
                fence=values[4].fence,
                intent=replace(action.intent, action_key="earlier-result-reader-lock"),
            )
        assert lower.action_id < action.action_id
        record = current(repo, tx, record)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    args = dict(command_args(record, action, decision), authority=values[2])
    ready, state = Event(), {}

    def run():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout=3000")
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            prepared = repo.prepare_action_reconciliation(tx, **args)
            assert not prepared.replayed
            return repo.reconcile_action(tx, **args)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout=1000")
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            repo.get_action(
                tx, owner_id="owner", assignment_id=record.assignment_id, action_id=lower_id
            )
            future = pool.submit(run)
            assert ready.wait(3)
            _wait_for_lock(tx, state["pid"], pid)
            assert (
                repo.get_action(
                    tx,
                    owner_id="owner",
                    assignment_id=record.assignment_id,
                    action_id=action.action_id,
                ).state
                == "uncertain"
            )
        assert future.result(timeout=5).state == "succeeded"


@pytest.mark.postgresql_lock_catalog
def test_production_sql_wait_cap_refuses_without_partial_writes_and_retry_settles(database, repo):
    import psycopg2

    with database.transaction() as tx:
        _reset(tx)
        values, record, action, decision = uncertain(tx, repo)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
        before = snapshot(tx)
    args = dict(command_args(record, action, decision), authority=values[2])
    ready, state = Event(), {}

    def run():
        with (
            independent_database(schema) as db,
            pytest.raises(psycopg2.errors.LockNotAvailable),
            db.transaction() as tx,
        ):
            SessionRepository.bound_request_execution_waits(tx)
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            repo.prepare_action_reconciliation(tx, **args)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            tx.fetch_one(
                "SELECT id FROM persistent_assignment_action WHERE id=%s FOR UPDATE",
                (action.action_id,),
            )
            future = pool.submit(run)
            assert ready.wait(3)
            _wait_for_lock(tx, state["pid"], pid)
            future.result(timeout=3)
        with database.transaction() as tx:
            assert snapshot(tx) == before
            SessionRepository.bound_request_execution_waits(tx)
            repo.prepare_action_reconciliation(tx, **args)
            repo.reconcile_action(tx, **args)
        with database.transaction() as tx:
            assert current(repo, tx, record).usage["spent"]["tool_calls"] == 1


@pytest.mark.parametrize("mutation", ["missing_permit", "attempt_state", "unknown_action_field"])
def test_malformed_target_cannot_claim_to_be_an_authentic_liability(tx, repo, mutation):
    values, record, action, decision = uncertain(tx, repo)
    data = plain(
        tx.fetch_one(
            "SELECT data FROM persistent_assignment_action WHERE id=%s", (action.action_id,)
        )["data"]
    )
    if mutation == "missing_permit":
        data["attempts"][-1]["dispatch_token"] = None
    elif mutation == "attempt_state":
        data["attempts"][-1]["state"] = "reserved"
    else:
        data["foreign_receipt"] = "not-authentic"
    tx.execute(
        "UPDATE persistent_assignment_action SET data=%s::jsonb WHERE id=%s",
        (canonical(data), action.action_id),
    )
    before = snapshot(tx)
    with pytest.raises(RepositoryDataError):
        repo.prepare_action_reconciliation(
            tx, **command_args(record, action, decision), authority=values[2]
        )
    with pytest.raises(RepositoryDataError):
        repo.reconcile_action(tx, **command_args(record, action, decision), authority=values[2])
    assert snapshot(tx) == before


@pytest.mark.parametrize("method", ["prepare_action_reconciliation", "reconcile_action"])
def test_mapping_verdict_is_a_closed_validation_refusal(tx, repo, method):
    values, record, action, decision = uncertain(tx, repo)
    before = snapshot(tx)
    with pytest.raises(RepositoryValidationError):
        getattr(repo, method)(
            tx, **command_args(record, action, replace(decision, decision={})), authority=values[2]
        )
    assert snapshot(tx) == before


def test_concurrent_reconciliation_preparations_append_one_audit_and_charge_once(database, repo):
    with database.transaction() as tx:
        _reset(tx)
        values, record, action, decision = uncertain(tx, repo)
    args = dict(command_args(record, action, decision), authority=values[2])

    def reconcile(tx):
        prepared = repo.prepare_action_reconciliation(tx, **args)
        receipt = None if prepared.replayed else append_audit(tx, record.assignment_id)
        result = repo.reconcile_action(tx, **args)
        return prepared.replayed, receipt, result

    outcomes = parallel_transactions(database, (reconcile, reconcile))
    assert sorted(item[0] for item in outcomes) == [False, True]
    assert sum(item[1] is not None for item in outcomes) == 1
    assert outcomes[0][2] == outcomes[1][2]
    with database.transaction() as tx:
        after = current(repo, tx, record)
        assert after.usage["spent"]["tool_calls"] == 1
        assert after.usage["outstanding"]["tool_calls"] == 0
        assert after.wake_generation == record.wake_generation + 1


def test_successful_continuation_clears_obsolete_retry_and_error_disposition(tx, repo):
    values, record, action, decision = uncertain(tx, repo)
    mutate(
        tx,
        record,
        lambda data: data.update(
            next_retry_at=plain(values[2].started_at + timedelta(seconds=45)),
            safe_error_code="prior_uncertain_transport",
        ),
    )
    before = current(repo, tx, record)
    args = dict(command_args(before, action, decision), authority=values[2])
    repo.prepare_action_reconciliation(tx, **args)
    repo.reconcile_action(tx, **args)
    read = repo.get_operation(tx, owner_id="owner", assignment_id=record.assignment_id)
    assert read.assignment.phase == "waiting"
    assert read.assignment.safe_error_code is None
    assert (
        tx.fetch_one(
            "SELECT data->'next_retry_at' AS retry FROM persistent_assignment WHERE id=%s",
            (record.assignment_id,),
        )["retry"]
        is None
    )
