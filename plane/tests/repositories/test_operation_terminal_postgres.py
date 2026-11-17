"""Real-PostgreSQL tests for astralplane.repositories.assignments: action commands
require strict observed state, orphan liabilities defer cleanup instead of guessing,
and retirement serializes against new admission.
"""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_assignments_postgres import (
    action,
    bind,
    claim_operations,
    create,
    create_operation,
    expire_claim,
    outcome,
    parallel_transactions,
    reserve,
    start,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_control_postgres import control, current, mutate, operation_claim

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.assignments import (
    AssignmentActionDecision,
    AssignmentActionOutcome,
    AssignmentActionReconciliation,
    canonical,
    digest,
    plain,
)


def proposed(repo, tx):
    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    value = action(
        repo,
        tx,
        claim.fence,
        sensitivity="sensitive",
        approval_expires_at=tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
        + timedelta(minutes=2),
    )
    decision = AssignmentActionDecision(
        value.intent.request_digest,
        "approve",
        uid(),
        digest("approve"),
        value.intent.permission_digest,
        value.intent.precondition_digest,
    )
    return current(repo, tx, record), value, decision


def uncertain(repo, tx):
    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    binding = bind(repo, tx, claim.fence)
    value = action(repo, tx, claim.fence, boundary="unreplayable")
    permit = start(repo, tx, claim.fence, reserve(repo, tx, claim.fence, value), binding)
    unknown = AssignmentActionOutcome("uncertain", digest("uncertain"), {})
    outcome(repo, tx, permit, record.assignment_id, outcome=unknown)
    decision = AssignmentActionReconciliation(
        unknown.result_digest, "confirmed_applied", "verified:receipt", uid(), digest("reconcile")
    )
    return current(repo, tx, record), value, decision


def command_args(record, value, decision):
    return dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=value.action_id,
        expected_instruction_revision=record.instruction_revision,
        expected_control_epoch=record.control_epoch,
        expected_state_version=record.state_version,
        decision=decision,
    )


def delete_args(record):
    return dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        expected_control_epoch=record.control_epoch,
        expected_state_version=record.state_version,
    )


@pytest.mark.parametrize("kind", ["decide", "reconcile", "delete"])
@pytest.mark.parametrize("version", [None, True, 1.0, "2", 0, -1])
def test_commands_require_strict_observed_state(tx, repo, kind, version):
    if kind == "delete":
        record = control(repo, tx, create_operation(repo, tx), "stop").assignment
        args = delete_args(record)
        command = repo.delete_for_owner
    else:
        record, value, decision = (proposed if kind == "decide" else uncertain)(repo, tx)
        args = command_args(record, value, decision)
        command = repo.decide_action if kind == "decide" else repo.reconcile_action
    args["expected_state_version"] = version
    with pytest.raises(RepositoryValidationError):
        command(tx, **args)
    assert current(repo, tx, record) == record


@pytest.mark.parametrize("kind", ["decide", "reconcile"])
def test_action_command_stale_state_denial_and_exact_replay(tx, repo, kind):
    record, value, decision = (proposed if kind == "decide" else uncertain)(repo, tx)
    args = command_args(record, value, decision)
    command = repo.decide_action if kind == "decide" else repo.reconcile_action
    with pytest.raises(RepositoryConflictError, match="assignment_revision_conflict"):
        command(tx, **dict(args, expected_state_version=record.state_version - 1))
    accepted = command(tx, **args)
    after = current(repo, tx, record)
    assert after.state_version > record.state_version
    assert command(tx, **args) == accepted
    assert current(repo, tx, record) == after
    with pytest.raises(RepositoryConflictError):
        command(tx, **dict(args, decision=replace(decision, submission_digest=digest("changed"))))


