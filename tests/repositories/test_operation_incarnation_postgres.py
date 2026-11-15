"""Real-PostgreSQL tests for astralplane.repositories.assignments and history: admission
binds an exact session incarnation, claims are exclusive and replay-safe, and legacy
v1 permits settle once without reopening execution.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from test_assignment_execution_guard_postgres import _reset, _wait_for_lock
from test_assignments_postgres import (
    action,
    claim_operations,
    create_operation,
    definition,
    independent_database,
    operation_observation,
    reserve,
    session_observation,
    start,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_control_postgres import control, current, mutate, operation_claim
from test_operation_payload_postgres import admission, settle_args

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.assignments import (
    AssignmentOperationAuthority,
    AssignmentOperationSpec,
    AssignmentRepository,
    canonical,
    digest,
    plain,
)
from astralplane.repositories.history import SessionExecutionObservation, SessionRepository


def arguments(tx):
    observation = session_observation(tx, session_id=uid())
    values = definition(tx)
    now = observation.started_at
    return dict(
        owner_id="owner",
        assignment_id=uid(),
        origin_namespace="web",
        caller_key=uid(),
        command_digest=digest("fresh"),
        definition=replace(
            values,
            source={},
            allowed_tools=(),
            offline_grant_id=None,
            limits={
                k: v
                for k, v in values.limits.items()
                if not k.startswith("daily_") and k != "cadence_seconds"
            },
        ),
        operation=AssignmentOperationSpec(
            "chat",
            AssignmentOperationAuthority(
                "owner",
                "interactive",
                "session_incarnation",
                observation.credential.incarnation_id,
                now + timedelta(minutes=5),
            ),
            now + timedelta(minutes=5),
            "none",
        ),
        authority=observation,
    )


def rows(tx):
    return (
        tx.fetch_all("SELECT * FROM persistent_assignment ORDER BY id"),
        tx.fetch_all("SELECT * FROM assignment_operation_receipt ORDER BY assignment_id"),
        tx.fetch_all("SELECT * FROM persistent_assignment_action ORDER BY id"),
    )


def exact_claim(repo, tx, record, **changes):
    args = dict(
        owner_id=record.owner_id,
        assignment_id=record.assignment_id,
        expected_state_version=record.state_version,
        worker_id="operation-v2",
        authority=changes.get("authority")
        if "authority" in changes
        else operation_observation(tx, record),
    )
    args.update(changes)
    return repo.claim_operation_for_administration(tx, **args)


def retire_session(tx, observation):
    sessions = SessionRepository()
    old = sessions.delete_and_return(
        tx,
        owner_id=observation.credential.owner_id,
        session_id=observation.credential.session_id,
        expected_incarnation_id=observation.credential.incarnation_id,
    )
    assert old is not None
    replacement = sessions.put(tx, replace(old, incarnation_id=None))
    state = sessions.get_execution_state(tx, owner_id=old.owner_id, session_id=old.session_id)
    assert replacement.incarnation_id != old.incarnation_id
    return SessionExecutionObservation(
        state.credential, state.observed_at, state.observed_at + timedelta(seconds=15)
    )


def test_new_operation_stores_original_incarnation_without_observation_payload(tx, repo):
    args = arguments(tx)
    record = repo.create_operation(tx, **args)
    assert record.operation["version"] == 2
    assert (
        record.operation["authority"]["reference_id"] == args["authority"].credential.incarnation_id
    )
    data = canonical(rows(tx)[0][0]["data"])
    for private in (
        args["authority"].credential.session_id,
        args["authority"].credential.encrypted_state_binding,
        "valid_until",
    ):
        assert private not in data
    assert repo.get_operation(
        tx, owner_id="owner", assignment_id=record.assignment_id
    ).continuation_supported


@pytest.mark.parametrize(
    "loss",
    [
        "missing",
        "mapping",
        "old_fence",
        "owner",
        "incarnation",
        "expired",
        "future",
        "overlong",
        "replacement_old",
        "replacement_current",
        "hard_expiry",
        "v1",
        "legacy_kind",
    ],
)
def test_new_admission_refuses_bad_or_replaced_authority_without_rows(tx, repo, loss):
    args = arguments(tx)
    observed = args["authority"]
    if loss == "missing":
        args["authority"] = None
    elif loss == "mapping":
        args["authority"] = plain(observed)
    elif loss in {"old_fence", "owner", "incarnation"}:
        change = {
            "old_fence": {"version": 1},
            "owner": {"owner_id": "foreign"},
            "incarnation": {"incarnation_id": uid()},
        }[loss]
        args["authority"] = replace(observed, credential=replace(observed.credential, **change))
    elif loss in {"expired", "future", "overlong"}:
        change = {
            "expired": dict(
                started_at=observed.started_at - timedelta(seconds=16),
                valid_until=observed.valid_until - timedelta(seconds=16),
            ),
            "future": dict(started_at=observed.started_at + timedelta(seconds=5)),
            "overlong": dict(valid_until=observed.valid_until + timedelta(seconds=1)),
        }[loss]
        args["authority"] = replace(observed, **change)
    elif loss.startswith("replacement"):
        fresh = retire_session(tx, observed)
        if loss == "replacement_current":
            args["authority"] = fresh
    elif loss == "hard_expiry":
        args["operation"] = replace(
            args["operation"],
            authority=replace(
                args["operation"].authority,
                expires_at=datetime.fromtimestamp(observed.credential.hard_expires_at + 1, UTC),
            ),
        )
    else:
        args["operation"] = replace(
            args["operation"],
            version=1 if loss == "v1" else 2,
            authority=replace(
                args["operation"].authority,
                reference_kind="session",
                reference_id=observed.credential.session_id,
            ),
        )
    before = rows(tx)
    with pytest.raises((RepositoryConflictError, RepositoryValidationError)):
        repo.create_operation(tx, **args)
    assert rows(tx) == before


def test_receipt_replay_keeps_v1_binding_and_precedes_new_observation_or_intent(tx, repo):
    args = arguments(tx)
    accepted = repo.create_operation(tx, **args)
    mutate(
        tx,
        accepted,
        lambda data: data["operation"].update(
            version=1,
            authority=dict(
                data["operation"]["authority"],
                reference_kind="session",
                reference_id=args["authority"].credential.session_id,
            ),
        ),
    )
    original = current(repo, tx, accepted)
    retire_session(tx, args["authority"])
    before = rows(tx)
    replay = repo.create_operation(
        tx, **dict(args, assignment_id=uid(), definition=None, operation=None, authority=None)
    )
    assert replay == original and replay.operation["version"] == 1
    assert rows(tx) == before
    assert not repo.get_operation(
        tx, owner_id="owner", assignment_id=accepted.assignment_id
    ).continuation_supported
    with pytest.raises(RepositoryConflictError, match="assignment_idempotency_conflict"):
        repo.create_operation(tx, **dict(args, command_digest=digest("changed")))


@pytest.mark.parametrize("year", [2030, 9999])
@pytest.mark.parametrize("overrun_microseconds", [0, 1])
def test_creation_expiry_ceiling_preserves_exact_subsecond_boundary(
    tx, repo, year, overrun_microseconds
):
    args = arguments(tx)
    observed = args["authority"]
    sessions = SessionRepository()
    original = sessions.delete_and_return(
        tx,
        owner_id="owner",
        session_id=observed.credential.session_id,
        expected_incarnation_id=observed.credential.incarnation_id,
    )
    hard_expiry = datetime(year, 1, 1, tzinfo=UTC)
    hard_seconds = (hard_expiry - datetime(1970, 1, 1, tzinfo=UTC)).days * 86400
    sessions.put(tx, replace(original, incarnation_id=None, hard_expires_at=hard_seconds))
    fresh = session_observation(tx, session_id=original.session_id)
    expiry = hard_expiry + timedelta(microseconds=overrun_microseconds)
    if year == 9999:
        assert expiry.timestamp() == hard_seconds
    args["authority"] = fresh
    args["operation"] = replace(
        args["operation"],
        authority=replace(
            args["operation"].authority,
            reference_id=fresh.credential.incarnation_id,
            expires_at=expiry,
        ),
    )
    before = rows(tx)
    if overrun_microseconds:
        with pytest.raises(RepositoryConflictError, match="assignment_authorization_unavailable"):
            repo.create_operation(tx, **args)
        assert rows(tx) == before
    else:
        assert repo.create_operation(tx, **args).operation["authority"]["expires_at"] == plain(
            expiry
        )


def test_late_create_refusal_rolls_back_receipt_and_assignment_when_caught(tx):
    args = arguments(tx)

    class ExpireAfterInsert(AssignmentRepository):
        checks = 0

        def _assert_creation_authority(
            self, transaction, owner_id, definition, operation, authority
        ):
            self.checks += 1
            if self.checks == 2:
                authority = replace(
                    authority,
                    started_at=authority.started_at - timedelta(seconds=16),
                    valid_until=authority.valid_until - timedelta(seconds=16),
                )
            return super()._assert_creation_authority(
                transaction, owner_id, definition, operation, authority
            )

    repo = ExpireAfterInsert()
    before = rows(tx)
    with pytest.raises(RepositoryConflictError):
        repo.create_operation(tx, **args)
    assert repo.checks == 2 and rows(tx) == before
    assert tx.fetch_one("SELECT 1 AS unrelated")["unrelated"] == 1


def test_readonly_discovery_and_exact_claim_keep_bulk_api_closed(tx, repo):
    first = create_operation(repo, tx)
    second = create_operation(repo, tx, caller_key="second")
    before = rows(tx)
    page = repo.discover_due_operations_for_administration(tx, limit=1)
    assert len(page) == 1 and page[0].assignment_id == first.assignment_id
    following = repo.discover_due_operations_for_administration(
        tx, after_due_at=page[0].next_wake_at, after_id=page[0].assignment_id, limit=1
    )
    assert [r.assignment_id for r in following] == [second.assignment_id]
    assert rows(tx) == before
    with pytest.raises(RepositoryConflictError, match="assignment_authorization_unavailable"):
        repo.claim_operations_for_administration(tx, worker_id="untyped")
    claimed = exact_claim(repo, tx, first)
    assert claimed.assignment.assignment_id == first.assignment_id
    with pytest.raises(RepositoryConflictError, match="assignment_revision_conflict"):
        exact_claim(repo, tx, first)
    assert repo.discover_due_operations_for_administration(tx) == (second,)


@pytest.mark.parametrize(
    "change",
    [
        {"limit": True},
        {"limit": 1.0},
        {"limit": 101},
        {"after_id": "bad"},
        {"after_due_at": datetime.now(UTC)},
        {"after_id": uid(), "after_due_at": True},
    ],
)
def test_discovery_cursor_is_strict(tx, repo, change):
    with pytest.raises((RepositoryValidationError, ValueError, TypeError)):
        repo.discover_due_operations_for_administration(tx, **change)


@pytest.mark.parametrize(
    "loss",
    [
        "observation",
        "replacement",
        "revision",
        "future_due",
        "paused",
        "phase",
        "version1",
        "version3",
    ],
)
def test_exact_claim_refuses_stale_or_nonexecutable_candidate(tx, repo, loss):
    args = arguments(tx)
    record = repo.create_operation(tx, **args)
    changes = {"authority": args["authority"]}
    if loss == "observation":
        changes["authority"] = None
    elif loss == "replacement":
        changes["authority"] = retire_session(tx, args["authority"])
    elif loss == "revision":
        changes["expected_state_version"] = record.state_version + 1
    elif loss == "paused":
        record = control(repo, tx, record).assignment
    elif loss == "future_due":
        data = plain(rows(tx)[0][0]["data"])
        data["next_wake_at"] = plain(args["authority"].started_at + timedelta(days=1))
        tx.execute(
            "UPDATE persistent_assignment SET data=%s::jsonb,next_wake_at=%s WHERE id=%s",
            (canonical(data), data["next_wake_at"], record.assignment_id),
        )
    elif loss == "phase":
        mutate(tx, record, lambda data: data.update(phase="awaiting_event"))
    else:
        mutate(
            tx,
            record,
            lambda data: data["operation"].update(
                version=1 if loss == "version1" else 3,
                authority=dict(
                    data["operation"]["authority"],
                    reference_kind="session",
                    reference_id=args["authority"].credential.session_id,
                ),
            ),
        )
    before = rows(tx)
    with pytest.raises(RepositoryConflictError):
        exact_claim(repo, tx, record, **changes)
    assert rows(tx) == before


@pytest.mark.parametrize("boundary", ["session", "assignment", "receipt"])
@pytest.mark.postgresql_lock_catalog
def test_actual_lock_wait_crossing_observation_expiry_has_no_partial_claim_or_create(
    database, repo, boundary
):
    with database.transaction() as tx:
        _reset(tx)
        args = arguments(tx)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
        record = repo.create_operation(tx, **args) if boundary == "assignment" else None
    ready = Event()
    state = {}

    def worker():
        with independent_database(schema) as db, db.transaction() as tx:
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            with pytest.raises(RepositoryConflictError):
                if record:
                    exact_claim(repo, tx, record, authority=args["authority"])
                else:
                    repo.create_operation(tx, **args)
            return rows(tx)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            args["authority"] = replace(
                args["authority"],
                valid_until=tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
                + timedelta(seconds=1),
            )
            if boundary == "session":
                tx.fetch_one(
                    "SELECT sid FROM web_session WHERE sid=%s FOR UPDATE",
                    (args["authority"].credential.session_id,),
                )
            elif boundary == "assignment":
                tx.fetch_one(
                    "SELECT id FROM persistent_assignment WHERE id=%s FOR UPDATE",
                    (record.assignment_id,),
                )
            else:
                tx.execute("LOCK TABLE assignment_operation_receipt IN SHARE MODE")
            before = rows(tx)
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            pending = pool.submit(worker)
            assert ready.wait(3)
            _wait_for_lock(tx, state["pid"], blocker)
            tx.fetch_one("SELECT pg_sleep(1.05)")
        assert pending.result(timeout=5) == before
    with database.transaction() as tx:
        assert rows(tx) == before


def legacy_record(tx, record, session_id="session-reference"):
    mutate(
        tx,
        record,
        lambda data: data["operation"].update(
            version=1,
            authority=dict(
                data["operation"]["authority"], reference_kind="session", reference_id=session_id
            ),
        ),
    )


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "uncertain"])
def test_authentic_v1_permit_settles_once_without_output_or_wake(tx, repo, outcome):
    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    _, _, binding = admission(repo, tx, claim)
    intent = action(repo, tx, claim.fence)
    permit = start(repo, tx, claim.fence, reserve(repo, tx, claim.fence, intent), binding)
    legacy_record(tx, record)
    before = current(repo, tx, record)
    args = settle_args(tx, record, claim, binding, permit)
    args["outcome"] = replace(args["outcome"], outcome=outcome)
    settled = repo.record_action_outcome(tx, **args)
    after = current(repo, tx, record)
    assert not settled.result["result_available"] and settled.result["result"] == {}
    assert after.wake_generation == before.wake_generation
    assert after.checkpoint == before.checkpoint
    assert after.next_wake_at == before.next_wake_at
    assert after.usage["spent"].get("tool_calls", 0) == (0 if outcome == "uncertain" else 1)
    assert after.usage["outstanding"]["tool_calls"] == (1 if outcome == "uncertain" else 0)
    receipt = rows(tx)
    assert repo.record_action_outcome(tx, **args) == settled
    assert rows(tx) == receipt
    with pytest.raises(RepositoryConflictError):
        repo.record_action_outcome(tx, **dict(args, dispatch_token=uid()))
    assert rows(tx) == receipt


@pytest.mark.parametrize(
    "boundary",
    ["bind", "renew", "claim", "prepare", "reserve", "permit", "checkpoint", "resume", "wake"],
)
def test_v1_readable_but_cannot_reenter_any_execution_boundary(tx, repo, boundary):
    from test_assignments_postgres import finish
    from test_operation_control_postgres import wake_args

    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    _, _, binding = admission(repo, tx, claim)
    prepared = action(repo, tx, claim.fence)
    reserved = reserve(repo, tx, claim.fence, prepared)
    legacy_record(tx, record)
    before = rows(tx)
    calls = {
        "bind": lambda: repo.bind_operation(tx, fence=claim.fence, binding=binding),
        "renew": lambda: repo.renew_claim(tx, fence=claim.fence),
        "claim": lambda: repo.claim_operation_for_administration(
            tx,
            owner_id="owner",
            assignment_id=record.assignment_id,
            expected_state_version=current(repo, tx, record).state_version,
            worker_id="old",
            authority=session_observation(tx),
        ),
        "prepare": lambda: action(repo, tx, claim.fence),
        "reserve": lambda: reserve(repo, tx, claim.fence, prepared),
        "permit": lambda: start(repo, tx, claim.fence, reserved, binding),
        "checkpoint": lambda: finish(repo, tx, claim.fence),
        "resume": lambda: control(repo, tx, current(repo, tx, record), "resume"),
        "wake": lambda: repo.accept_wake(tx, **wake_args(current(repo, tx, record))),
    }
    with pytest.raises(RepositoryConflictError):
        calls[boundary]()
    assert rows(tx) == before
    assert repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)


@pytest.mark.parametrize("boundary", ["read_only", "unreplayable"])
def test_v1_expired_lease_recovers_liabilities_without_scheduling_retry(tx, repo, boundary):
    from test_assignments_postgres import expire_claim

    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    _, _, binding = admission(repo, tx, claim)
    prepared = action(repo, tx, claim.fence, boundary=boundary)
    start(repo, tx, claim.fence, reserve(repo, tx, claim.fence, prepared), binding)
    legacy_record(tx, record)
    expire_claim(tx, record)
    recovered = repo.recover_expired_operations_for_administration(tx)
    assert record.assignment_id in recovered.reclaimed_assignment_ids
    after = current(repo, tx, record)
    assert after.next_wake_at is None
    raw = rows(tx)[0][0]["data"]
    assert raw["next_retry_at"] is None and raw["claim_token"] is None
    assert after.usage["spent"].get("tool_calls", 0) == (1 if boundary == "read_only" else 0)
    assert after.usage["outstanding"]["tool_calls"] == (0 if boundary == "read_only" else 1)
    assert repo.discover_due_operations_for_administration(tx) == ()
    assert claim_operations(repo, tx, worker_id="new") == ()


def install_historical_ledgers(tx):
    import hashlib
    import json
    from pathlib import Path

    data = (Path(__file__).parents[1] / "fixtures/session_incarnation_088001.json").read_bytes()
    assert (
        hashlib.sha256(data).hexdigest()
        == "36ef17d004c846209ab2f806c02ec33e487491d914f86062d9851cbc46849530"
    )
    fixture = json.loads(data)
    assert fixture["source_commit"] == "718021ba019abcd3a04ffbd6b80b88e51395bda6"
    for table in (
        "persistent_assignment",
        "assignment_operation_receipt",
        "persistent_assignment_action",
        "persistent_assignment_activity",
    ):
        for row in fixture["tables"][table]:
            columns = tuple(row)
            assert all(key.replace("_", "").isalnum() for key in columns)
            values = tuple(
                canonical(row[key]) if isinstance(row[key], dict) else row[key] for key in columns
            )
            placeholders = ",".join(
                "%s::jsonb" if isinstance(row[key], dict) else "%s" for key in columns
            )
            tx.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", values)
    return fixture


@pytest.mark.parametrize("resolution", ["receipt", "reconcile_applied", "reconcile_not_applied"])
def test_genuine_718_issued_and_uncertain_ledgers_remain_settleable(tx, repo, resolution):
    from astralplane.repositories.assignments import (
        AssignmentActionOutcome,
        AssignmentActionReconciliation,
    )

    fixture = install_historical_ledgers(tx)
    tx.execute("DELETE FROM web_session WHERE user_id='owner'")
    for permit in fixture["permits"]:
        assignment = repo.get_assignment(
            tx, owner_id="owner", assignment_id=permit["assignment_id"]
        )
        before_wake = assignment.wake_generation
        original_action = repo.get_action(
            tx,
            owner_id="owner",
            assignment_id=permit["assignment_id"],
            action_id=permit["action_id"],
        )
        if resolution != "receipt" and original_action.state == "uncertain":
            decision = AssignmentActionReconciliation(
                submission_id=uid(),
                submission_digest=digest("reconcile"),
                decision="confirmed_applied"
                if resolution == "reconcile_applied"
                else "confirmed_not_applied",
                prior_result_digest=original_action.result["result_digest"],
                evidence_reference="synthetic-settlement-proof",
            )
            args = dict(
                owner_id="owner",
                assignment_id=permit["assignment_id"],
                action_id=permit["action_id"],
                expected_instruction_revision=assignment.instruction_revision,
                expected_control_epoch=assignment.control_epoch,
                expected_state_version=assignment.state_version,
                decision=decision,
            )
            apply = repo.reconcile_action
        else:
            args = dict(
                owner_id="owner",
                assignment_id=permit["assignment_id"],
                action_id=permit["action_id"],
                attempt_id=permit["attempt_id"],
                dispatch_token=permit["dispatch_token"],
                expected_request_digest=permit["request_digest"],
                outcome=AssignmentActionOutcome(
                    "succeeded", digest("legacy-result"), {"private": "late"}
                ),
            )
            apply = repo.record_action_outcome
        settled = apply(tx, **args)
        assert settled.result["result"] == {} and not settled.result["result_available"]
        after = repo.get_assignment(tx, owner_id="owner", assignment_id=permit["assignment_id"])
        assert after.usage["outstanding"]["tool_calls"] == 0
        assert after.usage["spent"]["tool_calls"] == 1
        assert after.wake_generation == before_wake and after.checkpoint == assignment.checkpoint
        before_replay = rows(tx)
        assert apply(tx, **args) == settled
        assert rows(tx) == before_replay
    assert repo.discover_due_operations_for_administration(tx) == ()


def approved_arguments(repo, tx):
    from test_assignments_postgres import finish

    from astralplane.repositories.assignments import AssignmentActionDecision

    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    prepared = action(
        repo,
        tx,
        claim.fence,
        sensitivity="sensitive",
        interactive_only=True,
        approval_expires_at=tx.fetch_one("SELECT clock_timestamp()+interval '5 minutes' AS due")[
            "due"
        ],
    )
    finish(repo, tx, claim.fence, phase="waiting_approval", wake_reason="approval")
    observed = current(repo, tx, record)
    repo.decide_action(
        tx,
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=prepared.action_id,
        expected_instruction_revision=1,
        expected_control_epoch=1,
        expected_state_version=observed.state_version,
        decision=AssignmentActionDecision(
            prepared.intent.request_digest,
            "approve",
            uid(),
            digest("approve"),
            prepared.intent.permission_digest,
            prepared.intent.precondition_digest,
        ),
    )
    return dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=prepared.action_id,
        expected_instruction_revision=1,
        expected_control_epoch=1,
        expected_state_version=current(repo, tx, record).state_version,
        expected_request_digest=prepared.intent.request_digest,
        interactive_receipt_id=uid(),
        submission_id=uid(),
        submission_digest=digest("approved claim"),
        worker_id="foreground",
        authority=operation_observation(tx, record),
    )


@pytest.mark.parametrize("loss", [None, "missing", "replacement", "stale_state", "late"])
def test_approved_one_shot_claim_requires_current_original_authority_and_rolls_back(tx, repo, loss):
    args = approved_arguments(repo, tx)
    if loss == "missing":
        args["authority"] = None
    elif loss == "replacement":
        args["authority"] = retire_session(tx, args["authority"])
    elif loss == "stale_state":
        args["expected_state_version"] -= 1
    elif loss == "late":

        class ExpireFinal(AssignmentRepository):
            checks = 0

            def _assert_operation_claim_current(self, transaction, data, authority):
                self.checks += 1
                if self.checks == 2:
                    authority = replace(
                        authority,
                        started_at=authority.started_at - timedelta(seconds=16),
                        valid_until=authority.valid_until - timedelta(seconds=16),
                    )
                return super()._assert_operation_claim_current(transaction, data, authority)

        repo = ExpireFinal()
    before = rows(tx)
    if loss:
        with pytest.raises(RepositoryConflictError):
            repo.claim_for_approved_action(tx, **args)
        assert rows(tx) == before
        if loss == "late":
            assert repo.checks == 2
    else:
        claimed = repo.claim_for_approved_action(tx, **args)
        assert claimed.approved_action_id == args["action_id"]
        stored = repo.get_action(
            tx, owner_id="owner", assignment_id=args["assignment_id"], action_id=args["action_id"]
        )
        assert stored.state == "approved"
        raw = tx.fetch_one(
            "SELECT data FROM persistent_assignment_action WHERE id=%s", (args["action_id"],)
        )["data"]
        assert raw["foreground_admission"]["claim_generation"] == claimed.fence.claim_generation
        with pytest.raises(RepositoryConflictError):
            repo.claim_for_approved_action(tx, **args)


@pytest.mark.parametrize("envelope", ["v1", "future", "scheduled", "framework", "delegation"])
def test_discovery_filters_nonexecutable_neighbors_before_page_limit(tx, repo, envelope):
    first = create_operation(repo, tx)

    def change(data):
        op = data["operation"]
        if envelope == "v1":
            op.update(
                version=1,
                authority=dict(
                    op["authority"], reference_kind="session", reference_id="session-reference"
                ),
            )
        elif envelope == "future":
            op.update(version=3, future={"opaque": True})
        else:
            origin, kind = {
                "scheduled": ("scheduled", "offline_grant"),
                "framework": ("framework", "credential"),
                "delegation": ("interactive", "delegation"),
            }[envelope]
            op["authority"].update(origin=origin, reference_kind=kind, reference_id="opaque")

    mutate(tx, first, change)
    next_record = create_operation(repo, tx, caller_key="next")
    assert repo.discover_due_operations_for_administration(tx, limit=1) == (next_record,)
    before = rows(tx)
    with pytest.raises(RepositoryConflictError):
        repo.claim_operation_for_administration(
            tx,
            owner_id="owner",
            assignment_id=first.assignment_id,
            expected_state_version=first.state_version,
            worker_id="refused",
            authority=session_observation(tx),
        )
    assert rows(tx) == before


def test_two_exact_claimers_cannot_obtain_same_operation(database, repo):
    from test_assignments_postgres import parallel_transactions

    with database.transaction() as tx:
        _reset(tx)
        record = create_operation(repo, tx)
        observed = operation_observation(tx, record)

    def attempt(tx):
        return repo.claim_operation_for_administration(
            tx,
            owner_id=record.owner_id,
            assignment_id=record.assignment_id,
            expected_state_version=record.state_version,
            authority=observed,
            worker_id=uid(),
        )

    results = parallel_transactions(database, (attempt, attempt))
    assert sum(not isinstance(value, Exception) for value in results) == 1
    assert sum(isinstance(value, RepositoryConflictError) for value in results) == 1


def test_genuine_v1_late_receipt_racing_reconciliation_charges_once(database, repo):
    from test_assignments_postgres import parallel_transactions

    from astralplane.repositories.assignments import (
        AssignmentActionOutcome,
        AssignmentActionReconciliation,
    )

    with database.transaction() as tx:
        _reset(tx)
        fixture = install_historical_ledgers(tx)
        permit = fixture["permits"][1]
        assignment = repo.get_assignment(
            tx, owner_id="owner", assignment_id=permit["assignment_id"]
        )
        prior = repo.get_action(
            tx,
            owner_id="owner",
            assignment_id=permit["assignment_id"],
            action_id=permit["action_id"],
        )
        assert prior.state == "uncertain"
        decision = AssignmentActionReconciliation(
            submission_id=uid(),
            submission_digest=digest("racing-reconcile"),
            decision="confirmed_applied",
            prior_result_digest=prior.result["result_digest"],
            evidence_reference="synthetic-observed-effect",
        )

    def receipt(tx):
        return repo.record_action_outcome(
            tx,
            owner_id="owner",
            assignment_id=permit["assignment_id"],
            action_id=permit["action_id"],
            attempt_id=permit["attempt_id"],
            dispatch_token=permit["dispatch_token"],
            expected_request_digest=permit["request_digest"],
            outcome=AssignmentActionOutcome("succeeded", digest("factual"), {"private": "late"}),
        )

    def reconcile(tx):
        return repo.reconcile_action(
            tx,
            owner_id="owner",
            assignment_id=permit["assignment_id"],
            action_id=permit["action_id"],
            expected_instruction_revision=assignment.instruction_revision,
            expected_control_epoch=assignment.control_epoch,
            expected_state_version=assignment.state_version,
            decision=decision,
        )

    results = parallel_transactions(database, (receipt, reconcile))
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, RepositoryConflictError) for result in results) == 1
    with database.transaction() as tx:
        after = repo.get_assignment(tx, owner_id="owner", assignment_id=permit["assignment_id"])
        assert (
            after.usage["spent"]["tool_calls"] == 1
            and after.usage["outstanding"]["tool_calls"] == 0
        )
        assert after.wake_generation == assignment.wake_generation
        stored = repo.get_action(
            tx,
            owner_id="owner",
            assignment_id=permit["assignment_id"],
            action_id=permit["action_id"],
        )
        assert stored.result["result"] == {} and not stored.result["result_available"]


def test_v1_reserved_no_permit_can_release_but_issued_cannot_refund(tx, repo):
    record = create_operation(repo, tx)
    claimed = operation_claim(repo, tx)
    _, _, binding = admission(repo, tx, claimed)
    prepared = action(repo, tx, claimed.fence)
    reserved = reserve(repo, tx, claimed.fence, prepared)
    issued_action = action(repo, tx, claimed.fence)
    issued = start(
        repo, tx, claimed.fence, reserve(repo, tx, claimed.fence, issued_action), binding
    )
    legacy_record(tx, record)
    args = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=prepared.action_id,
        attempt_id=reserved.attempt_id,
        expected_request_digest=prepared.intent.request_digest,
        reason_code="assignment_authorization_unavailable",
    )
    released = repo.release_unstarted_action(tx, **args)
    assert released.state == "failed_not_started"
    before = rows(tx)
    assert repo.release_unstarted_action(tx, **args) == released
    with pytest.raises(RepositoryConflictError):
        repo.release_unstarted_action(
            tx,
            **dict(
                args,
                action_id=issued.action_id,
                attempt_id=issued.attempt_id,
                expected_request_digest=issued.request_digest,
            ),
        )
    assert rows(tx) == before
    assert current(repo, tx, record).usage["outstanding"]["tool_calls"] == 1


@pytest.mark.parametrize("blocker", ["owner", "session", "assignment"])
@pytest.mark.postgresql_lock_catalog
def test_request_host_caps_before_first_operation_call_release_worker_while_blocker_remains(
    database, repo, blocker
):
    with database.transaction() as tx:
        _reset(tx)
        args = arguments(tx)
        record = repo.create_operation(tx, **args) if blocker == "assignment" else None
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]

    def worker():
        import psycopg2

        with independent_database(schema) as db:
            with pytest.raises(psycopg2.Error) as failure, db.transaction() as tx:
                SessionRepository.bound_request_execution_waits(tx)
                if record:
                    exact_claim(repo, tx, record, authority=args["authority"])
                else:
                    repo.create_operation(tx, **args)
            assert failure.value.pgcode in {"55P03", "57014"}
            with db.transaction() as tx:
                assert tx.fetch_one("SELECT 1 AS alive")["alive"] == 1
                assert (
                    tx.fetch_one(
                        "SELECT count(*) AS n FROM pg_locks "
                        "WHERE pid=pg_backend_pid() AND locktype='advisory'"
                    )["n"]
                    == 0
                )
                assert tx.fetch_one("SHOW lock_timeout")["lock_timeout"] == "0"
            return True

    with ThreadPoolExecutor(max_workers=1) as pool, database.transaction() as tx:
        if blocker == "owner":
            tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended('owner',79))")
        elif blocker == "session":
            tx.fetch_one(
                "SELECT sid FROM web_session WHERE sid=%s FOR UPDATE",
                (args["authority"].credential.session_id,),
            )
        else:
            tx.fetch_one(
                "SELECT id FROM persistent_assignment WHERE id=%s FOR UPDATE",
                (record.assignment_id,),
            )
        before = rows(tx)
        assert pool.submit(worker).result(timeout=3) is True
        assert rows(tx) == before
