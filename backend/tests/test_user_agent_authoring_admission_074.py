"""Atomic authoring admission and optional online-authority tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import threading
from types import SimpleNamespace
import uuid

import pytest

from orchestrator import user_agents
from orchestrator.user_agents import (
    AgentDeletedError,
    AgentOfflineError,
    PersonalAgentNotFoundError,
    PersonalAgentRuntimeRepository,
    RuntimeCompatibilityPolicy,
    StaleRuntimeGenerationError,
    UserAgentOwnershipConflict,
    UserAgentRegistry,
)
from tests.helpers.user_agent_registry import (
    InMemoryAgentRepository,
    make_user_agent_registry_with_repository,
)


_EXPECTED_CONSTITUTION = "agent-constitution-v1"


def _admit(
    registry: UserAgentRegistry,
    **overrides: object,
):
    values = {
        "agent_id": "agent-a",
        "owner_user_id": "owner-a",
        "display_name": "Agent A",
        "draft_id": "draft-a",
        "expected_constitution_version": _EXPECTED_CONSTITUTION,
        "owner_email": "owner-a@example.test",
        "declared_tools": ["lookup", "summarize"],
        "declared_scopes": ["tools:read", "records:read"],
        "declared_egress": ["api.example.test"],
    }
    values.update(overrides)
    return user_agents.admit_authoring_target(registry, **values)


def test_new_target_concurrent_replay_creates_once_without_cas() -> None:
    registry, repository = make_user_agent_registry_with_repository()
    worker_count = 12
    barrier = threading.Barrier(worker_count)

    def admit_once(_worker: int):
        barrier.wait()
        return _admit(registry)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        rows = tuple(executor.map(admit_once, range(worker_count)))

    assert rows == (rows[0],) * worker_count
    assert rows[0].status == "authoring"
    assert rows[0].constitution_version is None
    assert rows[0].state_revision == 0
    assert repository.records == {"agent-a": rows[0]}
    assert repository.compare_and_set_calls == 0
    assert repository.locked_owner_ids == ["owner-a"] * worker_count


def test_active_new_target_replay_and_revision_admission_are_read_only() -> None:
    registry, repository = make_user_agent_registry_with_repository()
    created = _admit(registry)
    incumbent = replace(
        created,
        status="live",
        constitution_version=_EXPECTED_CONSTITUTION,
        validated_at=10,
        active_revision_id=str(uuid.uuid4()),
        authoritative_instance_id=str(uuid.uuid4()),
        lifecycle_generation=4,
        generation_counter=4,
        state_revision=9,
        updated_at=20,
    )
    repository.records[incumbent.agent_id] = incumbent

    assert _admit(registry, owner_email="new-address@example.test") == incumbent
    assert _admit(
        registry,
        revises_agent_id="agent-a",
        display_name="Renamed revision",
        draft_id="revision-draft",
        declared_tools=["write"],
        declared_scopes=["records:write"],
        declared_egress=None,
    ) == incumbent
    assert repository.records[incumbent.agent_id] == incumbent
    assert repository.compare_and_set_calls == 0


@pytest.mark.parametrize(
    ("record_changes", "request_changes"),
    (
        ({"display_name": "Different"}, {}),
        ({"draft_id": "different-draft"}, {}),
        ({"declared_tools": ("different",)}, {}),
        ({"declared_scopes": ("different",)}, {}),
        ({"declared_egress": ("different.example",)}, {}),
        ({"status": "validated"}, {}),
        (
            {
                "status": "live",
                "constitution_version": "different-constitution",
            },
            {},
        ),
        ({}, {"display_name": "Changed on replay"}),
    ),
)
def test_new_target_replay_rejects_changed_semantics(
    record_changes: dict[str, object],
    request_changes: dict[str, object],
) -> None:
    registry, repository = make_user_agent_registry_with_repository()
    created = _admit(registry)
    repository.records[created.agent_id] = replace(created, **record_changes)

    with pytest.raises(
        StaleRuntimeGenerationError,
        match="authoring target replay changed semantics",
    ):
        _admit(registry, **request_changes)
    assert repository.compare_and_set_calls == 0


@pytest.mark.parametrize("revision", (False, True))
def test_authoring_admission_denies_cross_owner_and_tombstone(
    revision: bool,
) -> None:
    registry, _repository = make_user_agent_registry_with_repository()
    _admit(registry)
    revision_values = {"revises_agent_id": "agent-a"} if revision else {}

    with pytest.raises(UserAgentOwnershipConflict):
        _admit(registry, owner_user_id="owner-b", **revision_values)

    registry.soft_delete("agent-a")
    with pytest.raises(AgentDeletedError):
        _admit(registry, **revision_values)


def test_revision_admission_requires_the_exact_existing_target() -> None:
    registry, repository = make_user_agent_registry_with_repository()

    with pytest.raises(PersonalAgentNotFoundError):
        _admit(registry, revises_agent_id="agent-a")
    with pytest.raises(ValueError, match="must equal the authoring target"):
        _admit(registry, revises_agent_id="another-agent")
    assert repository.records == {}


class _AuthorityRepository(InMemoryAgentRepository):
    def __init__(self) -> None:
        super().__init__()
        self.runtimes: dict[str, object] = {}
        self.hosts: dict[str, object] = {}
        self.revisions: dict[str, object] = {}
        self.read_error: BaseException | None = None

    def get_agent(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
        for_update=False,
    ):
        del for_update
        if self.read_error is not None:
            raise self.read_error
        record = self.records.get(agent_id)
        return record if record is not None and record.owner_id == owner_id else None

    def get_runtime_instance(
        self,
        _transaction,
        *,
        owner_id,
        runtime_instance_id,
        for_update=False,
    ):
        del for_update
        record = self.runtimes.get(runtime_instance_id)
        return record if record is not None and record.owner_id == owner_id else None

    def get_host_session(
        self,
        _transaction,
        *,
        owner_id,
        host_session_id,
        for_update=False,
    ):
        del for_update
        record = self.hosts.get(host_session_id)
        return record if record is not None and record.owner_id == owner_id else None

    def get_revision(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
        revision_id,
        for_update=False,
    ):
        del for_update
        record = self.revisions.get(revision_id)
        if record is None:
            return None
        return (
            record
            if record.owner_id == owner_id and record.agent_id == agent_id
            else None
        )


class _AuthorityRuntime:
    def __init__(self, repository: _AuthorityRepository) -> None:
        self.repositories = SimpleNamespace(agents=repository)
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            yield object()


def _authority_seam():
    repository = _AuthorityRepository()
    runtime = _AuthorityRuntime(repository)
    policy = RuntimeCompatibilityPolicy(
        runtime_contract_version=1,
        runtime_lock_sha256="a" * 64,
    )
    registry = UserAgentRegistry(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    authority = PersonalAgentRuntimeRepository(
        compatibility_policy=policy,
        operation_repository=object(),
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    return registry, authority, repository, policy


def test_optional_online_authority_distinguishes_clean_absence_and_identity() -> None:
    registry, authority, repository, _policy = _authority_seam()
    _admit(registry)

    assert authority.get_current_online_authority_if_present(
        owner_user_id="owner-a",
        agent_id="agent-a",
    ) is None
    with pytest.raises(AgentOfflineError, match="no online authority"):
        authority.get_current_online_authority(
            owner_user_id="owner-a",
            agent_id="agent-a",
        )
    with pytest.raises(PersonalAgentNotFoundError):
        authority.get_current_online_authority_if_present(
            owner_user_id="owner-a",
            agent_id="missing",
        )

    registry.soft_delete("agent-a")
    with pytest.raises(AgentDeletedError):
        authority.get_current_online_authority_if_present(
            owner_user_id="owner-a",
            agent_id="agent-a",
        )
    assert repository.records["agent-a"].deleted_at is not None


def test_optional_online_authority_rejects_broken_pointer_and_propagates_reads() -> None:
    registry, authority, repository, _policy = _authority_seam()
    created = _admit(registry)
    repository.records[created.agent_id] = replace(
        created,
        authoritative_instance_id=str(uuid.uuid4()),
    )

    with pytest.raises(AgentOfflineError, match="no exact online authority"):
        authority.get_current_online_authority_if_present(
            owner_user_id="owner-a",
            agent_id="agent-a",
        )

    read_error = RuntimeError("plane read failed")
    repository.read_error = read_error
    with pytest.raises(RuntimeError) as raised:
        authority.get_current_online_authority_if_present(
            owner_user_id="owner-a",
            agent_id="agent-a",
        )
    assert raised.value is read_error


def test_optional_online_authority_returns_only_the_exact_runtime() -> None:
    registry, authority, repository, policy = _authority_seam()
    created = _admit(registry)
    host_id = str(uuid.uuid4())
    host_session_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    runtime_instance_id = str(uuid.uuid4())
    runtime = SimpleNamespace(
        runtime_instance_id=runtime_instance_id,
        agent_id=created.agent_id,
        owner_id=created.owner_id,
        host_id=host_id,
        host_session_id=host_session_id,
        delivery_id=str(uuid.uuid4()),
        revision_id=revision_id,
        process_id=str(uuid.uuid4()),
        lifecycle_generation=3,
        runtime_contract_version=policy.runtime_contract_version,
        operation_id=None,
        operation_execution_generation=0,
        state="online",
        is_authoritative=True,
        state_revision=7,
        created_at=None,
        started_at=None,
        registered_at=None,
        last_heartbeat_sequence=1,
        ready_at=None,
        last_liveness_at=None,
        terminal_at=None,
        failure_code=None,
    )
    repository.runtimes[runtime_instance_id] = runtime
    repository.hosts[host_session_id] = SimpleNamespace(
        host_session_id=host_session_id,
        host_id=host_id,
        owner_id=created.owner_id,
        state="connected",
        inventory_state="reconciled",
        runtime_contract_version=policy.runtime_contract_version,
        release_lock_digest=policy.runtime_lock_sha256,
    )
    repository.revisions[revision_id] = SimpleNamespace(
        revision_id=revision_id,
        agent_id=created.agent_id,
        owner_id=created.owner_id,
        state="active",
        compatibility_state="compatible",
        runtime_contract_version=policy.runtime_contract_version,
        release_lock_digest=policy.runtime_lock_sha256,
    )
    repository.records[created.agent_id] = replace(
        created,
        status="live",
        constitution_version=_EXPECTED_CONSTITUTION,
        active_revision_id=revision_id,
        selected_host_session_id=host_session_id,
        authoritative_instance_id=runtime_instance_id,
        lifecycle_generation=3,
    )

    resolved = authority.get_current_online_authority_if_present(
        owner_user_id="owner-a",
        agent_id="agent-a",
    )
    assert resolved is not None
    assert resolved.fence.runtime_instance_id == runtime_instance_id
    assert authority.get_current_online_authority(
        owner_user_id="owner-a",
        agent_id="agent-a",
    ) == resolved