@pytest.mark.parametrize("kind", ["decide", "reconcile"])
def test_action_command_owner_isolation(tx, repo, kind):
    record, value, decision = (proposed if kind == "decide" else uncertain)(repo, tx)
    command = repo.decide_action if kind == "decide" else repo.reconcile_action
    with pytest.raises(RepositoryNotFoundError):
        command(tx, **dict(command_args(record, value, decision), owner_id="other"))
    assert current(repo, tx, record) == record


@pytest.mark.parametrize("expiry", ["authority", "deadline", "retired", "revoked"])
def test_decision_does_not_restore_expired_or_revoked_authority(tx, repo, expiry):
    record, value, decision = proposed(repo, tx)
    expire_authority(tx, record, expiry)
    before = current(repo, tx, record)
    with pytest.raises(RepositoryConflictError):
        repo.decide_action(tx, **command_args(before, value, decision))
    assert current(repo, tx, record) == before
    assert (
        repo.get_action(
            tx, owner_id="owner", assignment_id=record.assignment_id, action_id=value.action_id
        ).state
        == "proposed"
    )


def expire_authority(tx, record, expiry):
    if expiry == "retired":
        tx.execute(
            "INSERT INTO astralplane_blob_owner_state "
            "(owner_id,state,version,retired_at,updated_at) "
            "VALUES('owner','retired',1,clock_timestamp(),clock_timestamp())"
        )
    elif expiry == "revoked":
        mutate(tx, record, lambda d: d.update(phase="waiting_authorization"))
    else:
        expired = plain(tx.fetch_one("SELECT clock_timestamp()-interval '1 second' AS now")["now"])

        def change(data):
            if expiry == "deadline":
                data["operation"]["deadline_at"] = expired
            else:
                data["operation"]["authority"]["expires_at"] = expired

        mutate(tx, record, change)


@pytest.mark.parametrize("expiry", ["authority", "deadline", "retired", "revoked", "stopped"])
def test_reconciliation_settles_liability_without_reviving_authority(tx, repo, expiry):
    record, value, decision = uncertain(repo, tx)
    if expiry == "stopped":
        control(repo, tx, record, "stop")
    else:
        expire_authority(tx, record, expiry)
    before = current(repo, tx, record)
    result = repo.reconcile_action(tx, **command_args(before, value, decision))
    after = current(repo, tx, record)
    assert result.state == "succeeded"
    assert after.usage["outstanding"]["tool_calls"] == 0
    assert after.usage["spent"]["tool_calls"] == 1
    assert after.next_wake_at is None
    assert after.wake_generation == before.wake_generation
    if expiry == "deadline":
        assert after.lifecycle == "completed"
        assert after.operation["terminal_outcome"] == "failed"
        assert after.safe_error_code == "assignment_deadline_exceeded"
    else:
        assert after.lifecycle == before.lifecycle
    assert claim_operations(repo, tx, worker_id="later") == ()
    if expiry == "deadline":
        retirement = repo.retire_operations_for_owner(tx, owner_id="owner")
        assert retirement.retained_assignment_ids == ()
        assert retirement.deleted_assignment_ids == (record.assignment_id,)


def test_delete_observed_state_and_strict_epoch(tx, repo):
    record = control(repo, tx, create_operation(repo, tx), "stop").assignment
    with pytest.raises(RepositoryConflictError, match="assignment_revision_conflict"):
        repo.delete_for_owner(tx, **dict(delete_args(record), expected_state_version=1))
    with pytest.raises(RepositoryValidationError):
        repo.delete_for_owner(tx, **dict(delete_args(record), expected_control_epoch=True))
    assert not repo.delete_for_owner(tx, **dict(delete_args(record), owner_id="other"))
    assert repo.delete_for_owner(tx, **delete_args(record))
    assert not repo.delete_for_owner(tx, **delete_args(record))


def action_bytes(tx, action_id):
    return canonical(
        tx.fetch_one("SELECT data FROM persistent_assignment_action WHERE id=%s", (action_id,))[
            "data"
        ]
    )


