"""Real-PostgreSQL tests for astralplane.repositories.assignments: one-shot
wait/wake/control transitions, retry and expiry bounds, concurrent-wake winner
selection, and completion-payload validation.
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
    finish,
    parallel_transactions,
    reserve,
    start,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import definition as legacy_definition
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.assignments import (
    AssignmentEpisodeCompletion,
    canonical,
    digest,
    plain,
)


def current(repo, tx, record):
    return repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)


def operation_claim(repo, tx):
    return claim_operations(repo, tx, worker_id="one-shot")[0]


def wait(repo, tx, claim, **changes):
    record = current(repo, tx, claim.assignment)
    values = dict(
        fence=claim.fence,
        expected_state_version=record.state_version,
        checkpoint={"schema_version": 1},
        completion_digest=digest("waiting"),
        event_key="source-observation",
        source_revision=1,
    )
    values.update(changes)
    return repo.set_event_wait(tx, **values)


def wake_args(record, **changes):
    values = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        expected_state_version=record.state_version,
        expected_instruction_revision=record.instruction_revision,
        expected_control_epoch=record.control_epoch,
        event_id="event-1",
        event_key="source-observation",
        source_revision=2,
        event_digest=digest("event-content"),
    )
    values.update(changes)
    return values


def control(repo, tx, record, command="pause", **changes):
    values = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        expected_state_version=record.state_version,
        expected_instruction_revision=record.instruction_revision,
        expected_control_epoch=record.control_epoch,
        submission_id=uid(),
        submission_digest=digest(command),
        control=command,
    )
    values.update(changes)
    return repo.apply_control(tx, **values)


def mutate(tx, record, change):
    data = plain(
        tx.fetch_one("SELECT data FROM persistent_assignment WHERE id=%s", (record.assignment_id,))[
            "data"
        ]
    )
    change(data)
    tx.execute(
        "UPDATE persistent_assignment SET data=%s::jsonb WHERE id=%s",
        (canonical(data), record.assignment_id),
    )


def test_wait_cleanup_and_exactly_once_wake(tx, repo):
    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    reserved = reserve(repo, tx, claim.fence, action(repo, tx, claim.fence))
    waiting = wait(repo, tx, claim)
    assert waiting.phase == "awaiting_event"
    assert waiting.next_wake_at is None
    assert waiting.usage["outstanding"]["tool_calls"] == 0
    assert (
        repo.list_actions(tx, owner_id="owner", assignment_id=record.assignment_id)[0].state
        == "failed_not_started"
    )
    assert reserved.action.assignment_id == record.assignment_id
    assert claim_operations(repo, tx, worker_id="later") == ()
    assert repo.claim_due_for_administration(tx, worker_id="legacy") == ()
    assert wait(repo, tx, claim, expected_state_version=waiting.state_version - 1) == waiting
    args = wake_args(waiting)
    first = repo.accept_wake(tx, **args)
    assert first.applied
    assert first.assignment.wake_generation == waiting.wake_generation + 1
    assert first.assignment.phase == "waiting"
    replay = repo.accept_wake(tx, **args)
    assert not replay.applied
    assert replay.assignment == first.assignment
    assert (
        repo.get_operation(tx, owner_id="owner", assignment_id=record.assignment_id).disposition
        == "queued"
    )


@pytest.mark.parametrize(
    "change,code",
    [
        ({"expected_state_version": 1}, "assignment_revision_conflict"),
        ({"expected_instruction_revision": 2}, "assignment_revision_conflict"),
        ({"expected_control_epoch": 2}, "assignment_revision_conflict"),
        ({"source_revision": 1}, "assignment_event_revision_conflict"),
        ({"event_key": "different"}, "assignment_event_key_conflict"),
    ],
)
def test_wake_denials_preserve_state(tx, repo, change, code):
    create_operation(repo, tx)
    waiting = wait(repo, tx, operation_claim(repo, tx))
    with pytest.raises(RepositoryConflictError, match=code):
        repo.accept_wake(tx, **wake_args(waiting, **change))
    assert current(repo, tx, waiting) == waiting


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_state_version", True),
        ("expected_control_epoch", False),
        ("expected_instruction_revision", 1.0),
        ("source_revision", "2"),
        ("source_revision", -1),
        ("event_id", ""),
        ("event_key", "x" * 129),
        ("event_digest", "not-a-digest"),
        ("control_version", 2),
    ],
)
def test_wake_rejects_malformed_commands(tx, repo, field, value):
    create_operation(repo, tx)
    waiting = wait(repo, tx, operation_claim(repo, tx))
    with pytest.raises(RepositoryValidationError):
        repo.accept_wake(tx, **wake_args(waiting, **{field: value}))


def test_wait_survives_pause_resume_and_duplicate_is_acknowledged_after_stop(tx, repo):
    create_operation(repo, tx)
    waiting = wait(repo, tx, operation_claim(repo, tx))
    paused = control(repo, tx, waiting).assignment
    assert paused.operation["control"]["wait"] == waiting.operation["control"]["wait"]
    with pytest.raises(RepositoryConflictError, match="assignment_not_waiting"):
        repo.accept_wake(tx, **wake_args(paused))
    resumed = control(repo, tx, paused, "resume").assignment
    assert resumed.phase == "awaiting_event" and resumed.next_wake_at is None
    args = wake_args(resumed)
    accepted = repo.accept_wake(tx, **args).assignment
    stopped = control(repo, tx, accepted, "stop").assignment
    assert not repo.accept_wake(tx, **args).applied
    assert current(repo, tx, stopped) == stopped
    with pytest.raises(RepositoryConflictError, match="assignment_idempotency_conflict"):
        repo.accept_wake(tx, **dict(args, event_digest=digest("different")))


def test_wait_and_wake_are_owner_and_profile_scoped(tx, repo):
    legacy = create(repo, tx)
    with pytest.raises(RepositoryConflictError, match="assignment_operation_required"):
        repo.accept_wake(tx, **wake_args(legacy))
    record = create_operation(repo, tx)
    waiting = wait(repo, tx, operation_claim(repo, tx))
    for owner, assignment_id in (("other", record.assignment_id), ("owner", uid())):
        with pytest.raises(RepositoryNotFoundError, match="assignment_not_found"):
            repo.accept_wake(tx, **wake_args(waiting, owner_id=owner, assignment_id=assignment_id))
    assert repo.get_operation(tx, owner_id="other", assignment_id=record.assignment_id) is None
    assert repo.get_operation(tx, owner_id="owner", assignment_id=legacy.assignment_id) is None
    assert repo.list_operations(tx, owner_id="other") == ()


@pytest.mark.parametrize("path", ["operation", "control", "checkpoint"])
def test_future_versions_inspect_cancel_and_do_not_starve_known_claims(tx, repo, path):
    unknown = create_operation(repo, tx)

    def upgrade(data):
        if path == "operation":
            data["operation"]["version"] = 3
            data["operation"]["future"] = {"opaque": "retained"}
        elif path == "control":
            data["operation"]["control"] = {"version": 2, "future": "retained"}
        else:
            data["checkpoint"] = {"schema_version": 2, "future": "retained"}

    mutate(tx, unknown, upgrade)
    read = repo.get_operation(tx, owner_id="owner", assignment_id=unknown.assignment_id)
    assert not read.continuation_supported and read.disposition == "unsupported_version"
    supported = create_operation(repo, tx, caller_key="later", command_digest=digest("later"))
    claimed = claim_operations(repo, tx, worker_id="safe", limit=1)
    assert [c.assignment.assignment_id for c in claimed] == [supported.assignment_id]
    for command in ("pause", "resume", "revise"):
        with pytest.raises(RepositoryConflictError, match="assignment_version_unsupported"):
            control(repo, tx, read.assignment, command)
    with pytest.raises(RepositoryConflictError, match="assignment_version_unsupported"):
        repo.accept_wake(tx, **wake_args(read.assignment))
    cancelled = control(repo, tx, read.assignment, "stop").assignment
    assert cancelled.operation == read.assignment.operation
    assert cancelled.checkpoint == read.assignment.checkpoint
    final = repo.get_operation(tx, owner_id="owner", assignment_id=unknown.assignment_id)
    assert final.disposition == "cancelled" and not final.continuation_supported


@pytest.mark.parametrize("path", ["version", "checkpoint", "control"])
def test_malformed_versions_are_corruption_not_forward_compatibility(tx, repo, path):
    record = create_operation(repo, tx)

    def corrupt(data):
        if path == "version":
            data["operation"]["version"] = True
        elif path == "checkpoint":
            data["checkpoint"]["schema_version"] = False
        else:
            data["operation"]["control"] = {"version": "2"}

    mutate(tx, record, corrupt)
    with pytest.raises(RepositoryDataError):
        repo.get_operation(tx, owner_id="owner", assignment_id=record.assignment_id)
    with pytest.raises(RepositoryDataError):
        control(repo, tx, record, "stop")


def test_one_shot_yield_retry_and_terminal_projection(tx, repo):
    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    yielded = finish(repo, tx, claim.fence, wake_reason="admission_retry", next_wake_at=now)
    assert yielded.next_wake_at is not None
    claim = operation_claim(repo, tx)
    failed = finish(repo, tx, claim.fence, phase="failed", wake_reason="transient")
    assert 4 <= (failed.next_wake_at - now).total_seconds() < 10
    assert (
        repo.get_operation(tx, owner_id="owner", assignment_id=record.assignment_id).disposition
        == "retry_eligible"
    )
    tx.execute(
        "WITH due AS MATERIALIZED (SELECT clock_timestamp()-interval '1 second' AS at) "
        "UPDATE persistent_assignment SET next_wake_at=due.at,"
        " data=jsonb_set(data,'{next_wake_at}',to_jsonb(due.at)) FROM due WHERE id=%s",
        (record.assignment_id,),
    )
    claim = operation_claim(repo, tx)
    done = finish(
        repo,
        tx,
        claim.fence,
        completed=True,
        terminal_outcome="failed",
        result_reference="result:owned:1",
    )
    assert done.lifecycle == "completed" and done.next_wake_at is None
    read = repo.get_operation(tx, owner_id="owner", assignment_id=record.assignment_id)
    assert read.disposition == "failed" and read.terminal_outcome == "failed"
    assert read.result_reference == "result:owned:1"


def test_no_implicit_one_shot_cadence_or_terminal_with_inflight_effect(tx, repo):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    with pytest.raises(RepositoryValidationError, match="explicit due"):
        finish(repo, tx, claim.fence)
    binding = bind(repo, tx, claim.fence)
    reservation = reserve(repo, tx, claim.fence, action(repo, tx, claim.fence))
    start(repo, tx, claim.fence, reservation, binding)
    with pytest.raises(RepositoryConflictError, match="assignment_action_in_flight"):
        wait(repo, tx, claim)
    stopped = control(repo, tx, current(repo, tx, claim.assignment), "stop")
    assert stopped.begun_action_ids == (reservation.action.action_id,)
    assert stopped.assignment.usage["outstanding"]["tool_calls"] == 1


def test_legacy_completion_signature_is_unchanged(tx, repo):
    record = create(repo, tx)
    claim = repo.claim_due_for_administration(tx, worker_id="legacy")[0]
    completion = AssignmentEpisodeCompletion(claim.assignment.state_version, {}, digest("legacy"))
    old_value = plain(completion)
    for key in ("terminal_outcome", "result_reference", "event_wait"):
        old_value.pop(key)
    result = repo.finish_episode(tx, fence=claim.fence, completion=completion)
    stored = tx.fetch_one(
        "SELECT data FROM persistent_assignment WHERE id=%s", (record.assignment_id,)
    )["data"]
    assert stored["last_completion"]["signature"] == digest(old_value)
    assert repo.finish_episode(tx, fence=claim.fence, completion=completion) == result


def test_concurrent_duplicate_wake_has_one_transition(database, repo):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        create_operation(repo, tx)
        waiting = wait(repo, tx, operation_claim(repo, tx))
    results = parallel_transactions(
        database,
        (
            lambda tx: repo.accept_wake(tx, **wake_args(waiting)),
            lambda tx: repo.accept_wake(tx, **wake_args(waiting)),
        ),
    )
    assert sorted(item.applied for item in results) == [False, True]
    assert {item.assignment.wake_generation for item in results} == {waiting.wake_generation + 1}


@pytest.mark.parametrize("command", ["pause", "stop"])
def test_concurrent_wake_control_has_one_observed_version_winner(database, repo, command):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        create_operation(repo, tx)
        waiting = wait(repo, tx, operation_claim(repo, tx))
    results = parallel_transactions(
        database,
        (
            lambda tx: repo.accept_wake(tx, **wake_args(waiting)),
            lambda tx: control(repo, tx, waiting, command),
        ),
    )
    assert sum(isinstance(item, RepositoryConflictError) for item in results) == 1
    with database.transaction() as tx:
        latest = current(repo, tx, waiting)
        assert latest.state_version == waiting.state_version + 1


def test_wake_receipts_never_evict_at_capacity_and_cancel_remains_available(tx, repo):
    create_operation(repo, tx)
    waiting = wait(repo, tx, operation_claim(repo, tx))

    def fill(data):
        data["operation"]["control"]["wake_receipts"] = {
            f"retained-{i}": digest(i) for i in range(128)
        }

    mutate(tx, waiting, fill)
    with pytest.raises(RepositoryConflictError, match="assignment_history_capacity_exhausted"):
        repo.accept_wake(tx, **wake_args(waiting))
    stopped = control(repo, tx, current(repo, tx, waiting), "stop").assignment
    assert len(stopped.operation["control"]["wake_receipts"]) == 128


@pytest.mark.parametrize("expired", ["authority", "deadline"])
def test_resume_and_new_wake_refuse_expiry_after_database_lock(tx, repo, expired):
    create_operation(repo, tx)
    waiting = wait(repo, tx, operation_claim(repo, tx))
    paused = control(repo, tx, waiting).assignment
    old = tx.fetch_one("SELECT clock_timestamp()-interval '1 second' AS now")["now"].isoformat()

    def expire(data):
        if expired == "authority":
            data["operation"]["authority"]["expires_at"] = old
        else:
            data["operation"]["deadline_at"] = old

    mutate(tx, waiting, expire)
    with pytest.raises(
        RepositoryConflictError, match=r"assignment_(authorization_unavailable|deadline_exceeded)"
    ):
        control(repo, tx, paused, "resume")


def test_operation_list_is_stable_bounded_and_filters_before_limit(tx, repo):
    create(repo, tx)
    records = [
        create_operation(repo, tx, caller_key=str(i), command_digest=digest(i)) for i in range(3)
    ]
    expected = sorted(item.assignment_id for item in records)
    first = repo.list_operations(tx, owner_id="owner", limit=2)
    assert [item.assignment.assignment_id for item in first] == expected[:2]
    last = repo.list_operations(tx, owner_id="owner", after_id=expected[1], limit=2)
    assert [item.assignment.assignment_id for item in last] == expected[2:]


@pytest.mark.parametrize(
    "change",
    [
        {"completed": 1},
        {"terminal_outcome": []},
        {"terminal_outcome": "cancelled", "completed": True},
        {"result_reference": "owned:result"},
        {"terminal_outcome": "failed"},
        {"completed": True, "result_reference": "x" * 513},
        {"checkpoint": []},
        {"checkpoint": {"schema_version": 2}},
        {"phase": "awaiting_event"},
        {"phase": "awaiting_event", "event_wait": 1},
        {"phase": "awaiting_event", "event_wait": [{"source_revision": 0}]},
        {"event_wait": {"event_key": "key", "source_revision": 0}},
    ],
)
def test_completion_rejects_invalid_terminal_or_wait_data(tx, repo, change):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    with pytest.raises(RepositoryValidationError):
        finish(repo, tx, claim.fence, **change)
    assert current(repo, tx, claim.assignment) == claim.assignment


def test_legacy_cannot_register_one_shot_wait(tx, repo):
    create(repo, tx)
    claim = repo.claim_due_for_administration(tx, worker_id="legacy")[0]
    with pytest.raises(RepositoryValidationError, match="one-shot completion required"):
        wait(repo, tx, claim)


def test_wait_watermarks_cannot_recede_or_grow_unbounded(tx, repo):
    create_operation(repo, tx)
    first = wait(repo, tx, operation_claim(repo, tx))
    repo.accept_wake(tx, **wake_args(first))
    claim = operation_claim(repo, tx)
    with pytest.raises(RepositoryConflictError, match="assignment_event_revision_conflict"):
        wait(repo, tx, claim, source_revision=1)

    def fill(data):
        data["operation"]["control"]["watermarks"] = {f"key-{i}": 1 for i in range(64)}

    mutate(tx, first, fill)
    with pytest.raises(RepositoryConflictError, match="assignment_history_capacity_exhausted"):
        wait(repo, tx, claim, event_key="another")


@pytest.mark.parametrize("kind", ["retry", "deadline", "authority"])
def test_bounded_retry_exhaustion_or_expiry_does_not_recur(tx, repo, kind):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    soon = tx.fetch_one("SELECT clock_timestamp()+interval '2 seconds' AS now")["now"].isoformat()

    def change(data):
        if kind == "retry":
            data["consecutive_failures"] = 3
        elif kind == "deadline":
            data["operation"]["deadline_at"] = soon
        else:
            data["operation"]["authority"]["expires_at"] = soon

    mutate(tx, claim.assignment, change)
    failed = finish(repo, tx, claim.fence, phase="failed", wake_reason="transient")
    assert failed.next_wake_at is None
    assert claim_operations(repo, tx, worker_id="later") == ()
    if kind == "authority":
        assert failed.phase == "waiting_authorization"
        assert failed.lifecycle == "active"
    else:
        assert failed.lifecycle == "completed"
        assert failed.operation["terminal_outcome"] == "failed"


def test_pause_resume_preserves_retry_due_and_does_not_repeat_backoff(tx, repo):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    failed = finish(repo, tx, claim.fence, phase="failed", wake_reason="transient")
    paused = control(repo, tx, failed).assignment
    resumed = control(repo, tx, paused, "resume").assignment
    assert resumed.phase == "failed"
    assert resumed.next_wake_at == failed.next_wake_at


def test_one_shot_controls_require_state_and_keep_foundation_receipt_replay(tx, repo):
    record = create_operation(repo, tx)
    for value in (None, True, 0, "1"):
        with pytest.raises(RepositoryValidationError):
            control(repo, tx, record, expected_state_version=value)
    submission = uid()
    paused = control(repo, tx, record, submission_id=submission).assignment
    original_signature = digest(["pause", None, 1, 1, digest("pause")])
    mutate(tx, paused, lambda d: d["controls"][submission].update(signature=original_signature))
    replay = control(repo, tx, record, submission_id=submission)
    assert not replay.applied
    assert replay.assignment.state_version == paused.state_version


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(control=[]),
        lambda d: d.update(control=None),
        lambda d: d.update(terminal_outcome="invented"),
        lambda d: d.update(result_reference=123),
        lambda d: d["control"].update(extra="unexpected"),
        lambda d: d["control"].update(wait={"event_key": "key"}),
        lambda d: d["control"].update(watermarks=[]),
        lambda d: d["control"].update(wake_receipts=[]),
        lambda d: d["control"].update(wake_receipts={"event": "bad-digest"}),
    ],
)
def test_malformed_control_is_not_read_as_trusted_state(tx, repo, change):
    create_operation(repo, tx)
    waiting = wait(repo, tx, operation_claim(repo, tx))
    mutate(tx, waiting, lambda data: change(data["operation"]))
    with pytest.raises(RepositoryDataError):
        repo.get_operation(tx, owner_id="owner", assignment_id=waiting.assignment_id)


def test_control_cannot_be_injected_at_admission(tx, repo):
    record = create_operation(repo, tx)
    operation = plain(record.operation)
    operation["control"] = {"version": 1}
    with pytest.raises(RepositoryValidationError, match="repository-owned"):
        create_operation(
            repo,
            tx,
            caller_key="new",
            command_digest=digest("new"),
            operation=operation,
            definition=record.definition,
        )


def test_one_shot_revise_and_claimed_resume_preserve_profile(tx, repo):
    record = create_operation(repo, tx)
    revised = control(
        repo,
        tx,
        record,
        "revise",
        replacement=replace(record.definition, instructions="Revised bounded request"),
    ).assignment
    assert revised.instruction_revision == record.instruction_revision + 1
    assert revised.execution_profile == "one_shot"
    claim = operation_claim(repo, tx)
    paused = control(repo, tx, claim.assignment).assignment
    read = repo.get_operation(tx, owner_id="owner", assignment_id=record.assignment_id)
    assert read.disposition == "paused"
    resumed = control(repo, tx, paused, "resume").assignment
    assert resumed.phase == "waiting" and resumed.next_wake_at is not None


def test_revoked_one_shot_cannot_resume_by_pausing_its_authority_hold(tx, repo):
    record = create_operation(repo, tx)
    revoked = control(repo, tx, record, "revoke").assignment
    paused = control(repo, tx, revoked).assignment
    with pytest.raises(RepositoryConflictError, match="assignment_authorization_unavailable"):
        control(repo, tx, paused, "resume")
    with pytest.raises(RepositoryConflictError, match="assignment_authorization_unavailable"):
        control(repo, tx, paused, "revise", replacement=record.definition)


@pytest.mark.parametrize("command", ["resume", "wake"])
def test_retired_owner_cannot_gain_continuation(tx, repo, command):
    create_operation(repo, tx)
    waiting = wait(repo, tx, operation_claim(repo, tx))
    if command == "resume":
        waiting = control(repo, tx, waiting).assignment
    tx.execute(
        "INSERT INTO astralplane_blob_owner_state(owner_id,state,version,retired_at,updated_at) "
        "VALUES('owner','retired',1,clock_timestamp(),clock_timestamp())"
    )
    with pytest.raises(RepositoryConflictError, match="assignment_owner_retired"):
        if command == "resume":
            control(repo, tx, waiting, "resume")
        else:
            repo.accept_wake(tx, **wake_args(waiting))
    assert current(repo, tx, waiting) == waiting


@pytest.mark.parametrize("state", ["proposed", "approved", "uncertain"])
def test_event_wait_and_retry_cannot_bypass_unresolved_effect(tx, repo, state):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    item = action(repo, tx, claim.fence)
    tx.execute(
        "UPDATE persistent_assignment_action SET state=%s,"
        "data=jsonb_set(data,'{state}',to_jsonb(%s::text)) WHERE id=%s",
        (state, state, item.action_id),
    )
    with pytest.raises(RepositoryConflictError, match="assignment_unfinished_work"):
        wait(repo, tx, claim)
    with pytest.raises(RepositoryConflictError, match="assignment_unfinished_work"):
        finish(repo, tx, claim.fence, phase="failed", wake_reason="transient")


def test_total_json_overflow_rolls_back_wake_receipt_and_transition(database, repo):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        create_operation(repo, tx)
        waiting = wait(repo, tx, operation_claim(repo, tx))
        data = plain(
            tx.fetch_one(
                "SELECT data FROM persistent_assignment WHERE id=%s", (waiting.assignment_id,)
            )["data"]
        )
        data["retained_future_metadata"] = ""
        data["retained_future_metadata"] = "x" * (262144 - len(canonical(data).encode()))
        tx.execute(
            "UPDATE persistent_assignment SET data=%s::jsonb WHERE id=%s",
            (canonical(data), waiting.assignment_id),
        )
    with pytest.raises(RepositoryValidationError, match="bound"), database.transaction() as tx:
        repo.accept_wake(tx, **wake_args(waiting, event_id="e" * 128))
    with database.transaction() as tx:
        unchanged = current(repo, tx, waiting)
        assert unchanged.phase == "awaiting_event"
        assert unchanged.operation["control"]["wake_receipts"] == {}
        assert unchanged.state_version == waiting.state_version


def test_scheduled_resume_requires_qualified_adapter_even_with_current_grant(tx, repo):
    base = create_operation(repo, tx)
    grant = legacy_definition(tx).offline_grant_id
    operation = plain(base.operation)
    operation["authority"].update(
        origin="scheduled", reference_kind="offline_grant", reference_id=grant
    )
    record = create_operation(
        repo,
        tx,
        caller_key="scheduled",
        command_digest=digest("scheduled"),
        definition=replace(base.definition, offline_grant_id=grant),
        operation=operation,
    )
    paused = control(repo, tx, record).assignment
    with pytest.raises(RepositoryConflictError, match="assignment_version_unsupported"):
        control(repo, tx, paused, "resume")
    assert current(repo, tx, paused) == paused
    tx.execute("UPDATE user_offline_grant SET revoked_at=1 WHERE id=%s", (grant,))
    with pytest.raises(RepositoryConflictError, match="assignment_version_unsupported"):
        control(repo, tx, paused, "resume")
    assert current(repo, tx, paused) == paused


def test_yield_does_not_reset_one_shot_retry_allowance(tx, repo):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    mutate(tx, claim.assignment, lambda data: data.update(consecutive_failures=2))
    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    finish(repo, tx, claim.fence, wake_reason="admission_retry", next_wake_at=now)
    next_claim = operation_claim(repo, tx)
    failed = finish(repo, tx, next_claim.fence, phase="failed", wake_reason="transient")
    assert 44 <= (failed.next_wake_at - now).total_seconds() < 50


@pytest.mark.parametrize("path", ["operation", "control", "checkpoint"])
def test_future_version_does_not_block_recovery_or_change_its_issued_liability(tx, repo, path):
    unknown = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    binding = bind(repo, tx, claim.fence)
    reservation = reserve(repo, tx, claim.fence, action(repo, tx, claim.fence))
    permit = start(repo, tx, claim.fence, reservation, binding)
    supported = create_operation(repo, tx, caller_key="later", command_digest=digest("later"))
    operation_claim(repo, tx)
    old = tx.fetch_one("SELECT clock_timestamp()-interval '1 minute' AS at")["at"]
    for record in (unknown, supported):
        expired = (old - timedelta(minutes=int(record == unknown))).isoformat()
        mutate(tx, record, lambda data, expired=expired: data.update(lease_expires_at=expired))
        tx.execute(
            "UPDATE persistent_assignment SET lease_expires_at=%s WHERE id=%s",
            (expired, record.assignment_id),
        )

    def upgrade(data):
        if path == "operation":
            data["operation"]["version"] = 3
        elif path == "control":
            data["operation"]["control"] = {"version": 2, "future": "unchanged"}
        else:
            data["checkpoint"]["schema_version"] = 2

    mutate(tx, unknown, upgrade)
    before = tx.fetch_one(
        "SELECT data FROM persistent_assignment WHERE id=%s", (unknown.assignment_id,)
    )["data"]
    effect = tx.fetch_one(
        "SELECT data FROM persistent_assignment_action WHERE id=%s", (permit.action_id,)
    )["data"]
    result = repo.recover_expired_operations_for_administration(tx, limit=1)
    assert result.reclaimed_assignment_ids == (supported.assignment_id,)
    assert (
        tx.fetch_one(
            "SELECT data FROM persistent_assignment WHERE id=%s", (unknown.assignment_id,)
        )["data"]
        == before
    )
    assert (
        tx.fetch_one(
            "SELECT data FROM persistent_assignment_action WHERE id=%s", (permit.action_id,)
        )["data"]
        == effect
    )
    assert effect["state"] == "started"
    assert before["usage"]["outstanding"]["tool_calls"] == 1
