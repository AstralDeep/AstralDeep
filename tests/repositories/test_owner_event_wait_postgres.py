"""Real-PostgreSQL tests for astralplane.repositories.assignments and audit: owner wait
is read-only in its prepare phase and retires claim delivery on finish, preserving
issued charges and audit rollback together.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from test_assignment_execution_guard_postgres import _reset, _wait_for_lock
from test_assignments_postgres import action as make_action
from test_assignments_postgres import (
    claim_operations,
    create,
    independent_database,
    parallel_transactions,
    reserve,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_control_postgres import control, current, mutate, wake_args
from test_operation_payload_postgres import settle_args
from test_operation_terminal_postgres import change_action, command_args, expire_authority
from test_reconciliation_authority_postgres import append_audit, uncertain
from test_session_execution_postgres import operation, stored_rows

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.assignments import (
    AssignmentRepository,
    AssignmentTask,
    canonical,
    digest,
    plain,
)
from astralplane.repositories.audit import AuditRepository


def arguments(record, **changes):
    values = dict(
        owner_id=record.owner_id,
        assignment_id=record.assignment_id,
        expected_instruction_revision=record.instruction_revision,
        expected_control_epoch=record.control_epoch,
        expected_state_version=record.state_version,
        submission_id=uid(),
        submission_digest=digest("owner waits for source revision"),
        event_key="source-observation",
        source_revision=3,
    )
    values.update(changes)
    return values


def test_owner_wait_prepare_is_read_only_and_final_retires_claim(tx, repo):
    values = operation(tx, repo)
    before = current(repo, tx, values[3])
    args = arguments(before)
    rows = stored_rows(tx, before.assignment_id)
    prepare = repo.prepare_owner_event_wait(tx, **args)
    assert prepare.assignment == before and not prepare.replayed
    assert stored_rows(tx, before.assignment_id) == rows
    result = repo.set_owner_event_wait(tx, **args)
    waiting = result.assignment
    assert result.applied and waiting.phase == "awaiting_event"
    assert waiting.next_wake_at is None
    assert stored_rows(tx, before.assignment_id)[0][0]["data"]["claim_token"] is None
    assert waiting.control_epoch == before.control_epoch + 1
    assert waiting.state_version == before.state_version + 1
    assert waiting.checkpoint == before.checkpoint and waiting.usage == before.usage
    assert waiting.operation["control"]["wait"] == {
        "event_key": "source-observation",
        "source_revision": 3,
    }
    assert repo.prepare_owner_event_wait(tx, **args).replayed
    assert not repo.set_owner_event_wait(tx, **args).applied
    assert current(repo, tx, before) == waiting
    assert claim_operations(repo, tx, worker_id="must-not-run") == ()
    with pytest.raises(RepositoryConflictError):
        repo.assert_current_assignment_execution(
            tx, fence=values[4].fence, binding=values[7], authority=values[2]
        )


def test_owner_wait_preserves_issued_charge_but_denies_late_content(tx, repo):
    values = operation(tx, repo, issued=True)
    before = current(repo, tx, values[3])
    result = repo.set_owner_event_wait(tx, **arguments(before))
    assert result.begun_action_ids == (values[8].action_id,)
    assert result.assignment.phase == "reconciliation"
    assert result.assignment.usage == before.usage
    settled = repo.record_action_outcome(
        tx,
        **settle_args(tx, values[3], values[4], values[7], values[8], result_authority=values[2]),
    )
    after = current(repo, tx, before)
    assert settled.result["result_available"] is False
    assert settled.result["result"] == {}
    assert after.usage["spent"]["tool_calls"] == 1
    assert after.usage["outstanding"]["tool_calls"] == 0
    assert after.next_wake_at is None and after.checkpoint == before.checkpoint


@pytest.mark.parametrize("method", ["prepare_owner_event_wait", "set_owner_event_wait"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("submission_id", "not-a-uuid"),
        ("submission_digest", "not-a-digest"),
        ("expected_instruction_revision", True),
        ("expected_control_epoch", 1.0),
        ("expected_state_version", "2"),
        ("control_version", 2),
        ("event_key", ""),
        ("event_key", "x" * 129),
        ("source_revision", -1),
        ("source_revision", False),
    ],
)
def test_malformed_wait_never_mutates(tx, repo, method, field, value):
    values = operation(tx, repo)
    record = current(repo, tx, values[3])
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryValidationError):
        getattr(repo, method)(tx, **arguments(record, **{field: value}))
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize(
    "field", ["expected_instruction_revision", "expected_control_epoch", "expected_state_version"]
)
def test_new_wait_conflicts_with_changed_observed_counters(tx, repo, field):
    values = operation(tx, repo)
    record = current(repo, tx, values[3])
    args = arguments(record)
    args[field] += 1
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryConflictError, match="assignment_revision_conflict"):
        repo.set_owner_event_wait(tx, **args)
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize("identity", ["missing", "foreign"])
def test_missing_and_foreign_owner_have_same_refusal(tx, repo, identity):
    values = operation(tx, repo)
    args = arguments(current(repo, tx, values[3]))
    args["owner_id" if identity == "foreign" else "assignment_id"] = (
        "other" if identity == "foreign" else uid()
    )
    with pytest.raises(RepositoryNotFoundError, match="assignment_not_found"):
        repo.set_owner_event_wait(tx, **args)


@pytest.mark.parametrize("loss", ["deleted", "authority", "deadline", "retired"])
def test_safe_hold_does_not_require_original_execution_authority(tx, repo, loss):
    values = operation(tx, repo)
    if loss == "deleted":
        values[0].delete(
            tx,
            owner_id="owner",
            session_id=values[1].session_id,
            expected_incarnation_id=values[1].incarnation_id,
        )
    else:
        expire_authority(tx, values[3], loss)
    record = current(repo, tx, values[3])
    result = repo.set_owner_event_wait(tx, **arguments(record))
    assert result.applied and result.assignment.next_wake_at is None
    assert result.assignment.operation["authority"] == record.operation["authority"]
    assert result.assignment.operation["deadline_at"] == record.operation["deadline_at"]
    assert claim_operations(repo, tx, worker_id="no-new-authority") == ()


@pytest.mark.parametrize(
    "unsupported",
    [
        "persistent",
        "v1",
        "unknown",
        "checkpoint",
        "control",
        "stopped",
        "completed",
        "terminal_failed",
        "active_failed",
        "active_completed",
    ],
)
def test_unsupported_or_terminal_work_cannot_be_revived_by_wait(tx, repo, unsupported):
    if unsupported == "persistent":
        record = create(repo, tx)
    else:
        values = operation(tx, repo)
        record = current(repo, tx, values[3])

        def change(data):
            if unsupported in {"v1", "unknown"}:
                data["operation"]["version"] = 1 if unsupported == "v1" else 3
                if unsupported == "v1":
                    data["operation"]["authority"].update(
                        reference_kind="session", reference_id=values[1].session_id
                    )
            elif unsupported == "checkpoint":
                data["checkpoint"]["schema_version"] = 2
            elif unsupported == "control":
                data["operation"]["control"] = {
                    "version": 2,
                    "wait": None,
                    "watermarks": {},
                    "wake_receipts": {},
                }
            elif unsupported in {"stopped", "completed"}:
                data["lifecycle"] = unsupported
            elif unsupported == "active_failed":
                data.update(phase="failed", next_retry_at=None)
            else:
                data["operation"]["terminal_outcome"] = (
                    "completed" if unsupported == "active_completed" else "failed"
                )

        if unsupported in {"stopped", "completed"}:
            data = plain(stored_rows(tx, record.assignment_id)[0][0]["data"])
            change(data)
            tx.execute(
                "UPDATE persistent_assignment SET lifecycle=%s,data=%s::jsonb WHERE id=%s",
                (unsupported, canonical(data), record.assignment_id),
            )
        else:
            mutate(tx, record, change)
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryConflictError):
        repo.set_owner_event_wait(tx, **arguments(record))
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize("state", ["ready", "reserved", "proposed", "approved"])
def test_unstarted_work_is_explicitly_invalidated_and_only_unused_reservation_released(
    tx, repo, state
):
    values = operation(tx, repo)
    action = make_action(repo, tx, values[4].fence)
    if state == "reserved":
        reserve(repo, tx, values[4].fence, action)
    elif state in {"proposed", "approved"}:
        change_action(tx, action.action_id, lambda data: data.update(state=state))
    before = current(repo, tx, values[3])
    args = arguments(before)
    prepared = repo.prepare_owner_event_wait(tx, **args)
    assert prepared.invalidated_action_ids == (action.action_id,)
    result = repo.set_owner_event_wait(tx, **args)
    assert result.invalidated_action_ids == prepared.invalidated_action_ids
    assert result.assignment.usage["outstanding"].get("tool_calls", 0) == 0
    assert result.assignment.usage["spent"] == before.usage["spent"]
    assert (
        repo.get_action(
            tx, owner_id="owner", assignment_id=before.assignment_id, action_id=action.action_id
        ).state
        == "invalidated"
    )


def test_wait_and_reconciliation_keep_event_hold_without_scheduling(tx, repo):
    values, record, action, decision = uncertain(tx, repo)
    args = arguments(record)
    prepared = repo.prepare_owner_event_wait(tx, **args)
    assert prepared.begun_action_ids == (action.action_id,)
    waiting = repo.set_owner_event_wait(tx, **args).assignment
    assert waiting.phase == "reconciliation"
    assert waiting.usage == record.usage
    with pytest.raises(RepositoryConflictError, match="assignment_not_waiting"):
        repo.accept_wake(tx, **wake_args(waiting, source_revision=4))
    result = repo.reconcile_action(
        tx, **command_args(waiting, action, decision), authority=values[2]
    )
    after = current(repo, tx, record)
    assert result.result["result_available"] is False
    assert after.phase == "awaiting_event" and after.next_wake_at is None
    assert after.operation["control"]["wait"] == waiting.operation["control"]["wait"]
    assert after.usage["spent"]["tool_calls"] == 1


def test_paused_wait_preserves_checkpoint_and_task_generations(tx, repo):
    values = operation(tx, repo)
    record = current(repo, tx, values[3])
    record = control(repo, tx, record).assignment
    mutate(
        tx,
        record,
        lambda data: data.update(
            checkpoint={"schema_version": 1, "cursor": "preserved"},
            tasks=[
                plain(AssignmentTask("pending", "plan", 1, "Task", "Read", (), state="pending")),
                plain(
                    AssignmentTask("held", "plan", 1, "Hold", "Read", (), state="reconciliation")
                ),
            ],
        ),
    )
    before = current(repo, tx, record)
    waiting = repo.set_owner_event_wait(tx, **arguments(before)).assignment
    assert waiting.lifecycle == "paused" and waiting.phase == "reconciliation"
    assert waiting.checkpoint == before.checkpoint
    assert waiting.tasks[0]["task_generation"] == before.tasks[0]["task_generation"] + 1
    assert waiting.tasks[1] == before.tasks[1]
    assert waiting.next_wake_at is None


def test_replay_after_stop_returns_original_receipt_without_new_audit(tx, repo):
    values = operation(tx, repo)
    args = arguments(current(repo, tx, values[3]))
    waiting = repo.set_owner_event_wait(tx, **args).assignment
    stopped = control(repo, tx, waiting, "stop").assignment
    before = stored_rows(tx, stopped.assignment_id)
    assert repo.prepare_owner_event_wait(tx, **args).replayed
    assert not repo.set_owner_event_wait(tx, **args).applied
    assert (
        repo.get_submission_receipt(
            tx,
            owner_id="owner",
            assignment_id=stopped.assignment_id,
            submission_id=args["submission_id"],
            submission_digest=args["submission_digest"],
            command="wait",
        )
        == stopped
    )
    assert stored_rows(tx, stopped.assignment_id) == before
    for changes in (
        {"event_key": "other"},
        {"source_revision": 4},
        {"submission_digest": digest("other")},
        {"expected_control_epoch": 77},
    ):
        with pytest.raises(RepositoryConflictError, match="assignment_idempotency_conflict"):
            repo.set_owner_event_wait(tx, **dict(args, **changes))


@pytest.mark.parametrize("collision", ["create", "pause"])
def test_wait_cannot_reuse_other_command_receipt(tx, repo, collision):
    values = operation(tx, repo)
    record = current(repo, tx, values[3])
    submission = stored_rows(tx, record.assignment_id)[0][0]["data"]["submission_id"]
    if collision == "pause":
        submission = uid()
        record = control(repo, tx, record, submission_id=submission).assignment
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryConflictError, match="assignment_idempotency_conflict"):
        repo.set_owner_event_wait(tx, **arguments(record, submission_id=submission))
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize("value", [True, 1.0])
def test_replay_does_not_accept_numeric_coercion_in_corrupted_receipt(tx, repo, value):
    values = operation(tx, repo)
    args = arguments(current(repo, tx, values[3]))
    waiting = repo.set_owner_event_wait(tx, **args).assignment
    mutate(
        tx,
        waiting,
        lambda data: data["controls"][args["submission_id"]]["signature"].__setitem__(1, value),
    )
    before = stored_rows(tx, waiting.assignment_id)
    with pytest.raises(RepositoryConflictError, match="assignment_idempotency_conflict"):
        repo.set_owner_event_wait(tx, **args)
    assert stored_rows(tx, waiting.assignment_id) == before


@pytest.mark.parametrize("capacity", ["controls", "watermarks", "old_watermark"])
def test_receipt_and_event_history_limits_are_not_evicted(tx, repo, capacity):
    values = operation(tx, repo)
    record = current(repo, tx, values[3])

    def change(data):
        if capacity == "controls":
            data["controls"] = {uid(): {"command": "pause"} for _ in range(256)}
        else:
            data["operation"]["control"] = {
                "version": 1,
                "wait": None,
                "wake_receipts": {},
                "watermarks": {"source-observation": 4}
                if capacity == "old_watermark"
                else {"event-" + str(i): 1 for i in range(64)},
            }

    mutate(tx, record, change)
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryConflictError):
        repo.set_owner_event_wait(tx, **arguments(record))
    assert stored_rows(tx, record.assignment_id) == before


def test_caught_final_failure_rolls_back_action_receipt_activity_and_assignment(tx, repo):
    values = operation(tx, repo)
    action = make_action(repo, tx, values[4].fence)
    reserve(repo, tx, values[4].fence, action)
    record = current(repo, tx, values[3])
    before = stored_rows(tx, record.assignment_id)

    class FailingSave(AssignmentRepository):
        def _save(self, transaction, data):
            super()._save(transaction, data)
            raise RuntimeError("synthetic final storage failure")

    with pytest.raises(RuntimeError, match="synthetic final storage failure"):
        FailingSave().set_owner_event_wait(tx, **arguments(record))
    assert stored_rows(tx, record.assignment_id) == before


def test_concurrent_duplicate_prepares_append_one_audit_and_one_transition(database, repo):
    with database.transaction() as tx:
        _reset(tx)
        values = operation(tx, repo)
        record = current(repo, tx, values[3])
    args = arguments(record)

    def wait(tx):
        prepared = repo.prepare_owner_event_wait(tx, **args)
        receipt = None if prepared.replayed else append_audit(tx, record.assignment_id)
        return receipt, repo.set_owner_event_wait(tx, **args)

    results = parallel_transactions(database, (wait, wait))
    assert sum(receipt is not None for receipt, _ in results) == 1
    assert sorted(result.applied for _, result in results) == [False, True]
    assert results[0][1].assignment == results[1][1].assignment


def test_preparation_audit_and_wait_share_outer_rollback(database, repo):
    with database.transaction() as tx:
        _reset(tx)
        values = operation(tx, repo)
        record = current(repo, tx, values[3])
        before = stored_rows(tx, record.assignment_id)
    args = arguments(record)
    with pytest.raises(RuntimeError, match="lost before commit"), database.transaction() as tx:
        repo.prepare_owner_event_wait(tx, **args)
        receipt = append_audit(tx, record.assignment_id)
        repo.set_owner_event_wait(tx, **args)
        raise RuntimeError("lost before commit")
    with database.transaction() as tx:
        assert stored_rows(tx, record.assignment_id) == before
        assert (
            AuditRepository().get(
                tx, chain_id=receipt.event.chain_id, event_id=receipt.event.event_id
            )
            is None
        )


def test_final_preparation_rechecks_state_changed_after_prepare(tx, repo):
    values = operation(tx, repo)
    record = current(repo, tx, values[3])
    args = arguments(record)
    repo.prepare_owner_event_wait(tx, **args)
    paused = control(repo, tx, record).assignment
    before = stored_rows(tx, record.assignment_id)
    with pytest.raises(RepositoryConflictError, match="assignment_revision_conflict"):
        repo.set_owner_event_wait(tx, **args)
    assert current(repo, tx, record) == paused
    assert stored_rows(tx, record.assignment_id) == before


@pytest.mark.parametrize("hold", ["opaque_action", "phase_only", "outstanding"])
def test_unknown_or_unattributed_hold_cannot_be_erased_by_wait(tx, repo, hold):
    values = operation(tx, repo, issued=hold == "opaque_action")
    record = current(repo, tx, values[3])
    if hold == "opaque_action":
        change_action(tx, values[8].action_id, lambda data: data.update(future_receipt_version=2))
        action_before = stored_rows(tx, record.assignment_id)[1]
    else:

        def change(data):
            data["safe_error_code"] = "assignment_action_uncertain"
            if hold == "phase_only":
                data["phase"] = "reconciliation"
            else:
                data["usage"]["outstanding"]["tool_calls"] = 1

        mutate(tx, record, change)
    before = current(repo, tx, record)
    args = arguments(before)
    prepared = repo.prepare_owner_event_wait(tx, **args)
    waiting = repo.set_owner_event_wait(tx, **args).assignment
    assert waiting.phase == "reconciliation" and waiting.next_wake_at is None
    assert waiting.usage == before.usage
    if hold == "opaque_action":
        assert prepared.begun_action_ids == (values[8].action_id,)
        assert stored_rows(tx, record.assignment_id)[1] == action_before
    else:
        assert waiting.safe_error_code == before.safe_error_code


@pytest.mark.parametrize("blocker", ["owner", "assignment", "action", "audit"])
@pytest.mark.postgresql_lock_catalog
def test_owner_wait_uses_same_ordered_transaction_across_real_lock_wait(database, repo, blocker):
    with database.transaction() as tx:
        _reset(tx)
        values = operation(tx, repo, issued=True)
        record = current(repo, tx, values[3])
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    args = arguments(record)
    ready, identities = Event(), {}

    def run():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout=3000")
            identities["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            prepared = repo.prepare_owner_event_wait(tx, **args)
            receipt = append_audit(tx, record.assignment_id)
            return prepared, receipt, repo.set_owner_event_wait(tx, **args)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            if blocker == "owner":
                tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", ("owner",))
            elif blocker == "audit":
                tx.fetch_one(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("audit_events:reconciliation-tests",),
                )
            else:
                table, identity = (
                    ("persistent_assignment", record.assignment_id)
                    if blocker == "assignment"
                    else ("persistent_assignment_action", values[8].action_id)
                )
                tx.fetch_one("SELECT id FROM " + table + " WHERE id=%s FOR UPDATE", (identity,))
            future = pool.submit(run)
            assert ready.wait(3)
            _wait_for_lock(tx, identities["pid"], pid)
        prepared, receipt, result = future.result(timeout=6)
    assert not prepared.replayed and result.applied
    assert result.assignment.phase == "reconciliation"
    with database.transaction() as tx:
        assert (
            AuditRepository().get(
                tx, chain_id=receipt.event.chain_id, event_id=receipt.event.event_id
            )
            == receipt
        )
        assert current(repo, tx, record).usage == record.usage


@pytest.mark.postgresql_lock_catalog
def test_action_inventory_locks_sorted_before_a_result_reader_second_lock(database, repo):
    from unittest.mock import patch
    from uuid import UUID

    from astralplane.repositories import assignments

    with database.transaction() as tx:
        _reset(tx)
        values = operation(tx, repo, issued=True)
        original = repo.get_action(
            tx,
            owner_id="owner",
            assignment_id=values[3].assignment_id,
            action_id=values[8].action_id,
        )
        lower_id = "00000000-0000-4000-8000-000000000001"
        with patch.object(assignments.uuid, "uuid4", return_value=UUID(lower_id)):
            lower = repo.put_action(
                tx,
                fence=values[4].fence,
                intent=replace(original.intent, action_key="earlier-reader"),
            )
        assert lower.action_id < original.action_id
        record = current(repo, tx, values[3])
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    ready, identities = Event(), {}

    def run():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout=3000")
            identities["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            return repo.set_owner_event_wait(tx, **arguments(record))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout=1000")
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            repo.get_action(
                tx, owner_id="owner", assignment_id=record.assignment_id, action_id=lower.action_id
            )
            future = pool.submit(run)
            assert ready.wait(3)
            _wait_for_lock(tx, identities["pid"], pid)
            assert (
                repo.get_action(
                    tx,
                    owner_id="owner",
                    assignment_id=record.assignment_id,
                    action_id=original.action_id,
                ).state
                == "started"
            )
        result = future.result(timeout=6)
    assert result.invalidated_action_ids == (lower.action_id,)
    assert result.begun_action_ids == (original.action_id,)


def test_final_wait_reloads_actual_invalidation_set_after_read_only_prepare(tx, repo):
    values = operation(tx, repo)
    record = current(repo, tx, values[3])
    args = arguments(record)
    assert repo.prepare_owner_event_wait(tx, **args).invalidated_action_ids == ()
    action = make_action(repo, tx, values[4].fence)
    assert current(repo, tx, record).state_version == record.state_version
    final = repo.set_owner_event_wait(tx, **args)
    assert final.invalidated_action_ids == (action.action_id,)
