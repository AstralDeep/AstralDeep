"""Real-PostgreSQL tests for the shared execution guard in
astralplane.repositories.assignments and offline_grants: declared lock ordering,
contention without deadlock, and rejection of lost or malformed fences.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest
from test_assignments_postgres import (
    action,
    create,
    create_operation,
    definition,
    expire_claim,
    finish,
    independent_database,
    parallel_transactions,
    session_observation,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_control_postgres import control, current, mutate, operation_claim
from test_operation_payload_postgres import admission, issued, settle_args
from test_operation_terminal_postgres import expire_authority

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.assignments import (
    AssignmentActionDecision,
    digest,
    plain,
)
from astralplane.repositories.offline_grants import OfflineGrantRepository


def claimed(repo, tx, *, profile="interactive", admission_owner="owner"):
    if profile == "persistent":
        record = create(repo, tx)
        claim = repo.claim_due_for_administration(tx, worker_id="guard-test")[0]
    elif profile == "interactive_grant":
        grant_definition = definition(tx)
        record = create_operation(
            repo,
            tx,
            caller_key="interactive-grant",
            definition=replace(
                grant_definition,
                source={},
                allowed_tools=(),
                limits={
                    k: v
                    for k, v in grant_definition.limits.items()
                    if not k.startswith("daily_") and k != "cadence_seconds"
                },
            ),
        )
        claim = operation_claim(repo, tx)
    else:
        record = create_operation(repo, tx)
        claim = operation_claim(repo, tx)
    work, selected, binding = admission(repo, tx, claim, owner=admission_owner)
    return record, claim, work, selected, binding


@pytest.mark.parametrize("profile", ["interactive", "interactive_grant", "persistent"])
def test_current_execution_guard_preserves_supported_profiles_without_mutation(tx, repo, profile):
    record, claim, _, _, binding = claimed(repo, tx, profile=profile)
    before = repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
    guarded = repo.assert_current_assignment_execution(
        tx, fence=claim.fence, binding=binding, authority=session_observation(tx)
    )
    assert guarded == before
    assert repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id) == before
    assert binding.execution_lease_token not in str(plain(guarded))


@pytest.mark.parametrize(
    "loss",
    [
        "revision",
        "epoch",
        "generation",
        "token",
        "pause",
        "stop",
        "claim_expired",
        "authority",
        "deadline",
        "admission",
        "owner",
    ],
)
def test_guard_refuses_each_lost_fence_before_any_following_write(tx, repo, loss):
    record, claim, work, selected, binding = claimed(repo, tx)
    fence = claim.fence
    changes = {
        "revision": "instruction_revision",
        "epoch": "control_epoch",
        "generation": "claim_generation",
    }
    if loss in changes:
        fence = replace(fence, **{changes[loss]: getattr(fence, changes[loss]) + 1})
    elif loss == "token":
        fence = replace(fence, claim_token=uid())
    elif loss in {"pause", "stop"}:
        control(repo, tx, current(repo, tx, record), loss)
    elif loss == "claim_expired":
        expire_claim(tx, record)
    elif loss in {"authority", "deadline"}:
        expire_authority(tx, record, loss)
    elif loss == "admission":
        work.reselect_execution(tx, selected.fence, now=None, slot_lease=timedelta(minutes=1))
    else:
        repo.retire_operations_for_owner(tx, owner_id="owner")
    with pytest.raises(RepositoryConflictError):
        repo.assert_current_assignment_execution(
            tx, fence=fence, binding=binding, authority=session_observation(tx)
        )


@pytest.mark.parametrize(
    "kind",
    [
        "fence_type",
        "binding_type",
        "boolean_generation",
        "wrong_binding",
        "wrong_owner",
        "admission_owner",
        "malformed_action",
    ],
)
def test_guard_rejects_malformed_or_misbound_execution_context(tx, repo, kind):
    _, claim, _, _, binding = claimed(
        repo, tx, admission_owner="other" if kind == "admission_owner" else "owner"
    )
    values = {"fence": claim.fence, "binding": binding, "authority": session_observation(tx)}
    if kind == "fence_type":
        values["fence"] = plain(claim.fence)
    elif kind == "binding_type":
        values["binding"] = plain(binding)
    elif kind == "boolean_generation":
        values["fence"] = replace(claim.fence, claim_generation=True)
    elif kind == "wrong_binding":
        values["binding"] = replace(binding, execution_lease_token=uid())
    elif kind == "wrong_owner":
        values["fence"] = replace(claim.fence, owner_id="other")
    elif kind == "malformed_action":
        values["action_id"] = "private invalid reference"
    with pytest.raises(
        (RepositoryConflictError, RepositoryValidationError, RepositoryNotFoundError)
    ):
        repo.assert_current_assignment_execution(tx, **values)


class ObservedTransaction:
    def __init__(self, transaction):
        self.transaction = transaction
        self.statements = []

    def fetch_one(self, statement, parameters=()):
        self.statements.append(statement)
        return self.transaction.fetch_one(statement, parameters)

    def execute(self, statement, parameters=()):
        self.statements.append(statement)
        return self.transaction.execute(statement, parameters)

    def __getattr__(self, name):
        return getattr(self.transaction, name)


def test_guard_acquires_authority_assignment_admission_locks_in_declared_order(tx, repo):
    _, claim, _, _, binding = claimed(repo, tx, profile="interactive_grant")
    observed = ObservedTransaction(tx)
    repo.assert_current_assignment_execution(
        observed, fence=claim.fence, binding=binding, authority=session_observation(tx)
    )
    locked = [
        query
        for query in observed.statements
        if "FOR UPDATE" in query or "pg_advisory_xact_lock" in query
    ]
    assert "pg_advisory_xact_lock" in locked[0]
    indexes = {
        table: next(i for i, query in enumerate(locked) if table in query)
        for table in (
            "astralplane_blob_owner_state",
            "web_session",
            "user_offline_grant",
            "persistent_assignment",
            "operation_record",
        )
    }
    assert list(indexes.values()) == sorted(indexes.values())
    assert all(
        "refresh_token" not in query
        for query in observed.statements
        if "user_offline_grant" in query
    )


def _reset(tx):
    tx.execute("DELETE FROM assignment_operation_receipt")
    tx.execute("DELETE FROM persistent_assignment")
    tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")


def _wait_for_lock(tx, waiting_pid, blocking_pid):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if (
            blocking_pid
            in tx.fetch_one("SELECT pg_blocking_pids(%s) AS pids", (waiting_pid,))["pids"]
        ):
            return
        time.sleep(0.01)
    pytest.fail("execution guard did not wait on the expected PostgreSQL lock")


@pytest.mark.parametrize("profile", ["persistent", "interactive_grant"])
@pytest.mark.postgresql_lock_catalog
def test_grant_revocation_committed_during_lock_wait_refuses_guard(database, repo, profile):
    with database.transaction() as tx:
        _reset(tx)
        record, claim, _, _, binding = claimed(repo, tx, profile=profile)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    waiting, identities = Event(), {}

    def guard():
        with independent_database(schema) as db, db.transaction() as tx:
            identities["guard"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            waiting.set()
            try:
                return repo.assert_current_assignment_execution(
                    tx, fence=claim.fence, binding=binding, authority=session_observation(tx)
                )
            except RepositoryConflictError as error:
                return error

    with ThreadPoolExecutor(max_workers=1) as worker:
        with database.transaction() as tx:
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            OfflineGrantRepository().revoke_grant(
                tx, owner_id="owner", grant_id=record.definition.offline_grant_id, revoked_at=2
            )
            future = worker.submit(guard)
            assert waiting.wait(3)
            _wait_for_lock(tx, identities["guard"], blocker)
        result = future.result(timeout=5)
    assert isinstance(result, RepositoryConflictError)
    assert result.code == "assignment_authorization_unavailable"


@pytest.mark.parametrize("competitor", ["settlement", "retirement", "pause"])
def test_guard_contends_with_existing_owner_mutations_without_deadlock(database, repo, competitor):
    with database.transaction() as tx:
        _reset(tx)
        record, claim, _, _, binding, _, permit = issued(repo, tx)

    def guard(tx):
        tx.execute("SET LOCAL lock_timeout='2s'")
        return repo.assert_current_assignment_execution(
            tx, fence=claim.fence, binding=binding, authority=session_observation(tx)
        )

    def other(tx):
        tx.execute("SET LOCAL lock_timeout='2s'")
        if competitor == "settlement":
            return repo.record_action_outcome(tx, **settle_args(tx, record, claim, binding, permit))
        if competitor == "retirement":
            return repo.retire_operations_for_owner(tx, owner_id="owner")
        return control(repo, tx, current(repo, tx, record), "pause")

    guarded, changed = parallel_transactions(database, (guard, other))
    assert not isinstance(changed, Exception)
    if competitor == "settlement":
        assert not isinstance(guarded, Exception)
        assert changed.result["result_available"] is True
    else:
        assert isinstance(guarded, RepositoryConflictError) or guarded.lifecycle == "active"


@pytest.mark.parametrize("expiry", ["claim", "authority", "deadline", "grant"])
@pytest.mark.postgresql_lock_catalog
def test_guard_resamples_database_time_after_admission_lock_wait(database, repo, expiry):
    with database.transaction() as tx:
        _reset(tx)
        record, claim, _, _, binding = claimed(
            repo, tx, profile="interactive_grant" if expiry == "grant" else "interactive"
        )
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
        due = tx.fetch_one("SELECT clock_timestamp()+interval '1 second' AS due")["due"]
        if expiry == "claim":
            tx.execute(
                "UPDATE persistent_assignment SET lease_expires_at=%s,"
                "data=jsonb_set(data,'{lease_expires_at}',to_jsonb(%s::text)) WHERE id=%s",
                (due, plain(due), record.assignment_id),
            )
        elif expiry == "grant":
            tx.execute(
                "UPDATE user_offline_grant SET expires_at=%s WHERE id=%s",
                (int(due.timestamp() * 1000), record.definition.offline_grant_id),
            )
        else:

            def change(data):
                if expiry == "deadline":
                    data["operation"]["deadline_at"] = plain(due)
                else:
                    data["operation"]["authority"]["expires_at"] = plain(due)

            mutate(tx, record, change)
    waiting, identities = Event(), {}

    def guard():
        with independent_database(schema) as db, db.transaction() as tx:
            identities["guard"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            waiting.set()
            try:
                return repo.assert_current_assignment_execution(
                    tx, fence=claim.fence, binding=binding, authority=session_observation(tx)
                )
            except RepositoryConflictError as error:
                return error

    with ThreadPoolExecutor(max_workers=1) as worker:
        with database.transaction() as tx:
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            tx.fetch_one(
                "SELECT operation_id FROM operation_record WHERE operation_id=%s FOR UPDATE",
                (binding.operation_id,),
            )
            future = worker.submit(guard)
            assert waiting.wait(3)
            _wait_for_lock(tx, identities["guard"], blocker)
            deadline = time.monotonic() + 3
            while not tx.fetch_one("SELECT clock_timestamp()>%s AS expired", (due,))["expired"]:
                assert time.monotonic() < deadline
                time.sleep(0.01)
        result = future.result(timeout=5)
    assert isinstance(result, RepositoryConflictError)
    expected = {"claim": "assignment_claim_stale", "deadline": "assignment_deadline_exceeded"}.get(
        expiry, "assignment_authorization_unavailable"
    )
    assert result.code == expected


def test_approved_claim_guard_requires_exact_action_and_keeps_legacy_flow(tx, repo):
    record = create(repo, tx)
    claim = repo.claim_due_for_administration(tx, worker_id="test")[0]
    due = tx.fetch_one("SELECT clock_timestamp()+interval '1 minute' AS due")["due"]
    created = action(
        repo,
        tx,
        claim.fence,
        sensitivity="sensitive",
        interactive_only=True,
        approval_expires_at=due,
    )
    finish(repo, tx, claim.fence, phase="waiting_approval")
    repo.decide_action(
        tx,
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=created.action_id,
        expected_instruction_revision=1,
        expected_control_epoch=1,
        decision=AssignmentActionDecision(
            created.intent.request_digest,
            "approve",
            uid(),
            digest("approve"),
            created.intent.permission_digest,
            created.intent.precondition_digest,
        ),
    )
    approved = repo.claim_for_approved_action(
        tx,
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=created.action_id,
        expected_request_digest=created.intent.request_digest,
        expected_instruction_revision=1,
        expected_control_epoch=1,
        interactive_receipt_id="current-human-session",
        submission_id=uid(),
        submission_digest=digest("approved"),
        worker_id="foreground",
    )
    _, _, binding = admission(repo, tx, approved)
    for action_id in (None, uid()):
        with pytest.raises(RepositoryConflictError, match="assignment_action_claim_restricted"):
            repo.assert_current_assignment_execution(
                tx,
                fence=approved.fence,
                binding=binding,
                action_id=action_id,
                authority=session_observation(tx),
            )
    assert (
        repo.assert_current_assignment_execution(
            tx,
            fence=approved.fence,
            binding=binding,
            action_id=created.action_id,
            authority=session_observation(tx),
        ).assignment_id
        == record.assignment_id
    )


@pytest.mark.parametrize("case", ["framework", "future", "missing_grant", "changed_grant"])
def test_guard_refuses_unverified_or_replaced_local_authority(tx, repo, case):
    record, claim, _, _, binding = claimed(
        repo,
        tx,
        profile="interactive_grant"
        if case in {"missing_grant", "changed_grant"}
        else "interactive",
    )
    if case == "framework":
        mutate(
            tx,
            record,
            lambda data: data["operation"]["authority"].update(
                origin="framework", reference_kind="credential", reference_id="unverified-reference"
            ),
        )
    elif case == "future":
        mutate(tx, record, lambda data: data["operation"].update(version=3))
    elif case == "missing_grant":
        tx.execute(
            "DELETE FROM user_offline_grant WHERE id=%s", (record.definition.offline_grant_id,)
        )
    else:
        replacement = definition(tx).offline_grant_id

        class ReplaceSelectedGrant(ObservedTransaction):
            def fetch_one(self, statement, parameters=()):
                if "persistent_assignment" in statement and "FOR UPDATE" in statement:
                    mutate(
                        self.transaction,
                        record,
                        lambda data: data["definition"].update(offline_grant_id=replacement),
                    )
                return super().fetch_one(statement, parameters)

        tx = ReplaceSelectedGrant(tx)
    with pytest.raises(RepositoryConflictError):
        repo.assert_current_assignment_execution(
            tx, fence=claim.fence, binding=binding, authority=session_observation(tx)
        )