def change_action(tx, action_id, change):
    data = tx.fetch_one("SELECT data FROM persistent_assignment_action WHERE id=%s", (action_id,))[
        "data"
    ]
    data = plain(data)
    change(data)
    tx.execute(
        "UPDATE persistent_assignment_action SET data=%s::jsonb,state=%s WHERE id=%s",
        (canonical(data), data["state"], action_id),
    )


@pytest.mark.parametrize(
    "opaque", ["version", "intent", "attempt", "identity", "state", "proposal"]
)
def test_unknown_assignment_and_opaque_action_survive_retirement_without_starvation(
    tx, repo, opaque
):
    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    value = action(repo, tx, claim.fence)
    reservation = reserve(repo, tx, claim.fence, value)

    def corrupt(data):
        if opaque == "version":
            data["version"] = 2
        elif opaque == "intent":
            data["intent"]["future"] = {"liability": "unknown"}
        elif opaque == "attempt":
            data["attempts"][0]["future"] = {"liability": "unknown"}
        elif opaque == "identity":
            data["owner_id"] = "other"
        elif opaque == "state":
            data["attempts"][0]["state"] = "future"
        else:
            data["interactive_proposal_id"] = {"untrusted": "proposal-id"}
        data["state"] = "succeeded"

    change_action(tx, value.action_id, corrupt)
    mutate(tx, record, lambda d: d["operation"].update(version=3, future={"opaque": True}))
    before_action = action_bytes(tx, value.action_id)
    before = current(repo, tx, record)
    neighbor = create(repo, tx)
    with pytest.raises(RepositoryConflictError, match="assignment_operation_required"):
        repo.retire_owner(tx, owner_id="owner")
    assert current(repo, tx, neighbor).lifecycle == "active"
    retired = repo.retire_operations_for_owner(tx, owner_id="owner")
    assert retired.unresolved_action_ids == (value.action_id,)
    assert retired.retained_assignment_ids == (record.assignment_id,)
    assert retired.deleted_assignment_ids == (neighbor.assignment_id,)
    after = current(repo, tx, record)
    assert after.lifecycle == "stopped"
    assert after.operation == before.operation
    assert after.usage == before.usage
    assert after.control_epoch == before.control_epoch + 1
    assert action_bytes(tx, value.action_id) == before_action
    assert reservation.attempt_id in before_action
    assert (
        tx.fetch_one("SELECT state FROM astralplane_blob_owner_state WHERE owner_id='owner'")[
            "state"
        ]
        == "retired"
    )
    with pytest.raises(RepositoryConflictError, match="assignment_action_uncertain"):
        repo.delete_for_owner(tx, **delete_args(after))
    assert repo.retire_operations_for_owner(tx, owner_id="owner").retained_assignment_ids == (
        record.assignment_id,
    )


@pytest.mark.parametrize("version_path", ["operation", "checkpoint", "control"])
def test_unknown_stop_receipt_and_safe_empty_purge(tx, repo, version_path):
    record = create_operation(repo, tx)

    def change(data):
        if version_path == "operation":
            data["operation"]["version"] = 3
        elif version_path == "checkpoint":
            data["checkpoint"] = {"schema_version": 2, "opaque": [1]}
        else:
            data["operation"]["control"] = {"version": 2, "opaque": [1]}

    mutate(tx, record, change)
    record = current(repo, tx, record)
    submission = uid()
    stopped = control(repo, tx, record, "stop", submission_id=submission).assignment
    args = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        submission_id=submission,
        submission_digest=digest("stop"),
        command="stop",
    )
    assert repo.get_submission_receipt(tx, **args) == stopped
    with pytest.raises(RepositoryConflictError, match="assignment_idempotency_conflict"):
        repo.get_submission_receipt(tx, **dict(args, submission_digest=digest("changed")))
    with pytest.raises(RepositoryConflictError, match="assignment_version_unsupported"):
        repo.get_submission_receipt(tx, **dict(args, command="resume"))
    assert repo.get_submission_receipt(tx, **dict(args, owner_id="other")) is None
    assert repo.delete_for_owner(tx, **delete_args(stopped))


