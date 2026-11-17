"""Real-PostgreSQL tests for astralplane.repositories.assignments: pause/resume
preserves issued liabilities without scheduling, and a due claim always rechecks
factual consumption rather than trusting a status label.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest
from test_assignment_execution_guard_postgres import _reset, _wait_for_lock, claimed
from test_assignments_postgres import (
    action,
    independent_database,
    reserve,
    session_observation,
    start,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_control_postgres import control, current, mutate, wait
from test_session_execution_postgres import operation, stored_rows, wrapper_arguments

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.assignments import (
    AssignmentActionOutcome,
    AssignmentActionReconciliation,
    AssignmentRepository,
    canonical,
    digest,
    plain,
)


def held_operation(tx, repo, *, state="uncertain", boundary="unreplayable"):
    values = operation(tx, repo)
    claim, binding = values[4], values[7]
    item = action(repo, tx, claim.fence, boundary=boundary)
    reserved = reserve(repo, tx, claim.fence, item)
    permit = start(repo, tx, claim.fence, reserved, binding)
    if state == "uncertain":
        repo.record_action_outcome(
            tx,
            owner_id="owner",
            assignment_id=item.assignment_id,
            action_id=item.action_id,
            attempt_id=permit.attempt_id,
            dispatch_token=permit.dispatch_token,
            expected_request_digest=permit.request_digest,
            outcome=AssignmentActionOutcome("uncertain", digest("stale-unknown"), {}),
        )
    return values, permit, current(repo, tx, values[3])


def claim_args(record, authority):
    return dict(
        owner_id=record.owner_id,
        assignment_id=record.assignment_id,
        expected_state_version=record.state_version,
        worker_id="continuation-held",
        authority=authority,
        lease_seconds=30,
    )


def payload(tx, record):
    return tx.fetch_one(
        "SELECT data FROM persistent_assignment WHERE id=%s", (record.assignment_id,)
    )["data"]


@pytest.mark.parametrize("boundary", ["read_only", "unreplayable"])
@pytest.mark.parametrize("state", ["started", "uncertain"])
def test_pause_resume_keeps_issued_liabilities_and_never_schedules(tx, repo, state, boundary):
    values, permit, before = held_operation(tx, repo, state=state, boundary=boundary)
    assert before.phase == "checking"
    before_action = repo.get_action(
        tx, owner_id="owner", assignment_id=before.assignment_id, action_id=permit.action_id
    )
    paused = control(repo, tx, before).assignment
    assert paused.lifecycle == "paused" and paused.phase == "reconciliation"
    resumed = control(repo, tx, paused, "resume").assignment
    assert resumed.lifecycle == "active" and resumed.phase == "reconciliation"
    assert resumed.next_wake_at is None and payload(tx, resumed)["next_retry_at"] is None
    assert resumed.usage == before.usage and payload(tx, resumed)["claim_token"] is None
    assert (
        repo.get_action(
            tx, owner_id="owner", assignment_id=before.assignment_id, action_id=permit.action_id
        )
        == before_action
    )
    with pytest.raises(RepositoryConflictError, match="assignment_not_due"):
        repo.claim_operation_for_administration(tx, **claim_args(resumed, values[2]))


@pytest.mark.parametrize("state", ["ready", "reserved"])
def test_safe_unstarted_authority_is_invalidated_before_hold_computation(tx, repo, state):
    values = operation(tx, repo)
    item = action(repo, tx, values[4].fence)
    if state == "reserved":
        reserve(repo, tx, values[4].fence, item)
    paused = control(repo, tx, current(repo, tx, values[3])).assignment
    resumed = control(repo, tx, paused, "resume").assignment
    assert resumed.phase == "waiting" and resumed.next_wake_at is not None
    assert not any(resumed.usage["outstanding"].values())
    assert (
        repo.get_action(
            tx, owner_id="owner", assignment_id=item.assignment_id, action_id=item.action_id
        ).state
        == "invalidated"
    )
    assert repo.claim_operation_for_administration(tx, **claim_args(resumed, values[2]))


@pytest.mark.parametrize(
    "phase", ["waiting_authorization", "awaiting_event", "waiting_approval", "budget_exhausted"]
)
def test_control_preserves_existing_safe_hold_and_retry_history(tx, repo, phase):
    _, _, record = held_operation(tx, repo)
    retry = record.updated_at + timedelta(seconds=45)
    mutate(
        tx,
        record,
        lambda data: data.update(
            phase=phase,
            consecutive_failures=2,
            next_retry_at=retry.isoformat(),
            next_wake_at=retry.isoformat(),
        ),
    )
    tx.execute(
        "UPDATE persistent_assignment SET next_wake_at=%s WHERE id=%s",
        (retry, record.assignment_id),
    )
    before = current(repo, tx, record)
    paused = control(repo, tx, before).assignment
    assert paused.phase == phase and paused.next_wake_at is None
    assert payload(tx, paused)["next_retry_at"] is None
    assert payload(tx, paused)["consecutive_failures"] == 2 and paused.usage == before.usage
    stopped = control(repo, tx, paused, "stop").assignment
    assert stopped.lifecycle == "stopped" and stopped.usage == before.usage


def test_due_label_cannot_hide_unknown_consumption_from_exact_claim(tx, repo):
    values, _, before = held_operation(tx, repo)
    paused = control(repo, tx, before).assignment
    resumed = control(repo, tx, paused, "resume").assignment
    mutate(tx, resumed, lambda data: data.update(phase="waiting", next_wake_at=data["updated_at"]))
    tx.execute(
        "UPDATE persistent_assignment SET next_wake_at=%s WHERE id=%s",
        (resumed.updated_at, resumed.assignment_id),
    )
    waiting = current(repo, tx, resumed)
    with pytest.raises(RepositoryConflictError, match="assignment_action_uncertain"):
        repo.claim_operation_for_administration(tx, **claim_args(waiting, values[2]))
    assert current(repo, tx, waiting) == waiting


def test_factual_reconciliation_is_required_before_a_new_claim(tx, repo):
    values, permit, before = held_operation(tx, repo)
    paused = control(repo, tx, before).assignment
    resumed = control(repo, tx, paused, "resume").assignment
    item = repo.get_action(
        tx, owner_id="owner", assignment_id=resumed.assignment_id, action_id=permit.action_id
    )
    decision = AssignmentActionReconciliation(
        item.result["result_digest"],
        "confirmed_applied",
        "verified:synthetic-receipt",
        uid(),
        digest("reconcile"),
    )
    authority = session_observation(tx, session_id=values[1].session_id)
    args = dict(
        owner_id="owner",
        assignment_id=resumed.assignment_id,
        action_id=item.action_id,
        expected_instruction_revision=resumed.instruction_revision,
        expected_control_epoch=resumed.control_epoch,
        expected_state_version=resumed.state_version,
        decision=decision,
        authority=authority,
    )
    settled = repo.reconcile_action(tx, **args)
    assert repo.reconcile_action(tx, **args) == settled
    after = current(repo, tx, resumed)
    assert after.usage["spent"]["tool_calls"] == 1
    assert after.usage["outstanding"]["tool_calls"] == 0 and after.phase == "waiting"
    assert repo.claim_operation_for_administration(tx, **claim_args(after, authority))


def test_due_label_cannot_hide_an_existing_event_wait(tx, repo):
    values = operation(tx, repo)
    waiting = wait(repo, tx, values[4])
    mutate(tx, waiting, lambda data: data.update(phase="waiting", next_wake_at=data["updated_at"]))
    tx.execute(
        "UPDATE persistent_assignment SET next_wake_at=%s WHERE id=%s",
        (waiting.updated_at, waiting.assignment_id),
    )
    current_wait = current(repo, tx, waiting)
    with pytest.raises(RepositoryConflictError, match="assignment_action_uncertain"):
        repo.claim_operation_for_administration(tx, **claim_args(current_wait, values[2]))
    assert current(repo, tx, waiting) == current_wait


@pytest.mark.postgresql_lock_catalog
def test_claim_rechecks_original_observation_after_real_action_lock_wait(database, repo):
    with database.transaction() as tx:
        _reset(tx)
        values = operation(tx, repo)
        item = action(repo, tx, values[4].fence)
        paused = control(repo, tx, current(repo, tx, values[3])).assignment
        resumed = control(repo, tx, paused, "resume").assignment
        authority = session_observation(tx, session_id=values[1].session_id)
        authority = replace(authority, valid_until=authority.started_at + timedelta(seconds=0.5))
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    ready, state = Event(), {}

    def attempt():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout=3000")
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            try:
                repo.claim_operation_for_administration(tx, **claim_args(resumed, authority))
            except RepositoryConflictError as error:
                return error.code
            return "unexpected_claim"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            repo.get_action(
                tx, owner_id="owner", assignment_id=item.assignment_id, action_id=item.action_id
            )
            future = pool.submit(attempt)
            assert ready.wait(3)
            _wait_for_lock(tx, state["pid"], pid)
            tx.fetch_one("SELECT pg_sleep(0.55)")
        assert future.result(timeout=5) == "assignment_authorization_unavailable"
    with database.transaction() as tx:
        assert current(repo, tx, resumed) == resumed


def test_caught_final_claim_failure_rolls_back_minting_but_not_unrelated_work(database, repo):
    with database.transaction() as tx:
        _reset(tx)
        values = operation(tx, repo)
        paused = control(repo, tx, current(repo, tx, values[3])).assignment
        resumed = control(repo, tx, paused, "resume").assignment

    class FailedFinalClaim(AssignmentRepository):
        calls = 0

        def _assert_operation_claim_current(self, *args, **kwargs):
            super()._assert_operation_claim_current(*args, **kwargs)
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("synthetic-final-claim-failure")

    checked = FailedFinalClaim()
    with database.transaction() as tx:
        observation = session_observation(tx, session_id=values[1].session_id)
        with pytest.raises(RuntimeError, match="synthetic-final-claim-failure"):
            checked.claim_operation_for_administration(tx, **claim_args(resumed, observation))
        values[0].mark_resumed(
            tx,
            owner_id="owner",
            session_id=values[1].session_id,
            expected_incarnation_id=values[1].incarnation_id,
            expected_resumed=False,
            resumed=True,
        )
    with database.transaction() as tx:
        assert current(repo, tx, resumed) == resumed
        assert values[0].get(tx, owner_id="owner", session_id=values[1].session_id).resumed


@pytest.mark.parametrize("method", ["put", "reserve", "start"])
@pytest.mark.parametrize(
    "hold",
    [
        "uncertain",
        "old_fence",
        "old_binding",
        "unknown_version",
        "coerced_fence",
        "coerced_binding",
    ],
)
def test_named_writes_refuse_unknown_or_stale_sibling_without_mutation(tx, repo, method, hold):
    values = operation(tx, repo)
    arguments = wrapper_arguments(tx, repo, values, method)
    sibling = action(repo, tx, values[4].fence, boundary="unreplayable")
    permit = start(
        repo, tx, values[4].fence, reserve(repo, tx, values[4].fence, sibling), values[7]
    )
    if hold == "uncertain":
        repo.record_action_outcome(
            tx,
            owner_id="owner",
            assignment_id=sibling.assignment_id,
            action_id=sibling.action_id,
            attempt_id=permit.attempt_id,
            dispatch_token=permit.dispatch_token,
            expected_request_digest=permit.request_digest,
            outcome=AssignmentActionOutcome("uncertain", digest("unknown-live-sibling"), {}),
        )
    else:
        data = plain(
            tx.fetch_one(
                "SELECT data FROM persistent_assignment_action WHERE id=%s", (sibling.action_id,)
            )["data"]
        )
        if hold == "old_fence":
            data["attempts"][0]["assignment_fence"]["claim_token"] = uid()
        elif hold == "old_binding":
            data["attempts"][0]["binding"]["execution_lease_token"] = uid()
        elif hold == "coerced_fence":
            data["attempts"][0]["assignment_fence"]["claim_generation"] = True
        elif hold == "coerced_binding":
            data["attempts"][0]["binding"]["execution_generation"] = True
        else:
            data["future_version"] = 3
        tx.execute(
            "UPDATE persistent_assignment_action SET data=%s::jsonb WHERE id=%s",
            (canonical(data), sibling.action_id),
        )
    before = stored_rows(tx, sibling.assignment_id)
    with pytest.raises(RepositoryConflictError, match="assignment_action_uncertain"):
        getattr(repo, method + "_action_for_execution")(tx, **arguments)
    assert stored_rows(tx, sibling.assignment_id) == before


@pytest.mark.parametrize("sibling_state", ["reserved", "started"])
@pytest.mark.parametrize("method", ["put", "reserve", "start"])
def test_named_writes_preserve_known_current_sibling_concurrency(tx, repo, method, sibling_state):
    values = operation(tx, repo)
    arguments = wrapper_arguments(tx, repo, values, method)
    sibling = action(repo, tx, values[4].fence, boundary="unreplayable")
    reserved = reserve(repo, tx, values[4].fence, sibling)
    if sibling_state == "started":
        start(repo, tx, values[4].fence, reserved, values[7])
    observed = repo.get_action(
        tx, owner_id="owner", assignment_id=sibling.assignment_id, action_id=sibling.action_id
    )
    assert getattr(repo, method + "_action_for_execution")(tx, **arguments)
    assert (
        repo.get_action(
            tx, owner_id="owner", assignment_id=sibling.assignment_id, action_id=sibling.action_id
        )
        == observed
    )


def test_exact_reserved_replay_and_permit_refusal_remain_once_only(tx, repo):
    values = operation(tx, repo)
    args = wrapper_arguments(tx, repo, values, "reserve")
    first = repo.reserve_action_for_execution(tx, **args)
    replay = repo.reserve_action_for_execution(tx, **args)
    assert first.created and not replay.created
    assert first.action == replay.action and first.attempt_id == replay.attempt_id
    item = first.action
    start_args = {
        name: args[name]
        for name in (
            "fence",
            "binding",
            "authority",
            "action_id",
            "attempt_id",
            "expected_request_digest",
        )
    }
    start_args.update(
        current_permission_digest=item.intent.permission_digest,
        current_precondition_digest=item.intent.precondition_digest,
    )
    permit = repo.start_action_for_execution(tx, **start_args)
    before = stored_rows(tx, item.assignment_id)
    with pytest.raises(RepositoryConflictError, match="assignment_action_already_started"):
        repo.start_action_for_execution(tx, **start_args)
    assert stored_rows(tx, item.assignment_id) == before
    assert permit.dispatch_token


@pytest.mark.parametrize("method", ["put", "reserve", "start"])
def test_persistent_named_wrapper_retains_existing_sibling_policy(tx, repo, method):
    record, claim, _, _, binding = claimed(repo, tx, profile="persistent")
    values = (None, None, None, record, claim, None, None, binding)
    args = wrapper_arguments(tx, repo, values, method)
    sibling = action(repo, tx, claim.fence, boundary="unreplayable")
    permit = start(repo, tx, claim.fence, reserve(repo, tx, claim.fence, sibling), binding)
    repo.record_action_outcome(
        tx,
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=sibling.action_id,
        attempt_id=permit.attempt_id,
        dispatch_token=permit.dispatch_token,
        expected_request_digest=permit.request_digest,
        outcome=AssignmentActionOutcome("uncertain", digest("persistent-sibling"), {}),
    )
    held = repo.get_action(
        tx, owner_id="owner", assignment_id=record.assignment_id, action_id=sibling.action_id
    )
    assert getattr(repo, method + "_action_for_execution")(tx, **args)
    assert (
        repo.get_action(
            tx, owner_id="owner", assignment_id=record.assignment_id, action_id=sibling.action_id
        )
        == held
    )
