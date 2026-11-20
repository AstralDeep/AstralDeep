"""Tests for astralplane.repositories.agents: ownership, revision, host/runtime session,
and request-record CRUD, CAS fencing, replay safety, and owner-scoped expiry
discovery.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _support import Result, ScriptedTransaction

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
)
from astralplane.repositories.agents import AgentRepository

NOW = datetime(2026, 8, 14, tzinfo=UTC)
OWNER = "owner-1"
AGENT = "agent-1"
REVISION = str(uuid.UUID("10000000-0000-4000-8000-000000000001"))
SESSION = str(uuid.UUID("20000000-0000-4000-8000-000000000001"))
HOST = str(uuid.UUID("30000000-0000-4000-8000-000000000001"))
SCOPE = str(uuid.UUID("40000000-0000-4000-8000-000000000001"))
RUNTIME = str(uuid.UUID("50000000-0000-4000-8000-000000000001"))
DELIVERY = str(uuid.UUID("60000000-0000-4000-8000-000000000001"))
REQUEST = str(uuid.UUID("70000000-0000-4000-8000-000000000001"))
REQUEST_GENERATION = str(uuid.UUID("80000000-0000-4000-8000-000000000001"))
DIGEST = "a" * 64


def ownership_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "agent_id": AGENT,
        "owner_email": "owner@example.test",
        "is_public": False,
        "created_at": 1,
        "updated_at": 1,
    }
    row.update(changes)
    return row


def agent_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "agent_id": AGENT,
        "owner_user_id": OWNER,
        "owner_email": "owner@example.test",
        "display_name": "Agent",
        "status": "authoring",
        "declared_tools": '["search"]',
        "declared_scopes": '["tools:search"]',
        "declared_egress": None,
        "constitution_version": None,
        "validated_at": None,
        "revalidation_required": False,
        "draft_id": "draft-1",
        "host_client_id": None,
        "host_session_id": None,
        "host_last_seen_at": None,
        "is_public": False,
        "deleted_at": None,
        "created_at": 1,
        "updated_at": 1,
        "active_revision_id": None,
        "last_known_good_revision_id": None,
        "selected_host_session_id": None,
        "authoritative_instance_id": None,
        "lifecycle_generation": 0,
        "generation_counter": 0,
        "state_revision": 0,
        "validated_policy_revision": None,
    }
    row.update(changes)
    return row


def revision_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "revision_id": REVISION,
        "agent_id": AGENT,
        "owner_user_id": OWNER,
        "revision_number": 1,
        "parent_revision_id": None,
        "previous_good_revision_id": None,
        "artifact_digest": DIGEST,
        "manifest_json": {"contract": 2},
        "artifact_relative_path": "agents/agent-1/rev-1",
        "runtime_contract_version": 2,
        "release_lock_digest": DIGEST,
        "compatibility_state": "compatible",
        "state": "prepared",
        "promotion_token": SCOPE,
        "state_revision": 0,
        "created_at": NOW,
        "confirmed_at": None,
        "promoted_at": None,
        "failed_at": None,
        "failure_code": None,
    }
    row.update(changes)
    return row


def host_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "host_session_id": SESSION,
        "host_id": HOST,
        "owner_user_id": OWNER,
        "connection_scope_id": SCOPE,
        "platform": "windows",
        "client_version": "1.2.3",
        "host_generation": 1,
        "supersedes_session_id": None,
        "supported_runtime_contract_versions": [2],
        "runtime_contract_version": 2,
        "release_lock_digest": DIGEST,
        "state": "connected",
        "inventory_state": "pending",
        "eligible_since": NOW,
        "accepted_at": NOW,
        "last_seen_at": NOW,
        "disconnected_at": None,
        "inventory_reconciled_at": None,
        "failure_code": None,
    }
    row.update(changes)
    return row


def runtime_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "runtime_instance_id": RUNTIME,
        "agent_id": AGENT,
        "owner_user_id": OWNER,
        "host_id": HOST,
        "host_session_id": SESSION,
        "delivery_id": DELIVERY,
        "revision_id": REVISION,
        "process_id": None,
        "lifecycle_generation": 1,
        "runtime_contract_version": 2,
        "operation_id": None,
        "operation_execution_generation": 1,
        "state": "delivering",
        "is_authoritative": False,
        "state_revision": 0,
        "created_at": NOW,
        "started_at": None,
        "registered_at": None,
        "last_heartbeat_sequence": None,
        "ready_at": None,
        "last_liveness_at": None,
        "terminal_at": None,
        "failure_code": None,
    }
    row.update(changes)
    return row


def request_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "request_id": REQUEST,
        "request_generation": REQUEST_GENERATION,
        "operation_id": None,
        "operation_execution_generation": 1,
        "runtime_instance_id": RUNTIME,
        "agent_id": AGENT,
        "owner_user_id": OWNER,
        "state": "assigned",
        "state_revision": 0,
        "assigned_at": NOW,
        "terminal_at": None,
        "terminal_code": None,
        "result_digest": None,
    }
    row.update(changes)
    return row


def test_ownership_is_immutable_and_visibility_write_requires_the_owner() -> None:
    repository = AgentRepository()
    assert not repository.upsert_ownership(
        ScriptedTransaction(one=[ownership_row()]),
        agent_id=AGENT,
        owner_email="owner@example.test",
        is_public=False,
        observed_at=1,
    ).is_public
    with pytest.raises(RepositoryConflictError):
        repository.upsert_ownership(
            ScriptedTransaction(one=[None, ownership_row(owner_email="other@example.test")]),
            agent_id=AGENT,
            owner_email="owner@example.test",
            is_public=False,
            observed_at=1,
        )
    transaction = ScriptedTransaction(one=[ownership_row(is_public=True)])
    assert repository.set_visibility(
        transaction,
        agent_id=AGENT,
        owner_email="owner@example.test",
        is_public=True,
        updated_at=2,
    ).is_public
    assert transaction.calls[0][0] == "execute"
    assert "pg_advisory_xact_lock" in transaction.calls[0][1]
    assert transaction.calls[1][2][-1] == "owner@example.test"  # type: ignore[index]

    assert repository.remove_ownership(
        ScriptedTransaction(execute=[Result(), Result(rowcount=1)]),
        agent_id=AGENT,
        owner_email="owner@example.test",
    )
    assert not repository.remove_ownership(
        ScriptedTransaction(execute=[Result(), Result(rowcount=0)]),
        agent_id=AGENT,
        owner_email="owner@example.test",
    )
    with pytest.raises(RepositoryDataError, match="row count"):
        repository.remove_ownership(
            ScriptedTransaction(execute=[Result(), Result(rowcount=2)]),
            agent_id=AGENT,
            owner_email="owner@example.test",
        )


def test_user_agent_create_is_same_owner_idempotent_and_tombstone_safe() -> None:
    repository = AgentRepository()
    created = repository.create_agent(
        ScriptedTransaction(one=[agent_row()]),
        agent_id=AGENT,
        owner_id=OWNER,
        display_name="Agent",
        owner_email="owner@example.test",
        draft_id="draft-1",
        declared_tools=("search",),
        declared_scopes=("tools:search",),
        observed_at=1,
    )
    assert created.owner_id == OWNER
    assert created.declared_tools == ("search",)
    replay = repository.create_agent(
        ScriptedTransaction(one=[None, agent_row()]),
        agent_id=AGENT,
        owner_id=OWNER,
        display_name="Agent",
        owner_email="owner@example.test",
        draft_id="draft-1",
        declared_tools=("search",),
        declared_scopes=("tools:search",),
        observed_at=1,
    )
    assert replay == created
    with pytest.raises(RepositoryConflictError, match="changed initial semantics"):
        repository.create_agent(
            ScriptedTransaction(one=[None, agent_row(display_name="Updated")]),
            agent_id=AGENT,
            owner_id=OWNER,
            display_name="Agent",
            owner_email="owner@example.test",
            draft_id="draft-1",
            declared_tools=("search",),
            declared_scopes=("tools:search",),
            observed_at=1,
        )
    with pytest.raises(RepositoryConflictError, match="tombstone"):
        repository.create_agent(
            ScriptedTransaction(one=[None, {"owner_user_id": OWNER, "deleted_at": 4}]),
            agent_id=AGENT,
            owner_id=OWNER,
            display_name="Agent",
            observed_at=1,
        )
    with pytest.raises(RepositoryConflictError, match="another owner"):
        repository.create_agent(
            ScriptedTransaction(
                one=[None, agent_row(owner_user_id="owner-2")]
            ),
            agent_id=AGENT,
            owner_id=OWNER,
            display_name="Agent",
            observed_at=1,
        )


def test_agent_update_uses_owner_and_revision_compare_and_set() -> None:
    repository = AgentRepository()
    transaction = ScriptedTransaction(
        one=[agent_row(status="validated", state_revision=1, updated_at=2)]
    )
    updated = repository.compare_and_set_agent(
        transaction,
        owner_id=OWNER,
        agent_id=AGENT,
        expected_revision=0,
        updates={"status": "validated", "updated_at": 2},
    )
    assert updated.state_revision == 1
    sql = transaction.fetch_sql()
    assert "owner_user_id = %s" in sql
    assert "state_revision = %s" in sql
    with pytest.raises(RepositoryValidationError):
        repository.compare_and_set_agent(
            ScriptedTransaction(),
            owner_id=OWNER,
            agent_id=AGENT,
            expected_revision=0,
            updates={"owner_user_id": "other"},
        )


def test_revision_host_runtime_and_request_records_share_owner_fences() -> None:
    repository = AgentRepository()
    revision = repository.create_revision(
        ScriptedTransaction(one=[revision_row()]),
        revision_id=REVISION,
        agent_id=AGENT,
        owner_id=OWNER,
        revision_number=1,
        artifact_digest=DIGEST,
        manifest={"contract": 2},
        artifact_relative_path="agents/agent-1/rev-1",
        runtime_contract_version=2,
        release_lock_digest=DIGEST,
        compatibility_state="compatible",
        state="prepared",
        promotion_token=SCOPE,
    )
    assert revision.manifest == {"contract": 2}

    host = repository.create_host_session(
        ScriptedTransaction(one=[host_row()]),
        host_session_id=SESSION,
        host_id=HOST,
        owner_id=OWNER,
        connection_scope_id=SCOPE,
        platform="windows",
        client_version="1.2.3",
        host_generation=1,
        supported_runtime_contract_versions=(2,),
        runtime_contract_version=2,
        release_lock_digest=DIGEST,
        eligible_since=NOW,
        accepted_at=NOW,
        last_seen_at=NOW,
    )
    assert host.owner_id == OWNER

    runtime = repository.create_runtime_instance(
        ScriptedTransaction(one=[runtime_row()]),
        runtime_instance_id=RUNTIME,
        agent_id=AGENT,
        owner_id=OWNER,
        host_id=HOST,
        host_session_id=SESSION,
        delivery_id=DELIVERY,
        revision_id=REVISION,
        lifecycle_generation=1,
        runtime_contract_version=2,
        operation_execution_generation=1,
    )
    assert runtime.lifecycle_generation == 1

    request = repository.create_runtime_request(
        ScriptedTransaction(one=[request_row()]),
        request_id=REQUEST,
        request_generation=REQUEST_GENERATION,
        runtime_instance_id=RUNTIME,
        agent_id=AGENT,
        owner_id=OWNER,
        operation_execution_generation=1,
    )
    assert request.owner_id == OWNER


def test_runtime_transition_rejects_stale_generation_and_invalid_uuid() -> None:
    repository = AgentRepository()
    with pytest.raises(RepositoryConflictError):
        repository.transition_runtime_instance(
            ScriptedTransaction(one=[None]),
            owner_id=OWNER,
            runtime_instance_id=RUNTIME,
            expected_revision=0,
            expected_states=("delivering",),
            updates={"state": "starting", "process_id": SCOPE},
        )
    with pytest.raises(RepositoryValidationError):
        repository.get_runtime_instance(
            ScriptedTransaction(), owner_id=OWNER, runtime_instance_id="not-a-uuid"
        )


def test_owner_lock_ownership_inventory_and_trust_storage() -> None:
    repository = AgentRepository()
    lock_tx = ScriptedTransaction()
    repository.lock_owner(lock_tx, owner_id=OWNER)
    assert "pg_advisory_xact_lock" in lock_tx.fetch_sql()

    assert (
        repository.get_ownership(ScriptedTransaction(one=[ownership_row()]), agent_id=AGENT)
        == repository.list_ownership_for_administration(
            ScriptedTransaction(all_rows=[(ownership_row(),)]), limit=1
        )[0]
    )
    assert repository.get_ownership(ScriptedTransaction(one=[None]), agent_id=AGENT) is None

    trust_row = {
        "agent_id": AGENT,
        "is_safe": True,
        "marked_by": OWNER,
        "marked_at": NOW,
        "prior_state": False,
        "revised_reset_at": None,
    }
    assert repository.get_trust(ScriptedTransaction(one=[trust_row]), agent_id=AGENT).is_safe  # type: ignore[union-attr]
    assert repository.get_trust(ScriptedTransaction(one=[None]), agent_id=AGENT) is None
    reset_row = dict(trust_row, is_safe=False, revised_reset_at=NOW, prior_state=True)
    reset = repository.set_trust(
        ScriptedTransaction(one=[reset_row]),
        agent_id=AGENT,
        is_safe=False,
        marked_by=OWNER,
        reset_for_revision=True,
    )
    assert reset.prior_state and reset.revised_reset_at == NOW


def test_policy_reconciliation_is_atomic_admin_named_and_exactly_replay_safe() -> None:
    repository = AgentRepository()
    changed_tx = ScriptedTransaction(
        one=[{"value": "policy-v1"}],
        execute=[Result(), Result(rowcount=3), Result(rowcount=1)],
    )

    changed = repository.reconcile_validation_policy_for_administration(
        changed_tx,
        policy_revision="policy-v2",
    )

    assert changed.policy_revision == "policy-v2"
    assert changed.marker_changed
    assert changed.agents_marked_for_revalidation == 3
    assert [call[0] for call in changed_tx.calls] == ["execute", "one", "execute", "execute"]
    sql = changed_tx.fetch_sql()
    assert "pg_advisory_xact_lock" in sql
    assert "deleted_at IS NULL" in sql
    assert "validated_policy_revision IS DISTINCT FROM %s" in sql
    assert "revalidation_required = FALSE" in sql
    assert "ON CONFLICT (key) DO UPDATE" in sql
    assert changed_tx.calls[2][2] == ("policy-v2",)
    assert changed_tx.calls[3][2] == ("user_agent_policy_revision", "policy-v2")

    replay_tx = ScriptedTransaction(one=[{"value": "policy-v2"}], execute=[Result()])
    replay = repository.reconcile_validation_policy_for_administration(
        replay_tx,
        policy_revision="policy-v2",
    )
    assert not replay.marker_changed
    assert replay.agents_marked_for_revalidation == 0
    assert [call[0] for call in replay_tx.calls] == ["execute", "one"]


def test_policy_reconciliation_validates_input_marker_and_write_counts() -> None:
    repository = AgentRepository()
    with pytest.raises(RepositoryValidationError, match="policy_revision"):
        repository.reconcile_validation_policy_for_administration(
            ScriptedTransaction(),
            policy_revision="",
        )
    with pytest.raises(RepositoryDataError, match="persisted text"):
        repository.reconcile_validation_policy_for_administration(
            ScriptedTransaction(one=[{"value": 7}], execute=[Result()]),
            policy_revision="policy-v2",
        )
    with pytest.raises(RepositoryDataError, match="invalid row count"):
        repository.reconcile_validation_policy_for_administration(
            ScriptedTransaction(
                one=[None],
                execute=[Result(), Result(rowcount=-1)],
            ),
            policy_revision="policy-v2",
        )
    with pytest.raises(RepositoryDataError, match="marker write"):
        repository.reconcile_validation_policy_for_administration(
            ScriptedTransaction(
                one=[None],
                execute=[Result(), Result(rowcount=0), Result(rowcount=2)],
            ),
            policy_revision="policy-v2",
        )


def test_agent_get_list_tombstone_and_stale_classification() -> None:
    repository = AgentRepository()
    assert (
        repository.get_agent(
            ScriptedTransaction(one=[agent_row()]),
            owner_id=OWNER,
            agent_id=AGENT,
            for_update=True,
        ).owner_id
        == OWNER
    )  # type: ignore[union-attr]
    assert (
        repository.get_agent(ScriptedTransaction(one=[None]), owner_id="other", agent_id=AGENT)
        is None
    )
    listed = repository.list_agents(
        ScriptedTransaction(all_rows=[(agent_row(),)]),
        owner_id=OWNER,
        include_deleted=False,
        limit=1,
    )
    assert listed[0].agent_id == AGENT

    locked = ScriptedTransaction(one=[agent_row()])
    repository.get_agent(locked, owner_id=OWNER, agent_id=AGENT, for_update=True)
    assert locked.fetch_sql().rstrip().endswith("FOR UPDATE")

    administrative = ScriptedTransaction(one=[agent_row()])
    assert repository.get_agent_for_administration(
        administrative,
        agent_id=AGENT,
        for_update=True,
    ).owner_id == OWNER  # type: ignore[union-attr]
    assert administrative.fetch_sql().rstrip().endswith("FOR UPDATE")
    assert "owner_user_id" not in administrative.fetch_sql().split("WHERE", 1)[1]

    tombstoned = repository.tombstone_agent(
        ScriptedTransaction(
            one=[agent_row(status="disabled", deleted_at=9, updated_at=9, state_revision=1)]
        ),
        owner_id=OWNER,
        agent_id=AGENT,
        expected_revision=0,
        deleted_at=9,
    )
    assert tombstoned.deleted_at == 9
    with pytest.raises(RepositoryConflictError, match="tombstone"):
        repository.compare_and_set_agent(
            ScriptedTransaction(one=[None, {"deleted_at": 9, "state_revision": 2}]),
            owner_id=OWNER,
            agent_id=AGENT,
            expected_revision=1,
            updates={"status": "live"},
        )
    with pytest.raises(RepositoryConflictError, match="revision"):
        repository.compare_and_set_agent(
            ScriptedTransaction(one=[None, {"deleted_at": None, "state_revision": 2}]),
            owner_id=OWNER,
            agent_id=AGENT,
            expected_revision=1,
            updates={"status": "live"},
        )


def test_revision_reads_lists_transitions_and_replay_conflicts() -> None:
    repository = AgentRepository()
    assert (
        repository.get_revision(
            ScriptedTransaction(one=[revision_row()]),
            owner_id=OWNER,
            agent_id=AGENT,
            revision_id=REVISION,
            for_update=True,
        ).revision_id
        == REVISION
    )  # type: ignore[union-attr]
    assert (
        repository.get_revision(
            ScriptedTransaction(one=[None]),
            owner_id=OWNER,
            agent_id=AGENT,
            revision_id=REVISION,
        )
        is None
    )
    assert (
        repository.list_revisions(
            ScriptedTransaction(all_rows=[(revision_row(),)]),
            owner_id=OWNER,
            agent_id=AGENT,
            limit=1,
        )[0].revision_number
        == 1
    )

    transitioned = repository.transition_revision(
        ScriptedTransaction(
            one=[revision_row(state="starting", state_revision=1, confirmed_at=NOW)]
        ),
        owner_id=OWNER,
        agent_id=AGENT,
        revision_id=REVISION,
        expected_revision=0,
        expected_state="prepared",
        updates={"state": "starting", "confirmed_at": NOW},
    )
    assert transitioned.confirmed_at == NOW
    with pytest.raises(RepositoryConflictError):
        repository.transition_revision(
            ScriptedTransaction(one=[None]),
            owner_id=OWNER,
            agent_id=AGENT,
            revision_id=REVISION,
            expected_revision=0,
            expected_state="prepared",
            updates={"state": "starting"},
        )
    with pytest.raises(RepositoryConflictError, match="immutable"):
        repository.create_revision(
            ScriptedTransaction(one=[None, revision_row(revision_number=2)]),
            revision_id=REVISION,
            agent_id=AGENT,
            owner_id=OWNER,
            revision_number=1,
            artifact_digest=DIGEST,
            manifest={"contract": 2},
            artifact_relative_path="agents/agent-1/rev-1",
            runtime_contract_version=2,
            release_lock_digest=DIGEST,
            compatibility_state="compatible",
            state="prepared",
            promotion_token=SCOPE,
        )
    with pytest.raises(RepositoryConflictError, match="immutable"):
        repository.create_revision(
            ScriptedTransaction(one=[None, revision_row()]),
            revision_id=REVISION,
            agent_id=AGENT,
            owner_id=OWNER,
            revision_number=1,
            previous_good_revision_id=SCOPE,
            artifact_digest=DIGEST,
            manifest={"contract": 2},
            artifact_relative_path="agents/agent-1/rev-1",
            runtime_contract_version=2,
            release_lock_digest=DIGEST,
            compatibility_state="compatible",
            state="prepared",
            promotion_token=SCOPE,
        )

    advanced = repository.create_revision(
        ScriptedTransaction(
            one=[None, revision_row(state="active", state_revision=3, promoted_at=NOW)]
        ),
        revision_id=REVISION,
        agent_id=AGENT,
        owner_id=OWNER,
        revision_number=1,
        artifact_digest=DIGEST,
        manifest={"contract": 2},
        artifact_relative_path="agents/agent-1/rev-1",
        runtime_contract_version=2,
        release_lock_digest=DIGEST,
        compatibility_state="compatible",
        state="prepared",
        promotion_token=SCOPE,
    )
    assert advanced.state == "active"


def test_host_session_reads_lists_transitions_and_replay_conflicts() -> None:
    repository = AgentRepository()
    assert (
        repository.get_host_session(
            ScriptedTransaction(one=[host_row()]),
            owner_id=OWNER,
            host_session_id=SESSION,
            for_update=True,
        ).host_id
        == HOST
    )  # type: ignore[union-attr]
    assert (
        repository.get_host_session(
            ScriptedTransaction(one=[None]), owner_id=OWNER, host_session_id=SESSION
        )
        is None
    )
    assert (
        repository.list_host_sessions(
            ScriptedTransaction(all_rows=[(host_row(),)]),
            owner_id=OWNER,
            state="connected",
            host_id=HOST,
            inventory_state="pending",
            for_update=True,
            limit=1,
        )[0].state
        == "connected"
    )
    transitioned = repository.transition_host_session(
        ScriptedTransaction(one=[host_row(state="disconnected", disconnected_at=NOW)]),
        owner_id=OWNER,
        host_session_id=SESSION,
        expected_state="connected",
        updates={"state": "disconnected", "disconnected_at": NOW},
    )
    assert transitioned.disconnected_at == NOW
    with pytest.raises(RepositoryConflictError):
        repository.transition_host_session(
            ScriptedTransaction(one=[None]),
            owner_id=OWNER,
            host_session_id=SESSION,
            expected_state="connected",
            updates={"state": "disconnected"},
        )
    with pytest.raises(RepositoryConflictError, match="immutable"):
        repository.create_host_session(
            ScriptedTransaction(one=[None, host_row(host_generation=2)]),
            host_session_id=SESSION,
            host_id=HOST,
            owner_id=OWNER,
            connection_scope_id=SCOPE,
            platform="windows",
            client_version="1.2.3",
            host_generation=1,
            supported_runtime_contract_versions=(2,),
            runtime_contract_version=2,
            release_lock_digest=DIGEST,
            eligible_since=NOW,
            accepted_at=NOW,
            last_seen_at=NOW,
        )


@pytest.mark.parametrize(
    "persisted",
    [
        host_row(eligible_since=NOW + timedelta(seconds=1)),
        host_row(accepted_at=NOW + timedelta(seconds=1)),
    ],
)
def test_host_session_replay_fences_immutable_admission_chronology(
    persisted: dict[str, object],
) -> None:
    with pytest.raises(RepositoryConflictError, match="immutable"):
        AgentRepository().create_host_session(
            ScriptedTransaction(one=[None, persisted]),
            host_session_id=SESSION,
            host_id=HOST,
            owner_id=OWNER,
            connection_scope_id=SCOPE,
            platform="windows",
            client_version="1.2.3",
            host_generation=1,
            supported_runtime_contract_versions=(2,),
            runtime_contract_version=2,
            release_lock_digest=DIGEST,
            eligible_since=NOW,
            accepted_at=NOW,
            last_seen_at=NOW,
        )


def test_host_session_exact_replay_allows_mutable_liveness_to_advance() -> None:
    advanced = AgentRepository().create_host_session(
        ScriptedTransaction(
            one=[
                None,
                host_row(
                    state="disconnected",
                    inventory_state="reconciled",
                    last_seen_at=NOW + timedelta(minutes=1),
                    disconnected_at=NOW + timedelta(minutes=1),
                ),
            ]
        ),
        host_session_id=SESSION,
        host_id=HOST,
        owner_id=OWNER,
        connection_scope_id=SCOPE,
        platform="windows",
        client_version="1.2.3",
        host_generation=1,
        supported_runtime_contract_versions=(2,),
        runtime_contract_version=2,
        release_lock_digest=DIGEST,
        eligible_since=NOW,
        accepted_at=NOW,
        last_seen_at=NOW,
    )
    assert advanced.state == "disconnected"
    assert advanced.last_seen_at == NOW + timedelta(minutes=1)


def test_runtime_reads_lists_authority_transition_and_replay_conflicts() -> None:
    repository = AgentRepository()
    assert (
        repository.get_runtime_instance(
            ScriptedTransaction(one=[runtime_row()]),
            owner_id=OWNER,
            runtime_instance_id=RUNTIME,
            for_update=True,
        ).runtime_instance_id
        == RUNTIME
    )  # type: ignore[union-attr]
    assert (
        repository.get_runtime_instance(
            ScriptedTransaction(one=[None]), owner_id=OWNER, runtime_instance_id=RUNTIME
        )
        is None
    )
    assert (
        repository.list_runtime_instances(
            ScriptedTransaction(all_rows=[(runtime_row(),)]),
            owner_id=OWNER,
            agent_id=AGENT,
            host_session_id=SESSION,
            states=("delivering",),
            for_update=True,
            limit=1,
        )[0].agent_id
        == AGENT
    )
    assert repository.get_authoritative_runtime(
        ScriptedTransaction(one=[runtime_row(is_authoritative=True)]),
        owner_id=OWNER,
        agent_id=AGENT,
    ).is_authoritative  # type: ignore[union-attr]
    transitioned = repository.transition_runtime_instance(
        ScriptedTransaction(
            one=[
                runtime_row(
                    state="starting",
                    process_id=SCOPE,
                    started_at=NOW,
                    state_revision=1,
                )
            ]
        ),
        owner_id=OWNER,
        runtime_instance_id=RUNTIME,
        expected_revision=0,
        expected_states=("delivering",),
        updates={"state": "starting", "process_id": SCOPE, "started_at": NOW},
    )
    assert transitioned.process_id == SCOPE
    with pytest.raises(RepositoryConflictError, match="immutable"):
        repository.create_runtime_instance(
            ScriptedTransaction(one=[None, runtime_row(lifecycle_generation=2)]),
            runtime_instance_id=RUNTIME,
            agent_id=AGENT,
            owner_id=OWNER,
            host_id=HOST,
            host_session_id=SESSION,
            delivery_id=DELIVERY,
            revision_id=REVISION,
            lifecycle_generation=1,
            runtime_contract_version=2,
            operation_execution_generation=1,
        )


def test_runtime_administrative_resolution_and_latest_owner_projection() -> None:
    repository = AgentRepository()
    resolved = ScriptedTransaction(one=[runtime_row()])
    assert repository.get_runtime_instance_for_administration(
        resolved,
        runtime_instance_id=RUNTIME,
        for_update=True,
    ).owner_id == OWNER  # type: ignore[union-attr]
    assert "owner_user_id" not in resolved.fetch_sql().split("WHERE", 1)[1]
    assert resolved.fetch_sql().rstrip().endswith("FOR UPDATE")

    latest_query = ScriptedTransaction(all_rows=[(runtime_row(),)])
    latest = repository.list_latest_runtime_instances(
        latest_query,
        owner_id=OWNER,
        limit=20,
    )
    assert latest[0].runtime_instance_id == RUNTIME
    assert "DISTINCT ON (agent_id)" in latest_query.fetch_sql()
    assert latest_query.calls[0][2] == (OWNER, 20)


def test_runtime_request_reads_transitions_and_replay_conflicts() -> None:
    repository = AgentRepository()
    assert (
        repository.get_runtime_request(
            ScriptedTransaction(one=[request_row()]),
            owner_id=OWNER,
            request_id=REQUEST,
            for_update=True,
        ).request_generation
        == REQUEST_GENERATION
    )  # type: ignore[union-attr]
    assert (
        repository.get_runtime_request(
            ScriptedTransaction(one=[None]), owner_id=OWNER, request_id=REQUEST
        )
        is None
    )
    transitioned = repository.transition_runtime_request(
        ScriptedTransaction(
            one=[
                request_row(
                    state="completed",
                    state_revision=1,
                    terminal_at=NOW,
                    terminal_code="ok",
                    result_digest=DIGEST,
                )
            ]
        ),
        owner_id=OWNER,
        request_id=REQUEST,
        expected_revision=0,
        expected_states=("assigned", "running"),
        updates={
            "state": "completed",
            "terminal_at": NOW,
            "terminal_code": "ok",
            "result_digest": DIGEST,
        },
    )
    assert transitioned.result_digest == DIGEST
    with pytest.raises(RepositoryConflictError):
        repository.transition_runtime_request(
            ScriptedTransaction(one=[None]),
            owner_id=OWNER,
            request_id=REQUEST,
            expected_revision=0,
            expected_states=("assigned",),
            updates={"state": "running"},
        )
    with pytest.raises(RepositoryConflictError, match="immutable"):
        repository.create_runtime_request(
            ScriptedTransaction(one=[None, request_row(request_generation=str(uuid.uuid4()))]),
            request_id=REQUEST,
            request_generation=REQUEST_GENERATION,
            runtime_instance_id=RUNTIME,
            agent_id=AGENT,
            owner_id=OWNER,
            operation_execution_generation=1,
        )


def test_runtime_request_administrative_resolution_and_owner_listing() -> None:
    repository = AgentRepository()
    resolved = ScriptedTransaction(one=[request_row()])
    assert repository.get_runtime_request_for_administration(
        resolved,
        request_id=REQUEST,
        for_update=True,
    ).owner_id == OWNER  # type: ignore[union-attr]
    assert "owner_user_id" not in resolved.fetch_sql().split("WHERE", 1)[1]

    listed_query = ScriptedTransaction(all_rows=[(request_row(),)])
    listed = repository.list_runtime_requests(
        listed_query,
        owner_id=OWNER,
        runtime_instance_id=RUNTIME,
        states=("assigned", "running"),
        for_update=True,
        limit=10,
    )
    assert listed[0].request_id == REQUEST
    assert "owner_user_id = %s" in listed_query.fetch_sql()
    assert "runtime_instance_id = %s::uuid" in listed_query.fetch_sql()
    assert listed_query.fetch_sql().rstrip().endswith("FOR UPDATE")


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        (
            "create_host_session",
            {
                "host_session_id": SESSION,
                "host_id": HOST,
                "owner_id": OWNER,
                "connection_scope_id": SCOPE,
                "platform": "windows",
                "client_version": "1.2.3",
                "host_generation": 1,
                "supported_runtime_contract_versions": (1,),
                "runtime_contract_version": 2,
                "release_lock_digest": DIGEST,
                "eligible_since": NOW,
                "accepted_at": NOW,
                "last_seen_at": NOW,
            },
        ),
        (
            "transition_runtime_instance",
            {
                "owner_id": OWNER,
                "runtime_instance_id": RUNTIME,
                "expected_revision": 0,
                "expected_states": (),
                "updates": {"state": "starting"},
            },
        ),
        (
            "transition_runtime_request",
            {
                "owner_id": OWNER,
                "request_id": REQUEST,
                "expected_revision": 0,
                "expected_states": (),
                "updates": {"state": "running"},
            },
        ),
    ],
)
def test_lifecycle_input_validation(method: str, kwargs: dict[str, object]) -> None:
    with pytest.raises(RepositoryValidationError):
        getattr(AgentRepository(), method)(ScriptedTransaction(), **kwargs)


def test_runtime_expiry_selectors_use_database_time_and_lock_owner_rows() -> None:
    repository = AgentRepository()
    startup_tx = ScriptedTransaction(one=[runtime_row()])
    startup = repository.lock_runtime_if_startup_expired(
        startup_tx,
        owner_id=OWNER,
        runtime_instance_id=RUNTIME,
        timeout_seconds=30,
    )
    assert startup is not None and startup.state == "delivering"
    assert "COALESCE(started_at, created_at)" in startup_tx.fetch_sql()
    assert "clock_timestamp()" in startup_tx.fetch_sql()
    assert "FOR UPDATE" in startup_tx.fetch_sql()
    assert startup_tx.calls[0][2] == (RUNTIME, OWNER, 30.0)

    liveness_tx = ScriptedTransaction(
        one=[runtime_row(state="online", last_liveness_at=NOW)]
    )
    liveness = repository.lock_runtime_if_liveness_expired(
        liveness_tx,
        owner_id=OWNER,
        runtime_instance_id=RUNTIME,
        timeout_seconds=5,
    )
    assert liveness is not None and liveness.state == "online"
    assert "last_liveness_at IS NOT NULL" in liveness_tx.fetch_sql()
    assert "clock_timestamp()" in liveness_tx.fetch_sql()
    assert liveness_tx.calls[0][2] == (RUNTIME, OWNER, 5.0)

    assert (
        repository.lock_runtime_if_startup_expired(
            ScriptedTransaction(one=[None]),
            owner_id="another-owner",
            runtime_instance_id=RUNTIME,
            timeout_seconds=1,
        )
        is None
    )


@pytest.mark.parametrize(
    ("method_name", "timeout"),
    (
        ("lock_runtime_if_startup_expired", 0),
        ("lock_runtime_if_startup_expired", 301),
        ("lock_runtime_if_liveness_expired", True),
        ("lock_runtime_if_liveness_expired", 61),
    ),
)
def test_runtime_expiry_selectors_reject_unbounded_timeouts(
    method_name: str,
    timeout: object,
) -> None:
    with pytest.raises(RepositoryValidationError, match="timeout_seconds"):
        getattr(AgentRepository(), method_name)(
            ScriptedTransaction(),
            owner_id=OWNER,
            runtime_instance_id=RUNTIME,
            timeout_seconds=timeout,
        )


def test_admin_runtime_expiry_discovery_is_bounded_and_uses_database_time() -> None:
    transaction = ScriptedTransaction(
        all_rows=[
            (
                {
                    "runtime_instance_id": RUNTIME,
                    "owner_user_id": OWNER,
                    "state": "starting",
                    "expiry_reason": "startup",
                },
                {
                    "runtime_instance_id": REQUEST,
                    "owner_user_id": "owner-2",
                    "state": "online",
                    "expiry_reason": "liveness",
                },
            )
        ]
    )
    candidates = AgentRepository().list_expired_runtime_candidates_for_administration(
        transaction,
        startup_timeout_seconds=30,
        liveness_timeout_seconds=5,
        limit=100,
    )
    assert tuple(candidate.reason for candidate in candidates) == (
        "startup",
        "liveness",
    )
    assert candidates[1].owner_id == "owner-2"
    assert transaction.calls[0][2] == (30.0, 5.0, 100)
    sql = transaction.fetch_sql()
    assert "clock_timestamp()" in sql
    assert "ORDER BY runtime_instance_id" in sql
    assert "FOR UPDATE" not in sql

    corrupt = ScriptedTransaction(
        all_rows=[
            (
                {
                    "runtime_instance_id": RUNTIME,
                    "owner_user_id": OWNER,
                    "state": "ready",
                    "expiry_reason": "startup",
                },
            )
        ]
    )
    with pytest.raises(RepositoryDataError, match="invalid persisted semantics"):
        AgentRepository().list_expired_runtime_candidates_for_administration(
            corrupt,
            startup_timeout_seconds=30,
            liveness_timeout_seconds=5,
        )


@pytest.mark.parametrize(
    ("startup_timeout", "liveness_timeout", "limit"),
    ((0, 5, 100), (30, 61, 100), (30, 5, 2001)),
)
def test_admin_runtime_expiry_discovery_rejects_unbounded_inputs(
    startup_timeout: object,
    liveness_timeout: object,
    limit: object,
) -> None:
    with pytest.raises(RepositoryValidationError):
        AgentRepository().list_expired_runtime_candidates_for_administration(
            ScriptedTransaction(),
            startup_timeout_seconds=startup_timeout,
            liveness_timeout_seconds=liveness_timeout,
            limit=limit,
        )