@pytest.mark.parametrize("profile", ["one_shot", "persistent"])
def test_orphan_liability_defers_operation_cleanup_and_blocks_legacy_caller(tx, repo, profile):
    record = (create_operation if profile == "one_shot" else create)(repo, tx)
    mutate(tx, record, lambda d: d["usage"]["outstanding"].update(tool_calls=1))
    with pytest.raises(RepositoryConflictError), tx.savepoint("legacy_retire"):
        result = repo.retire_owner(tx, owner_id="owner")
        assert result.unresolved_action_ids
    retired = repo.retire_operations_for_owner(tx, owner_id="owner")
    assert retired.unresolved_action_ids == ()
    assert retired.retained_assignment_ids == (record.assignment_id,)
    assert current(repo, tx, record).lifecycle == "stopped"
    assert tx.fetch_one("SELECT count(*) AS n FROM persistent_assignment")["n"] == 1


def test_operation_decision_and_pause_race_serialize_observed_state(database, repo):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        record, value, decision = proposed(repo, tx)
    result = parallel_transactions(
        database,
        (
            lambda tx: repo.decide_action(tx, **command_args(record, value, decision)),
            lambda tx: control(repo, tx, record),
        ),
    )
    assert sum(isinstance(item, RepositoryConflictError) for item in result) == 1
    with database.transaction() as tx:
        after = current(repo, tx, record)
        assert after.state_version == record.state_version + 1


@pytest.mark.parametrize("liability", [False, True])
def test_recovery_deadline_terminal_only_without_issued_liability(tx, repo, liability):
    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    if liability:
        binding = bind(repo, tx, claim.fence)
        value = action(repo, tx, claim.fence, boundary="unreplayable")
        start(repo, tx, claim.fence, reserve(repo, tx, claim.fence, value), binding)
    expire_claim(tx, record)
    expire_authority(tx, record, "authority")
    expire_authority(tx, record, "deadline")
    repo.recover_expired_operations_for_administration(tx)
    after = current(repo, tx, record)
    assert after.next_wake_at is None
    if liability:
        assert after.lifecycle == "active"
        assert after.phase == "reconciliation"
        assert after.operation.get("terminal_outcome") is None
        assert after.usage["outstanding"]["tool_calls"] == 1
    else:
        assert after.lifecycle == "completed"
        assert after.operation["terminal_outcome"] == "failed"
        assert after.safe_error_code == "assignment_deadline_exceeded"


@pytest.mark.parametrize("authority_expired", [False, True])
def test_unclaimed_deadline_expires_without_execution_authority(tx, repo, authority_expired):
    record = create_operation(repo, tx)
    expire_authority(tx, record, "deadline")
    if authority_expired:
        expire_authority(tx, record, "authority")
    before = current(repo, tx, record)
    recovered = repo.recover_expired_operations_for_administration(tx)
    after = current(repo, tx, record)
    assert recovered.reclaimed_assignment_ids == (record.assignment_id,)
    assert recovered.operation_bindings == ()
    assert recovered.uncertain_action_ids == ()
    assert after.lifecycle == "completed"
    assert after.operation["terminal_outcome"] == "failed"
    assert after.safe_error_code == "assignment_deadline_exceeded"
    assert after.next_wake_at is None
    persisted = tx.fetch_one(
        "SELECT data FROM persistent_assignment WHERE id=%s", (record.assignment_id,)
    )["data"]
    assert persisted["claim_token"] is None
    assert persisted["claim_generation"] == 0
    assert after.usage == before.usage
    assert tx.fetch_one("SELECT count(*) AS n FROM persistent_assignment_action")["n"] == 0
    assert tx.fetch_one(
        "SELECT data->'consecutive_failures' AS failures FROM persistent_assignment WHERE id=%s",
        (record.assignment_id,),
    )["failures"] == 0
    assert repo.recover_expired_operations_for_administration(tx).reclaimed_assignment_ids == ()
    assert current(repo, tx, record) == after
    assert claim_operations(repo, tx, worker_id="late") == ()


