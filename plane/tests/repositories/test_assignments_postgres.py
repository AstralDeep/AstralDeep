"""Real-PostgreSQL tests for astralplane.repositories.assignments: one-shot operation
creation, claim, recovery, framework-credential binding, and owner/replay/race
conformance against a live schema.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_REGISTRY,
    MigrationRunner,
)
from astralplane.database.pool import ConnectionPool
from astralplane.database.transaction import PlaneDatabase
from astralplane.errors import SchemaRevisionError
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.assignments import (
    AssignmentActionDecision,
    AssignmentActionIntent,
    AssignmentActionOutcome,
    AssignmentActionReconciliation,
    AssignmentActivityRecord,
    AssignmentDefinition,
    AssignmentEpisodeCompletion,
    AssignmentOperationBinding,
    AssignmentRepository,
    AssignmentResourceAmount,
    AssignmentSourceBatch,
    AssignmentSourceEvent,
    AssignmentTask,
    AssignmentTaskResult,
    canonical,
    digest,
    plain,
)
from tests.fixtures.migrated_template import bound_clone, current_lineages


def uid():
    return str(uuid.uuid4())


class Pool:
    def __init__(self, connection):
        self.connection = connection

    def getconn(self):
        return self.connection

    def putconn(self, connection, *, close=False):
        assert connection is self.connection

    def closeall(self):
        pass


def standalone_database():
    if not os.environ.get("ASTRALPLANE_TEST_POSTGRES_DSN"):
        pytest.skip("isolated PostgreSQL DSN required")
    with bound_clone(current_lineages().current()) as clone:
        pool = ConnectionPool(Pool(clone.connection))
        try:
            yield PlaneDatabase(pool)
        finally:
            pool.close()


@pytest.fixture(scope="module")
def database(migrated_template):
    with bound_clone(migrated_template) as clone:
        pool = ConnectionPool(Pool(clone.connection))
        try:
            yield PlaneDatabase(pool)
        finally:
            pool.close()


@pytest.fixture
def tx(database):
    with database.transaction() as transaction:
        transaction.execute("DELETE FROM assignment_operation_receipt")
        transaction.execute("DELETE FROM persistent_assignment")
        transaction.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        yield transaction


@pytest.fixture
def repo():
    return AssignmentRepository()


def definition(tx, **changes):
    grant = uid()
    tx.execute(
        "INSERT INTO user_offline_grant(id,user_id,refresh_token_enc,issued_at,expires_at) "
        "VALUES(%s,'owner',%s,1,9999999999999)",
        (grant, b"opaque-test-only"),
    )
    limits = dict(
        cadence_seconds=60,
        max_retries=3,
        max_concurrent_tasks=2,
        max_depth=4,
        max_tasks=32,
        model_calls=100,
        tool_calls=1000,
        tokens=100000,
        elapsed_ms=1000000,
        daily_model_calls=100,
        daily_tool_calls=1000,
        daily_tokens=100000,
        daily_elapsed_ms=1000000,
    )
    values = dict(
        name="Watch releases",
        instructions="Summarize new public releases",
        source={"reader": "web-research-1.fetch_page", "url": "https://example.org"},
        allowed_tools=("web-research-1.fetch_page",),
        consented_scopes=("tools:read",),
        offline_grant_id=grant,
        limits=limits,
    )
    values.update(changes)
    return AssignmentDefinition(**values)


def create(repo, tx, **changes):
    args = dict(
        owner_id="owner",
        assignment_id=uid(),
        submission_id=uid(),
        submission_digest=digest("create"),
        definition=definition(tx),
    )
    args.update(changes)
    return repo.create_assignment(tx, **args)


def claim(repo, tx):
    return repo.claim_due_for_administration(tx, worker_id="worker")[0]


def bind(repo, tx, fence):
    binding = AssignmentOperationBinding(uid(), 1, uid())
    repo.bind_operation(tx, fence=fence, binding=binding)
    return binding


def action(repo, tx, fence, **changes):
    request = {"tool": "web-research-1.fetch_page", "url": "https://example.org"}
    values = dict(
        action_key=uid(),
        request=request,
        request_digest=digest(request),
        maximum=AssignmentResourceAmount(tool_calls=1, elapsed_ms=1000),
        permission_digest=digest("permission"),
        precondition_digest=digest("precondition"),
        boundary="read_only",
    )
    values.update(changes)
    intent = AssignmentActionIntent(**values)
    return repo.put_action(tx, fence=fence, intent=intent)


def reserve(repo, tx, fence, record, **changes):
    values = dict(
        fence=fence,
        action_id=record.action_id,
        attempt_id=uid(),
        expected_request_digest=record.intent.request_digest,
        maximum=record.intent.maximum,
    )
    values.update(changes)
    return repo.reserve_action(tx, **values)


def start(repo, tx, fence, reservation, binding, **changes):
    record = reservation.action
    values = dict(
        fence=fence,
        action_id=record.action_id,
        attempt_id=reservation.attempt_id,
        expected_request_digest=record.intent.request_digest,
        current_permission_digest=record.intent.permission_digest,
        current_precondition_digest=record.intent.precondition_digest,
        binding=binding,
    )
    values.update(changes)
    return repo.start_action(tx, **values)


def outcome(repo, tx, permit, assignment_id, **changes):
    values = dict(
        owner_id="owner",
        assignment_id=assignment_id,
        action_id=permit.action_id,
        attempt_id=permit.attempt_id,
        dispatch_token=permit.dispatch_token,
        expected_request_digest=permit.request_digest,
        outcome=AssignmentActionOutcome("succeeded", digest("done"), {"text": "done"}),
    )
    values.update(changes)
    return repo.record_action_outcome(tx, **values)


def control(repo, tx, record, value="pause", **changes):
    values = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        expected_instruction_revision=record.instruction_revision,
        expected_control_epoch=record.control_epoch,
        submission_id=uid(),
        submission_digest=digest(value),
        control=value,
    )
    if record.execution_profile == "one_shot":
        values["expected_state_version"] = record.state_version
    values.update(changes)
    return repo.apply_control(tx, **values)


def finish(repo, tx, fence, **changes):
    current = repo.get_assignment(tx, owner_id="owner", assignment_id=fence.assignment_id)
    values = dict(
        expected_state_version=current.state_version,
        checkpoint=current.checkpoint,
        completion_digest=digest("finish"),
    )
    values.update(changes)
    return repo.finish_episode(tx, fence=fence, completion=AssignmentEpisodeCompletion(**values))


@pytest.mark.postgresql_schema_catalog
def test_current_assignment_structure_digest(database):
    from astralplane.database import migrations as m

    with database.transaction() as transaction:
        assert (
            m._schema_structure_digest(transaction.fetch_all(m.CURRENT_SCHEMA_STRUCTURE_QUERY))
            == m.CURRENT_SCHEMA_STRUCTURE_DIGEST
        )


def session_observation(tx, *, owner_id="owner", session_id="session-reference"):
    from astralplane.repositories.history import (
        SessionExecutionObservation,
        SessionRecord,
        SessionRepository,
    )

    sessions = SessionRepository()
    if sessions.get(tx, owner_id=owner_id, session_id=session_id) is None:
        now = int(tx.fetch_one("SELECT clock_timestamp() AS now")["now"].timestamp())
        sessions.put(
            tx,
            SessionRecord(
                session_id,
                owner_id,
                "synthetic-encrypted-access",
                "synthetic-encrypted-refresh",
                now,
                now + 3600,
                now,
                False,
                now,
            ),
        )
    state = sessions.get_execution_state(tx, owner_id=owner_id, session_id=session_id)
    return SessionExecutionObservation(
        state.credential, state.observed_at, state.observed_at + timedelta(seconds=15)
    )


def framework_credential_observation(tx, *, owner_id="owner", credential_id=None):
    from astralplane.repositories.framework_credentials import FrameworkCredentialRepository
    from astralplane.repositories.history import (
        FrameworkCredentialFence,
        FrameworkCredentialObservation,
        SessionRecord,
        SessionRepository,
    )

    sessions = SessionRepository()
    session_id = "framework-issuer-" + owner_id
    if sessions.get(tx, owner_id=owner_id, session_id=session_id) is None:
        now = int(tx.fetch_one("SELECT clock_timestamp() AS now")["now"].timestamp())
        sessions.put(
            tx,
            SessionRecord(
                session_id,
                owner_id,
                "synthetic-encrypted-access",
                "synthetic-encrypted-refresh",
                now,
                now + 3600,
                now,
                False,
                now,
            ),
        )
    incarnation = sessions.get(tx, owner_id=owner_id, session_id=session_id).incarnation_id
    token_hash = "a" * 64
    record = FrameworkCredentialRepository().issue(
        tx,
        owner_id=owner_id,
        credential_id=credential_id or uid(),
        name="test framework credential",
        scopes=("operations.submit",),
        token_hash=token_hash,
        token_prefix="afk_test",
        issuer_kind="session_incarnation",
        issuer_reference=incarnation,
        max_admissions=10,
        ttl_seconds=3600,
    )
    started = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    fence = FrameworkCredentialFence(
        owner_id=owner_id,
        credential_id=record.credential_id,
        token_hash=token_hash,
        scopes=record.scopes,
        max_admissions=record.max_admissions,
        consumed_admissions=record.consumed_admissions,
        created_at=record.created_at,
        expires_at=record.expires_at,
        revoked_at=record.revoked_at,
    )
    return record, FrameworkCredentialObservation(fence, started, started + timedelta(seconds=15))


def create_operation(repo, tx, **changes):
    from astralplane.repositories.assignment_models import (
        AssignmentOperationAuthority,
        AssignmentOperationSpec,
    )

    values = definition(tx)
    limits = {
        k: v
        for k, v in values.limits.items()
        if not k.startswith("daily_") and k != "cadence_seconds"
    }
    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    observed = session_observation(tx)
    args = dict(
        authority=observed,
        owner_id="owner",
        assignment_id=uid(),
        origin_namespace="web",
        caller_key="ordinary-send",
        command_digest=digest("ordinary-send"),
        definition=replace(
            values, source={}, allowed_tools=(), offline_grant_id=None, limits=limits
        ),
        operation=AssignmentOperationSpec(
            kind="chat",
            authority=AssignmentOperationAuthority(
                owner_id="owner",
                origin="interactive",
                reference_kind="session_incarnation",
                reference_id=observed.credential.incarnation_id,
                expires_at=now + timedelta(minutes=10),
            ),
            deadline_at=now + timedelta(minutes=5),
            source_retention="none",
        ),
    )
    args.update(changes)
    return repo.create_operation(tx, **args)


def operation_observation(tx, record):
    from astralplane.repositories.history import SessionExecutionObservation, SessionRepository

    sessions = SessionRepository()
    session = sessions.get_by_incarnation(
        tx, owner_id=record.owner_id, incarnation_id=record.operation["authority"]["reference_id"]
    )
    assert session is not None
    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    return SessionExecutionObservation(
        sessions.execution_fence(session), now, now + timedelta(seconds=15)
    )


def claim_operations(repo, tx, *, worker_id, limit=20, lease_seconds=30):
    return tuple(
        repo.claim_operation_for_administration(
            tx,
            owner_id=record.owner_id,
            assignment_id=record.assignment_id,
            expected_state_version=record.state_version,
            worker_id=worker_id,
            authority=operation_observation(tx, record),
            lease_seconds=lease_seconds,
        )
        for record in repo.discover_due_operations_for_administration(tx, limit=limit)
    )


def test_one_shot_profile_isolated_from_legacy_claims_and_capacity(tx, repo):
    first = create_operation(repo, tx)
    assert first.execution_profile == "one_shot"
    assert first.definition.offline_grant_id is None
    assert first.definition.source == {}
    assert repo.claim_due_for_administration(tx, worker_id="legacy") == ()
    assert repo.list_assignments(tx, owner_id="owner") == ()
    legacy = create(repo, tx, max_retained_assignments=1, max_owned_assignments=1)
    assert legacy.execution_profile == "persistent"
    assert claim(repo, tx).assignment.assignment_id == legacy.assignment_id
    durable = claim_operations(repo, tx, worker_id="operations")
    assert [item.assignment.assignment_id for item in durable] == [first.assignment_id]


def test_one_shot_receipt_is_original_key_bound_and_resolved_before_expansion(tx, repo):
    first = create_operation(repo, tx)
    replay = create_operation(repo, tx, assignment_id=uid(), definition=None, operation=None)
    assert replay == first
    assert (
        repo.get_operation_receipt(
            tx,
            owner_id="other",
            origin_namespace="web",
            caller_key="ordinary-send",
            command_digest=digest("ordinary-send"),
        )
        is None
    )
    with pytest.raises(RepositoryConflictError):
        create_operation(repo, tx, command_digest=digest("different"))
    second = create_operation(repo, tx, caller_key="different-key")
    assert second.assignment_id != first.assignment_id


def test_one_shot_authority_owner_expiry_and_unknown_fields_refused(tx, repo):
    from astralplane.repositories.assignment_models import (
        AssignmentOperationAuthority,
        AssignmentOperationSpec,
    )

    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    authority = AssignmentOperationAuthority(
        "other",
        "interactive",
        "session_incarnation",
        session_observation(tx).credential.incarnation_id,
        now + timedelta(minutes=5),
    )
    operation = AssignmentOperationSpec("chat", authority, now + timedelta(minutes=1), "none")
    with pytest.raises(RepositoryValidationError):
        create_operation(repo, tx, operation=operation)
    expired = replace(authority, owner_id="owner", expires_at=now - timedelta(seconds=1))
    with pytest.raises(RepositoryConflictError):
        create_operation(repo, tx, operation=replace(operation, authority=expired))
    unknown = replace(authority, owner_id="owner", reference_kind="trusted_admin")
    with pytest.raises(RepositoryValidationError):
        create_operation(repo, tx, operation=replace(operation, authority=unknown))


def test_one_shot_unattended_requires_current_offline_grant(tx, repo):
    from astralplane.repositories.assignment_models import (
        AssignmentOperationAuthority,
        AssignmentOperationSpec,
    )

    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    authority = AssignmentOperationAuthority(
        "owner", "scheduled", "offline_grant", uid(), now + timedelta(minutes=5)
    )
    operation = AssignmentOperationSpec("chat", authority, now + timedelta(minutes=1), "none")
    with pytest.raises(RepositoryConflictError):
        create_operation(repo, tx, operation=operation)


def test_one_shot_creation_does_not_rewrite_legacy_private_json(tx, repo):
    legacy = create(repo, tx)
    before = tx.fetch_one(
        "SELECT data FROM persistent_assignment WHERE id=%s", (legacy.assignment_id,)
    )["data"]
    create_operation(repo, tx)
    after = tx.fetch_one(
        "SELECT data FROM persistent_assignment WHERE id=%s", (legacy.assignment_id,)
    )["data"]
    assert canonical(after) == canonical(before)
    assert "execution_profile" not in after
    assert "operation" not in after


def test_one_shot_deleted_receipt_cannot_admit_a_second_effect(tx, repo):
    record = create_operation(repo, tx)
    stopped = control(repo, tx, record, "stop").assignment
    assert repo.delete_for_owner(
        tx,
        owner_id="owner",
        assignment_id=record.assignment_id,
        expected_control_epoch=stopped.control_epoch,
        expected_state_version=stopped.state_version,
    )
    receipt = tx.fetch_one(
        "SELECT assignment_id,live_assignment_id FROM assignment_operation_receipt"
    )
    assert str(receipt["assignment_id"]) == record.assignment_id
    assert receipt["live_assignment_id"] is None
    with pytest.raises(RepositoryConflictError, match="assignment_operation_deleted"):
        create_operation(repo, tx)
    with pytest.raises(RepositoryConflictError, match="assignment_history_capacity_exhausted"):
        create_operation(repo, tx, caller_key="new-key", max_retained_receipts=1)


@pytest.mark.parametrize("new_profile", ["one_shot", "persistent"])
def test_deleted_operation_receipt_never_follows_reused_assignment_uuid(tx, repo, new_profile):
    first = create_operation(repo, tx)
    stopped = control(repo, tx, first, "stop").assignment
    repo.delete_for_owner(
        tx,
        owner_id="owner",
        assignment_id=first.assignment_id,
        expected_control_epoch=stopped.control_epoch,
        expected_state_version=stopped.state_version,
    )
    if new_profile == "one_shot":
        replacement = create_operation(
            repo,
            tx,
            assignment_id=first.assignment_id,
            caller_key="another-key",
            command_digest=digest("another-command"),
        )
    else:
        replacement = create(repo, tx, assignment_id=first.assignment_id)
    assert replacement.assignment_id == first.assignment_id
    with pytest.raises(RepositoryConflictError, match="assignment_operation_deleted"):
        repo.get_operation_receipt(
            tx,
            owner_id="owner",
            origin_namespace="web",
            caller_key="ordinary-send",
            command_digest=digest("ordinary-send"),
        )


def test_one_shot_capacity_and_retirement_are_separate_current_gates(tx, repo):
    create_operation(repo, tx)
    with pytest.raises(RepositoryConflictError, match="assignment_capacity_exhausted"):
        create_operation(repo, tx, caller_key="next", max_owned_operations=1)
    tx.execute(
        "INSERT INTO astralplane_blob_owner_state(owner_id,state,version,retired_at,updated_at) "
        "VALUES('owner','retired',1,clock_timestamp(),clock_timestamp())"
    )
    with pytest.raises(RepositoryConflictError, match="assignment_owner_retired"):
        create_operation(repo, tx, caller_key="next")


def test_one_shot_owner_retirement_removes_receipts_but_cannot_resume(tx, repo):
    create_operation(repo, tx)
    assert not repo.retire_operations_for_owner(tx, owner_id="owner").retained_assignment_ids
    assert tx.fetch_one("SELECT count(*) AS n FROM assignment_operation_receipt")["n"] == 0
    with pytest.raises(RepositoryConflictError, match="assignment_owner_retired"):
        create_operation(repo, tx)


def test_one_shot_legacy_run_now_is_explicitly_refused(tx, repo):
    record = create_operation(repo, tx)
    with pytest.raises(
        RepositoryConflictError, match="assignment_operation_requires_explicit_wake"
    ):
        repo.request_check(
            tx,
            owner_id="owner",
            assignment_id=record.assignment_id,
            expected_instruction_revision=record.instruction_revision,
            expected_control_epoch=record.control_epoch,
            submission_id=uid(),
            submission_digest=digest("run-now"),
        )


@pytest.mark.parametrize("profile", ["persistent", "one_shot"])
def test_recovery_filters_profiles_before_batch_limit(tx, repo, profile):
    persistent = create(repo, tx)
    operation = create_operation(repo, tx)
    persistent_claim = claim(repo, tx)
    operation_claim = claim_operations(repo, tx, worker_id="operation")[0]
    records = {"persistent": persistent, "one_shot": operation}
    claims = {"persistent": persistent_claim, "one_shot": operation_claim}
    recover = {
        "persistent": repo.recover_expired_for_administration,
        "one_shot": repo.recover_expired_operations_for_administration,
    }
    other = "one_shot" if profile == "persistent" else "persistent"
    expire_claim(tx, records[other])
    expire_claim(tx, records[profile])
    result = recover[profile](tx, limit=1)
    assert result.reclaimed_assignment_ids == (records[profile].assignment_id,)
    retained = repo.get_assignment(tx, owner_id="owner", assignment_id=records[other].assignment_id)
    assert retained.state_version == claims[other].assignment.state_version
    result = recover[other](tx, limit=1)
    assert result.reclaimed_assignment_ids == (records[other].assignment_id,)
    recovered = repo.get_assignment(tx, owner_id="owner", assignment_id=operation.assignment_id)
    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    assert now + timedelta(seconds=4) <= recovered.next_wake_at <= now + timedelta(seconds=6)
    assert repo.recover_expired_for_administration(tx).reclaimed_assignment_ids == ()


def test_one_shot_recovery_exhausts_finite_backoff_without_recurring(tx, repo):
    operation = create_operation(repo, tx)
    for delay in (5, 15, 45, None):
        claim_operations(repo, tx, worker_id="operation")
        expire_claim(tx, operation)
        repo.recover_expired_operations_for_administration(tx)
        recovered = repo.get_assignment(tx, owner_id="owner", assignment_id=operation.assignment_id)
        now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
        if delay is None:
            assert recovered.next_wake_at is None
            assert (
                tx.fetch_one(
                    "SELECT data->>'next_retry_at' AS retry FROM persistent_assignment WHERE id=%s",
                    (operation.assignment_id,),
                )["retry"]
                is None
            )
            assert recovered.safe_error_code == "assignment_retry_exhausted"
            assert recovered.lifecycle == "completed"
            assert recovered.operation["terminal_outcome"] == "failed"
        else:
            assert now + timedelta(seconds=delay - 1) <= recovered.next_wake_at
            assert recovered.next_wake_at <= now + timedelta(seconds=delay + 1)
            tx.execute(
                "UPDATE persistent_assignment SET next_wake_at=statement_timestamp(), "
                "data=jsonb_set(data,'{next_wake_at}',to_jsonb(statement_timestamp()::text)) "
                "WHERE id=%s",
                (operation.assignment_id,),
            )
    assert claim_operations(repo, tx, worker_id="again") == ()


@pytest.mark.parametrize("boundary", ["read_only", "unreplayable"])
def test_one_shot_recovery_preserves_issued_liability_and_stale_fence(tx, repo, boundary):
    operation = create_operation(repo, tx)
    current = claim_operations(repo, tx, worker_id="operation")[0]
    binding = bind(repo, tx, current.fence)
    created = action(repo, tx, current.fence, boundary=boundary)
    permit = start(repo, tx, current.fence, reserve(repo, tx, current.fence, created), binding)
    expire_claim(tx, operation)
    result = repo.recover_expired_operations_for_administration(tx)
    recovered = repo.get_assignment(tx, owner_id="owner", assignment_id=operation.assignment_id)
    with pytest.raises(RepositoryConflictError):
        repo.assert_current_claim(tx, fence=current.fence)
    if boundary == "read_only":
        assert recovered.usage["spent"]["tool_calls"] == 1
        assert recovered.usage["outstanding"]["tool_calls"] == 0
    else:
        assert result.uncertain_action_ids == (created.action_id,)
        assert recovered.phase == "reconciliation"
        assert recovered.next_wake_at is None
        assert recovered.usage["outstanding"]["tool_calls"] == 1
        assert outcome(repo, tx, permit, operation.assignment_id).state == "succeeded"


@pytest.mark.parametrize(
    "expired_field,expected",
    [
        ("{operation,deadline_at}", "assignment_deadline_exceeded"),
        ("{operation,authority,expires_at}", "assignment_authorization_unavailable"),
    ],
)
def test_one_shot_recovery_cannot_renew_expired_authority_or_deadline(
    tx, repo, expired_field, expected
):
    operation = create_operation(repo, tx)
    claim_operations(repo, tx, worker_id="operation")
    expire_claim(tx, operation)
    tx.execute(
        "UPDATE persistent_assignment SET data=jsonb_set(data,%s::text[], "
        "to_jsonb((clock_timestamp()-interval '1 second')::text)) WHERE id=%s",
        (expired_field, operation.assignment_id),
    )
    repo.recover_expired_operations_for_administration(tx)
    recovered = repo.get_assignment(tx, owner_id="owner", assignment_id=operation.assignment_id)
    assert recovered.next_wake_at is None
    assert recovered.safe_error_code == expected
    if expected == "assignment_deadline_exceeded":
        assert recovered.lifecycle == "completed"
        assert recovered.operation["terminal_outcome"] == "failed"
    else:
        assert recovered.lifecycle == "active"
        assert recovered.operation.get("terminal_outcome") is None
    assert claim_operations(repo, tx, worker_id="again") == ()


def test_one_shot_framework_receipt_is_bound_to_issuing_reference(tx, repo):
    from astralplane.repositories.assignment_models import (
        AssignmentOperationAuthority,
        AssignmentOperationSpec,
    )

    credential, observation = framework_credential_observation(tx)
    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    authority = AssignmentOperationAuthority(
        "owner", "framework", "credential", credential.credential_id, now + timedelta(minutes=5)
    )
    operation = AssignmentOperationSpec("chat", authority, now + timedelta(minutes=1), "none")
    with pytest.raises(RepositoryValidationError, match="credential reference mismatch"):
        create_operation(repo, tx, authority=observation, operation=operation)
    record = create_operation(
        repo, tx, authority=observation, operation=operation, credential_id=credential.credential_id
    )
    assert (
        create_operation(
            repo,
            tx,
            authority=observation,
            definition=None,
            operation=None,
            credential_id=credential.credential_id,
        )
        == record
    )
    with pytest.raises(RepositoryConflictError, match="assignment_idempotency_conflict"):
        create_operation(
            repo,
            tx,
            authority=observation,
            definition=None,
            operation=None,
            credential_id="other-id",
        )


def test_one_shot_framework_authority_refuses_an_unverified_observation(tx, repo):
    from astralplane.repositories.assignment_models import (
        AssignmentOperationAuthority,
        AssignmentOperationSpec,
    )

    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    authority = AssignmentOperationAuthority(
        "owner", "framework", "credential", "unissued-credential-id", now + timedelta(minutes=5)
    )
    operation = AssignmentOperationSpec("chat", authority, now + timedelta(minutes=1), "none")
    with pytest.raises(RepositoryConflictError, match="assignment_authorization_unavailable"):
        create_operation(
            repo,
            tx,
            authority=session_observation(tx),
            operation=operation,
            credential_id="unissued-credential-id",
        )


@pytest.mark.parametrize("change", ["deadline", "long_deadline", "research_without_source"])
def test_one_shot_deadline_and_research_source_bounds(tx, repo, change):
    from astralplane.repositories.assignment_models import (
        AssignmentOperationAuthority,
        AssignmentOperationSpec,
    )

    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    authority = AssignmentOperationAuthority(
        "owner", "interactive", "session", "opaque", now + timedelta(days=3)
    )
    deadline = now - timedelta(seconds=1) if change == "deadline" else now + timedelta(days=2)
    if change == "research_without_source":
        deadline = now + timedelta(minutes=1)
    operation = AssignmentOperationSpec(
        "research" if change == "research_without_source" else "chat", authority, deadline, "none"
    )
    with pytest.raises((RepositoryValidationError, RepositoryConflictError)):
        create_operation(repo, tx, operation=operation)


def test_one_shot_binding_checks_original_deadline_after_claim(tx, repo):
    record = create_operation(repo, tx)
    current = claim_operations(repo, tx, worker_id="operations")[0]
    tx.execute(
        "UPDATE persistent_assignment SET data=jsonb_set(data,'{operation,deadline_at}', "
        "to_jsonb((clock_timestamp()-interval '1 second')::text)) WHERE id=%s",
        (record.assignment_id,),
    )
    with pytest.raises(RepositoryConflictError, match="assignment_deadline_exceeded"):
        bind(repo, tx, current.fence)


def test_one_shot_grant_and_conversation_references_remain_owner_scoped(tx, repo):
    record = create_operation(repo, tx)
    value = replace(record.definition, conversation_id=uid())
    with pytest.raises(RepositoryConflictError, match="assignment_conversation_not_owned"):
        create_operation(repo, tx, caller_key="other", definition=value)
    grant_value = definition(tx)
    value = replace(record.definition, offline_grant_id=grant_value.offline_grant_id)
    assert create_operation(repo, tx, caller_key="with-grant", definition=value)


@pytest.mark.postgresql_schema_catalog
def test_profile_and_receipt_structure_repeat_verification(database):
    runner = MigrationRunner(
        database, revision=CURRENT_DATA_PLANE_REVISION, registry=MIGRATION_REGISTRY
    )
    assert runner.run(
        expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision
    ).applied_steps == ()
    with database.transaction() as transaction:
        rows = transaction.fetch_all("SELECT execution_profile,data FROM persistent_assignment")
        for row in rows:
            assert row["execution_profile"] == row["data"].get("execution_profile", "persistent")


def assignment_079_runner(upgrade_database):
    from astralplane.database import migrations as m

    old_registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "079.001"
        ),
        current_schema_verifier=lambda transaction: m._verify_predecessor_plane_schema(
            transaction, "079.001"
        ),
        current_schema_verifier_checksum="1987a3e7b27787ef5c4dcc4552e2713b1627b82aaf0760d8ccb881e5a4f30017",
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum="7a881bf3c3753eee9ec320444f7f0d029da03bbf77ef74c9160921435ef54e25",
    )
    assert old_registry.digest == m.PLANE_SCHEMA_079_REGISTRY_DIGEST
    old_revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="079.001",
        migration_digest=old_registry.digest,
        read_compatible_from=tuple(
            v for v in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if v < "079.001"
        ),
        accepted_predecessor_digests=tuple(
            v
            for v in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if v[0] < "079.001"
        ),
    )
    return m.MigrationRunner(upgrade_database, revision=old_revision, registry=old_registry)


@pytest.mark.postgresql_schema_catalog
def test_populated_079_upgrade_preserves_legacy_bytes_and_repeats(
    database, repo, lineage_templates
):
    from astralplane.database import migrations as m

    with database.transaction() as transaction:
        transaction.execute("DELETE FROM assignment_operation_receipt")
        transaction.execute("DELETE FROM persistent_assignment")
        legacy = create(repo, transaction)
        snapshot = transaction.fetch_one(
            "SELECT * FROM persistent_assignment WHERE id=%s", (legacy.assignment_id,)
        )
    with bound_clone(lineage_templates.template(assignment_079_runner)) as clone:
        pool = ConnectionPool(Pool(clone.connection))
        upgrade_database = PlaneDatabase(pool)
        try:
            assert assignment_079_runner(upgrade_database).run(
                expected_revision="079.001"
            ).already_current
            with upgrade_database.transaction() as transaction:
                transaction.execute(
                    "INSERT INTO persistent_assignment(id,owner_user_id,submission_id,"
                    "submission_digest,lifecycle,next_wake_at,state_version,data) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                    (
                        *(
                            snapshot[k]
                            for k in (
                                "id",
                                "owner_user_id",
                                "submission_id",
                                "submission_digest",
                                "lifecycle",
                                "next_wake_at",
                                "state_version",
                            )
                        ),
                        canonical(snapshot["data"]),
                    ),
                )
            runner = m.MigrationRunner(
                upgrade_database,
                revision=m.CURRENT_DATA_PLANE_REVISION,
                registry=m.MIGRATION_REGISTRY,
            )
            assert runner.run(
                expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision
            ).applied_steps == (
                "astralplane-088-one-shot-operations",
                "astralplane-088-session-incarnation",
                "astralplane-088-session-issuer",
                "astralplane-088-declarative-agents",
                "astralplane-088-owner-guidance",
                "astralplane-088-selected-input",
                "astralplane-088-scheduler-policy",
                "astralplane-088-framework-credentials",
                "astralplane-089-typesafe-credentials",
                "astralplane-089-mesh-enrollment-records",
                "astralplane-owner-stop-epochs",
                "astralplane-089-completion-wake",
                "astralplane-089-notification-records",
                "astralplane-089-atlas-pages",
                "astralplane-089-atlas-revisions",
                "astralplane-089-atlas-links",
                "astralplane-089-capability-publications",
                "astralplane-089-operation-harness-bindings",
                "astralplane-089-push-deliveries",
                "astralplane-089-observation-records",
                "astralplane-089-projects",
                "astralplane-089-harness-captures",
                "astralplane-089-audit-conversation-index",
                "astralplane-089-chat-queue",
                "astralplane-089-conversation-branches",
                "astralplane-089-computer-use",
                "astralplane-089-verification-credentials",
                "astralplane-089-chat-read-receipts",
                "astralplane-089-mesh-event-store",
                "astralplane-089-run-and-learning-ledger",
            )
            assert runner.run(
                expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision
            ).already_current
            with upgrade_database.transaction() as transaction:
                migrated = transaction.fetch_one(
                    "SELECT * FROM persistent_assignment WHERE id=%s", (legacy.assignment_id,)
                )
                assert canonical(migrated["data"]) == canonical(snapshot["data"])
                assert migrated["submission_digest"] == snapshot["submission_digest"]
                assert migrated["execution_profile"] == "persistent"
                transaction.execute(
                    "ALTER TABLE assignment_operation_receipt "
                    "DROP CONSTRAINT assignment_operation_receipt_pkey"
                )
            with pytest.raises(SchemaRevisionError):
                runner.run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
        finally:
            pool.close()


def test_concurrent_one_shot_acceptance_has_one_logical_identity(database, repo):
    with database.transaction() as transaction:
        transaction.execute("DELETE FROM assignment_operation_receipt")
        transaction.execute("DELETE FROM persistent_assignment")
    records = parallel_transactions(
        database,
        (
            lambda transaction: create_operation(repo, transaction),
            lambda transaction: create_operation(repo, transaction),
        ),
    )
    assert records[0].assignment_id == records[1].assignment_id
    with database.transaction() as transaction:
        assert (
            transaction.fetch_one("SELECT count(*) AS n FROM assignment_operation_receipt")["n"]
            == 1
        )


def test_operation_receipt_and_target_are_read_in_one_snapshot(database, repo):
    with database.transaction() as transaction:
        transaction.execute("DELETE FROM assignment_operation_receipt")
        transaction.execute("DELETE FROM persistent_assignment")
        first = create_operation(repo, transaction)
        schema = transaction.fetch_one("SELECT current_schema() AS name")["name"]

    class ReplaceAfterReceiptRead:
        def __init__(self, transaction):
            self.transaction = transaction
            self.replaced = False

        def fetch_one(self, statement, parameters):
            row = self.transaction.fetch_one(statement, parameters)
            if not self.replaced:
                self.replaced = True
                with independent_database(schema) as other, other.transaction() as writer:
                    stopped = control(repo, writer, first, "stop").assignment
                    repo.delete_for_owner(
                        writer,
                        owner_id="owner",
                        assignment_id=first.assignment_id,
                        expected_control_epoch=stopped.control_epoch,
                        expected_state_version=stopped.state_version,
                    )
                    create_operation(
                        repo,
                        writer,
                        assignment_id=first.assignment_id,
                        caller_key="replacement",
                        command_digest=digest("replacement"),
                    )
            return row

    with database.transaction() as transaction:
        query = ReplaceAfterReceiptRead(transaction)
        assert (
            repo.get_operation_receipt(
                query,
                owner_id="owner",
                origin_namespace="web",
                caller_key="ordinary-send",
                command_digest=digest("ordinary-send"),
            )
            == first
        )
        assert query.replaced
        if transaction.dialect.is_sqlite:
            assert (
                repo.get_operation_receipt(
                    transaction,
                    owner_id="owner",
                    origin_namespace="web",
                    caller_key="ordinary-send",
                    command_digest=digest("ordinary-send"),
                )
                == first
            )
        else:
            with pytest.raises(RepositoryConflictError, match="assignment_operation_deleted"):
                repo.get_operation_receipt(
                    transaction,
                    owner_id="owner",
                    origin_namespace="web",
                    caller_key="ordinary-send",
                    command_digest=digest("ordinary-send"),
                )
    with (
        database.transaction() as transaction,
        pytest.raises(RepositoryConflictError, match="assignment_operation_deleted"),
    ):
        repo.get_operation_receipt(
            transaction,
            owner_id="owner",
            origin_namespace="web",
            caller_key="ordinary-send",
            command_digest=digest("ordinary-send"),
        )


def test_create_replay_owner_controls_and_retirement(tx, repo):
    definition_value = definition(tx)
    identifier, submission = uid(), uid()
    args = dict(
        owner_id="owner",
        assignment_id=identifier,
        submission_id=submission,
        submission_digest=digest("create"),
        definition=definition_value,
    )
    record = repo.create_assignment(tx, **args)
    assert repo.create_assignment(tx, **args) == record
    assert repo.get_assignment(tx, owner_id="other", assignment_id=identifier) is None
    assert len(repo.list_assignments(tx, owner_id="owner")) == 1
    assert repo.list_assignments(tx, owner_id="other") == ()
    with pytest.raises(RepositoryConflictError):
        repo.create_assignment(
            tx, **dict(args, definition=replace(definition_value, name="Different"))
        )
    current = claim(repo, tx)
    bind(repo, tx, current.fence)
    paused = control(repo, tx, record)
    assert paused.assignment.lifecycle == "paused"
    assert repo.claim_due_for_administration(tx, worker_id="two") == ()
    with pytest.raises(RepositoryConflictError):
        repo.renew_claim(tx, fence=current.fence)
    resumed = control(repo, tx, paused.assignment, "resume").assignment
    assert resumed.control_epoch == 3
    stopped = control(repo, tx, resumed, "stop").assignment
    with pytest.raises(RepositoryConflictError):
        control(repo, tx, stopped, "resume")
    assert repo.delete_for_owner(
        tx, owner_id="owner", assignment_id=identifier, expected_control_epoch=stopped.control_epoch
    )
    assert not repo.delete_for_owner(
        tx, owner_id="owner", assignment_id=identifier, expected_control_epoch=stopped.control_epoch
    )


def test_owner_quota_counts_stopped_rows(tx, repo):
    first = create(repo, tx, max_retained_assignments=1)
    control(repo, tx, first, "stop")
    with pytest.raises(RepositoryConflictError, match="capacity"):
        create(repo, tx, max_retained_assignments=1)


def test_completed_assignment_can_be_retired(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    complete = finish(repo, tx, current.fence, completed=True)
    assert complete.lifecycle == "completed"
    assert repo.delete_for_owner(
        tx,
        owner_id="owner",
        assignment_id=record.assignment_id,
        expected_control_epoch=complete.control_epoch,
    )


def test_usage_reservation_start_outcome_replay_and_stop_fence(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    binding = bind(repo, tx, current.fence)
    created = action(repo, tx, current.fence)
    assert repo.put_action(tx, fence=current.fence, intent=created.intent) == created
    assert (
        repo.get_action_by_key(
            tx,
            owner_id="owner",
            assignment_id=record.assignment_id,
            action_key=created.intent.action_key,
        )
        == created
    )
    reserved = reserve(repo, tx, current.fence, created)
    assert not reserve(repo, tx, current.fence, created, attempt_id=reserved.attempt_id).created
    permit = start(repo, tx, current.fence, reserved, binding)
    stopped = control(repo, tx, record, "stop")
    assert stopped.begun_action_ids == (created.action_id,)
    with pytest.raises(RepositoryConflictError):
        start(repo, tx, current.fence, reserved, binding)
    result = outcome(repo, tx, permit, record.assignment_id)
    assert result.state == "succeeded"
    assert outcome(repo, tx, permit, record.assignment_id) == result
    with pytest.raises(RepositoryConflictError):
        outcome(
            repo,
            tx,
            permit,
            record.assignment_id,
            outcome=AssignmentActionOutcome("succeeded", digest("changed"), {}),
        )
    stored = repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
    assert stored.usage["spent"]["tool_calls"] == 1
    assert stored.usage["outstanding"]["tool_calls"] == 0
    assert stored.usage["money_status"] == "unknown"
    assert "dispatch_token" not in repr(result)


def test_usage_basis_persists_through_settlement_and_is_absent_from_legacy_rows(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    binding = bind(repo, tx, current.fence)
    created = action(repo, tx, current.fence)
    reserved = reserve(repo, tx, current.fence, created)
    permit = start(repo, tx, current.fence, reserved, binding)
    outcome(
        repo,
        tx,
        permit,
        record.assignment_id,
        outcome=AssignmentActionOutcome(
            "succeeded",
            digest("done"),
            {"text": "done"},
            actual=AssignmentResourceAmount(
                model_calls=0,
                tool_calls=1,
                tokens=10,
                elapsed_ms=5,
                basis={"tool_calls": "observed", "tokens": "estimated"},
            ),
        ),
    )
    row = tx.fetch_one(
        "SELECT data FROM persistent_assignment_action WHERE id=%s", (created.action_id,)
    )
    persisted_actual = row["data"]["result"]["actual"]
    assert persisted_actual["basis"] == {"tool_calls": "observed", "tokens": "estimated"}
    other = create(repo, tx, assignment_id=uid())
    other_current = claim(repo, tx)
    other_binding = bind(repo, tx, other_current.fence)
    other_action = action(repo, tx, other_current.fence)
    other_reserved = reserve(repo, tx, other_current.fence, other_action)
    other_permit = start(repo, tx, other_current.fence, other_reserved, other_binding)
    outcome(repo, tx, other_permit, other.assignment_id)
    other_row = tx.fetch_one(
        "SELECT data FROM persistent_assignment_action WHERE id=%s", (other_action.action_id,)
    )
    assert "basis" not in (other_row["data"]["result"].get("actual") or {})


def test_reservation_denies_shared_limit_and_zero_retries_allows_first_call(tx, repo):
    value = definition(tx)
    value = replace(value, limits=dict(value.limits, tool_calls=1, max_retries=0))
    create(repo, tx, definition=value)
    current = claim(repo, tx)
    binding = bind(repo, tx, current.fence)
    first = action(repo, tx, current.fence)
    reserved = reserve(repo, tx, current.fence, first)
    second = action(repo, tx, current.fence)
    with pytest.raises(RepositoryConflictError, match="budget"):
        reserve(repo, tx, current.fence, second)
    permit = start(repo, tx, current.fence, reserved, binding)
    outcome(repo, tx, permit, current.fence.assignment_id)
    with pytest.raises(RepositoryConflictError):
        reserve(repo, tx, current.fence, first)


def test_approval_new_foreground_claim_expiry_and_single_use(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    created = action(
        repo,
        tx,
        current.fence,
        sensitivity="sensitive",
        interactive_only=True,
        approval_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    finish(repo, tx, current.fence, phase="waiting_approval")
    decision = AssignmentActionDecision(
        created.intent.request_digest,
        "approve",
        uid(),
        digest("approve"),
        created.intent.permission_digest,
        created.intent.precondition_digest,
    )
    approved = repo.decide_action(
        tx,
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=created.action_id,
        expected_instruction_revision=1,
        expected_control_epoch=1,
        decision=decision,
    )
    assert approved.state == "approved"
    args = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=created.action_id,
        expected_request_digest=created.intent.request_digest,
        expected_instruction_revision=1,
        expected_control_epoch=1,
        interactive_receipt_id="trusted-interaction",
        submission_id=uid(),
        submission_digest=digest("foreground"),
        worker_id="foreground",
    )
    foreground = repo.claim_for_approved_action(tx, **args)
    with pytest.raises(RepositoryConflictError):
        repo.claim_for_approved_action(tx, **args)
    binding = bind(repo, tx, foreground.fence)
    reserved = reserve(repo, tx, foreground.fence, approved)
    with pytest.raises(RepositoryConflictError):
        start(repo, tx, foreground.fence, reserved, binding)
    permit = start(
        repo, tx, foreground.fence, reserved, binding, interactive_receipt_id="trusted-interaction"
    )
    assert outcome(repo, tx, permit, record.assignment_id).state == "succeeded"
    with pytest.raises(RepositoryConflictError):
        action(repo, tx, foreground.fence)


def test_unstarted_reservation_is_released_on_pause(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    created = action(repo, tx, current.fence)
    reserve(repo, tx, current.fence, created)
    result = control(repo, tx, record)
    assert result.invalidated_action_ids == (created.action_id,)
    assert result.assignment.usage["outstanding"]["tool_calls"] == 0


def test_uncertain_effect_requires_explicit_reconciliation(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    binding = bind(repo, tx, current.fence)
    created = action(repo, tx, current.fence, boundary="unreplayable")
    permit = start(repo, tx, current.fence, reserve(repo, tx, current.fence, created), binding)
    unknown = AssignmentActionOutcome("uncertain", digest("uncertain"), {})
    outcome(repo, tx, permit, record.assignment_id, outcome=unknown)
    with pytest.raises(RepositoryConflictError):
        reserve(repo, tx, current.fence, created)
    decision = AssignmentActionReconciliation(
        unknown.result_digest, "confirmed_applied", "verified:receipt", uid(), digest("reconcile")
    )
    result = repo.reconcile_action(
        tx,
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=created.action_id,
        expected_instruction_revision=1,
        expected_control_epoch=1,
        decision=decision,
    )
    assert result.state == "succeeded"
    assert result.result["outcome"] == "reconciled_applied"
    assert result.result["result_available"] is False
    assert result.result["result"] == {}
    assert (
        repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id).usage[
            "spent"
        ]["tool_calls"]
        == 1
    )


def test_source_batch_cursor_task_results_and_atomic_publication(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    context = {"text": "new release"}
    event = AssignmentSourceEvent(
        uid(), "page", "release", "1", digest("event"), digest(context), context
    )
    batch = AssignmentSourceBatch(
        "batch1",
        digest("batch1"),
        "page",
        digest(record.definition.source),
        digest(None),
        {"digest": "1", "sequence": 1},
        (event,),
    )
    stored, events = repo.record_source_batch(
        tx,
        fence=current.fence,
        expected_state_version=current.assignment.state_version,
        batch=batch,
    )
    assert events == (event,)
    assert (
        repo.record_source_batch(tx, fence=current.fence, expected_state_version=0, batch=batch)[1]
        == events
    )
    tasks = (
        AssignmentTask(
            "first",
            "plan",
            1,
            "Read",
            "Read release",
            record.definition.allowed_tools,
            event_id=event.event_id,
        ),
        AssignmentTask(
            "second",
            "plan",
            1,
            "Compare",
            "Compare release",
            record.definition.allowed_tools,
            event_id=event.event_id,
            depends_on=("first",),
        ),
    )
    stored = repo.put_task_plan(
        tx,
        fence=current.fence,
        expected_state_version=stored.state_version,
        plan_key="plan",
        plan_digest=digest(tasks),
        tasks=tasks,
    )
    with pytest.raises(RepositoryConflictError, match="dependency"):
        repo.claim_task(tx, fence=current.fence, task_id="second", expected_task_generation=0)
    first = repo.claim_task(tx, fence=current.fence, task_id="first", expected_task_generation=0)
    result = AssignmentTaskResult("completed", digest("read"), "Read release")
    repo.complete_task(tx, claim=first, result=result)
    repo.complete_task(tx, claim=first, result=result)
    second = repo.claim_task(tx, fence=current.fence, task_id="second", expected_task_generation=0)
    repo.complete_task(tx, claim=second, result=result)
    saved = finish(
        repo,
        tx,
        current.fence,
        incorporations=tuple(
            {
                "task_id": key,
                "parent_task_id": "__assignment__",
                "result_digest": result.result_digest,
            }
            for key in ("first", "second")
        ),
        event_receipts=(
            {
                "event_id": event.event_id,
                "disposition": "completed",
                "result_digest": digest("finding"),
            },
        ),
        activity=AssignmentActivityRecord("finding1", "finding", "New release", "Found release"),
    )
    assert saved.tasks[0]["incorporated_by"]["__assignment__"] == result.result_digest
    assert (
        repo.list_events(tx, owner_id="owner", assignment_id=record.assignment_id)[0].disposition
        == "completed"
    )
    assert len(repo.list_activity(tx, owner_id="owner", assignment_id=record.assignment_id)) == 1


def expire_claim(tx, record):
    expiry = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    tx.execute(
        "UPDATE persistent_assignment SET lease_expires_at=%s, "
        "data=jsonb_set(data,'{lease_expires_at}',to_jsonb(%s::text)) WHERE id=%s",
        (expiry, expiry, record.assignment_id),
    )


def test_claim_renewal_operation_fences_and_recovery(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    first_binding = bind(repo, tx, current.fence)
    repo.bind_operation(tx, fence=current.fence, binding=first_binding)
    with pytest.raises(RepositoryConflictError):
        bind(repo, tx, current.fence)
    assert repo.assert_current_claim(tx, fence=current.fence).assignment_id == record.assignment_id
    renewed = repo.renew_claim(tx, fence=current.fence)
    assert renewed.lease_expires_at >= current.lease_expires_at
    never_started = action(repo, tx, current.fence)
    reserve(repo, tx, current.fence, never_started)
    read = action(repo, tx, current.fence)
    start(repo, tx, current.fence, reserve(repo, tx, current.fence, read), first_binding)
    expire_claim(tx, record)
    recovered = repo.recover_expired_for_administration(tx)
    assert recovered.reclaimed_assignment_ids == (record.assignment_id,)
    assert len(recovered.operation_bindings) == 1
    assert recovered.uncertain_action_ids == ()
    with pytest.raises(RepositoryConflictError):
        repo.assert_current_claim(tx, fence=current.fence)
    assert (
        repo.get_action(
            tx,
            owner_id="owner",
            assignment_id=record.assignment_id,
            action_id=never_started.action_id,
        ).state
        == "failed_not_started"
    )
    after = repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
    assert after.usage["spent"]["tool_calls"] == 1
    assert after.usage["outstanding"]["tool_calls"] == 0


def test_unreplayable_recovery_retains_liability_and_late_receipt(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    binding = bind(repo, tx, current.fence)
    created = action(repo, tx, current.fence, boundary="unreplayable")
    permit = start(repo, tx, current.fence, reserve(repo, tx, current.fence, created), binding)
    expire_claim(tx, record)
    result = repo.recover_expired_for_administration(tx)
    assert result.uncertain_action_ids == (created.action_id,)
    assert (
        repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id).phase
        == "reconciliation"
    )
    stopped = control(repo, tx, record, "stop").assignment
    with pytest.raises(RepositoryConflictError):
        repo.delete_for_owner(
            tx,
            owner_id="owner",
            assignment_id=record.assignment_id,
            expected_control_epoch=stopped.control_epoch,
            expected_state_version=stopped.state_version,
        )
    assert outcome(repo, tx, permit, record.assignment_id).state == "succeeded"


def test_revision_revocation_reconsent_and_control_replay(tx, repo):
    record = create(repo, tx)
    submission = uid()
    revised = control(
        repo,
        tx,
        record,
        "revise",
        submission_id=submission,
        replacement=replace(record.definition, instructions="Watch security fixes"),
    )
    assert revised.assignment.instruction_revision == 2
    replay = control(
        repo,
        tx,
        record,
        "revise",
        submission_id=submission,
        replacement=replace(record.definition, instructions="Watch security fixes"),
    )
    assert not replay.applied
    with pytest.raises(RepositoryConflictError):
        control(
            repo,
            tx,
            record,
            "revise",
            submission_id=submission,
            replacement=replace(record.definition, instructions="Different"),
        )
    revoked = control(repo, tx, revised.assignment, "revoke").assignment
    assert revoked.phase == "waiting_authorization"
    assert revoked.definition.offline_grant_id is None
    assert repo.claim_due_for_administration(tx, worker_id="other") == ()
    with pytest.raises(RepositoryConflictError):
        create(repo, tx, definition=replace(record.definition, offline_grant_id=uid()))
    with pytest.raises(RepositoryConflictError):
        create(repo, tx, definition=replace(record.definition, conversation_id=uid()))


def test_request_check_is_coalesced_and_cadence_bounded(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    batch = AssignmentSourceBatch(
        "empty",
        digest("empty"),
        "page",
        digest(record.definition.source),
        digest(None),
        {"sequence": 1},
        (),
    )
    repo.record_source_batch(
        tx,
        fence=current.fence,
        expected_state_version=current.assignment.state_version,
        batch=batch,
    )
    record = finish(repo, tx, current.fence)
    values = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        expected_instruction_revision=1,
        expected_control_epoch=1,
        submission_id=uid(),
        submission_digest=digest("check"),
    )
    requested = repo.request_check(tx, **values)
    assert requested.next_wake_at >= datetime.now(UTC) + timedelta(seconds=50)
    assert repo.request_check(tx, **values) == requested
    with pytest.raises(RepositoryConflictError):
        repo.request_check(tx, **dict(values, submission_digest=digest("different")))
    paused = control(repo, tx, record).assignment
    with pytest.raises(RepositoryConflictError):
        repo.request_check(tx, **dict(values, expected_control_epoch=paused.control_epoch))


def test_activity_replay_notifications_and_retention(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    item = AssignmentActivityRecord(
        "progress", "finding", "Result", "A safe summary", notification_state="pending"
    )
    created = repo.append_activity(tx, fence=current.fence, activity=item)
    assert repo.append_activity(tx, fence=current.fence, activity=item) == created
    with pytest.raises(RepositoryConflictError):
        repo.append_activity(tx, fence=current.fence, activity=replace(item, summary="changed"))
    args = dict(
        owner_id="owner", assignment_id=record.assignment_id, activity_id=created.activity_id
    )
    assert repo.mark_activity_notified(tx, **args)
    assert not repo.mark_activity_notified(tx, **args)
    with pytest.raises(RepositoryValidationError):
        repo.mark_activity_notified(tx, **args, expected_state="none")
    assert (
        repo.list_activity(tx, owner_id="owner", assignment_id=record.assignment_id)[
            0
        ].notification_state
        == "notified"
    )
    tx.execute(
        "UPDATE persistent_assignment_activity SET data=jsonb_set(data,'{created_at}', "
        "to_jsonb((clock_timestamp()-interval '40 days')::text)) WHERE id=%s",
        (created.activity_id,),
    )
    assert repo.retain_for_administration(tx).activity_removals == 1
    assert repo.list_activity(tx, owner_id="owner", assignment_id=record.assignment_id) == ()


def test_release_unstarted_is_idempotent_and_started_cannot_refund(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    binding = bind(repo, tx, current.fence)
    created = action(repo, tx, current.fence)
    reserved = reserve(repo, tx, current.fence, created)
    args = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=created.action_id,
        attempt_id=reserved.attempt_id,
        expected_request_digest=created.intent.request_digest,
        reason_code="cancelled_before_send",
    )
    assert repo.release_unstarted_action(tx, **args).state == "failed_not_started"
    assert repo.release_unstarted_action(tx, **args).state == "failed_not_started"
    retried = reserve(repo, tx, current.fence, created)
    start(repo, tx, current.fence, retried, binding)
    with pytest.raises(RepositoryConflictError):
        repo.release_unstarted_action(tx, **dict(args, attempt_id=retried.attempt_id))


def test_money_caps_quote_expiry_actual_overrun_and_lifetime_accounting(tx, repo):
    value = definition(tx)
    coverage = {
        "quote_digest": digest("tariff"),
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    value = replace(
        value,
        limits=dict(
            value.limits, currency="USD", spend_micro_units=100, daily_spend_micro_units=100
        ),
        cost_quote_coverage=coverage,
    )
    record = create(repo, tx, definition=value)
    current = claim(repo, tx)
    binding = bind(repo, tx, current.fence)
    amount = AssignmentResourceAmount(
        tool_calls=1, elapsed_ms=1000, spend_micro_units=50, currency="USD"
    )
    quote_expiry = datetime.now(UTC) + timedelta(minutes=1)
    created = action(
        repo,
        tx,
        current.fence,
        maximum=amount,
        quote_digest=digest("price"),
        quote_expires_at=quote_expiry,
    )
    with pytest.raises(RepositoryConflictError):
        reserve(repo, tx, current.fence, created)
    with pytest.raises(RepositoryConflictError):
        reserve(
            repo,
            tx,
            current.fence,
            created,
            quote_digest=digest("price"),
            quote_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    reserved = reserve(
        repo,
        tx,
        current.fence,
        created,
        quote_digest=digest("price"),
        quote_expires_at=quote_expiry,
    )
    permit = start(repo, tx, current.fence, reserved, binding)
    actual = replace(amount, spend_micro_units=110)
    outcome(
        repo,
        tx,
        permit,
        record.assignment_id,
        outcome=AssignmentActionOutcome("succeeded", digest("overrun"), {}, actual=actual),
    )
    after = repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
    assert after.usage["spent"]["spend_micro_units"] == 110
    assert after.usage["money_status"] == "reported"
    another = action(
        repo,
        tx,
        current.fence,
        maximum=amount,
        quote_digest=digest("price"),
        quote_expires_at=quote_expiry,
    )
    with pytest.raises(RepositoryConflictError, match="budget"):
        reserve(
            repo,
            tx,
            current.fence,
            another,
            quote_digest=digest("price"),
            quote_expires_at=quote_expiry,
        )


def test_remote_approval_bridge_matches_actual_persisted_proposal(tx, repo):
    import hashlib
    import json

    from astralplane.repositories.remote_proposals import (
        RemoteOperationProposalRecord,
        RemoteOperationProposalRepository,
    )

    record = create(repo, tx)
    current = claim(repo, tx)
    arguments = {"target": "file.txt"}
    request = dict(
        kind="tool", agent_id="remote-compute-1", tool_name="delete_file", arguments=arguments
    )
    created = action(
        repo,
        tx,
        current.fence,
        request=request,
        request_digest=digest(request),
        sensitivity="sensitive",
        interactive_only=True,
        approval_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    proposals = RemoteOperationProposalRepository()
    proposal_id = uuid.uuid4().hex
    now = int(datetime.now(UTC).timestamp())
    proposal = RemoteOperationProposalRecord(
        proposal_id,
        "owner",
        None,
        "machine",
        "remote-compute-1",
        "delete_file",
        hashlib.sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        arguments,
        "Delete file.txt",
        "pending",
        now,
        now + 300,
    )
    proposals.create(tx, proposal)
    values = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=created.action_id,
        expected_request_digest=created.intent.request_digest,
        proposal_id=proposal_id,
        expected_instruction_revision=1,
        expected_control_epoch=1,
    )
    linked = repo.link_interactive_proposal(tx, **values)
    assert linked.interactive_proposal_id == proposal_id
    assert repo.link_interactive_proposal(tx, **values) == linked
    assert (
        repo.get_action_for_interactive_proposal(tx, owner_id="owner", proposal_id=proposal_id)
        == linked
    )
    assert (
        repo.get_action_for_interactive_proposal(tx, owner_id="other", proposal_id=proposal_id)
        is None
    )
    assert (
        repo.observe_interactive_proposal(tx, owner_id="owner", proposal_id=proposal_id).state
        == "proposed"
    )
    proposals.decide_if_pending(
        tx, owner_id="owner", proposal_id=proposal_id, decision="declined", decided_at=now
    )
    assert (
        repo.observe_interactive_proposal(tx, owner_id="owner", proposal_id=proposal_id).state
        == "declined"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda t: replace(t, depends_on=("missing",)),
        lambda t: replace(t, depends_on=(t.task_id,)),
        lambda t: replace(t, instruction_revision=2),
        lambda t: replace(t, parent_task_id="missing"),
        lambda t: replace(t, depth=1),
        lambda t: replace(t, allowed_tools=("forbidden",)),
        lambda t: replace(t, state="completed"),
    ],
)
def test_task_graph_denial(tx, repo, mutation):
    record = create(repo, tx)
    current = claim(repo, tx)
    task = mutation(
        AssignmentTask("task", "plan", 1, "Read", "Read source", record.definition.allowed_tools)
    )
    with pytest.raises(RepositoryValidationError):
        repo.put_task_plan(
            tx,
            fence=current.fence,
            expected_state_version=current.assignment.state_version,
            plan_key="plan",
            plan_digest=digest("plan"),
            tasks=(task,),
        )


def test_task_result_replay_graph_replacement_and_hold_recovery(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    tasks = (
        AssignmentTask("task", "plan", 1, "Read", "Read source", record.definition.allowed_tools),
    )
    stored = repo.put_task_plan(
        tx,
        fence=current.fence,
        expected_state_version=current.assignment.state_version,
        plan_key="plan",
        plan_digest=digest(tasks),
        tasks=tasks,
    )
    assert (
        repo.put_task_plan(
            tx,
            fence=current.fence,
            expected_state_version=0,
            plan_key="plan",
            plan_digest=digest(tasks),
            tasks=tasks,
        )
        == stored
    )
    running = repo.claim_task(tx, fence=current.fence, task_id="task", expected_task_generation=0)
    held = finish(repo, tx, current.fence, phase="waiting_authorization")
    assert held.tasks[0]["state"] == "pending"
    with pytest.raises(RepositoryConflictError):
        repo.complete_task(
            tx, claim=running, result=AssignmentTaskResult("completed", digest("old"), "old")
        )


def test_finish_replay_and_failure_retry_bound(tx, repo):
    value = definition(tx)
    record = create(repo, tx, definition=replace(value, limits=dict(value.limits, max_retries=0)))
    current = claim(repo, tx)
    completion = AssignmentEpisodeCompletion(
        current.assignment.state_version, {"summary": "waiting"}, digest("failed"), phase="failed"
    )
    result = repo.finish_episode(tx, fence=current.fence, completion=completion)
    assert result.next_wake_at is None
    assert result.safe_error_code == "assignment_retry_exhausted"
    assert repo.finish_episode(tx, fence=current.fence, completion=completion) == result
    with pytest.raises(RepositoryConflictError):
        repo.finish_episode(
            tx,
            fence=current.fence,
            completion=replace(completion, completion_digest=digest("other")),
        )
    assert (
        repo.get_action(tx, owner_id="owner", assignment_id=record.assignment_id, action_id=uid())
        is None
    )
    assert repo.list_actions(tx, owner_id="owner", assignment_id=record.assignment_id) == ()


def test_submission_receipts_survive_revisions_without_grant_recapture(tx, repo):
    identifier, submission = uid(), uid()
    record = create(repo, tx, assignment_id=identifier, submission_id=submission)
    revision_submission = uid()
    current = control(
        repo,
        tx,
        record,
        "revise",
        submission_id=revision_submission,
        replacement=replace(record.definition, name="Revised name"),
    ).assignment
    args = dict(
        owner_id="owner",
        assignment_id=identifier,
        submission_id=submission,
        submission_digest=digest("create"),
        command="create",
    )
    assert repo.get_submission_receipt(tx, **args) == current
    assert (
        repo.get_submission_receipt(
            tx,
            **dict(
                args,
                submission_id=revision_submission,
                submission_digest=digest("revise"),
                command="revise",
            ),
        )
        == current
    )
    assert repo.get_submission_receipt(tx, **dict(args, owner_id="other")) is None
    assert repo.get_submission_receipt(tx, **dict(args, submission_id=uid())) is None
    with pytest.raises(RepositoryConflictError):
        repo.get_submission_receipt(tx, **dict(args, command="stop"))
    check_submission = uid()
    checked = repo.request_check(
        tx,
        owner_id="owner",
        assignment_id=identifier,
        expected_instruction_revision=2,
        expected_control_epoch=2,
        submission_id=check_submission,
        submission_digest=digest("check"),
    )
    assert (
        repo.get_submission_receipt(
            tx,
            **dict(
                args,
                submission_id=check_submission,
                submission_digest=digest("check"),
                command="run-now",
            ),
        )
        == checked
    )
    with pytest.raises(RepositoryConflictError):
        repo.get_submission_receipt(
            tx,
            **dict(
                args,
                submission_id=check_submission,
                submission_digest=digest("changed"),
                command="run-now",
            ),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"request_digest": "z" * 64},
        {"request_digest": digest("wrong")},
        {"sensitivity": "safe_bypass"},
        {"interactive_only": "false"},
        {"boundary": "unknown"},
        {"boundary": "downstream_key"},
        {"task_id": "missing"},
        {"event_id": "invalid"},
        {"sensitivity": "sensitive"},
        {"maximum": AssignmentResourceAmount(tool_calls=-1)},
        {"maximum": AssignmentResourceAmount(currency="USD")},
    ],
)
def test_invalid_action_intents_are_refused(tx, repo, changes):
    create(repo, tx)
    current = claim(repo, tx)
    with pytest.raises(
        (RepositoryValidationError, RepositoryConflictError, RepositoryNotFoundError)
    ):
        action(repo, tx, current.fence, **changes)


def test_action_query_boundaries_and_precondition_mismatch(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    binding = bind(repo, tx, current.fence)
    created = action(repo, tx, current.fence)
    assert repo.list_actions(
        tx, owner_id="owner", assignment_id=record.assignment_id, states=("ready",)
    ) == (created,)
    assert (
        repo.list_actions(
            tx, owner_id="owner", assignment_id=record.assignment_id, after_id=created.action_id
        )
        == ()
    )
    assert (
        repo.get_action_by_key(
            tx, owner_id="owner", assignment_id=record.assignment_id, action_key="missing"
        )
        is None
    )
    reserved = reserve(repo, tx, current.fence, created)
    with pytest.raises(RepositoryConflictError):
        start(
            repo,
            tx,
            current.fence,
            reserved,
            binding,
            current_precondition_digest=digest("changed"),
        )
    with pytest.raises(RepositoryConflictError):
        start(repo, tx, current.fence, reserved, AssignmentOperationBinding(uid(), 1, uid()))
    with pytest.raises(RepositoryConflictError):
        reserve(
            repo,
            tx,
            current.fence,
            created,
            attempt_id=reserved.attempt_id,
            maximum=AssignmentResourceAmount(tool_calls=0, elapsed_ms=1000),
        )
    with pytest.raises(RepositoryConflictError):
        reserve(repo, tx, current.fence, created, expected_request_digest=digest("other"))
    permit = start(repo, tx, current.fence, reserved, binding)
    with pytest.raises(RepositoryConflictError):
        outcome(repo, tx, permit, record.assignment_id, dispatch_token=uid())
    with pytest.raises(RepositoryValidationError):
        outcome(
            repo,
            tx,
            permit,
            record.assignment_id,
            outcome=AssignmentActionOutcome("failed_not_started", digest("refund"), {}),
        )
    with pytest.raises(RepositoryValidationError):
        outcome(
            repo,
            tx,
            permit,
            record.assignment_id,
            outcome=AssignmentActionOutcome("invalid", digest("invalid"), {}),
        )


def test_source_duplicate_provider_revision_uses_retained_identity(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    event = AssignmentSourceEvent(uid(), "page", "item", "v1", digest("id"), digest({}), {})
    first = AssignmentSourceBatch(
        "one", digest("one"), "page", digest(record.definition.source), digest(None), 1, (event,)
    )
    stored, _ = repo.record_source_batch(
        tx,
        fence=current.fence,
        expected_state_version=current.assignment.state_version,
        batch=first,
    )
    second = replace(
        first,
        batch_key="two",
        batch_digest=digest("two"),
        expected_cursor_digest=digest(1),
        next_cursor=2,
        events=(replace(event, event_id=uid()),),
    )
    stored, events = repo.record_source_batch(
        tx, fence=current.fence, expected_state_version=stored.state_version, batch=second
    )
    assert events[0].event_id == event.event_id
    assert (
        repo.record_source_batch(tx, fence=current.fence, expected_state_version=0, batch=second)[1]
        == events
    )
    with pytest.raises(RepositoryConflictError):
        repo.record_source_batch(
            tx,
            fence=current.fence,
            expected_state_version=stored.state_version,
            batch=replace(second, next_cursor=3),
        )
    with pytest.raises(RepositoryConflictError):
        finish(repo, tx, current.fence, checkpoint={"cursor": 999})


def test_graph_cycles_result_conflicts_and_completed_plan_compaction(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    a = AssignmentTask(
        "a", "plan", 1, "A", "Work A", record.definition.allowed_tools, depends_on=("b",)
    )
    b = AssignmentTask(
        "b", "plan", 1, "B", "Work B", record.definition.allowed_tools, depends_on=("a",)
    )
    with pytest.raises(RepositoryValidationError, match="cycle"):
        repo.put_task_plan(
            tx,
            fence=current.fence,
            expected_state_version=current.assignment.state_version,
            plan_key="plan",
            plan_digest=digest("cycle"),
            tasks=(a, b),
        )
    a = replace(a, depends_on=())
    stored = repo.put_task_plan(
        tx,
        fence=current.fence,
        expected_state_version=current.assignment.state_version,
        plan_key="plan",
        plan_digest=digest((a,)),
        tasks=(a,),
    )
    with pytest.raises(RepositoryConflictError):
        repo.put_task_plan(
            tx,
            fence=current.fence,
            expected_state_version=stored.state_version,
            plan_key="new",
            plan_digest=digest("new"),
            tasks=(replace(a, plan_key="new"),),
        )
    task_claim = repo.claim_task(tx, fence=current.fence, task_id="a", expected_task_generation=0)
    result = AssignmentTaskResult("completed", digest("result"), "result")
    stored = repo.complete_task(tx, claim=task_claim, result=result)
    with pytest.raises(RepositoryConflictError):
        repo.complete_task(tx, claim=task_claim, result=replace(result, bounded_result="altered"))
    with pytest.raises(RepositoryConflictError):
        repo.put_task_plan(
            tx,
            fence=current.fence,
            expected_state_version=stored.state_version,
            plan_key="new",
            plan_digest=digest("new"),
            tasks=(replace(a, plan_key="new"),),
        )
    with pytest.raises(RepositoryConflictError):
        finish(
            repo,
            tx,
            current.fence,
            incorporations=(
                {"task_id": "a", "parent_task_id": "wrong", "result_digest": result.result_digest},
            ),
        )


def test_expired_approval_and_changed_decision_are_refused(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    created = action(
        repo,
        tx,
        current.fence,
        sensitivity="sensitive",
        approval_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    args = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=created.action_id,
        expected_instruction_revision=1,
        expected_control_epoch=1,
    )
    decision = AssignmentActionDecision(
        created.intent.request_digest,
        "decline",
        uid(),
        digest("decline"),
        created.intent.permission_digest,
        created.intent.precondition_digest,
    )
    declined = repo.decide_action(tx, **args, decision=decision)
    assert declined.state == "declined"
    assert repo.decide_action(tx, **args, decision=decision) == declined
    with pytest.raises(RepositoryConflictError):
        repo.decide_action(tx, **args, decision=replace(decision, decision="approve"))
    expired = action(
        repo,
        tx,
        current.fence,
        sensitivity="sensitive",
        approval_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    tx.execute(
        "UPDATE persistent_assignment_action "
        "SET data=jsonb_set(data,'{intent,approval_expires_at}', "
        "to_jsonb((clock_timestamp()-interval '1 second')::text)) WHERE id=%s",
        (expired.action_id,),
    )
    with pytest.raises(RepositoryConflictError):
        repo.decide_action(
            tx,
            **dict(args, action_id=expired.action_id),
            decision=replace(decision, proposal_digest=expired.intent.request_digest),
        )


@contextmanager
def independent_database(schema):
    import psycopg2

    database_url = os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"]
    if database_url.startswith("sqlite:"):
        from astralplane.database.sqlite import create_sqlite_driver_pool

        pool = ConnectionPool(create_sqlite_driver_pool(database_url))
        try:
            yield PlaneDatabase(pool)
        finally:
            pool.close()
        return
    connection = psycopg2.connect(os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"])
    with connection.cursor() as cursor:
        cursor.execute(f'SET search_path TO "{schema}",pg_catalog')
    connection.commit()
    pool = ConnectionPool(Pool(connection))
    try:
        yield PlaneDatabase(pool)
    finally:
        pool.close()
        connection.close()


def parallel_transactions(database, operations):
    from threading import Event

    from astralplane import is_lock_contention

    with database.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    barrier = Barrier(len(operations))
    peer_finished = Event()

    def run(operation):
        with independent_database(schema) as db:
            barrier.wait(timeout=10)
            try:
                for attempt in range(4):
                    try:
                        with db.transaction() as tx:
                            return operation(tx)
                    except RepositoryConflictError as error:
                        if not (tx.dialect.is_sqlite and is_lock_contention(error) and attempt < 3):
                            return error
                        assert peer_finished.wait(timeout=10)
            finally:
                peer_finished.set()

    with ThreadPoolExecutor(max_workers=len(operations)) as workers:
        return tuple(workers.map(run, operations))


def test_two_workers_cannot_claim_the_same_assignment(database, repo):
    with database.transaction() as tx:
        tx.execute("DELETE FROM persistent_assignment")
        create(repo, tx)
    results = parallel_transactions(
        database,
        (
            lambda tx: repo.claim_due_for_administration(tx, worker_id="one"),
            lambda tx: repo.claim_due_for_administration(tx, worker_id="two"),
        ),
    )
    assert sorted(len(result) for result in results) == [0, 1]


def test_concurrent_action_reservations_share_the_last_budget_unit(database, repo):
    with database.transaction() as tx:
        tx.execute("DELETE FROM persistent_assignment")
        definition_value = definition(tx)
        limits = dict(definition_value.limits, tool_calls=1)
        create(repo, tx, definition=replace(definition_value, limits=limits))
        current = claim(repo, tx)
        actions = (action(repo, tx, current.fence), action(repo, tx, current.fence))
    results = parallel_transactions(
        database,
        tuple(lambda tx, item=item: reserve(repo, tx, current.fence, item) for item in actions),
    )
    assert sum(isinstance(result, RepositoryConflictError) for result in results) == 1
    with database.transaction() as tx:
        stored = repo.get_assignment(
            tx, owner_id="owner", assignment_id=current.fence.assignment_id
        )
        assert stored.usage["outstanding"]["tool_calls"] == 1


def test_control_and_dispatch_have_one_durable_order(database, repo):
    with database.transaction() as tx:
        tx.execute("DELETE FROM persistent_assignment")
        record = create(repo, tx)
        current = claim(repo, tx)
        binding = bind(repo, tx, current.fence)
        reserved = reserve(repo, tx, current.fence, action(repo, tx, current.fence))
    started, paused = parallel_transactions(
        database,
        (
            lambda tx: start(repo, tx, current.fence, reserved, binding),
            lambda tx: control(repo, tx, record),
        ),
    )
    assert paused.assignment.lifecycle == "paused"
    with database.transaction() as tx:
        with pytest.raises(RepositoryConflictError):
            start(repo, tx, current.fence, reserved, binding)
        if isinstance(started, RepositoryConflictError):
            assert paused.begun_action_ids == ()
            assert paused.assignment.usage["outstanding"]["tool_calls"] == 0
        else:
            assert paused.begun_action_ids == (reserved.action.action_id,)
            assert outcome(repo, tx, started, record.assignment_id).state == "succeeded"


def test_remote_proposal_link_and_create_roll_back_together(database, repo):
    from astralplane.repositories.remote_proposals import (
        RemoteOperationProposalRecord,
        RemoteOperationProposalRepository,
    )

    with database.transaction() as tx:
        tx.execute("DELETE FROM persistent_assignment")
        record = create(repo, tx)
        current = claim(repo, tx)
        request = dict(
            kind="tool", agent_id="remote-compute-1", tool_name="delete_file", arguments={}
        )
        created = action(
            repo,
            tx,
            current.fence,
            request=request,
            request_digest=digest(request),
            sensitivity="sensitive",
            interactive_only=True,
            approval_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    proposals = RemoteOperationProposalRepository()
    now = int(datetime.now(UTC).timestamp())
    proposal = RemoteOperationProposalRecord(
        uuid.uuid4().hex,
        "owner",
        None,
        "machine",
        "remote-compute-1",
        "delete_file",
        digest({}),
        {},
        "Delete file",
        "pending",
        now,
        now + 300,
    )
    args = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=created.action_id,
        expected_request_digest=created.intent.request_digest,
        proposal_id=proposal.proposal_id,
        expected_instruction_revision=1,
        expected_control_epoch=1,
    )
    with pytest.raises(RepositoryConflictError), database.transaction() as tx:
        proposals.create(tx, proposal)
        repo.link_interactive_proposal(tx, **dict(args, expected_request_digest=digest("wrong")))
    with database.transaction() as tx:
        assert proposals.get(tx, owner_id="owner", proposal_id=proposal.proposal_id) is None
        proposals.create(tx, proposal)
        repo.link_interactive_proposal(tx, **args)
    with database.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    with independent_database(schema) as restarted, restarted.transaction() as tx:
        assert (
            AssignmentRepository()
            .get_action_for_interactive_proposal(
                tx, owner_id="owner", proposal_id=proposal.proposal_id
            )
            .action_id
            == created.action_id
        )
        proposals.decide_if_pending(
            tx,
            owner_id="owner",
            proposal_id=proposal.proposal_id,
            decision="declined",
            decided_at=now,
        )
        assert (
            repo.observe_interactive_proposal(
                tx, owner_id="owner", proposal_id=proposal.proposal_id
            ).state
            == "declined"
        )


def test_finishing_releases_unstarted_but_cannot_orphan_started_action(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    reserved = reserve(repo, tx, current.fence, action(repo, tx, current.fence))
    ended = finish(repo, tx, current.fence)
    assert ended.usage["outstanding"]["tool_calls"] == 0
    assert (
        repo.get_action(
            tx,
            owner_id="owner",
            assignment_id=record.assignment_id,
            action_id=reserved.action.action_id,
        ).state
        == "failed_not_started"
    )
    tx.execute(
        "UPDATE persistent_assignment SET next_wake_at=statement_timestamp(), "
        "data=jsonb_set(data,'{next_wake_at}',to_jsonb(statement_timestamp()::text)) WHERE id=%s",
        (record.assignment_id,),
    )
    current = claim(repo, tx)
    binding = bind(repo, tx, current.fence)
    started = start(
        repo,
        tx,
        current.fence,
        reserve(repo, tx, current.fence, action(repo, tx, current.fence)),
        binding,
    )
    with pytest.raises(RepositoryConflictError, match="in_flight"):
        finish(repo, tx, current.fence)
    outcome(repo, tx, started, record.assignment_id)
    finish(repo, tx, current.fence)


def test_event_completion_cannot_hide_an_uncertain_direct_effect(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    event = AssignmentSourceEvent(
        uid(), "source", "item", "revision", digest("id"), digest("ctx"), "ctx"
    )
    batch = AssignmentSourceBatch(
        "batch",
        digest("batch"),
        "source",
        digest(record.definition.source),
        digest(None),
        1,
        (event,),
    )
    repo.record_source_batch(
        tx,
        fence=current.fence,
        expected_state_version=current.assignment.state_version,
        batch=batch,
    )
    created = action(repo, tx, current.fence, event_id=event.event_id)
    reserved = reserve(repo, tx, current.fence, created)
    with pytest.raises(RepositoryConflictError, match="uncertain"):
        finish(
            repo,
            tx,
            current.fence,
            event_receipts=(
                {
                    "event_id": event.event_id,
                    "disposition": "completed",
                    "result_digest": digest("done"),
                },
            ),
        )
    repo.release_unstarted_action(
        tx,
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=created.action_id,
        attempt_id=reserved.attempt_id,
        expected_request_digest=created.intent.request_digest,
        reason_code="cancelled",
    )
    finish(
        repo,
        tx,
        current.fence,
        event_receipts=(
            {
                "event_id": event.event_id,
                "disposition": "irrelevant",
                "result_digest": digest("done"),
            },
        ),
    )


@pytest.mark.parametrize("remote_state", ["pending", "approved"])
def test_terminal_retirement_expires_remote_capability_before_removing_link(tx, repo, remote_state):
    from astralplane.repositories.remote_proposals import (
        RemoteOperationProposalRecord,
        RemoteOperationProposalRepository,
    )

    record = create(repo, tx)
    current = claim(repo, tx)
    request = dict(kind="tool", agent_id="remote-compute-1", tool_name="delete_file", arguments={})
    created = action(
        repo,
        tx,
        current.fence,
        request=request,
        request_digest=digest(request),
        sensitivity="sensitive",
        interactive_only=True,
        approval_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    proposals = RemoteOperationProposalRepository()
    now = int(datetime.now(UTC).timestamp())
    proposal = RemoteOperationProposalRecord(
        uuid.uuid4().hex,
        "owner",
        None,
        "machine",
        "remote-compute-1",
        "delete_file",
        digest({}),
        {},
        "Delete file",
        "pending",
        now,
        now + 300,
    )
    proposals.create(tx, proposal)
    repo.link_interactive_proposal(
        tx,
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=created.action_id,
        expected_request_digest=created.intent.request_digest,
        proposal_id=proposal.proposal_id,
        expected_instruction_revision=1,
        expected_control_epoch=1,
    )
    if remote_state == "approved":
        proposals.decide_if_pending(
            tx,
            owner_id="owner",
            proposal_id=proposal.proposal_id,
            decision="approved",
            decided_at=now,
        )
    stopped = control(repo, tx, record, "stop").assignment
    assert proposals.get(tx, owner_id="owner", proposal_id=proposal.proposal_id).status == "expired"
    invalidated = repo.get_action(
        tx, owner_id="owner", assignment_id=record.assignment_id, action_id=created.action_id
    )
    assert not invalidated.ever_started
    tx.execute(
        "UPDATE remote_operation_proposal SET status=%s WHERE proposal_id=%s",
        (remote_state, proposal.proposal_id),
    )
    repo.delete_for_owner(
        tx,
        owner_id="owner",
        assignment_id=record.assignment_id,
        expected_control_epoch=stopped.control_epoch,
        expected_state_version=stopped.state_version,
    )
    assert (
        repo.get_action_for_interactive_proposal(
            tx, owner_id="owner", proposal_id=proposal.proposal_id
        )
        is None
    )
    assert proposals.get(tx, owner_id="owner", proposal_id=proposal.proposal_id).status == "expired"


def test_account_retirement_stops_and_retains_unresolved_effect_then_finishes(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    binding = bind(repo, tx, current.fence)
    created = action(repo, tx, current.fence, boundary="unreplayable")
    permit = start(repo, tx, current.fence, reserve(repo, tx, current.fence, created), binding)
    safe = create(repo, tx)
    retired = repo.retire_owner(tx, owner_id="owner")
    assert set(retired.stopped_assignment_ids) == {record.assignment_id, safe.assignment_id}
    assert retired.deleted_assignment_ids == (safe.assignment_id,)
    assert retired.unresolved_action_ids == (created.action_id,)
    assert (
        repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id).lifecycle
        == "stopped"
    )
    with pytest.raises(RepositoryConflictError, match="retired"):
        create(repo, tx)
    with pytest.raises(RepositoryConflictError):
        repo.delete_for_owner(
            tx, owner_id="owner", assignment_id=record.assignment_id, expected_control_epoch=2
        )
    result = outcome(repo, tx, permit, record.assignment_id)
    assert result.ever_started
    assert repo.retire_owner(tx, owner_id="owner").deleted_assignment_ids == (record.assignment_id,)
    assert repo.retire_owner(tx, owner_id="owner").unresolved_action_ids == ()


def test_revise_supersedes_old_source_work_and_keeps_completed_results_inspectable(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    event = AssignmentSourceEvent(
        uid(), "source", "item", "revision", digest("id"), digest("ctx"), "ctx"
    )
    batch = AssignmentSourceBatch(
        "batch",
        digest("batch"),
        "source",
        digest(record.definition.source),
        digest(None),
        1,
        (event,),
    )
    stored, _ = repo.record_source_batch(
        tx,
        fence=current.fence,
        expected_state_version=current.assignment.state_version,
        batch=batch,
    )
    task = AssignmentTask(
        "old",
        "old-plan",
        1,
        "Read release",
        "Investigate",
        record.definition.allowed_tools,
        event_id=event.event_id,
    )
    repo.put_task_plan(
        tx,
        fence=current.fence,
        expected_state_version=stored.state_version,
        plan_key="old-plan",
        plan_digest=digest((task,)),
        tasks=(task,),
    )
    task_claim = repo.claim_task(tx, fence=current.fence, task_id="old", expected_task_generation=0)
    repo.complete_task(
        tx,
        claim=task_claim,
        result=AssignmentTaskResult("completed", digest("found"), "Original result"),
    )
    replacement = replace(
        record.definition, instructions="Different instructions", source={"reader": "new"}
    )
    revised = control(repo, tx, record, "revise", replacement=replacement).assignment
    assert revised.tasks[0]["state"] == "superseded"
    assert "cursor" not in revised.checkpoint
    assert (
        repo.list_events(
            tx, owner_id="owner", assignment_id=record.assignment_id, disposition="pending"
        )
        == ()
    )
    assert (
        repo.list_events(tx, owner_id="owner", assignment_id=record.assignment_id)[0].disposition
        == "superseded"
    )
    current = claim(repo, tx)
    task = replace(task, task_id="new", plan_key="new-plan", instruction_revision=2, event_id=None)
    repo.put_task_plan(
        tx,
        fence=current.fence,
        expected_state_version=current.assignment.state_version,
        plan_key="new-plan",
        plan_digest=digest((task,)),
        tasks=(task,),
    )
    archived = repo.list_activity(tx, owner_id="owner", assignment_id=record.assignment_id)[0]
    assert archived.activity_type == "task_superseded" and archived.summary == "Original result"
    assert archived.references["result_digest"] == digest("found")


def test_failed_episodes_enforce_exponential_retry_not_bypassed_by_owner_check(tx, repo):
    record = create(repo, tx)
    for index in (1, 2):
        current = claim(repo, tx)
        ended = finish(repo, tx, current.fence, phase="failed", next_wake_at=datetime.now(UTC))
        remaining = (ended.next_wake_at - datetime.now(UTC)).total_seconds()
        assert 60 * 2 ** (index - 1) - 5 <= remaining <= 60 * 2 ** (index - 1) + 5
        checked = repo.request_check(
            tx,
            owner_id="owner",
            assignment_id=record.assignment_id,
            expected_instruction_revision=1,
            expected_control_epoch=1,
            submission_id=uid(),
            submission_digest=digest(str(index)),
        )
        assert checked.next_wake_at >= ended.next_wake_at
        tx.execute(
            "UPDATE persistent_assignment SET next_wake_at=statement_timestamp(), "
            "data=jsonb_set(data,'{next_wake_at}',to_jsonb(statement_timestamp()::text)) "
            "WHERE id=%s",
            (record.assignment_id,),
        )


@pytest.mark.parametrize(
    "path,value",
    [
        ("{owner_id}", "other"),
        ("{phase}", "invalid"),
        ("{checkpoint}", []),
        ("{instruction_revision}", 0),
        ("{usage,spent,tokens}", -1),
        ("{definition,name}", None),
        ("{tasks}", [{}]),
    ],
)
def test_corrupt_assignment_state_fails_closed(tx, repo, path, value):
    record = create(repo, tx)
    tx.execute(
        "UPDATE persistent_assignment SET data=jsonb_set(data,%s::text[],%s::jsonb) WHERE id=%s",
        (path, canonical(value), record.assignment_id),
    )
    with pytest.raises(RepositoryDataError):
        repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)


def test_corrupt_action_owner_and_missing_parent_are_refused(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    created = action(repo, tx, current.fence)
    tx.execute(
        "UPDATE persistent_assignment_action SET data=jsonb_set(data,'{owner_id}',%s::jsonb) "
        "WHERE id=%s",
        (canonical("other"), created.action_id),
    )
    with pytest.raises(RepositoryDataError):
        repo.get_action(
            tx, owner_id="owner", assignment_id=record.assignment_id, action_id=created.action_id
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.list_activity(tx, owner_id="other", assignment_id=record.assignment_id)
    assert (
        repo.observe_interactive_proposal(tx, owner_id="owner", proposal_id=uuid.uuid4().hex)
        is None
    )


def test_create_denies_missing_revoked_and_cross_owner_consent(tx, repo):
    value = definition(tx)
    for definition_value in (
        replace(value, offline_grant_id=None),
        replace(value, offline_grant_id=uid()),
        replace(value, conversation_id=uid()),
    ):
        with pytest.raises(RepositoryConflictError):
            create(repo, tx, definition=definition_value)
    tx.execute("UPDATE user_offline_grant SET revoked_at=1 WHERE id=%s", (value.offline_grant_id,))
    with pytest.raises(RepositoryConflictError):
        create(repo, tx, definition=value)


def test_completed_episode_generation_is_nonsecret_restart_evidence(tx, repo):
    record = create(repo, tx)
    assert record.last_completed_generation == 0
    current = claim(repo, tx)
    ended = finish(repo, tx, current.fence)
    assert ended.last_completed_generation == current.fence.claim_generation
    stored = AssignmentRepository().get_assignment(
        tx, owner_id="owner", assignment_id=record.assignment_id
    )
    assert stored.last_completed_generation == current.fence.claim_generation
    assert "claim_token" not in plain(stored) and "dispatch_token" not in plain(stored)


def test_public_assignment_contract_behaviors():
    fixture = standalone_database()
    db = next(fixture)
    try:
        repository = AssignmentRepository()
        with db.transaction() as transaction:
            test_create_replay_owner_controls_and_retirement(transaction, repository)
        test_two_workers_cannot_claim_the_same_assignment(db, repository)
    finally:
        fixture.close()


@pytest.mark.parametrize("change", ["quote_digest", "quote_expiry", "currency", "spending"])
def test_reservation_cannot_substitute_immutable_quote_or_money_bound(tx, repo, change):
    create(repo, tx)
    current = claim(repo, tx)
    expiry = datetime.now(UTC) + timedelta(minutes=2)
    maximum = AssignmentResourceAmount(
        tool_calls=1, elapsed_ms=1000, spend_micro_units=50, currency="USD"
    )
    created = action(
        repo,
        tx,
        current.fence,
        maximum=maximum,
        quote_digest=digest("quote"),
        quote_expires_at=expiry,
    )
    kwargs = {"quote_digest": digest("quote"), "quote_expires_at": expiry}
    if change == "quote_digest":
        kwargs["quote_digest"] = digest("different")
    elif change == "quote_expiry":
        kwargs["quote_expires_at"] += timedelta(minutes=1)
    elif change == "currency":
        kwargs["maximum"] = replace(maximum, currency="EUR")
    else:
        kwargs["maximum"] = replace(maximum, spend_micro_units=49)
    with pytest.raises(RepositoryConflictError):
        reserve(repo, tx, current.fence, created, **kwargs)
    reserved = reserve(
        repo, tx, current.fence, created, quote_digest=digest("quote"), quote_expires_at=expiry
    )
    with pytest.raises(RepositoryConflictError):
        reserve(repo, tx, current.fence, created, attempt_id=reserved.attempt_id, **kwargs)


def test_full_history_still_persists_attention_and_releases_claim(tx, repo):
    record = create(repo, tx)
    current = claim(repo, tx)
    activity = repo.append_activity(
        tx,
        fence=current.fence,
        activity=AssignmentActivityRecord("first", "checked", "Checked", "No change"),
    )
    if tx.dialect.is_sqlite:
        import hashlib
        import json
        from uuid import UUID

        stored = tx.fetch_one(
            "SELECT * FROM persistent_assignment_activity WHERE id=%s", (activity.activity_id,)
        )
        for sequence in range(2, 1001):
            identifier = hashlib.md5(f"copy{sequence}".encode()).hexdigest()
            data = dict(
                plain(stored["data"]),
                activity_key=f"copy{sequence}",
                sequence=sequence,
                activity_id=identifier,
            )
            tx.execute(
                "INSERT INTO persistent_assignment_activity "
                "(id,assignment_id,owner_user_id,activity_key,sequence,notification_state,data) "
                "VALUES(%s,%s,%s,%s,%s,'none',%s::jsonb)",
                (
                    str(UUID(identifier)),
                    stored["assignment_id"],
                    stored["owner_user_id"],
                    f"copy{sequence}",
                    sequence,
                    json.dumps(data),
                ),
            )
    else:
        tx.execute(
        "INSERT INTO persistent_assignment_activity "
        "(id,assignment_id,owner_user_id,activity_key,sequence,notification_state,data) "
        "SELECT md5('copy'||n)::uuid,assignment_id,owner_user_id,'copy'||n,n,'none', "
        "data || jsonb_build_object('activity_key','copy'||n,'sequence',n, "
        "'activity_id',md5('copy'||n)) FROM persistent_assignment_activity, "
        "generate_series(2,1000) AS n WHERE id=%s",
        (activity.activity_id,),
    )
    ended = finish(
        repo,
        tx,
        current.fence,
        phase="failed",
        safe_error_code="assignment_history_capacity_exhausted",
        activity=AssignmentActivityRecord(
            "capacity", "attention", "History is full", "Review storage"
        ),
    )
    assert ended.safe_error_code == "assignment_history_capacity_exhausted"
    assert ended.phase == "failed"
    with pytest.raises(RepositoryConflictError):
        repo.assert_current_claim(tx, fence=current.fence)
    assert (
        tx.fetch_one(
            "SELECT count(*) AS n FROM persistent_assignment_activity WHERE assignment_id=%s",
            (record.assignment_id,),
        )["n"]
        == 1000
    )


@pytest.mark.parametrize("prior_currency,settled", [(None, False), ("USD", False), ("USD", True)])
def test_revision_cannot_reinterpret_spent_or_outstanding_currency(
    tx, repo, prior_currency, settled
):
    value = definition(tx)
    expiry = datetime.now(UTC) + timedelta(hours=1)
    quote = {"quote_digest": digest("quote"), "expires_at": expiry.isoformat()}
    if prior_currency:
        value = replace(
            value,
            limits=dict(
                value.limits,
                currency=prior_currency,
                spend_micro_units=100,
                daily_spend_micro_units=100,
            ),
            cost_quote_coverage=quote,
        )
    record = create(repo, tx, definition=value)
    current = claim(repo, tx)
    maximum = AssignmentResourceAmount(
        tool_calls=1,
        elapsed_ms=1000,
        currency=prior_currency,
        spend_micro_units=1 if prior_currency else None,
    )
    quote_args = (
        {"quote_digest": digest("quote"), "quote_expires_at": expiry} if prior_currency else {}
    )
    created = action(repo, tx, current.fence, maximum=maximum, **quote_args)
    reservation = reserve(repo, tx, current.fence, created, **quote_args)
    if settled:
        binding = bind(repo, tx, current.fence)
        permit = start(repo, tx, current.fence, reservation, binding)
        outcome(repo, tx, permit, record.assignment_id)
    replacement = replace(
        value,
        limits=dict(
            value.limits, currency="JPY", spend_micro_units=100, daily_spend_micro_units=100
        ),
        cost_quote_coverage=quote,
    )
    with pytest.raises(
        RepositoryConflictError, match=r"prior_cost_unknown|currency_change_invalid"
    ):
        control(repo, tx, record, "revise", replacement=replacement)
