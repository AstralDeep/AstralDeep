"""Transactional personal-agent runtime fencing tests for feature 060.

The suite creates a throwaway PostgreSQL database.  It never mutates the
configured development database and deliberately exercises repository
reconstruction so PostgreSQL, not process-local state, remains authoritative.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest

from orchestrator.agent_generator import AgentCodeGenerator
from orchestrator.agent_constitution import (
    AGENT_CONSTITUTION_VERSION,
    USER_AGENT_POLICY_REVISION,
)
from orchestrator.agent_lifecycle import (
    ActiveRevisionReplay,
    AgentRevisionActivator,
    CandidateAgentMetadata,
    CandidatePreparation,
    PostgresPersonalAgentRevisionStore,
    RevisionActivationError,
    RevisionActivationRecoveryPendingError,
)
from orchestrator.user_agents import (
    AgentDeletedError,
    AgentOfflineError,
    HostRegistrationRefused,
    PersonalAgentRuntimeRepository,
    RuntimeCompatibilityPolicy,
    StaleRuntimeGenerationError,
    UserAgentOwnershipConflict,
    authorize_registration,
    can_user_use_agent,
    create_user_agent,
    go_live,
    mark_revalidation_required,
    mark_validated,
    soft_delete,
    touch_liveness,
)
from orchestrator.work_admission import (
    ExecutionFence,
    OperationState,
    PlaneWorkAdmissionRepository,
)
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


_FIXTURE = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "runtime_reliability_060"
        / "runtime-lock-contract.json"
    ).read_text(encoding="utf-8")
)
_LOCK_DIGEST = str(_FIXTURE["lock_digest"])
_POLICY = RuntimeCompatibilityPolicy(
    runtime_contract_version=int(_FIXTURE["runtime_contract_version"]),
    runtime_lock_sha256=_LOCK_DIGEST,
)
_OWNER = "owner-us2"
_AGENT = "agent-us2"
_CANDIDATE_DISPLAY_NAME = "Promoted US2 Agent"
_CANDIDATE_TOOLS = ("lookup",)
_CANDIDATE_SCOPES = ("tools:read",)
_CANDIDATE_EGRESS = ("api.example.test",)


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[PlaneTestRuntime]:
    with isolated_plane_runtime("byo_runtime") as runtime:
        yield runtime


@pytest.fixture
def clean_database(postgres_database: PlaneTestRuntime) -> PlaneTestRuntime:
    with postgres_database.transaction() as transaction:
        transaction.execute(
            "UPDATE user_agent SET active_revision_id = NULL, "
            "last_known_good_revision_id = NULL, selected_host_session_id = NULL, "
            "authoritative_instance_id = NULL"
        )
        transaction.execute("DELETE FROM agent_runtime_request")
        transaction.execute("DELETE FROM agent_runtime_instance")
        transaction.execute("DELETE FROM draft_agents")
        transaction.execute("DELETE FROM user_agent_revision")
        transaction.execute("DELETE FROM agent_host_session")
        transaction.execute("DELETE FROM user_agent")
        transaction.execute("DELETE FROM operation_submission_result")
        transaction.execute(
            "UPDATE operation_admission_slot SET operation_id = NULL, "
            "lease_token = NULL, lease_expires_at = NULL"
        )
        transaction.execute("DELETE FROM operation_record")
    return postgres_database


@pytest.fixture
def repository(clean_database: PlaneTestRuntime) -> PersonalAgentRuntimeRepository:
    return _runtime_repository(clean_database)


def _runtime_repository(
    runtime: PlaneTestRuntime,
) -> PersonalAgentRuntimeRepository:
    return PersonalAgentRuntimeRepository(
        compatibility_policy=_POLICY,
        operation_repository=PlaneWorkAdmissionRepository(
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
        ),
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


def _running_operation(
    db: PlaneTestRuntime,
    *,
    owner_user_id: str = _OWNER,
    operation_kind: str = "agent_runtime_request",
) -> ExecutionFence:
    operation_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    db.execute(
        """
        INSERT INTO operation_record (
            operation_id, operation_kind, admission_class, owner_scope,
            owner_user_id, state, execution_generation,
            execution_lease_token, started_at
        ) VALUES (?, ?, 'interactive', 'user', ?, 'running', 1, ?, now())
        """,
        (str(operation_id), operation_kind, owner_user_id, str(lease_token)),
    )
    return ExecutionFence(operation_id, 1, lease_token)


def _purge_terminal_operations(
    repository: PersonalAgentRuntimeRepository,
) -> None:
    result = repository._operations.purge_expired(
        now=datetime.now(UTC) + timedelta(days=2),
        limit=500,
    )
    assert result.operations >= 1


def _terminalize_delivery_operation(
    repository: PersonalAgentRuntimeRepository,
    operation: ExecutionFence,
    *,
    state: OperationState = OperationState.RETRYABLE,
) -> None:
    terminal_code = (
        "revision_delivery_retryable"
        if state is OperationState.RETRYABLE
        else "revision_delivery_terminal"
    )
    repository._operations.terminalize(
        operation,
        state=state,
        terminal_code=terminal_code,
        safe_summary=None,
        retry_after_ms=0 if state is OperationState.RETRYABLE else None,
        now=None,
        retention=repository._operation_retention,
    )


def _host(
    repository: PersonalAgentRuntimeRepository,
    *,
    host_id: str | None = None,
    connection_scope_id: str | None = None,
):
    return repository.register_host_session(
        owner_user_id=_OWNER,
        connection_scope_id=connection_scope_id or str(uuid.uuid4()),
        host_id=host_id or str(uuid.uuid4()),
        platform="windows",
        client_version="0.4.0",
        supported_runtime_contract_versions=(_POLICY.runtime_contract_version,),
        runtime_lock_sha256=_LOCK_DIGEST,
    )


def _agent_revision(
    repository: PersonalAgentRuntimeRepository,
    db: PlaneTestRuntime,
):
    create_user_agent(
        db,
        agent_id=_AGENT,
        owner_user_id=_OWNER,
        display_name="US2 Agent",
    )
    mark_validated(db, _AGENT, "0.1.0")
    return repository.create_revision(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
        artifact_digest=hashlib.sha256(b"us2-agent-bundle").hexdigest(),
        manifest={
            "runtime_contract_version": _POLICY.runtime_contract_version,
            "files": [],
        },
        artifact_relative_path=f"{_AGENT}/revision-1",
        runtime_contract_version=_POLICY.runtime_contract_version,
        release_lock_digest=_LOCK_DIGEST,
    )


def _runtime(
    repository: PersonalAgentRuntimeRepository,
    db: PlaneTestRuntime,
    *,
    online: bool,
):
    revision = _agent_revision(repository, db)
    host = _host(repository)
    host = repository.mark_inventory_reconciled(host.fence)
    selection = repository.select_host_for_agent(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
    )
    assert selection.session is not None
    assert selection.session.host_session_id == host.host_session_id
    delivery_operation = _running_operation(
        db,
        operation_kind="agent_runtime_delivery",
    )
    instance = repository.create_prelaunch_instance(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
        host_session_id=host.host_session_id,
        revision_id=revision.revision_id,
        operation_fence=delivery_operation,
    )
    process_id = str(uuid.uuid4())
    instance = repository.bind_runtime_process(
        instance.fence,
        process_id=process_id,
        expected_state_revision=instance.state_revision,
    )
    if online:
        instance = repository.accept_runtime_registration(
            instance.fence,
            runtime_contract_version=_POLICY.runtime_contract_version,
            bundle_sha256=revision.artifact_digest,
        )
        instance = repository.record_runtime_heartbeat(
            instance.fence,
            heartbeat_sequence=1,
        )
        with db.transaction() as transaction:
            transaction.execute(
                "UPDATE user_agent_revision SET state = 'active', "
                "confirmed_at = now(), promoted_at = now(), "
                "state_revision = state_revision + 1 WHERE revision_id = %s",
                (revision.revision_id,),
            )
            transaction.execute(
                "UPDATE agent_runtime_instance SET state = 'online', "
                "is_authoritative = TRUE, ready_at = now(), "
                "last_liveness_at = now(), state_revision = state_revision + 1 "
                "WHERE runtime_instance_id = %s",
                (instance.fence.runtime_instance_id,),
            )
            transaction.execute(
                "UPDATE user_agent SET active_revision_id = %s, "
                "last_known_good_revision_id = %s, authoritative_instance_id = %s, "
                "lifecycle_generation = %s, state_revision = state_revision + 1 "
                "WHERE agent_id = %s AND owner_user_id = %s",
                (
                    revision.revision_id,
                    revision.revision_id,
                    instance.fence.runtime_instance_id,
                    instance.fence.lifecycle_generation,
                    _AGENT,
                    _OWNER,
                ),
            )
        instance = repository.get_runtime_instance(instance.fence.runtime_instance_id)
    return revision, host, instance


def test_latest_runtime_instances_are_owner_scoped_durable_hydration(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    revision, _host_record, online = _runtime(repository, clean_database, online=True)

    latest = repository.list_latest_runtime_instances(owner_user_id=_OWNER)

    assert latest == (online,)
    assert latest[0].active_revision_id == revision.revision_id
    assert latest[0].authoritative_instance_id == online.fence.runtime_instance_id
    assert (
        repository.list_latest_runtime_instances(owner_user_id="different-owner") == ()
    )


def test_registration_validates_before_allocating_and_persisting_session(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    invalid_cases = (
        ({"host_id": "not-a-uuid"}, "invalid_host_registration"),
        ({"platform": "linux"}, "invalid_host_registration"),
        ({"client_version": "0.4"}, "invalid_host_registration"),
        ({"supported_runtime_contract_versions": ()}, "invalid_host_registration"),
        (
            {
                "supported_runtime_contract_versions": (
                    _POLICY.runtime_contract_version,
                    _POLICY.runtime_contract_version,
                )
            },
            "invalid_host_registration",
        ),
        ({"supported_runtime_contract_versions": (1,)}, "runtime_contract_unsupported"),
        ({"runtime_lock_sha256": "0" * 64}, "runtime_lock_mismatch"),
    )
    base = {
        "owner_user_id": _OWNER,
        "connection_scope_id": str(uuid.uuid4()),
        "host_id": str(uuid.uuid4()),
        "platform": "windows",
        "client_version": "0.4.0",
        "supported_runtime_contract_versions": (_POLICY.runtime_contract_version,),
        "runtime_lock_sha256": _LOCK_DIGEST,
    }
    for changes, expected_code in invalid_cases:
        with pytest.raises(HostRegistrationRefused) as raised:
            repository.register_host_session(**(base | changes))
        assert raised.value.code == expected_code

    assert (
        clean_database.fetch_one("SELECT count(*) AS count FROM agent_host_session")[
            "count"
        ]
        == 0
    )

    accepted = repository.register_host_session(**base)
    assert uuid.UUID(accepted.host_session_id).version == 4
    assert accepted.host_session_id not in {
        accepted.host_id,
        accepted.connection_scope_id,
    }
    assert accepted.state == "connected"
    assert accepted.inventory_state == "pending"
    assert accepted.runtime_contract_version == _POLICY.runtime_contract_version
    persisted = clean_database.fetch_one(
        "SELECT * FROM agent_host_session WHERE host_session_id = ?",
        (accepted.host_session_id,),
    )
    assert str(persisted["host_id"]) == accepted.host_id
    assert str(persisted["connection_scope_id"]) == accepted.connection_scope_id


def test_legacy_create_preserves_owner_and_rejects_cross_owner_overwrite(
    clean_database: PlaneTestRuntime,
) -> None:
    create_user_agent(
        clean_database,
        agent_id=_AGENT,
        owner_user_id=_OWNER,
        display_name="Original",
    )
    create_user_agent(
        clean_database,
        agent_id=_AGENT,
        owner_user_id=_OWNER,
        display_name="Same-owner update",
    )
    updated = clean_database.fetch_one(
        "SELECT owner_user_id, display_name FROM user_agent WHERE agent_id = ?",
        (_AGENT,),
    )
    assert updated == {
        "owner_user_id": _OWNER,
        "display_name": "Same-owner update",
    }

    with pytest.raises(UserAgentOwnershipConflict):
        create_user_agent(
            clean_database,
            agent_id=_AGENT,
            owner_user_id="different-owner",
            display_name="Stolen",
        )
    unchanged = clean_database.fetch_one(
        "SELECT owner_user_id, display_name FROM user_agent WHERE agent_id = ?",
        (_AGENT,),
    )
    assert unchanged == updated


def test_legacy_lifecycle_mutations_cannot_revive_a_tombstoned_agent(
    clean_database: PlaneTestRuntime,
) -> None:
    create_user_agent(
        clean_database,
        agent_id=_AGENT,
        owner_user_id=_OWNER,
        display_name="Deleted",
    )
    mark_validated(clean_database, _AGENT, "0.1.0")
    soft_delete(clean_database, _AGENT)

    with pytest.raises(AgentDeletedError):
        create_user_agent(
            clean_database,
            agent_id=_AGENT,
            owner_user_id=_OWNER,
            display_name="Resurrected",
        )
    with pytest.raises(AgentDeletedError):
        mark_validated(clean_database, _AGENT, "0.1.0")
    with pytest.raises(AgentDeletedError):
        go_live(clean_database, _AGENT, host_session_id="legacy-session")
    with pytest.raises(AgentDeletedError):
        touch_liveness(clean_database, _AGENT)
    with pytest.raises(AgentDeletedError):
        mark_revalidation_required(clean_database, _AGENT, True)

    accepted, reason = authorize_registration(clean_database, _OWNER, _AGENT)
    assert accepted is False
    assert reason == "agent is deleted"
    assert can_user_use_agent(clean_database, _OWNER, _AGENT) is False
    tombstone = clean_database.fetch_one(
        "SELECT status, deleted_at, display_name FROM user_agent WHERE agent_id = ?",
        (_AGENT,),
    )
    assert tombstone["status"] == "disabled"
    assert tombstone["deleted_at"] is not None
    assert tombstone["display_name"] == "Deleted"


def test_sticky_selection_same_host_rollover_then_deterministic_failover(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _agent_revision(repository, clean_database)
    host_a_id = "11111111-1111-4111-8111-111111111111"
    host_b_id = "22222222-2222-4222-8222-222222222222"
    host_a1 = repository.mark_inventory_reconciled(
        _host(repository, host_id=host_a_id).fence
    )
    host_b = repository.mark_inventory_reconciled(
        _host(repository, host_id=host_b_id).fence
    )
    clean_database.execute(
        "UPDATE agent_host_session SET eligible_since = now() - interval '2 seconds' "
        "WHERE host_session_id = ?",
        (host_a1.host_session_id,),
    )
    selected = repository.select_host_for_agent(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
    )
    assert selected.session is not None
    assert selected.session.host_id == host_a_id

    host_a2 = repository.mark_inventory_reconciled(
        _host(repository, host_id=host_a_id).fence
    )
    assert host_a2.host_generation == host_a1.host_generation + 1
    assert host_a2.supersedes_session_id == host_a1.host_session_id
    selected = repository.select_host_for_agent(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
    )
    assert selected.session is not None
    assert selected.session.host_session_id == host_a2.host_session_id

    disconnected = repository.disconnect_host_session(
        host_a2.fence,
        failure_code="host_lost",
    )
    assert disconnected.selected_sessions[_AGENT] == host_b.host_session_id
    pointer = clean_database.fetch_one(
        "SELECT selected_host_session_id FROM user_agent WHERE agent_id = ?",
        (_AGENT,),
    )
    assert str(pointer["selected_host_session_id"]) == host_b.host_session_id


def test_restarted_desktop_gets_its_agent_back_through_sticky_reselection(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    """Feature 077 live finding: with ONE desktop, losing its session cleared
    the agent's host selection and nothing re-made it — the restarted client
    reconciled its retained bundle as keep_stopped/host_not_selected forever.
    A live agent whose selected session is gone is re-selected (same host id
    first) both when the host frame adapter asks which entries need a delivery
    fence and inside reconciliation, so the retained bundle starts."""
    revision = _agent_revision(repository, clean_database)
    host_id = "33333333-3333-4333-8333-333333333333"
    host_a1 = _host(repository, host_id=host_id)
    selection = repository.select_host_for_agent(owner_user_id=_OWNER, agent_id=_AGENT)
    assert selection.session is not None
    assert selection.session.host_session_id == host_a1.host_session_id
    clean_database.execute(
        "UPDATE user_agent_revision SET state = 'active', promoted_at = now() "
        "WHERE revision_id = ?",
        (revision.revision_id,),
    )
    clean_database.execute(
        "UPDATE user_agent SET active_revision_id = ?, "
        "last_known_good_revision_id = ? WHERE agent_id = ?",
        (revision.revision_id, revision.revision_id, _AGENT),
    )
    disconnected = repository.disconnect_host_session(host_a1.fence, failure_code="host_lost")
    assert disconnected.selected_sessions.get(_AGENT) is None       # nobody else to fail over to
    pointer = clean_database.fetch_one(
        "SELECT selected_host_session_id FROM user_agent WHERE agent_id = ?", (_AGENT,))
    assert pointer["selected_host_session_id"] is None

    # the same desktop comes back as a new session of the same host id
    host_a2 = _host(repository, host_id=host_id)
    assert host_a2.host_session_id != host_a1.host_session_id
    selected = repository.get_selected_session_revision(host_a2.fence, agent_id=_AGENT)
    assert selected.revision.revision_id == revision.revision_id
    pointer = clean_database.fetch_one(
        "SELECT selected_host_session_id FROM user_agent WHERE agent_id = ?", (_AGENT,))
    assert str(pointer["selected_host_session_id"]) == host_a2.host_session_id

    entry = {
        "agent_id": _AGENT,
        "revision_id": revision.revision_id,
        "bundle_sha256": revision.artifact_digest,
        "runtime_contract_version": _POLICY.runtime_contract_version,
        "required_runtime_lock_sha256": _LOCK_DIGEST,
    }
    operation = _running_operation(clean_database, operation_kind="agent_runtime_delivery")
    result = repository.reconcile_host_inventory(
        host_a2.fence,
        inventory_id=str(uuid.uuid4()),
        entries=(entry,),
        delivery_operation_fences={(_AGENT, revision.revision_id): operation},
    )
    assert [a.action for a in result.actions] == ["start"]
    assert result.actions[0].selected_delivery is not None
    instance = repository.get_runtime_instance(result.actions[0].selected_delivery.runtime_instance_id)
    assert instance.state == "delivering" and instance.fence.host_session_id == host_a2.host_session_id


def test_inventory_reconciliation_is_all_or_nothing_and_allocates_exact_start(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    revision = _agent_revision(repository, clean_database)
    host = _host(repository)
    selection = repository.select_host_for_agent(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
    )
    assert selection.session is not None
    assert selection.session.host_session_id == host.host_session_id
    clean_database.execute(
        "UPDATE user_agent_revision SET state = 'active', promoted_at = now() "
        "WHERE revision_id = ?",
        (revision.revision_id,),
    )
    clean_database.execute(
        "UPDATE user_agent SET active_revision_id = ?, "
        "last_known_good_revision_id = ? WHERE agent_id = ?",
        (revision.revision_id, revision.revision_id, _AGENT),
    )
    selected = repository.get_selected_session_revision(
        host.fence,
        agent_id=_AGENT,
    )
    assert selected.host.inventory_state == "pending"
    assert selected.revision.revision_id == revision.revision_id

    entry = {
        "agent_id": _AGENT,
        "revision_id": revision.revision_id,
        "bundle_sha256": revision.artifact_digest,
        "runtime_contract_version": _POLICY.runtime_contract_version,
        "required_runtime_lock_sha256": _LOCK_DIGEST,
    }
    before = clean_database.fetch_one(
        "SELECT generation_counter FROM user_agent WHERE agent_id = ?",
        (_AGENT,),
    )
    with pytest.raises(
        ValueError,
        match="delivery operations must exactly match inventory start actions",
    ):
        repository.reconcile_host_inventory(
            host.fence,
            inventory_id=str(uuid.uuid4()),
            entries=(entry,),
        )
    still_pending = clean_database.fetch_one(
        "SELECT inventory_state FROM agent_host_session WHERE host_session_id = ?",
        (host.host_session_id,),
    )
    after = clean_database.fetch_one(
        "SELECT generation_counter FROM user_agent WHERE agent_id = ?",
        (_AGENT,),
    )
    assert still_pending["inventory_state"] == "pending"
    assert after["generation_counter"] == before["generation_counter"]
    assert (
        clean_database.fetch_one(
            "SELECT count(*) AS count FROM agent_runtime_instance"
        )["count"]
        == 0
    )

    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    inventory_id = str(uuid.uuid4())
    result = repository.reconcile_host_inventory(
        host.fence,
        inventory_id=inventory_id,
        entries=(entry,),
        delivery_operation_fences={
            (_AGENT, revision.revision_id): operation,
        },
    )
    assert result.inventory_id == inventory_id
    assert result.host.inventory_state == "reconciled"
    assert result.reconciled_at == result.host.inventory_reconciled_at
    assert len(result.actions) == 1
    action = result.actions[0]
    assert action.action == "start"
    assert action.reason_code is None
    assert action.selected_delivery is not None
    assert action.selected_delivery.bundle_sha256 == revision.artifact_digest
    instance = repository.get_runtime_instance(
        action.selected_delivery.runtime_instance_id
    )
    assert instance.state == "delivering"
    assert instance.fence.process_id is None
    assert instance.fence.delivery_id == action.selected_delivery.delivery_id
    assert (
        instance.fence.lifecycle_generation
        == action.selected_delivery.lifecycle_generation
    )
    bound = repository.bind_runtime_process(
        instance.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=instance.state_revision,
    )
    with pytest.raises(StaleRuntimeGenerationError):
        repository.promote_recovered_runtime(bound.fence)
    registered = repository.accept_runtime_registration(
        bound.fence,
        runtime_contract_version=_POLICY.runtime_contract_version,
        bundle_sha256=revision.artifact_digest,
    )
    live = repository.record_runtime_heartbeat(
        registered.fence,
        heartbeat_sequence=1,
    )
    ready = repository.mark_runtime_ready(live.fence)
    promoted = repository.promote_recovered_runtime(ready.fence)
    assert promoted.state == "online"
    assert promoted.is_authoritative is True
    pointers = clean_database.fetch_one(
        "SELECT active_revision_id, last_known_good_revision_id, "
        "authoritative_instance_id, lifecycle_generation FROM user_agent "
        "WHERE agent_id = ?",
        (_AGENT,),
    )
    assert str(pointers["active_revision_id"]) == revision.revision_id
    assert str(pointers["last_known_good_revision_id"]) == revision.revision_id
    assert str(pointers["authoritative_instance_id"]) == (
        promoted.fence.runtime_instance_id
    )
    assert pointers["lifecycle_generation"] == promoted.fence.lifecycle_generation
    assert repository.promote_recovered_runtime(ready.fence) == promoted
    assert (
        repository.get_current_online_authority(
            owner_user_id=_OWNER,
            agent_id=_AGENT,
        )
        == promoted
    )
    settled_delivery = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(operation.operation_id),),
    )
    assert settled_delivery["state"] == "completed"
    assert settled_delivery["terminal_code"] is None


def test_inventory_validation_and_stale_operation_leave_no_partial_commit(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    revision = _agent_revision(repository, clean_database)
    host = _host(repository)
    repository.select_host_for_agent(owner_user_id=_OWNER, agent_id=_AGENT)
    clean_database.execute(
        "UPDATE user_agent_revision SET state = 'active', promoted_at = now() "
        "WHERE revision_id = ?",
        (revision.revision_id,),
    )
    clean_database.execute(
        "UPDATE user_agent SET active_revision_id = ? WHERE agent_id = ?",
        (revision.revision_id, _AGENT),
    )
    entry = {
        "agent_id": _AGENT,
        "revision_id": revision.revision_id,
        "bundle_sha256": revision.artifact_digest,
        "runtime_contract_version": _POLICY.runtime_contract_version,
        "required_runtime_lock_sha256": _LOCK_DIGEST,
    }
    with pytest.raises(ValueError, match="unique agent/revision pairs"):
        repository.reconcile_host_inventory(
            host.fence,
            inventory_id=str(uuid.uuid4()),
            entries=(entry, entry),
        )

    stale_operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    clean_database.execute(
        "UPDATE operation_record SET "
        "execution_generation = execution_generation + 1, "
        "execution_lease_token = ? WHERE operation_id = ?",
        (str(uuid.uuid4()), str(stale_operation.operation_id)),
    )
    with pytest.raises(StaleRuntimeGenerationError):
        repository.reconcile_host_inventory(
            host.fence,
            inventory_id=str(uuid.uuid4()),
            entries=(entry,),
            delivery_operation_fences={
                (_AGENT, revision.revision_id): stale_operation,
            },
        )
    persisted = clean_database.fetch_one(
        "SELECT inventory_state FROM agent_host_session WHERE host_session_id = ?",
        (host.host_session_id,),
    )
    assert persisted["inventory_state"] == "pending"
    assert (
        clean_database.fetch_one(
            "SELECT count(*) AS count FROM agent_runtime_instance"
        )["count"]
        == 0
    )


def test_inventory_returns_one_ordered_action_for_every_retained_entry(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    active = _agent_revision(repository, clean_database)
    clean_database.execute(
        "UPDATE user_agent_revision SET state = 'active', promoted_at = now() "
        "WHERE revision_id = ?",
        (active.revision_id,),
    )
    clean_database.execute(
        "UPDATE user_agent SET active_revision_id = ? WHERE agent_id = ?",
        (active.revision_id, _AGENT),
    )
    inactive = repository.create_revision(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
        artifact_digest=hashlib.sha256(b"inactive-retained-bundle").hexdigest(),
        manifest={
            "runtime_contract_version": _POLICY.runtime_contract_version,
            "files": [],
        },
        artifact_relative_path=f"{_AGENT}/revision-2",
        runtime_contract_version=_POLICY.runtime_contract_version,
        release_lock_digest=_LOCK_DIGEST,
        parent_revision_id=active.revision_id,
    )
    host = _host(repository)
    repository.select_host_for_agent(owner_user_id=_OWNER, agent_id=_AGENT)
    unknown_revision = str(uuid.uuid4())

    def entry(revision_id: str, digest: str) -> dict[str, object]:
        return {
            "agent_id": _AGENT,
            "revision_id": revision_id,
            "bundle_sha256": digest,
            "runtime_contract_version": _POLICY.runtime_contract_version,
            "required_runtime_lock_sha256": _LOCK_DIGEST,
        }

    result = repository.reconcile_host_inventory(
        host.fence,
        inventory_id=str(uuid.uuid4()),
        entries=(
            entry(active.revision_id, active.artifact_digest),
            entry(inactive.revision_id, inactive.artifact_digest),
            entry(unknown_revision, hashlib.sha256(b"unknown").hexdigest()),
        ),
        delivery_operation_fences={
            (active.agent_id, active.revision_id): _running_operation(
                clean_database,
                operation_kind="agent_runtime_delivery",
            )
        },
    )
    assert [
        (action.revision_id, action.action, action.reason_code)
        for action in result.actions
    ] == [
        (active.revision_id, "start", None),
        (inactive.revision_id, "keep_stopped", "revision_not_active"),
        (unknown_revision, "delete", "revision_unknown"),
    ]
    assert result.actions[0].selected_delivery is not None
    assert result.actions[1].selected_delivery is None
    assert result.actions[2].selected_delivery is None


def test_current_online_authority_requires_every_durable_pointer_relation(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    revision, host, online = _runtime(repository, clean_database, online=True)
    resolved = repository.get_current_online_authority(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
    )
    assert resolved == online
    selected = repository.get_selected_session_revision(
        host.fence,
        agent_id=_AGENT,
    )
    assert selected.revision.revision_id == revision.revision_id

    clean_database.execute(
        "UPDATE agent_host_session SET inventory_state = 'pending', "
        "inventory_reconciled_at = NULL WHERE host_session_id = ?",
        (host.host_session_id,),
    )
    with pytest.raises(AgentOfflineError):
        repository.get_current_online_authority(
            owner_user_id=_OWNER,
            agent_id=_AGENT,
        )


def test_prelaunch_process_binding_is_nullable_once_only_and_replay_safe(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, _, instance = _runtime(repository, clean_database, online=False)
    assert instance.state == "starting"
    assert instance.fence.process_id is not None

    replay = repository.bind_runtime_process(
        dataclasses.replace(instance.fence, process_id=None),
        process_id=instance.fence.process_id,
        expected_state_revision=0,
    )
    assert replay == instance

    with pytest.raises(StaleRuntimeGenerationError):
        repository.bind_runtime_process(
            dataclasses.replace(instance.fence, process_id=None),
            process_id=str(uuid.uuid4()),
            expected_state_revision=0,
        )
    persisted = repository.get_runtime_instance(instance.fence.runtime_instance_id)
    assert persisted.fence.process_id == instance.fence.process_id


def test_first_starting_frame_reads_revision_metadata_before_the_process_binds(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    """Feature 077 live finding: the host's first ``starting`` frame carries the
    process it spawned while the durable instance is still pre-launch, and the
    server reads revision metadata under that fence BEFORE binding the process
    — an exact-fence read refused every real first start as stale."""
    revision = _agent_revision(repository, clean_database)
    host = _host(repository)
    host = repository.mark_inventory_reconciled(host.fence)
    selection = repository.select_host_for_agent(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
    )
    assert selection.session is not None
    delivery_operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    instance = repository.create_prelaunch_instance(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
        host_session_id=host.host_session_id,
        revision_id=revision.revision_id,
        operation_fence=delivery_operation,
    )
    assert instance.fence.process_id is None
    spawned = dataclasses.replace(instance.fence, process_id=str(uuid.uuid4()))
    # the metadata read admits the spawned process on a pre-launch instance …
    assert repository.get_runtime_revision(spawned).revision_id == revision.revision_id
    # … but any other dimension is still stale
    with pytest.raises(StaleRuntimeGenerationError):
        repository.get_runtime_revision(
            dataclasses.replace(spawned, lifecycle_generation=spawned.lifecycle_generation + 1)
        )
    # and once bound, a different process is stale as before
    bound = repository.bind_runtime_process(
        instance.fence, process_id=spawned.process_id,
        expected_state_revision=instance.state_revision,
    )
    assert bound.fence == spawned
    with pytest.raises(StaleRuntimeGenerationError):
        repository.get_runtime_revision(
            dataclasses.replace(spawned, process_id=str(uuid.uuid4()))
        )


def test_delivering_recovery_timeout_is_db_fenced_and_settles_delivery_operation(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    revision = _agent_revision(repository, clean_database)
    host = repository.mark_inventory_reconciled(_host(repository).fence)
    repository.select_host_for_agent(owner_user_id=_OWNER, agent_id=_AGENT)
    clean_database.execute(
        "UPDATE user_agent_revision SET state = 'active', promoted_at = now() "
        "WHERE revision_id = ?",
        (revision.revision_id,),
    )
    clean_database.execute(
        "UPDATE user_agent SET active_revision_id = ? WHERE agent_id = ?",
        (revision.revision_id, _AGENT),
    )
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    recovery = repository.create_selected_recovery_instance(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
        operation_fence=operation,
    )
    assert recovery.host.host_session_id == host.host_session_id
    with pytest.raises(
        StaleRuntimeGenerationError,
        match="deadline has not elapsed",
    ):
        repository.terminalize_expired_startup(
            recovery.instance.fence,
            timeout_seconds=20,
        )
    clean_database.execute(
        "UPDATE agent_runtime_instance SET created_at = now() - interval '30 seconds' "
        "WHERE runtime_instance_id = ?",
        (recovery.instance.fence.runtime_instance_id,),
    )
    started = time.monotonic()
    settlement = repository.terminalize_expired_startup(
        recovery.instance.fence,
        timeout_seconds=20,
    )
    assert time.monotonic() - started < 2.0
    assert settlement.instance.state == "failed"
    assert settlement.instance.failure_code == "child_registration_timeout"
    delivery_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(operation.operation_id),),
    )
    assert delivery_operation == {
        "state": "retryable",
        "terminal_code": "child_registration_timeout",
    }
    replay = repository.terminalize_expired_startup(
        recovery.instance.fence,
        timeout_seconds=20,
    )
    assert replay.instance == settlement.instance
    assert replay.settled_request_ids == ()


def test_every_runtime_fence_dimension_is_checked_before_state_change(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    revision, _, instance = _runtime(repository, clean_database, online=False)
    instance = repository.accept_runtime_registration(
        instance.fence,
        runtime_contract_version=_POLICY.runtime_contract_version,
        bundle_sha256=revision.artifact_digest,
    )
    instance = repository.record_runtime_heartbeat(
        instance.fence,
        heartbeat_sequence=1,
    )
    replacements = (
        {"agent_id": "other-agent"},
        {"host_id": str(uuid.uuid4())},
        {"host_session_id": str(uuid.uuid4())},
        {"delivery_id": str(uuid.uuid4())},
        {"revision_id": str(uuid.uuid4())},
        {"runtime_instance_id": str(uuid.uuid4())},
        {"process_id": str(uuid.uuid4())},
        {"lifecycle_generation": instance.fence.lifecycle_generation + 1},
    )
    for changes in replacements:
        with pytest.raises(StaleRuntimeGenerationError):
            repository.mark_runtime_ready(
                dataclasses.replace(instance.fence, **changes)
            )
        current = repository.get_runtime_instance(instance.fence.runtime_instance_id)
        assert current.state == "starting"
        assert current.state_revision == instance.state_revision

    ready = repository.mark_runtime_ready(instance.fence)
    assert ready.state == "ready"
    assert ready.state_revision == instance.state_revision + 1


def test_registration_precedes_durable_monotonic_heartbeat_and_ready(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    revision, _, instance = _runtime(repository, clean_database, online=False)
    with pytest.raises(StaleRuntimeGenerationError):
        repository.record_runtime_heartbeat(
            instance.fence,
            heartbeat_sequence=1,
        )
    unchanged = repository.get_runtime_instance(instance.fence.runtime_instance_id)
    assert unchanged.registered_at is None
    assert unchanged.last_heartbeat_sequence is None
    assert unchanged.last_liveness_at is None

    registered = repository.accept_runtime_registration(
        instance.fence,
        runtime_contract_version=_POLICY.runtime_contract_version,
        bundle_sha256=revision.artifact_digest,
    )
    assert registered.registered_at is not None
    assert registered.last_heartbeat_sequence is None
    assert registered.last_liveness_at is None
    assert (
        repository.accept_runtime_registration(
            instance.fence,
            runtime_contract_version=_POLICY.runtime_contract_version,
            bundle_sha256=revision.artifact_digest,
        )
        == registered
    )

    first = repository.record_runtime_heartbeat(
        instance.fence,
        heartbeat_sequence=1,
    )
    assert first.last_heartbeat_sequence == 1
    assert first.last_liveness_at is not None
    reconstructed = _runtime_repository(clean_database)
    assert (
        reconstructed.record_runtime_heartbeat(
            instance.fence,
            heartbeat_sequence=1,
        )
        == first
    )
    second = reconstructed.record_runtime_heartbeat(
        instance.fence,
        heartbeat_sequence=2,
    )
    assert second.last_heartbeat_sequence == 2
    assert second.last_liveness_at >= first.last_liveness_at
    assert reconstructed.mark_runtime_ready(instance.fence).state == "ready"


def test_requests_require_current_online_authority_and_complete_fence(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, _, starting = _runtime(repository, clean_database, online=False)
    request_operation = _running_operation(clean_database)
    with pytest.raises(AgentOfflineError):
        repository.assign_request(starting.fence, operation_fence=request_operation)

    clean_database.execute(
        "DELETE FROM operation_record WHERE operation_id = ?",
        (str(request_operation.operation_id),),
    )
    clean_database.execute(
        "UPDATE user_agent SET active_revision_id = NULL, "
        "last_known_good_revision_id = NULL, selected_host_session_id = NULL, "
        "authoritative_instance_id = NULL WHERE agent_id = ?",
        (_AGENT,),
    )
    clean_database.execute("DELETE FROM agent_runtime_instance")
    clean_database.execute("DELETE FROM user_agent_revision")
    clean_database.execute("DELETE FROM agent_host_session")
    clean_database.execute("DELETE FROM user_agent")

    _, _, online = _runtime(repository, clean_database, online=True)
    request_operation = _running_operation(clean_database)
    request = repository.assign_request(
        online.fence,
        operation_fence=request_operation,
    )
    assert request.state == "assigned"

    stale_fences = (
        dataclasses.replace(request.fence, request_id=str(uuid.uuid4())),
        dataclasses.replace(request.fence, request_generation=str(uuid.uuid4())),
        dataclasses.replace(
            request.fence,
            operation_execution_generation=(
                request.fence.operation_execution_generation + 1
            ),
        ),
        dataclasses.replace(
            request.fence,
            runtime=dataclasses.replace(
                request.fence.runtime,
                process_id=str(uuid.uuid4()),
            ),
        ),
    )
    digest = hashlib.sha256(b"normalized-result").hexdigest()
    for stale in stale_fences:
        with pytest.raises(StaleRuntimeGenerationError):
            repository.settle_request(
                stale,
                state="completed",
                result_digest=digest,
            )
        assert (
            repository.get_runtime_request(request.fence.request_id).state == "assigned"
        )

    completed = repository.settle_request(
        request.fence,
        state="completed",
        result_digest=digest,
    )
    assert completed.state == "completed"
    assert completed.result_digest == digest
    assert (
        repository.settle_request(
            request.fence,
            state="completed",
            result_digest=digest,
        )
        == completed
    )
    operation = clean_database.fetch_one(
        "SELECT state FROM operation_record WHERE operation_id = ?",
        (str(request_operation.operation_id),),
    )
    assert operation["state"] == "completed"


def test_known_runtime_failure_settles_instance_requests_and_operations_immediately(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, _, online = _runtime(repository, clean_database, online=True)
    first = repository.assign_request(
        online.fence,
        operation_fence=_running_operation(clean_database),
    )
    second = repository.assign_request(
        online.fence,
        operation_fence=_running_operation(clean_database),
    )

    started = time.monotonic()
    settlement = repository.terminalize_runtime(
        online.fence,
        failure_code="child_exited",
    )
    assert time.monotonic() - started < 2.0
    assert settlement.instance.state == "offline"
    assert settlement.settled_request_ids == (
        first.fence.request_id,
        second.fence.request_id,
    )
    for request_id in settlement.settled_request_ids:
        request = repository.get_runtime_request(request_id)
        assert request.state == "retryable"
        assert request.terminal_code == "child_exited"
        operation = clean_database.fetch_one(
            "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
            (request.fence.operation_id,),
        )
        assert operation["state"] == "retryable"
        assert operation["terminal_code"] == "child_exited"

    pointer = clean_database.fetch_one(
        "SELECT authoritative_instance_id FROM user_agent WHERE agent_id = ?",
        (_AGENT,),
    )
    assert pointer["authoritative_instance_id"] is None
    replay = repository.terminalize_runtime(
        online.fence,
        failure_code="child_exited",
    )
    assert replay.settled_request_ids == ()


def test_runtime_failure_staging_preserves_operations_until_exact_exit(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, _, online = _runtime(repository, clean_database, online=True)
    request_operation = _running_operation(clean_database)
    request = repository.assign_request(
        online.fence,
        operation_fence=request_operation,
    )
    before_delivery_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (online.operation_id,),
    )
    before_request_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(request_operation.operation_id),),
    )
    assert before_delivery_operation == {"state": "running", "terminal_code": None}
    assert before_request_operation == {"state": "running", "terminal_code": None}

    staged = repository.stage_runtime_failure(
        online.fence,
        failure_code="child_exited",
    )
    replay = repository.stage_runtime_failure(
        online.fence,
        failure_code="child_exited",
    )

    assert replay == staged
    assert staged.state == "stopping"
    assert staged.failure_code == "child_exited"
    assert not staged.is_authoritative
    pointer = clean_database.fetch_one(
        "SELECT authoritative_instance_id FROM user_agent WHERE agent_id = ?",
        (_AGENT,),
    )
    assert pointer["authoritative_instance_id"] is None
    assert repository.get_runtime_request(request.fence.request_id).state == "assigned"
    assert clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (online.operation_id,),
    ) == before_delivery_operation
    assert clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(request_operation.operation_id),),
    ) == before_request_operation

    settlement = repository.terminalize_runtime(
        online.fence,
        failure_code="child_exited",
    )

    assert settlement.instance.state == "offline"
    assert settlement.settled_request_ids == (request.fence.request_id,)
    settled_request = repository.get_runtime_request(request.fence.request_id)
    assert settled_request.state == "retryable"
    assert settled_request.terminal_code == "child_exited"
    for operation_id in (online.operation_id, str(request_operation.operation_id)):
        assert clean_database.fetch_one(
            "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
            (operation_id,),
        ) == {"state": "retryable", "terminal_code": "child_exited"}


def test_db_receipt_liveness_timeout_settles_hung_runtime_within_seven_seconds(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, _, online = _runtime(repository, clean_database, online=True)
    operation = _running_operation(clean_database)
    request = repository.assign_request(
        online.fence,
        operation_fence=operation,
    )
    with pytest.raises(
        StaleRuntimeGenerationError,
        match="deadline has not elapsed",
    ):
        repository.terminalize_expired_liveness(
            online.fence,
            timeout_seconds=5,
        )
    clean_database.execute(
        "UPDATE agent_runtime_instance "
        "SET last_liveness_at = now() - interval '5 seconds' "
        "WHERE runtime_instance_id = ?",
        (online.fence.runtime_instance_id,),
    )
    started = time.monotonic()
    settlement = repository.terminalize_expired_liveness(
        online.fence,
        timeout_seconds=5,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert 5.0 + elapsed < 7.0
    assert settlement.instance.state == "offline"
    assert settlement.instance.failure_code == "child_hung"
    assert settlement.settled_request_ids == (request.fence.request_id,)
    assert repository.get_runtime_request(request.fence.request_id).terminal_code == (
        "child_hung"
    )
    persisted_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(operation.operation_id),),
    )
    assert persisted_operation == {
        "state": "retryable",
        "terminal_code": "child_hung",
    }


def test_host_loss_terminalizes_exact_session_and_moves_selection_to_standby(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    revision, selected_host, online = _runtime(repository, clean_database, online=True)
    standby = repository.mark_inventory_reconciled(_host(repository).fence)
    request = repository.assign_request(
        online.fence,
        operation_fence=_running_operation(clean_database),
    )

    started = time.monotonic()
    result = repository.disconnect_host_session(
        selected_host.fence,
        failure_code="host_lost",
    )
    assert time.monotonic() - started < 2.0
    assert result.settled_request_ids == (request.fence.request_id,)
    assert result.selected_sessions[_AGENT] == standby.host_session_id
    assert (
        repository.get_runtime_request(request.fence.request_id).terminal_code
        == "host_lost"
    )
    assert (
        repository.get_runtime_instance(online.fence.runtime_instance_id).state
        == "offline"
    )

    delivery_operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    recovery = repository.create_selected_recovery_instance(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
        operation_fence=delivery_operation,
    )
    assert recovery.host.host_session_id == standby.host_session_id
    assert recovery.revision.revision_id == revision.revision_id
    assert recovery.instance.state == "delivering"
    assert recovery.instance.fence.process_id is None
    assert recovery.instance.is_authoritative is False
    with pytest.raises(StaleRuntimeGenerationError, match="already pending"):
        repository.create_selected_recovery_instance(
            owner_user_id=_OWNER,
            agent_id=_AGENT,
            operation_fence=delivery_operation,
        )


def test_same_host_session_rollover_fences_old_runtime_before_rebinding(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, old_host, online = _runtime(repository, clean_database, online=True)
    request = repository.assign_request(
        online.fence,
        operation_fence=_running_operation(clean_database),
    )

    replacement = _host(repository, host_id=old_host.host_id)
    assert replacement.supersedes_session_id == old_host.host_session_id
    assert replacement.inventory_state == "pending"
    old_row = clean_database.fetch_one(
        "SELECT state FROM agent_host_session WHERE host_session_id = ?",
        (old_host.host_session_id,),
    )
    assert old_row["state"] == "disconnected"
    assert (
        repository.get_runtime_instance(online.fence.runtime_instance_id).state
        == "offline"
    )
    assert (
        repository.get_runtime_request(request.fence.request_id).terminal_code
        == "host_lost"
    )
    pointer = clean_database.fetch_one(
        "SELECT selected_host_session_id FROM user_agent WHERE agent_id = ?",
        (_AGENT,),
    )
    assert str(pointer["selected_host_session_id"]) == replacement.host_session_id


def _candidate_preparation(
    *,
    revision_id: str,
    host_session_id: str,
    operation_fence: ExecutionFence,
    database: PlaneTestRuntime | None = None,
) -> CandidatePreparation:
    finalized = AgentCodeGenerator(
        llm_client=object(), llm_model="unused"
    ).finalize_byo_bundle(
        files={
            "agent_main.py": "from astralprims_ui import normalize_tool_result\n",
            "astralprims_ui.py": (
                "def normalize_tool_result(value):\n    return value\n"
            ),
            "protected_executor.py": "# public LETS executor adapter\n",
            "mcp_tools.py": "TOOL_REGISTRY = {}\n",
        },
        agent_id=_AGENT,
        revision_id=revision_id,
        agent_name=_CANDIDATE_DISPLAY_NAME,
        description="candidate promotion repository test",
        constitution_version=AGENT_CONSTITUTION_VERSION,
        required_runtime_lock_sha256=_LOCK_DIGEST,
    )
    metadata = CandidateAgentMetadata(
        draft_id=str(uuid.uuid4()),
        draft_state_revision=1,
        display_name=_CANDIDATE_DISPLAY_NAME,
        constitution_version=AGENT_CONSTITUTION_VERSION,
        validated_policy_revision=USER_AGENT_POLICY_REVISION,
        declared_tools=_CANDIDATE_TOOLS,
        declared_scopes=_CANDIDATE_SCOPES,
        declared_egress=_CANDIDATE_EGRESS,
    )
    request = CandidatePreparation(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
        revision_id=revision_id,
        bundle_sha256=finalized.bundle_sha256,
        runtime_manifest=finalized.manifest,
        artifact_relative_path=f"{_AGENT}/{revision_id}",
        runtime_contract_version=_POLICY.runtime_contract_version,
        required_runtime_lock_sha256=_LOCK_DIGEST,
        host_session_id=host_session_id,
        operation_fence=operation_fence,
        agent_metadata=metadata,
    )
    if database is not None:
        _seed_candidate_provenance(database, request)
    return request


def _seed_candidate_provenance(
    database: PlaneTestRuntime,
    request: CandidatePreparation,
) -> None:
    """Mirror Plane publication before lifecycle delivery consumes the revision."""

    metadata = request.agent_metadata
    observed_at = int(time.time() * 1000)
    plan = {
        "tools_used": list(metadata.declared_tools),
        "tool_scopes": {"lookup": "tools:read"},
        "declared_scopes": list(metadata.declared_scopes),
        "declared_egress": list(metadata.declared_egress),
    }
    tools_spec = [{"name": "lookup", "scope": "tools:read"}]
    analyze_result = {
        "passed": True,
        "constitution_version": metadata.constitution_version,
        "policy_revision": metadata.validated_policy_revision,
    }
    with database.transaction() as transaction:
        agents = database.repositories.agents
        drafts = database.repositories.draft_agents
        agents.lock_owner(transaction, owner_id=request.owner_user_id)
        agent = agents.get_agent(
            transaction,
            owner_id=request.owner_user_id,
            agent_id=request.agent_id,
            for_update=True,
        )
        assert agent is not None
        latest = agents.list_revisions(
            transaction,
            owner_id=request.owner_user_id,
            agent_id=request.agent_id,
            limit=1,
        )
        agents.create_revision(
            transaction,
            revision_id=request.revision_id,
            agent_id=request.agent_id,
            owner_id=request.owner_user_id,
            revision_number=0 if not latest else latest[0].revision_number + 1,
            parent_revision_id=agent.active_revision_id,
            previous_good_revision_id=agent.active_revision_id,
            artifact_digest=request.bundle_sha256,
            manifest=request.runtime_manifest,
            artifact_relative_path=request.artifact_relative_path,
            runtime_contract_version=request.runtime_contract_version,
            release_lock_digest=request.required_runtime_lock_sha256,
            compatibility_state="compatible",
            state="prepared",
            promotion_token=str(uuid.uuid4()),
        )
        draft = drafts.create_draft(
            transaction,
            draft_id=metadata.draft_id,
            owner_id=request.owner_user_id,
            agent_name=metadata.display_name,
            agent_slug=f"candidate-{metadata.draft_id}",
            description="published candidate provenance fixture",
            observed_at=observed_at,
            tools_spec=json.dumps(tools_spec, sort_keys=True),
            plan_json=json.dumps(plan, sort_keys=True),
            constitution_version=metadata.constitution_version,
            draft_uuid=metadata.draft_id,
            target_agent_id=request.agent_id,
            revises_agent_id=request.agent_id,
        )
        published = drafts.compare_and_set_draft(
            transaction,
            owner_id=request.owner_user_id,
            draft_id=metadata.draft_id,
            expected_revision=draft.state_revision,
            updates={
                "analyze_result": json.dumps(analyze_result, sort_keys=True),
                "published_revision_id": request.revision_id,
                "status": "generated",
            },
            updated_at=observed_at + 1,
        )
    assert published.state_revision == metadata.draft_state_revision
    assert published.published_revision_id == request.revision_id


def _promoted_active_replay(
    repository: PersonalAgentRuntimeRepository,
    database: PlaneTestRuntime,
) -> tuple[PostgresPersonalAgentRevisionStore, ActiveRevisionReplay, str, str]:
    """Promote one exact candidate and return its immutable replay identity."""

    previous_revision, host, previous_runtime = _runtime(
        repository,
        database,
        online=True,
    )
    store = PostgresPersonalAgentRevisionStore(repository)
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=host.host_session_id,
        operation_fence=_running_operation(
            database,
            operation_kind="agent_runtime_delivery",
        ),
        database=database,
    )
    candidate = store.prepare_candidate(request)
    prelaunch = repository.get_runtime_instance(candidate.runtime_instance_id)
    store.mark_candidate_starting(candidate)
    started = repository.bind_runtime_process(
        prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=prelaunch.state_revision,
    )
    registered = repository.accept_runtime_registration(
        started.fence,
        runtime_contract_version=_POLICY.runtime_contract_version,
        bundle_sha256=request.bundle_sha256,
    )
    live = repository.record_runtime_heartbeat(
        registered.fence,
        heartbeat_sequence=1,
    )
    ready = repository.mark_runtime_ready(live.fence)
    store.confirm_candidate_ready(candidate, ready.fence.runtime_instance_id)
    commit = store.promote_candidate(candidate)
    return (
        store,
        ActiveRevisionReplay(
            owner_user_id=request.owner_user_id,
            agent_id=request.agent_id,
            revision_id=request.revision_id,
            bundle_sha256=request.bundle_sha256,
            runtime_manifest=request.runtime_manifest,
            artifact_relative_path=request.artifact_relative_path,
            runtime_contract_version=request.runtime_contract_version,
            required_runtime_lock_sha256=(
                request.required_runtime_lock_sha256
            ),
            runtime_instance_id=commit.runtime_instance_id,
            agent_metadata=request.agent_metadata,
        ),
        previous_revision.revision_id,
        previous_runtime.fence.runtime_instance_id,
    )


def _replace_replay_draft_evidence(
    database: PlaneTestRuntime,
    replay: ActiveRevisionReplay,
    *,
    updates: dict[str, object],
) -> ActiveRevisionReplay:
    """CAS draft evidence and bind the forged replay to its new revision."""

    with database.transaction() as transaction:
        drafts = database.repositories.draft_agents
        draft = drafts.get_draft(
            transaction,
            owner_id=replay.owner_user_id,
            draft_id=replay.agent_metadata.draft_id,
            for_update=True,
        )
        assert draft is not None
        updated = drafts.compare_and_set_draft(
            transaction,
            owner_id=replay.owner_user_id,
            draft_id=draft.draft_id,
            expected_revision=draft.state_revision,
            updates=updates,
            updated_at=max(
                int(time.time() * 1000),
                (draft.updated_at or 0) + 1,
            ),
        )
    return dataclasses.replace(
        replay,
        agent_metadata=dataclasses.replace(
            replay.agent_metadata,
            draft_state_revision=updated.state_revision,
        ),
    )


def _mutate_active_replay_identity(
    database: PlaneTestRuntime,
    replay: ActiveRevisionReplay,
    *,
    mismatch: str,
    previous_revision_id: str,
    previous_runtime_instance_id: str,
) -> ActiveRevisionReplay:
    """Perturb exactly one persisted or presented replay-authority seam."""

    if mismatch == "draft_state_revision":
        return dataclasses.replace(
            replay,
            agent_metadata=dataclasses.replace(
                replay.agent_metadata,
                draft_state_revision=replay.agent_metadata.draft_state_revision + 1,
            ),
        )
    if mismatch == "target_agent_id":
        database.execute(
            "UPDATE draft_agents SET target_agent_id = NULL WHERE id = ?",
            (replay.agent_metadata.draft_id,),
        )
        return replay
    if mismatch == "published_revision_id":
        return _replace_replay_draft_evidence(
            database,
            replay,
            updates={"published_revision_id": None},
        )
    if mismatch in {
        "plan_tools",
        "plan_scopes",
        "plan_egress",
        "tools_spec",
        "analyze_policy",
    }:
        with database.transaction() as transaction:
            draft = database.repositories.draft_agents.get_draft(
                transaction,
                owner_id=replay.owner_user_id,
                draft_id=replay.agent_metadata.draft_id,
            )
        assert draft is not None
        if mismatch == "tools_spec":
            updates = {"tools_spec": "[]"}
        elif mismatch == "analyze_policy":
            analyze_result = json.loads(str(draft.analyze_result))
            analyze_result["policy_revision"] = "candidate-policy-stale"
            updates = {
                "analyze_result": json.dumps(analyze_result, sort_keys=True)
            }
        else:
            plan = json.loads(str(draft.plan_json))
            field = {
                "plan_tools": "tools_used",
                "plan_scopes": "declared_scopes",
                "plan_egress": "declared_egress",
            }[mismatch]
            plan[field] = []
            updates = {"plan_json": json.dumps(plan, sort_keys=True)}
        return _replace_replay_draft_evidence(
            database,
            replay,
            updates=updates,
        )
    if mismatch in {"shared_display_name", "shared_policy_revision"}:
        with database.transaction() as transaction:
            agents = database.repositories.agents
            agent = agents.get_agent(
                transaction,
                owner_id=replay.owner_user_id,
                agent_id=replay.agent_id,
                for_update=True,
            )
            assert agent is not None
            updates = (
                {"display_name": "Stale shared-row display name"}
                if mismatch == "shared_display_name"
                else {"validated_policy_revision": "candidate-policy-stale"}
            )
            agents.compare_and_set_agent(
                transaction,
                owner_id=replay.owner_user_id,
                agent_id=replay.agent_id,
                expected_revision=agent.state_revision,
                updates=updates,
            )
        return replay
    if mismatch in {
        "revision_manifest",
        "revision_artifact_path",
        "revision_digest",
        "revision_contract",
        "revision_lock",
    }:
        column, value = {
            "revision_manifest": ("manifest_json", "{}"),
            "revision_artifact_path": (
                "artifact_relative_path",
                f"{replay.agent_id}/stale-revision",
            ),
            "revision_digest": ("artifact_digest", "0" * 64),
            "revision_contract": (
                "runtime_contract_version",
                replay.runtime_contract_version + 1,
            ),
            "revision_lock": ("release_lock_digest", "0" * 64),
        }[mismatch]
        if mismatch == "revision_manifest":
            database.execute(
                "UPDATE user_agent_revision SET manifest_json = ?::jsonb "
                "WHERE revision_id = ?",
                (value, replay.revision_id),
            )
        else:
            database.execute(
                f"UPDATE user_agent_revision SET {column} = ? "
                "WHERE revision_id = ?",
                (value, replay.revision_id),
            )
        return replay
    if mismatch == "revision_state":
        with database.transaction() as transaction:
            agents = database.repositories.agents
            revision = agents.get_revision(
                transaction,
                owner_id=replay.owner_user_id,
                agent_id=replay.agent_id,
                revision_id=replay.revision_id,
                for_update=True,
            )
            assert revision is not None
            agents.transition_revision(
                transaction,
                owner_id=replay.owner_user_id,
                agent_id=replay.agent_id,
                revision_id=replay.revision_id,
                expected_revision=revision.state_revision,
                expected_state=revision.state,
                updates={"state": "retired"},
            )
        return replay
    if mismatch == "authoritative_pointer":
        with database.transaction() as transaction:
            agents = database.repositories.agents
            agent = agents.get_agent(
                transaction,
                owner_id=replay.owner_user_id,
                agent_id=replay.agent_id,
                for_update=True,
            )
            assert agent is not None
            agents.compare_and_set_agent(
                transaction,
                owner_id=replay.owner_user_id,
                agent_id=replay.agent_id,
                expected_revision=agent.state_revision,
                updates={"authoritative_instance_id": None},
            )
        return replay
    if mismatch in {
        "runtime_instance_id",
        "runtime_revision",
        "runtime_not_online",
        "runtime_not_authoritative",
    }:
        if mismatch == "runtime_instance_id":
            return dataclasses.replace(
                replay,
                runtime_instance_id=previous_runtime_instance_id,
            )
        with database.transaction() as transaction:
            agents = database.repositories.agents
            runtime = agents.get_runtime_instance(
                transaction,
                owner_id=replay.owner_user_id,
                runtime_instance_id=replay.runtime_instance_id,
                for_update=True,
            )
            assert runtime is not None
            if mismatch == "runtime_revision":
                # Revision identity is immutable and has no typed mutation API.
                transaction.execute(
                    "UPDATE agent_runtime_instance SET revision_id = %s "
                    "WHERE runtime_instance_id = %s",
                    (previous_revision_id, replay.runtime_instance_id),
                )
            else:
                agents.transition_runtime_instance(
                    transaction,
                    owner_id=replay.owner_user_id,
                    runtime_instance_id=replay.runtime_instance_id,
                    expected_revision=runtime.state_revision,
                    expected_states=(runtime.state,),
                    updates=(
                        {"state": "stopping"}
                        if mismatch == "runtime_not_online"
                        else {"is_authoritative": False}
                    ),
                )
        return replay
    raise AssertionError(f"unhandled replay mismatch: {mismatch}")


def test_revision_store_rejects_malformed_manifest_and_candidate_metadata(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    store = PostgresPersonalAgentRevisionStore(repository)
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=str(uuid.uuid4()),
        operation_fence=_running_operation(
            clean_database, operation_kind="agent_runtime_delivery"
        ),
    )
    wrong_revision = json.loads(json.dumps(request.runtime_manifest, default=dict))
    wrong_revision["revision_id"] = str(uuid.uuid4())
    missing_file = json.loads(json.dumps(request.runtime_manifest, default=dict))
    missing_file["files"].pop()
    bad_file_hash = json.loads(json.dumps(request.runtime_manifest, default=dict))
    bad_file_hash["files"][0]["sha256"] = "not-a-digest"
    bad_file_size = json.loads(json.dumps(request.runtime_manifest, default=dict))
    bad_file_size["files"][0]["size_bytes"] = True
    invalid = (
        dataclasses.replace(request, bundle_sha256="not-a-digest"),
        dataclasses.replace(request, runtime_contract_version=1),
        dataclasses.replace(request, required_runtime_lock_sha256="0" * 64),
        dataclasses.replace(request, artifact_relative_path="../escape"),
        dataclasses.replace(request, operation_fence=None),
        dataclasses.replace(request, runtime_manifest=wrong_revision),
        dataclasses.replace(request, runtime_manifest=missing_file),
        dataclasses.replace(request, runtime_manifest=bad_file_hash),
        dataclasses.replace(request, runtime_manifest=bad_file_size),
    )
    for malformed in invalid:
        with pytest.raises((TypeError, ValueError)):
            store.prepare_candidate(malformed)


def test_postgres_revision_store_accepts_exact_committed_active_replay(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    store, replay, _previous_revision_id, _previous_runtime_id = (
        _promoted_active_replay(repository, clean_database)
    )

    assert store.assert_active_replay(replay) is None


@pytest.mark.parametrize(
    "mismatch",
    (
        "draft_state_revision",
        "target_agent_id",
        "published_revision_id",
        "plan_tools",
        "tools_spec",
        "plan_scopes",
        "plan_egress",
        "analyze_policy",
        "shared_display_name",
        "shared_policy_revision",
        "revision_manifest",
        "revision_artifact_path",
        "revision_digest",
        "revision_contract",
        "revision_lock",
        "revision_state",
        "authoritative_pointer",
        "runtime_instance_id",
        "runtime_revision",
        "runtime_not_online",
        "runtime_not_authoritative",
    ),
    ids=lambda mismatch: f"stale-{mismatch.replace('_', '-')}",
)
def test_postgres_revision_store_rejects_stale_active_replay_identity(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
    mismatch: str,
) -> None:
    store, replay, previous_revision_id, previous_runtime_id = (
        _promoted_active_replay(repository, clean_database)
    )
    store.assert_active_replay(replay)
    stale_replay = _mutate_active_replay_identity(
        clean_database,
        replay,
        mismatch=mismatch,
        previous_revision_id=previous_revision_id,
        previous_runtime_instance_id=previous_runtime_id,
    )

    with pytest.raises(StaleRuntimeGenerationError):
        store.assert_active_replay(stale_replay)


def test_postgres_revision_store_promotes_ready_candidate_atomically(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    old_revision, host, old_runtime = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=host.host_session_id,
        operation_fence=_running_operation(
            clean_database, operation_kind="agent_runtime_delivery"
        ),
        database=clean_database,
    )

    candidate = store.prepare_candidate(request)
    assert store.prepare_candidate(request) == candidate
    prelaunch = repository.get_runtime_instance(candidate.runtime_instance_id)
    store.mark_candidate_starting(candidate)
    store.mark_candidate_starting(candidate)
    started = repository.bind_runtime_process(
        prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=prelaunch.state_revision,
    )
    registered = repository.accept_runtime_registration(
        started.fence,
        runtime_contract_version=_POLICY.runtime_contract_version,
        bundle_sha256=request.bundle_sha256,
    )
    live = repository.record_runtime_heartbeat(registered.fence, heartbeat_sequence=1)
    ready = repository.mark_runtime_ready(live.fence)
    store.confirm_candidate_ready(candidate, ready.fence.runtime_instance_id)
    store.confirm_candidate_ready(candidate, ready.fence.runtime_instance_id)

    commit = store.promote_candidate(candidate)
    replay = store.promote_candidate(candidate)

    assert commit.previous_revision_id == old_revision.revision_id
    assert commit.previous_runtime_instance_id == old_runtime.fence.runtime_instance_id
    assert replay.revision_id == commit.revision_id
    assert replay.runtime_instance_id == commit.runtime_instance_id
    assert replay.previous_revision_id == commit.previous_revision_id
    assert replay.previous_runtime_instance_id == commit.previous_runtime_instance_id
    pointers = clean_database.fetch_one(
        "SELECT active_revision_id, last_known_good_revision_id, "
        "authoritative_instance_id, lifecycle_generation FROM user_agent "
        "WHERE agent_id = ? AND owner_user_id = ?",
        (_AGENT, _OWNER),
    )
    assert str(pointers["active_revision_id"]) == candidate.revision_id
    assert str(pointers["last_known_good_revision_id"]) == old_revision.revision_id
    assert str(pointers["authoritative_instance_id"]) == candidate.runtime_instance_id
    assert int(pointers["lifecycle_generation"]) == ready.fence.lifecycle_generation
    with clean_database.transaction() as transaction:
        promoted_agent = clean_database.repositories.agents.get_agent(
            transaction,
            owner_id=_OWNER,
            agent_id=_AGENT,
        )
    assert promoted_agent is not None
    assert promoted_agent.display_name == request.agent_metadata.display_name
    assert promoted_agent.draft_id == request.agent_metadata.draft_id
    assert tuple(promoted_agent.declared_tools) == request.agent_metadata.declared_tools
    assert tuple(promoted_agent.declared_scopes) == request.agent_metadata.declared_scopes
    assert tuple(promoted_agent.declared_egress or ()) == (
        request.agent_metadata.declared_egress
    )
    assert (
        promoted_agent.constitution_version
        == request.agent_metadata.constitution_version
    )
    assert promoted_agent.validated_policy_revision == USER_AGENT_POLICY_REVISION
    assert promoted_agent.validated_at is not None
    assert promoted_agent.revalidation_required is False
    assert promoted_agent.status == "live"
    old = repository.get_runtime_instance(old_runtime.fence.runtime_instance_id)
    promoted = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert old.state == "stopping" and not old.is_authoritative
    assert promoted.state == "online" and promoted.is_authoritative
    states = {
        str(row["revision_id"]): row["state"]
        for row in clean_database.fetch_all(
            "SELECT revision_id, state FROM user_agent_revision WHERE agent_id = ?",
            (_AGENT,),
        )
    }
    assert states == {
        old_revision.revision_id: "retired",
        candidate.revision_id: "active",
    }


def test_postgres_prepare_recovers_committed_runtime_after_lost_ack(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=host.host_session_id,
        operation_fence=operation,
        database=clean_database,
    )
    create_prelaunch = repository.create_prelaunch_instance

    def commit_then_raise(**kwargs):
        create_prelaunch(**kwargs)
        raise OSError("runtime create commit acknowledgement was lost")

    monkeypatch.setattr(repository, "create_prelaunch_instance", commit_then_raise)

    candidate = store.prepare_candidate(request)
    replay = store.prepare_candidate(request)

    assert replay == candidate
    rows = clean_database.fetch_all(
        "SELECT runtime_instance_id, state, operation_id, "
        "operation_execution_generation FROM agent_runtime_instance "
        "WHERE owner_user_id = ? AND agent_id = ? AND revision_id = ?",
        (_OWNER, _AGENT, request.revision_id),
    )
    assert len(rows) == 1
    assert dict(rows[0]) == {
        "runtime_instance_id": candidate.runtime_instance_id,
        "state": "delivering",
        "operation_id": str(operation.operation_id),
        "operation_execution_generation": operation.execution_generation,
    }
    revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (request.revision_id,),
    )
    assert revision == {"state": "prepared", "failure_code": None}


def test_postgres_prepare_rejects_lost_ack_after_operation_authority_ended(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=host.host_session_id,
        operation_fence=operation,
        database=clean_database,
    )
    create_prelaunch = repository.create_prelaunch_instance

    def commit_settle_then_raise(**kwargs):
        create_prelaunch(**kwargs)
        repository._operations.terminalize(
            operation,
            state=OperationState.RETRYABLE,
            terminal_code="revision_delivery_retryable",
            safe_summary=None,
            retry_after_ms=0,
            now=None,
            retention=repository._operation_retention,
        )
        raise OSError("runtime create response arrived after lease settlement")

    monkeypatch.setattr(
        repository,
        "create_prelaunch_instance",
        commit_settle_then_raise,
    )

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_runtime_cleanup_pending",
    ):
        store.prepare_candidate(request)

    revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (request.revision_id,),
    )
    assert revision == {"state": "prepared", "failure_code": None}
    runtime = clean_database.fetch_one(
        "SELECT state FROM agent_runtime_instance WHERE revision_id = ?",
        (request.revision_id,),
    )
    assert runtime == {"state": "delivering"}


def test_postgres_revision_store_activates_first_revision_without_fake_lkg(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    create_user_agent(
        clean_database,
        agent_id=_AGENT,
        owner_user_id=_OWNER,
        display_name="First revision",
    )
    clean_database.execute(
        "UPDATE user_agent SET status = 'validated' WHERE agent_id = ?",
        (_AGENT,),
    )
    host = repository.mark_inventory_reconciled(_host(repository).fence)
    repository.select_host_for_agent(owner_user_id=_OWNER, agent_id=_AGENT)
    store = PostgresPersonalAgentRevisionStore(repository)
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=host.host_session_id,
        operation_fence=_running_operation(
            clean_database, operation_kind="agent_runtime_delivery"
        ),
        database=clean_database,
    )
    candidate = store.prepare_candidate(request)
    prelaunch = repository.get_runtime_instance(candidate.runtime_instance_id)
    store.mark_candidate_starting(candidate)
    started = repository.bind_runtime_process(
        prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=prelaunch.state_revision,
    )
    registered = repository.accept_runtime_registration(
        started.fence,
        runtime_contract_version=_POLICY.runtime_contract_version,
        bundle_sha256=request.bundle_sha256,
    )
    live = repository.record_runtime_heartbeat(registered.fence, heartbeat_sequence=1)
    ready = repository.mark_runtime_ready(live.fence)
    store.confirm_candidate_ready(candidate, ready.fence.runtime_instance_id)

    commit = store.promote_candidate(candidate)

    assert commit.previous_revision_id is None
    assert commit.previous_runtime_instance_id is None
    pointers = clean_database.fetch_one(
        "SELECT active_revision_id, last_known_good_revision_id, "
        "authoritative_instance_id FROM user_agent WHERE agent_id = ?",
        (_AGENT,),
    )
    assert str(pointers["active_revision_id"]) == candidate.revision_id
    assert pointers["last_known_good_revision_id"] is None
    assert str(pointers["authoritative_instance_id"]) == candidate.runtime_instance_id


def test_postgres_promotion_failure_preserves_old_and_terminalizes_candidate(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    old_revision, host, old_runtime = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database, operation_kind="agent_runtime_delivery"
    )
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=host.host_session_id,
        operation_fence=operation,
        database=clean_database,
    )
    candidate = store.prepare_candidate(request)
    prelaunch = repository.get_runtime_instance(candidate.runtime_instance_id)
    store.mark_candidate_starting(candidate)
    started = repository.bind_runtime_process(
        prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=prelaunch.state_revision,
    )
    registered = repository.accept_runtime_registration(
        started.fence,
        runtime_contract_version=_POLICY.runtime_contract_version,
        bundle_sha256=request.bundle_sha256,
    )
    live = repository.record_runtime_heartbeat(registered.fence, heartbeat_sequence=1)
    ready = repository.mark_runtime_ready(live.fence)
    store.confirm_candidate_ready(candidate, ready.fence.runtime_instance_id)
    before = clean_database.fetch_one(
        "SELECT active_revision_id, last_known_good_revision_id, "
        "authoritative_instance_id FROM user_agent WHERE agent_id = ?",
        (_AGENT,),
    )

    stale_candidate = dataclasses.replace(
        candidate,
        agent_metadata=dataclasses.replace(
            candidate.agent_metadata,
            display_name="Stale promoted display name",
        ),
    )
    with pytest.raises(StaleRuntimeGenerationError):
        store.promote_candidate(stale_candidate)

    assert (
        clean_database.fetch_one(
            "SELECT active_revision_id, last_known_good_revision_id, "
            "authoritative_instance_id FROM user_agent WHERE agent_id = ?",
            (_AGENT,),
        )
        == before
    )
    still_old = repository.get_runtime_instance(old_runtime.fence.runtime_instance_id)
    assert still_old.state == "online" and still_old.is_authoritative
    assert store.stage_candidate_failure(candidate, "revision_promotion_failed")
    staged = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert staged.state == "stopping"
    assert staged.failure_code == "revision_promotion_failed"
    staged_revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (candidate.revision_id,),
    )
    assert staged_revision == {
        "state": "ready",
        "failure_code": "revision_promotion_failed",
    }
    staged_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(operation.operation_id),),
    )
    assert staged_operation == {
        "state": "failed",
        "terminal_code": "revision_promotion_failed",
    }
    store.fail_candidate(candidate, "revision_promotion_failed")
    failed = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert failed.state == "offline" and not failed.is_authoritative
    candidate_state = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (candidate.revision_id,),
    )
    assert candidate_state == {
        "state": "failed",
        "failure_code": "revision_promotion_failed",
    }
    delivery_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(operation.operation_id),),
    )
    assert delivery_operation == {
        "state": "failed",
        "terminal_code": "revision_promotion_failed",
    }
    active_state = clean_database.fetch_one(
        "SELECT state FROM user_agent_revision WHERE revision_id = ?",
        (old_revision.revision_id,),
    )
    assert active_state["state"] == "active"


@pytest.mark.parametrize(
    "failure_code",
    (
        "bundle_install_failed",
        "child_start_failed",
        "child_registration_timeout",
        "revision_promotion_failed",
    ),
)
def test_postgres_terminal_replay_retains_exact_activation_failure_after_purge(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
    failure_code: str,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=host.host_session_id,
        operation_fence=operation,
        database=clean_database,
    )
    candidate = store.prepare_candidate(request)
    assert not store.stage_candidate_failure(candidate, failure_code)
    store.fail_candidate(candidate, failure_code)
    _purge_terminal_operations(repository)

    status = store.inspect_recovery_status(
        _OWNER,
        _AGENT,
        candidate.revision_id,
    )
    assert status is not None
    assert status.revision_state == "failed"
    assert status.runtime_instance_id == candidate.runtime_instance_id
    assert status.runtime_failure_code == failure_code
    assert status.operation_state is None
    persisted_revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (candidate.revision_id,),
    )
    assert persisted_revision == {
        "state": "failed",
        "failure_code": failure_code,
    }


def test_postgres_retryable_attempt_reset_fences_runtime_and_preserves_revision(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, host, _authoritative = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    first_operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    revision_id = str(uuid.uuid4())
    request = _candidate_preparation(
        revision_id=revision_id,
        host_session_id=host.host_session_id,
        operation_fence=first_operation,
        database=clean_database,
    )
    candidate = store.prepare_candidate(request)
    store.mark_candidate_starting(candidate)
    prelaunch = repository.get_runtime_instance(candidate.runtime_instance_id)
    repository.bind_runtime_process(
        prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=prelaunch.state_revision,
    )
    repository._operations.terminalize(
        first_operation,
        state=OperationState.RETRYABLE,
        terminal_code="revision_delivery_retryable",
        safe_summary=None,
        retry_after_ms=0,
        now=None,
        retention=repository._operation_retention,
    )

    old_runtime_id = store.stage_retryable_candidate_reset(
        _OWNER,
        _AGENT,
        revision_id,
    )

    assert old_runtime_id == candidate.runtime_instance_id
    assert (
        store.stage_retryable_candidate_reset(_OWNER, _AGENT, revision_id)
        == old_runtime_id
    )
    staged_runtime = repository.get_runtime_instance(old_runtime_id)
    assert staged_runtime.state == "stopping"
    assert staged_runtime.failure_code == "revision_delivery_retry_reset_pending"
    staged_revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (revision_id,),
    )
    assert staged_revision == {
        "state": "starting",
        "failure_code": "revision_delivery_retry_reset_pending",
    }
    staged_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(first_operation.operation_id),),
    )
    assert staged_operation == {
        "state": "retryable",
        "terminal_code": "revision_delivery_retryable",
    }

    store.finalize_retryable_candidate_reset(
        _OWNER,
        _AGENT,
        revision_id,
        old_runtime_id,
    )
    store.finalize_retryable_candidate_reset(
        _OWNER,
        _AGENT,
        revision_id,
        old_runtime_id,
    )

    old_runtime = repository.get_runtime_instance(old_runtime_id)
    assert old_runtime.state == "offline"
    assert old_runtime.failure_code == "revision_delivery_retry_reset"
    preserved_revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (revision_id,),
    )
    assert preserved_revision == {"state": "prepared", "failure_code": None}
    preserved_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(first_operation.operation_id),),
    )
    assert preserved_operation == {
        "state": "retryable",
        "terminal_code": "revision_delivery_retryable",
    }

    second_operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    replay = store.prepare_candidate(
        dataclasses.replace(request, operation_fence=second_operation)
    )
    assert replay.revision_id == revision_id
    assert replay.runtime_instance_id != old_runtime_id


def test_postgres_retry_reset_recovers_after_physical_proof_and_operation_purge(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    revision_id = str(uuid.uuid4())
    request = _candidate_preparation(
        revision_id=revision_id,
        host_session_id=host.host_session_id,
        operation_fence=operation,
        database=clean_database,
    )
    candidate = store.prepare_candidate(request)
    store.mark_candidate_starting(candidate)
    prelaunch = repository.get_runtime_instance(candidate.runtime_instance_id)
    repository.bind_runtime_process(
        prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=prelaunch.state_revision,
    )
    repository._operations.terminalize(
        operation,
        state=OperationState.RETRYABLE,
        terminal_code="revision_delivery_retryable",
        safe_summary=None,
        retry_after_ms=0,
        now=None,
        retention=repository._operation_retention,
    )
    assert (
        store.stage_retryable_candidate_reset(_OWNER, _AGENT, revision_id)
        == candidate.runtime_instance_id
    )
    staged = repository.get_runtime_instance(candidate.runtime_instance_id)
    exact_exit = repository.record_runtime_physical_exit(
        staged.fence,
        proof_code="child_exited",
    )
    assert exact_exit.instance.failure_code == "child_exited"
    preserved_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(operation.operation_id),),
    )
    assert preserved_operation == {
        "state": "retryable",
        "terminal_code": "revision_delivery_retryable",
    }

    _purge_terminal_operations(repository)
    retained = clean_database.fetch_one(
        "SELECT operation_id, state, failure_code FROM agent_runtime_instance "
        "WHERE runtime_instance_id = ?",
        (candidate.runtime_instance_id,),
    )
    assert retained == {
        "operation_id": None,
        "state": "offline",
        "failure_code": "child_exited",
    }
    staged_revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (revision_id,),
    )
    assert staged_revision == {
        "state": "starting",
        "failure_code": "revision_delivery_retry_reset_pending",
    }
    plan = store.recovery_plan(_OWNER, _AGENT)
    assert plan.retry_reset_candidates == (
        (revision_id, candidate.runtime_instance_id),
    )
    assert plan.stop_runtime_instance_ids == ()
    assert plan.finalize_runtime_instance_ids == ()

    proof_checks: list[str] = []
    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda runtime_id: proof_checks.append(runtime_id),
    )
    asyncio.run(activator.reconcile_after_crash(_OWNER, _AGENT))

    assert proof_checks == [candidate.runtime_instance_id]
    finalized = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert finalized.state == "offline"
    assert finalized.failure_code == "revision_delivery_retry_reset"
    reusable_revision = clean_database.fetch_one(
        "SELECT state, confirmed_at, failure_code FROM user_agent_revision "
        "WHERE revision_id = ?",
        (revision_id,),
    )
    assert reusable_revision == {
        "state": "prepared",
        "confirmed_at": None,
        "failure_code": None,
    }
    store.finalize_retryable_candidate_reset(
        _OWNER,
        _AGENT,
        revision_id,
        candidate.runtime_instance_id,
    )

    replay = store.prepare_candidate(
        dataclasses.replace(
            request,
            operation_fence=_running_operation(
                clean_database,
                operation_kind="agent_runtime_delivery",
            ),
        )
    )
    assert replay.runtime_instance_id != candidate.runtime_instance_id


def test_postgres_processless_retry_reset_recovers_after_operation_purge(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    revision_id = str(uuid.uuid4())
    request = _candidate_preparation(
        revision_id=revision_id,
        host_session_id=host.host_session_id,
        operation_fence=operation,
        database=clean_database,
    )
    candidate = store.prepare_candidate(request)
    repository._operations.terminalize(
        operation,
        state=OperationState.RETRYABLE,
        terminal_code="revision_delivery_retryable",
        safe_summary=None,
        retry_after_ms=0,
        now=None,
        retention=repository._operation_retention,
    )
    assert (
        store.stage_retryable_candidate_reset(_OWNER, _AGENT, revision_id)
        == candidate.runtime_instance_id
    )
    staged = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert staged.state == "delivering"
    assert staged.fence.process_id is None
    assert staged.failure_code == "revision_delivery_retry_reset_pending"

    _purge_terminal_operations(repository)
    plan = store.recovery_plan(_OWNER, _AGENT)
    assert plan.retry_reset_candidates == (
        (revision_id, candidate.runtime_instance_id),
    )
    assert plan.stop_runtime_instance_ids == ()
    assert plan.finalize_runtime_instance_ids == ()

    stop_requests: list[str] = []
    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda runtime_id: stop_requests.append(runtime_id),
    )
    asyncio.run(activator.reconcile_after_crash(_OWNER, _AGENT))

    assert stop_requests == [candidate.runtime_instance_id]
    finalized = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert finalized.state == "failed"
    assert finalized.failure_code == "revision_delivery_retry_reset"
    revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (revision_id,),
    )
    assert revision == {"state": "prepared", "failure_code": None}


def test_postgres_arbitrary_mutable_revision_disposition_fails_closed(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=host.host_session_id,
        operation_fence=_running_operation(
            clean_database,
            operation_kind="agent_runtime_delivery",
        ),
        database=clean_database,
    )
    candidate = store.prepare_candidate(request)
    with pytest.raises(ValueError, match="permanent failure code is invalid"):
        store.stage_candidate_failure(candidate, "untrusted_cleanup_hint")
    clean_database.execute(
        "UPDATE user_agent_revision SET failure_code = ?, "
        "state_revision = state_revision + 1 WHERE revision_id = ?",
        ("untrusted_cleanup_hint", candidate.revision_id),
    )

    with pytest.raises(
        StaleRuntimeGenerationError,
        match="candidate revision disposition is invalid",
    ):
        store.inspect_recovery_status(_OWNER, _AGENT, candidate.revision_id)
    with pytest.raises(
        StaleRuntimeGenerationError,
        match="candidate revision disposition is invalid",
    ):
        store.recovery_plan(_OWNER, _AGENT)


def test_postgres_retry_reset_selects_only_unfinished_terminal_attempt(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    revision_id = str(uuid.uuid4())
    first_operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    request = _candidate_preparation(
        revision_id=revision_id,
        host_session_id=host.host_session_id,
        operation_fence=first_operation,
        database=clean_database,
    )
    first = store.prepare_candidate(request)
    store.mark_candidate_starting(first)
    repository._operations.terminalize(
        first_operation,
        state=OperationState.RETRYABLE,
        terminal_code="revision_delivery_retryable",
        safe_summary=None,
        retry_after_ms=0,
        now=None,
        retention=repository._operation_retention,
    )
    assert (
        store.stage_retryable_candidate_reset(_OWNER, _AGENT, revision_id)
        == first.runtime_instance_id
    )
    store.finalize_retryable_candidate_reset(
        _OWNER,
        _AGENT,
        revision_id,
        first.runtime_instance_id,
    )

    second_operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    second = store.prepare_candidate(
        dataclasses.replace(request, operation_fence=second_operation)
    )
    store.mark_candidate_starting(second)
    repository._operations.terminalize(
        second_operation,
        state=OperationState.RETRYABLE,
        terminal_code="revision_delivery_retryable",
        safe_summary=None,
        retry_after_ms=0,
        now=None,
        retention=repository._operation_retention,
    )
    repository.terminalize_runtime(
        repository.get_runtime_instance(second.runtime_instance_id).fence,
        failure_code="host_lost",
    )

    selected = store.stage_retryable_candidate_reset(
        _OWNER,
        _AGENT,
        revision_id,
    )

    assert selected == second.runtime_instance_id
    assert selected != first.runtime_instance_id
    staged = repository.get_runtime_instance(selected)
    assert staged.failure_code == "revision_delivery_retry_reset_pending"
    store.finalize_retryable_candidate_reset(
        _OWNER,
        _AGENT,
        revision_id,
        selected,
    )
    revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (revision_id,),
    )
    assert revision == {"state": "prepared", "failure_code": None}

    third_operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    create_prelaunch = repository.create_prelaunch_instance

    def commit_third_then_raise(**kwargs):
        create_prelaunch(**kwargs)
        raise OSError("third runtime create acknowledgement was lost")

    monkeypatch.setattr(
        repository,
        "create_prelaunch_instance",
        commit_third_then_raise,
    )
    third = store.prepare_candidate(
        dataclasses.replace(request, operation_fence=third_operation)
    )
    assert third.runtime_instance_id not in {
        first.runtime_instance_id,
        second.runtime_instance_id,
    }
    assert repository.get_runtime_instance(third.runtime_instance_id).state == (
        "delivering"
    )


def test_postgres_retry_reset_fails_closed_for_two_unfinished_terminal_attempts(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    revision_id = str(uuid.uuid4())
    request = _candidate_preparation(
        revision_id=revision_id,
        host_session_id=host.host_session_id,
        operation_fence=_running_operation(
            clean_database,
            operation_kind="agent_runtime_delivery",
        ),
        database=clean_database,
    )
    unfinished_ids: list[str] = []
    for attempt in range(2):
        operation = request.operation_fence
        candidate = store.prepare_candidate(request)
        store.mark_candidate_starting(candidate)
        repository._operations.terminalize(
            operation,
            state=OperationState.RETRYABLE,
            terminal_code="revision_delivery_retryable",
            safe_summary=None,
            retry_after_ms=0,
            now=None,
            retention=repository._operation_retention,
        )
        repository.terminalize_runtime(
            repository.get_runtime_instance(candidate.runtime_instance_id).fence,
            failure_code="host_lost",
        )
        unfinished_ids.append(candidate.runtime_instance_id)
        if attempt == 0:
            clean_database.execute(
                "UPDATE user_agent_revision SET state = 'prepared', "
                "state_revision = state_revision + 1 WHERE revision_id = ?",
                (revision_id,),
            )
            request = dataclasses.replace(
                request,
                operation_fence=_running_operation(
                    clean_database,
                    operation_kind="agent_runtime_delivery",
                ),
            )

    with pytest.raises(
        StaleRuntimeGenerationError,
        match="retryable candidate runtime identity is stale",
    ):
        store.stage_retryable_candidate_reset(_OWNER, _AGENT, revision_id)

    assert len(set(unfinished_ids)) == 2
    assert all(
        repository.get_runtime_instance(runtime_id).failure_code == "host_lost"
        for runtime_id in unfinished_ids
    )


def test_postgres_revision_recovery_follows_pointer_and_fences_orphans(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, host, authoritative = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    orphan_operation = _running_operation(
        clean_database, operation_kind="agent_runtime_delivery"
    )
    orphan_request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=host.host_session_id,
        operation_fence=orphan_operation,
        database=clean_database,
    )
    orphan = store.prepare_candidate(orphan_request)
    assert not store.stage_candidate_failure(
        orphan, "revision_promotion_failed"
    )

    plan = store.recovery_plan(_OWNER, _AGENT)

    assert plan.authoritative_runtime_instance_id == (
        authoritative.fence.runtime_instance_id
    )
    assert plan.start_revision_id is None
    assert plan.stop_runtime_instance_ids == ()
    assert plan.finalize_runtime_instance_ids == (orphan.runtime_instance_id,)
    staged = repository.get_runtime_instance(orphan.runtime_instance_id)
    assert staged.state == "delivering"
    assert staged.failure_code == "revision_promotion_failed"
    store.finalize_recovery_runtime(
        _OWNER,
        _AGENT,
        orphan.runtime_instance_id,
    )
    revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (orphan.revision_id,),
    )
    assert revision == {
        "state": "failed",
        "failure_code": "revision_promotion_failed",
    }
    terminal = repository.get_runtime_instance(orphan.runtime_instance_id)
    assert terminal.state == "failed"
    assert terminal.failure_code == "revision_promotion_failed"
    delivery_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(orphan_operation.operation_id),),
    )
    assert delivery_operation == {
        "state": "failed",
        "terminal_code": "revision_promotion_failed",
    }


def test_postgres_staged_failure_survives_disconnect_and_exact_recovery(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, host, _authoritative = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=host.host_session_id,
        operation_fence=operation,
        database=clean_database,
    )
    candidate = store.prepare_candidate(request)
    prelaunch = repository.get_runtime_instance(candidate.runtime_instance_id)
    store.mark_candidate_starting(candidate)
    repository.bind_runtime_process(
        prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=prelaunch.state_revision,
    )
    assert store.stage_candidate_failure(
        candidate,
        "child_registration_timeout",
    )

    repository.disconnect_host_session(host.fence, failure_code="host_lost")

    disconnected = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert disconnected.state == "offline"
    assert disconnected.failure_code == "child_registration_timeout"
    staged_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(operation.operation_id),),
    )
    assert staged_operation == {
        "state": "failed",
        "terminal_code": "child_registration_timeout",
    }
    status = store.inspect_recovery_status(
        _OWNER,
        _AGENT,
        candidate.revision_id,
    )
    assert status is not None
    assert status.runtime_instance_id == candidate.runtime_instance_id
    assert status.runtime_failure_code == "child_registration_timeout"
    assert status.operation_state is OperationState.FAILED
    plan = store.recovery_plan(_OWNER, _AGENT)
    assert candidate.runtime_instance_id in plan.stop_runtime_instance_ids
    assert candidate.runtime_instance_id not in plan.finalize_runtime_instance_ids

    stop_attempts: list[str] = []

    def disconnected_stop(runtime_instance_id: str) -> bool:
        stop_attempts.append(runtime_instance_id)
        return False

    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=disconnected_stop,
    )
    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_runtime_cleanup_pending",
    ):
        asyncio.run(activator.reconcile_after_crash(_OWNER, _AGENT))

    # A disconnected/superseded session is not exact process-exit proof. The
    # semantic failure remains immutable on the delivery operation while the
    # process-bearing runtime stays discoverable cleanup debt. In particular,
    # recovery must neither fail the revision early nor allocate another child.
    assert stop_attempts == [candidate.runtime_instance_id]

    pending_revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (candidate.revision_id,),
    )
    assert pending_revision == {
        "state": "starting",
        "failure_code": "child_registration_timeout",
    }
    runtime_count = clean_database.fetch_one(
        "SELECT COUNT(*) AS count FROM agent_runtime_instance WHERE revision_id = ?",
        (candidate.revision_id,),
    )
    assert int(runtime_count["count"]) == 1
    replay_plan = store.recovery_plan(_OWNER, _AGENT)
    assert candidate.runtime_instance_id in replay_plan.stop_runtime_instance_ids
    assert candidate.runtime_instance_id not in (
        replay_plan.finalize_runtime_instance_ids
    )

    # A later full-fence exit frame upgrades only the runtime's physical fact.
    # Once operation retention purges the FK, recovery still derives the
    # immutable activation meaning from the mutable revision marker.
    exact_exit = repository.record_runtime_physical_exit(
        disconnected.fence,
        proof_code="child_exited",
    )
    assert exact_exit.instance.state == "offline"
    assert exact_exit.instance.failure_code == "child_exited"
    _purge_terminal_operations(repository)
    purged_runtime = clean_database.fetch_one(
        "SELECT operation_id, failure_code FROM agent_runtime_instance "
        "WHERE runtime_instance_id = ?",
        (candidate.runtime_instance_id,),
    )
    assert purged_runtime == {
        "operation_id": None,
        "failure_code": "child_exited",
    }
    status_after_purge = store.inspect_recovery_status(
        _OWNER,
        _AGENT,
        candidate.revision_id,
    )
    assert status_after_purge is not None
    assert status_after_purge.runtime_failure_code == "child_registration_timeout"
    assert status_after_purge.operation_state is None
    exact_plan = store.recovery_plan(_OWNER, _AGENT)
    assert exact_plan.stop_runtime_instance_ids == ()
    assert exact_plan.finalize_runtime_instance_ids == (
        candidate.runtime_instance_id,
    )

    verified_proofs: list[str] = []
    exact_recovery = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda runtime_id: verified_proofs.append(runtime_id),
    )
    asyncio.run(exact_recovery.reconcile_after_crash(_OWNER, _AGENT))

    assert verified_proofs == [candidate.runtime_instance_id]
    failed_revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (candidate.revision_id,),
    )
    assert failed_revision == {
        "state": "failed",
        "failure_code": "child_registration_timeout",
    }
    terminal_status = store.inspect_recovery_status(
        _OWNER,
        _AGENT,
        candidate.revision_id,
    )
    assert terminal_status is not None
    assert terminal_status.runtime_instance_id == candidate.runtime_instance_id
    assert terminal_status.runtime_failure_code == "child_registration_timeout"
    assert terminal_status.operation_state is None


def test_postgres_agent_wide_recovery_skips_concurrent_running_revision(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, host, authoritative = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=host.host_session_id,
        operation_fence=operation,
        database=clean_database,
    )
    concurrent = store.prepare_candidate(request)
    store.mark_candidate_starting(concurrent)
    prelaunch = repository.get_runtime_instance(concurrent.runtime_instance_id)
    repository.bind_runtime_process(
        prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=prelaunch.state_revision,
    )

    plan = store.recovery_plan(_OWNER, _AGENT)

    assert plan.authoritative_runtime_instance_id == (
        authoritative.fence.runtime_instance_id
    )
    assert concurrent.runtime_instance_id not in plan.stop_runtime_instance_ids
    assert concurrent.runtime_instance_id not in plan.finalize_runtime_instance_ids
    unchanged_runtime = repository.get_runtime_instance(
        concurrent.runtime_instance_id
    )
    assert unchanged_runtime.state == "starting"
    assert unchanged_runtime.failure_code is None
    unchanged_revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (concurrent.revision_id,),
    )
    assert unchanged_revision == {"state": "starting", "failure_code": None}
    unchanged_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(operation.operation_id),),
    )
    assert unchanged_operation == {"state": "running", "terminal_code": None}


@pytest.mark.parametrize(
    ("operation_state", "terminal_code"),
    (
        (OperationState.FAILED, "revision_delivery_failed"),
        (OperationState.CANCELLED, "revision_delivery_cancelled"),
    ),
)
def test_postgres_recovery_converges_terminal_delivery_operation(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
    operation_state: OperationState,
    terminal_code: str,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=host.host_session_id,
        operation_fence=operation,
        database=clean_database,
    )
    candidate = store.prepare_candidate(request)
    store.mark_candidate_starting(candidate)
    prelaunch = repository.get_runtime_instance(candidate.runtime_instance_id)
    repository.bind_runtime_process(
        prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=prelaunch.state_revision,
    )
    repository._operations.terminalize(
        operation,
        state=operation_state,
        terminal_code=terminal_code,
        safe_summary=None,
        retry_after_ms=None,
        now=None,
        retention=repository._operation_retention,
    )

    plan = store.recovery_plan(_OWNER, _AGENT)

    assert candidate.runtime_instance_id in plan.stop_runtime_instance_ids
    staged = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert staged.state == "stopping"
    assert staged.failure_code == "revision_promotion_failed"
    store.finalize_recovery_runtime(
        _OWNER,
        _AGENT,
        candidate.runtime_instance_id,
    )
    revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision WHERE revision_id = ?",
        (candidate.revision_id,),
    )
    assert revision == {
        "state": "failed",
        "failure_code": "revision_promotion_failed",
    }
    preserved_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(operation.operation_id),),
    )
    assert preserved_operation == {
        "state": operation_state.value,
        "terminal_code": terminal_code,
    }


def test_postgres_purged_unmarked_process_bound_attempt_fails_closed(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    candidate = store.prepare_candidate(
        _candidate_preparation(
            revision_id=str(uuid.uuid4()),
            host_session_id=host.host_session_id,
            operation_fence=operation,
            database=clean_database,
        )
    )
    store.mark_candidate_starting(candidate)
    prelaunch = repository.get_runtime_instance(candidate.runtime_instance_id)
    repository.bind_runtime_process(
        prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=prelaunch.state_revision,
    )
    _terminalize_delivery_operation(repository, operation)
    _purge_terminal_operations(repository)

    purged = clean_database.fetch_one(
        "SELECT operation_id, operation_execution_generation FROM "
        "agent_runtime_instance WHERE runtime_instance_id = ?",
        (candidate.runtime_instance_id,),
    )
    assert purged["operation_id"] is None
    assert int(purged["operation_execution_generation"]) > 0

    plan = store.recovery_plan(_OWNER, _AGENT)
    assert plan.stop_runtime_instance_ids == (candidate.runtime_instance_id,)
    assert plan.finalize_runtime_instance_ids == ()
    status = store.inspect_recovery_status(
        _OWNER,
        _AGENT,
        candidate.revision_id,
    )
    assert status is not None
    assert status.revision_state == "starting"
    assert status.runtime_instance_id == candidate.runtime_instance_id
    assert status.runtime_failure_code == "revision_promotion_failed"
    assert status.operation_state is None
    staged = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert staged.state == "stopping"
    assert staged.failure_code == "revision_promotion_failed"
    assert clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision "
        "WHERE revision_id = ?",
        (candidate.revision_id,),
    ) == {"state": "starting", "failure_code": "revision_promotion_failed"}

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_runtime_physical_exit_pending",
    ):
        store.finalize_recovery_runtime(
            _OWNER,
            _AGENT,
            candidate.runtime_instance_id,
        )

    stopped: list[str] = []

    def confirm_exact_exit(runtime_instance_id: str) -> None:
        stopped.append(runtime_instance_id)
        stopping = repository.get_runtime_instance(runtime_instance_id)
        repository.record_runtime_physical_exit(
            stopping.fence,
            proof_code="child_exited",
        )

    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=confirm_exact_exit,
    )
    asyncio.run(activator.reconcile_after_crash(_OWNER, _AGENT))

    assert stopped == [candidate.runtime_instance_id]
    failed = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision "
        "WHERE revision_id = ?",
        (candidate.revision_id,),
    )
    assert failed == {
        "state": "failed",
        "failure_code": "revision_promotion_failed",
    }
    terminal = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert terminal.state == "offline"
    assert terminal.failure_code == "child_exited"


def test_postgres_purged_unmarked_processless_attempt_terminalizes_immediately(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    candidate = store.prepare_candidate(
        _candidate_preparation(
            revision_id=str(uuid.uuid4()),
            host_session_id=host.host_session_id,
            operation_fence=operation,
            database=clean_database,
        )
    )
    _terminalize_delivery_operation(repository, operation)
    _purge_terminal_operations(repository)

    status = store.inspect_recovery_status(
        _OWNER,
        _AGENT,
        candidate.revision_id,
    )

    assert status is not None
    assert status.revision_state == "failed"
    assert status.runtime_instance_id == candidate.runtime_instance_id
    assert status.runtime_failure_code == "revision_promotion_failed"
    assert status.operation_state is None
    terminal = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert terminal.state == "failed"
    assert terminal.fence.process_id is None
    assert terminal.failure_code == "revision_promotion_failed"
    revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision "
        "WHERE revision_id = ?",
        (candidate.revision_id,),
    )
    assert revision == {
        "state": "failed",
        "failure_code": "revision_promotion_failed",
    }
    plan = asyncio.run(
        AgentRevisionActivator(
            store,
            start_candidate=lambda _candidate: pytest.fail("must not start"),
            await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
            stop_runtime=lambda _runtime_id: pytest.fail("must not stop"),
        ).reconcile_after_crash(_OWNER, _AGENT)
    )
    assert plan.stop_runtime_instance_ids == ()
    assert plan.finalize_runtime_instance_ids == ()


def test_postgres_purged_unmarked_physical_proof_finalizes_directly(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    candidate = store.prepare_candidate(
        _candidate_preparation(
            revision_id=str(uuid.uuid4()),
            host_session_id=host.host_session_id,
            operation_fence=operation,
            database=clean_database,
        )
    )
    store.mark_candidate_starting(candidate)
    prelaunch = repository.get_runtime_instance(candidate.runtime_instance_id)
    started = repository.bind_runtime_process(
        prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=prelaunch.state_revision,
    )
    _terminalize_delivery_operation(
        repository,
        operation,
        state=OperationState.CANCELLED,
    )
    _purge_terminal_operations(repository)
    proof = repository.record_runtime_physical_exit(
        started.fence,
        proof_code="agent_offline",
    )
    assert proof.instance.failure_code == "agent_offline"

    plan = store.recovery_plan(_OWNER, _AGENT)

    assert plan.stop_runtime_instance_ids == ()
    assert plan.finalize_runtime_instance_ids == ()
    terminal = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert terminal.state == "offline"
    assert terminal.failure_code == "revision_promotion_failed"
    revision = clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision "
        "WHERE revision_id = ?",
        (candidate.revision_id,),
    )
    assert revision == {
        "state": "failed",
        "failure_code": "revision_promotion_failed",
    }
    status = store.inspect_recovery_status(
        _OWNER,
        _AGENT,
        candidate.revision_id,
    )
    assert status is not None
    assert status.runtime_failure_code == "revision_promotion_failed"
    assert status.operation_state is None


def test_postgres_purged_unmarked_attempt_with_terminal_history_stays_pending(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, host, _ = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    revision_id = str(uuid.uuid4())
    first_operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    request = _candidate_preparation(
        revision_id=revision_id,
        host_session_id=host.host_session_id,
        operation_fence=first_operation,
        database=clean_database,
    )
    first = store.prepare_candidate(request)
    store.mark_candidate_starting(first)
    first_prelaunch = repository.get_runtime_instance(first.runtime_instance_id)
    first_started = repository.bind_runtime_process(
        first_prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=first_prelaunch.state_revision,
    )
    _terminalize_delivery_operation(repository, first_operation)
    repository.terminalize_runtime(first_started.fence, failure_code="host_lost")
    clean_database.execute(
        "UPDATE user_agent_revision SET state = 'prepared', "
        "state_revision = state_revision + 1 WHERE revision_id = ?",
        (revision_id,),
    )

    second_operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    second = store.prepare_candidate(
        dataclasses.replace(request, operation_fence=second_operation)
    )
    store.mark_candidate_starting(second)
    second_prelaunch = repository.get_runtime_instance(second.runtime_instance_id)
    second_started = repository.bind_runtime_process(
        second_prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=second_prelaunch.state_revision,
    )
    _terminalize_delivery_operation(repository, second_operation)
    _purge_terminal_operations(repository)

    status = store.inspect_recovery_status(_OWNER, _AGENT, revision_id)
    assert status is not None
    assert status.runtime_instance_id == second.runtime_instance_id
    assert status.runtime_failure_code is None
    assert status.operation_state is None
    with pytest.raises(
        StaleRuntimeGenerationError,
        match="candidate recovery operation is unavailable",
    ):
        store.recovery_plan(_OWNER, _AGENT)

    unchanged = repository.get_runtime_instance(second.runtime_instance_id)
    assert unchanged.fence == second_started.fence
    assert unchanged.state == "starting"
    assert unchanged.failure_code is None
    assert clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision "
        "WHERE revision_id = ?",
        (revision_id,),
    ) == {"state": "starting", "failure_code": None}


@pytest.mark.parametrize(
    "authority_conflict",
    ("runtime", "pointer", "active_revision"),
)
def test_postgres_purged_unmarked_authority_conflict_stays_pending(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
    authority_conflict: str,
) -> None:
    _, host, authoritative = _runtime(repository, clean_database, online=True)
    store = PostgresPersonalAgentRevisionStore(repository)
    operation = _running_operation(
        clean_database,
        operation_kind="agent_runtime_delivery",
    )
    candidate = store.prepare_candidate(
        _candidate_preparation(
            revision_id=str(uuid.uuid4()),
            host_session_id=host.host_session_id,
            operation_fence=operation,
            database=clean_database,
        )
    )
    store.mark_candidate_starting(candidate)
    prelaunch = repository.get_runtime_instance(candidate.runtime_instance_id)
    repository.bind_runtime_process(
        prelaunch.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=prelaunch.state_revision,
    )
    _terminalize_delivery_operation(repository, operation)
    _purge_terminal_operations(repository)

    if authority_conflict == "runtime":
        clean_database.execute(
            "UPDATE agent_runtime_instance SET is_authoritative = FALSE, "
            "state_revision = state_revision + 1 WHERE runtime_instance_id = ?",
            (authoritative.fence.runtime_instance_id,),
        )
        clean_database.execute(
            "UPDATE agent_runtime_instance SET is_authoritative = TRUE, "
            "state_revision = state_revision + 1 WHERE runtime_instance_id = ?",
            (candidate.runtime_instance_id,),
        )
    elif authority_conflict == "pointer":
        clean_database.execute(
            "UPDATE user_agent SET authoritative_instance_id = ?, "
            "state_revision = state_revision + 1 WHERE agent_id = ?",
            (candidate.runtime_instance_id, _AGENT),
        )
    else:
        clean_database.execute(
            "UPDATE user_agent SET active_revision_id = ?, "
            "state_revision = state_revision + 1 WHERE agent_id = ?",
            (candidate.revision_id, _AGENT),
        )

    status = store.inspect_recovery_status(
        _OWNER,
        _AGENT,
        candidate.revision_id,
    )
    assert status is not None
    assert status.runtime_failure_code is None
    assert status.operation_state is None
    with pytest.raises(
        StaleRuntimeGenerationError,
        match="candidate recovery operation is unavailable",
    ):
        store.recovery_plan(_OWNER, _AGENT)

    unchanged = repository.get_runtime_instance(candidate.runtime_instance_id)
    assert unchanged.state == "starting"
    assert unchanged.failure_code is None
    assert clean_database.fetch_one(
        "SELECT state, failure_code FROM user_agent_revision "
        "WHERE revision_id = ?",
        (candidate.revision_id,),
    ) == {"state": "starting", "failure_code": None}


def test_revision_prepare_refuses_inventory_pending_before_runtime_insert(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    create_user_agent(
        clean_database,
        agent_id=_AGENT,
        owner_user_id=_OWNER,
        display_name="Inventory-gated",
    )
    clean_database.execute(
        "UPDATE user_agent SET status = 'validated' WHERE agent_id = ?",
        (_AGENT,),
    )
    pending_host = _host(repository)
    selected = repository.select_host_for_agent(owner_user_id=_OWNER, agent_id=_AGENT)
    assert selected.session is not None
    assert selected.session.host_session_id == pending_host.host_session_id
    store = PostgresPersonalAgentRevisionStore(repository)
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=pending_host.host_session_id,
        operation_fence=_running_operation(
            clean_database, operation_kind="agent_runtime_delivery"
        ),
        database=clean_database,
    )

    with pytest.raises(RevisionActivationError, match="inventory_required"):
        store.prepare_candidate(request)

    revision = clean_database.fetch_one(
        "SELECT state FROM user_agent_revision WHERE revision_id = ?",
        (request.revision_id,),
    )
    assert revision == {"state": "prepared"}
    assert (
        clean_database.fetch_one(
            "SELECT count(*) AS count FROM agent_runtime_instance"
        )["count"]
        == 0
    )


def test_delayed_candidate_registration_cannot_revive_durable_tombstone(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    revision, _, started = _runtime(repository, clean_database, online=False)
    tombstone = repository.tombstone_agent(owner_user_id=_OWNER, agent_id=_AGENT)

    with pytest.raises(AgentDeletedError):
        repository.accept_runtime_registration(
            started.fence,
            runtime_contract_version=_POLICY.runtime_contract_version,
            bundle_sha256=revision.artifact_digest,
        )

    row = clean_database.fetch_one(
        "SELECT status, deleted_at, active_revision_id, "
        "authoritative_instance_id, lifecycle_generation FROM user_agent "
        "WHERE agent_id = ? AND owner_user_id = ?",
        (_AGENT, _OWNER),
    )
    assert row["status"] == "disabled"
    assert row["deleted_at"] is not None
    assert row["active_revision_id"] is None
    assert row["authoritative_instance_id"] is None
    assert int(row["lifecycle_generation"]) == tombstone.lifecycle_generation


def test_post_tombstone_cleanup_settles_all_runtime_and_request_operations(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    _, _, online = _runtime(repository, clean_database, online=True)
    request_operation = _running_operation(clean_database)
    request = repository.assign_request(
        online.fence,
        operation_fence=request_operation,
    )
    assert online.operation_id is not None
    tombstone = repository.tombstone_agent(
        owner_user_id=_OWNER,
        agent_id=_AGENT,
    )
    with pytest.raises(AgentDeletedError):
        repository.terminalize_runtime(
            online.fence,
            failure_code="agent_deleted",
        )

    cleanup = repository.cleanup_tombstoned_agent(tombstone)
    assert cleanup.tombstone == tombstone
    assert cleanup.settled_request_ids == (request.fence.request_id,)
    assert [
        item.instance.fence.runtime_instance_id for item in cleanup.settlements
    ] == [online.fence.runtime_instance_id]
    terminal = repository.get_runtime_instance(online.fence.runtime_instance_id)
    assert terminal.state == "offline"
    assert terminal.failure_code == "agent_deleted"
    settled_request = repository.get_runtime_request(request.fence.request_id)
    assert settled_request.state == "retryable"
    assert settled_request.terminal_code == "agent_deleted"
    operations = clean_database.fetch_all(
        "SELECT operation_id, state, terminal_code FROM operation_record "
        "WHERE operation_id IN (?, ?) ORDER BY operation_id",
        (online.operation_id, request.fence.operation_id),
    )
    assert {
        str(row["operation_id"]): (row["state"], row["terminal_code"])
        for row in operations
    } == {
        online.operation_id: ("retryable", "agent_deleted"),
        request.fence.operation_id: ("retryable", "agent_deleted"),
    }
    agent = clean_database.fetch_one(
        "SELECT deleted_at, lifecycle_generation, state_revision, "
        "active_revision_id, selected_host_session_id, authoritative_instance_id "
        "FROM user_agent WHERE agent_id = ?",
        (_AGENT,),
    )
    assert int(agent["deleted_at"]) == tombstone.deleted_at
    assert int(agent["lifecycle_generation"]) == tombstone.lifecycle_generation
    assert int(agent["state_revision"]) == tombstone.state_revision
    assert agent["active_revision_id"] is None
    assert agent["selected_host_session_id"] is None
    assert agent["authoritative_instance_id"] is None

    replay = repository.cleanup_tombstoned_agent(tombstone)
    assert replay.settlements == ()
    assert replay.settled_request_ids == ()


def test_revision_prepare_cannot_recreate_deleted_agent(
    repository: PersonalAgentRuntimeRepository,
    clean_database: PlaneTestRuntime,
) -> None:
    create_user_agent(
        clean_database,
        agent_id=_AGENT,
        owner_user_id=_OWNER,
        display_name="Deleted candidate owner",
    )
    repository.tombstone_agent(owner_user_id=_OWNER, agent_id=_AGENT)
    request = _candidate_preparation(
        revision_id=str(uuid.uuid4()),
        host_session_id=str(uuid.uuid4()),
        operation_fence=_running_operation(
            clean_database, operation_kind="agent_runtime_delivery"
        ),
        database=clean_database,
    )

    with pytest.raises(RevisionActivationError, match="agent_deleted"):
        PostgresPersonalAgentRevisionStore(repository).prepare_candidate(request)

    revision = clean_database.fetch_one(
        "SELECT state FROM user_agent_revision WHERE revision_id = ?",
        (request.revision_id,),
    )
    assert revision == {"state": "prepared"}