@pytest.mark.parametrize("excluded", ["not_due", "claimed", "stopped", "unknown_version"])
def test_unclaimed_expiry_does_not_mutate_ineligible_work(tx, repo, excluded):
    record = create_operation(repo, tx)
    if excluded == "claimed":
        operation_claim(repo, tx)
    elif excluded == "stopped":
        control(repo, tx, record, "stop")
    if excluded != "not_due":
        expire_authority(tx, record, "deadline")
    if excluded == "unknown_version":
        mutate(tx, record, lambda d: d["operation"].update(version=3, deadline_at={"opaque": 1}))
    before = tx.fetch_one(
        "SELECT data FROM persistent_assignment WHERE id=%s", (record.assignment_id,)
    )["data"]
    assert repo.recover_expired_operations_for_administration(tx).reclaimed_assignment_ids == ()
    assert tx.fetch_one(
        "SELECT data FROM persistent_assignment WHERE id=%s", (record.assignment_id,)
    )["data"] == before


@pytest.mark.parametrize("liability", ["orphan", "approval", "uncertain"])
def test_unclaimed_deadline_retains_liability_for_reconciliation(tx, repo, liability):
    if liability in {"approval", "uncertain"}:
        record, _, _ = (proposed if liability == "approval" else uncertain)(repo, tx)
        data = plain(tx.fetch_one(
            "SELECT data FROM persistent_assignment WHERE id=%s", (record.assignment_id,)
        )["data"])
        repo._clear_claim(data)
        data["phase"] = "waiting"
        repo._save(tx, data)
    else:
        record = create_operation(repo, tx)
        mutate(tx, record, lambda d: d["usage"]["outstanding"].update(tool_calls=1))
    expire_authority(tx, record, "deadline")
    before = current(repo, tx, record)
    action_rows = tx.fetch_all("SELECT data FROM persistent_assignment_action ORDER BY id")
    result = repo.recover_expired_operations_for_administration(tx)
    assert result.reclaimed_assignment_ids == (record.assignment_id,)
    after = current(repo, tx, record)
    assert after.lifecycle == "active"
    assert after.phase == "reconciliation"
    assert after.operation.get("terminal_outcome") is None
    assert after.usage == before.usage
    assert tx.fetch_all("SELECT data FROM persistent_assignment_action ORDER BY id") == action_rows
    assert repo.recover_expired_operations_for_administration(tx).reclaimed_assignment_ids == ()
    assert current(repo, tx, record) == after


@pytest.mark.parametrize(
    "case",
    [
        "intent_digest",
        "foreground",
        "result",
        "missing_token_binding",
        "missing_outcome",
        "future_binding",
        "future_outcome",
        "future_actual",
        "invalid_outcome",
        "malformed_decision",
        "malformed_attempt",
        "unknown_decision",
        "unknown_reconciliation",
        "uncertain_outcome",
        "failed_outcome",
        "missing_terminal_proof",
        "contradictory_result",
        "missing_attempts",
    ],
)
def test_stop_and_purge_preserve_malformed_settled_action_envelopes(tx, repo, case):
    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    binding = bind(repo, tx, claim.fence)
    value = action(repo, tx, claim.fence)
    permit = start(repo, tx, claim.fence, reserve(repo, tx, claim.fence, value), binding)
    outcome(repo, tx, permit, record.assignment_id)

    def corrupt(data):
        attempt = data["attempts"][0]
        if case == "intent_digest":
            data["intent_digest"] = digest("wrong")
        elif case == "foreground":
            data["foreground_admission"] = {"future": 2}
        elif case == "result":
            data["result"]["future"] = 2
        elif case == "missing_token_binding":
            attempt["binding"] = None
        elif case == "missing_outcome":
            attempt["outcome"] = None
        elif case == "future_binding":
            attempt["binding"]["future"] = 2
        elif case == "future_outcome":
            attempt["outcome"]["future"] = 2
        elif case == "future_actual":
            attempt["outcome"]["actual"] = {"future": 2}
        elif case == "invalid_outcome":
            attempt["outcome"]["outcome"] = "future"
        elif case == "malformed_decision":
            data["decision"] = {"future": 2}
        elif case == "malformed_attempt":
            data["attempts"] = ["future"]
        elif case == "unknown_decision":
            data["decision"] = plain(
                AssignmentActionDecision(
                    digest("proposal"),
                    "future",
                    uid(),
                    digest("decision"),
                    digest("permission"),
                    digest("precondition"),
                )
            )
        elif case == "unknown_reconciliation":
            data["reconciliation"] = plain(
                AssignmentActionReconciliation(
                    digest("prior"),
                    "future",
                    "evidence:1",
                    uid(),
                    digest("decision"),
                )
            )
        elif case == "missing_terminal_proof":
            attempt.update(dispatch_token=None, binding=None, outcome=None)
        elif case == "contradictory_result":
            data["result"]["outcome"] = "uncertain"
        elif case == "missing_attempts":
            data["attempts"] = []
        else:
            attempt["outcome"]["outcome"] = "uncertain" if case == "uncertain_outcome" else "failed"

    change_action(tx, value.action_id, corrupt)
    before = action_bytes(tx, value.action_id)
    stopped = control(repo, tx, current(repo, tx, record), "stop")
    assert stopped.begun_action_ids == (value.action_id,)
    with pytest.raises(RepositoryConflictError, match="assignment_action_uncertain"):
        repo.delete_for_owner(tx, **delete_args(stopped.assignment))
    assert action_bytes(tx, value.action_id) == before


@pytest.mark.parametrize(
    "settlement", ["reserved", "succeeded", "uncertain_then_succeeded", "reconciled"]
)
def test_known_actions_can_be_cancelled_or_settled_then_purged(tx, repo, settlement):
    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    binding = bind(repo, tx, claim.fence)
    value = action(repo, tx, claim.fence)
    reservation = reserve(repo, tx, claim.fence, value)
    if settlement != "reserved":
        permit = start(repo, tx, claim.fence, reservation, binding)
        if settlement in {"uncertain_then_succeeded", "reconciled"}:
            unknown = AssignmentActionOutcome("uncertain", digest("unknown"), {})
            outcome(repo, tx, permit, record.assignment_id, outcome=unknown)
        if settlement == "reconciled":
            record = current(repo, tx, record)
            decision = AssignmentActionReconciliation(
                unknown.result_digest, "confirmed_not_applied", "receipt:none", uid(), digest("no")
            )
            repo.reconcile_action(tx, **command_args(record, value, decision))
        else:
            outcome(repo, tx, permit, record.assignment_id)
    stopped = control(repo, tx, current(repo, tx, record), "stop")
    assert stopped.begun_action_ids == ()
    assert stopped.assignment.usage["outstanding"]["tool_calls"] == 0
    assert repo.delete_for_owner(tx, **delete_args(stopped.assignment))


def test_reconciliation_task_without_action_defers_retirement(tx, repo):
    from astralplane.repositories.assignments import AssignmentTask

    record = create_operation(repo, tx)
    task = AssignmentTask("work", "plan", 1, "Task", "Inspect result", (), state="reconciliation")
    mutate(tx, record, lambda d: d.update(tasks=[plain(task)]))
    result = repo.retire_operations_for_owner(tx, owner_id="owner")
    assert result.retained_assignment_ids == (record.assignment_id,)
    assert result.unresolved_action_ids == ()
    after = current(repo, tx, record)
    assert after.tasks[0]["state"] == "reconciliation"
    with pytest.raises(RepositoryConflictError, match="assignment_action_uncertain"):
        repo.delete_for_owner(tx, **delete_args(after))


def test_mixed_profile_retirement_serializes_against_new_admission(database, repo):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        create(repo, tx)
        create_operation(repo, tx)
    results = parallel_transactions(
        database,
        (
            lambda tx: repo.retire_operations_for_owner(tx, owner_id="owner"),
            lambda tx: create_operation(repo, tx, caller_key="racing-admission"),
        ),
    )
    for result in results:
        if isinstance(result, RepositoryConflictError):
            assert result.code == "assignment_owner_retired"
    with database.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM persistent_assignment")["n"] == 0
        assert tx.fetch_one("SELECT count(*) AS n FROM assignment_operation_receipt")["n"] == 0
        assert (
            tx.fetch_one("SELECT state FROM astralplane_blob_owner_state WHERE owner_id='owner'")[
                "state"
            ]
            == "retired"
        )


@pytest.mark.parametrize(
    "corruption", ["prior_digest", "result_receipt", "attempt_state", "missing_receipt"]
)
def test_mismatched_reconciliation_receipt_cannot_prove_purge_safe(tx, repo, corruption):
    record, value, decision = uncertain(repo, tx)
    repo.reconcile_action(tx, **command_args(record, value, decision))

    def change(data):
        if corruption == "prior_digest":
            data["reconciliation"]["prior_result_digest"] = digest("another-observation")
        elif corruption == "result_receipt":
            data["result"]["reconciliation"]["prior_result_digest"] = digest("another-observation")
        elif corruption == "attempt_state":
            data["attempts"][0]["state"] = "failed"
        else:
            data["reconciliation"] = None

    change_action(tx, value.action_id, change)
    before = action_bytes(tx, value.action_id)
    result = repo.retire_operations_for_owner(tx, owner_id="owner")
    assert result.unresolved_action_ids == (value.action_id,)
    assert result.retained_assignment_ids == (record.assignment_id,)
    assert action_bytes(tx, value.action_id) == before


def test_recovery_uncertainty_reconciliation_can_eventually_purge(tx, repo):
    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    binding = bind(repo, tx, claim.fence)
    value = action(repo, tx, claim.fence, boundary="unreplayable")
    start(repo, tx, claim.fence, reserve(repo, tx, claim.fence, value), binding)
    expire_claim(tx, record)
    repo.recover_expired_operations_for_administration(tx)
    record = current(repo, tx, record)
    decision = AssignmentActionReconciliation(
        digest([value.action_id, "lease_expired"]),
        "confirmed_applied",
        "receipt:1",
        uid(),
        digest("decision"),
    )
    repo.reconcile_action(tx, **command_args(record, value, decision))
    result = repo.retire_operations_for_owner(tx, owner_id="owner")
    assert result.retained_assignment_ids == ()
    assert result.deleted_assignment_ids == (record.assignment_id,)


@pytest.mark.parametrize("hold", ["reconciliation_task", "outstanding_usage"])
def test_past_deadline_reconciliation_cannot_hide_other_liabilities(tx, repo, hold):
    from astralplane.repositories.assignments import AssignmentTask

    record, value, decision = uncertain(repo, tx)
    if hold == "reconciliation_task":
        task = AssignmentTask("work", "plan", 1, "Task", "Inspect", (), state="reconciliation")
        mutate(tx, record, lambda d: d.update(tasks=[plain(task)]))
    else:
        mutate(tx, record, lambda d: d["usage"]["outstanding"].update(tokens=1))
    expire_authority(tx, record, "deadline")
    before = current(repo, tx, record)
    repo.reconcile_action(tx, **command_args(before, value, decision))
    after = current(repo, tx, record)
    assert after.lifecycle == "active"
    assert after.phase == "reconciliation"
    assert after.next_wake_at is None
    assert after.operation.get("terminal_outcome") is None
    assert repo.retire_operations_for_owner(tx, owner_id="owner").retained_assignment_ids == (
        record.assignment_id,
    )
