from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import threading
from types import SimpleNamespace
import uuid

import pytest
from astralplane.authority import (
    AgentAuthorityBinding,
    AuthorityBindingState,
    AuthorityPopulation,
)

from orchestrator.lets_lifecycle import (
    GovernedLifecycleCoordinator,
    GovernedRuntime,
    LifecycleConvergence,
    LetsLifecycleError,
    LetsLifecycleService,
)
from orchestrator.user_agents import GovernedByoAgentLifecycle, RuntimeInstanceRecord
from shared.protocol import RuntimeFence


_DIGEST = "sha256:" + "1" * 64


def _binding(
    *,
    state: AuthorityBindingState = AuthorityBindingState.ACTIVE,
    generation: int = 1,
    runtime_id: str = "runtime-a",
    config_epoch: int = 7,
    expires_at_ns: int = 10_000,
) -> AgentAuthorityBinding:
    now = datetime.now(UTC)
    return AgentAuthorityBinding(
        binding_id=f"binding-{generation}",
        owner_id="owner-a",
        agent_id="agent-a",
        runtime_id=runtime_id,
        runtime_generation=generation,
        population=AuthorityPopulation.SERVER_DYNAMIC,
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        warden_id="warden-a",
        lease_id=f"lease-{generation}",
        lineage_id="lineage-a",
        subject_id="subject-a",
        policy_digest=_DIGEST,
        machine_digest=_DIGEST,
        config_epoch=config_epoch,
        capabilities=("astral.tools.read",),
        lease_sequence=0,
        lease_expires_at_ns=expires_at_ns,
        state=state,
        created_at=now,
        updated_at=now,
        version=0,
    )


class RecordingLifecycleService(LetsLifecycleService):
    def __init__(
        self,
        *,
        mode: str = "enforce",
        binding: AgentAuthorityBinding | None = None,
        config_epoch: int = 7,
    ) -> None:
        self.config = SimpleNamespace(
            mode=mode,
            governed_cohorts=("server_dynamic", "byo_user"),
            governed_agent_allowlist=(),
            trust_manifest=SimpleNamespace(config_epoch=config_epoch),
        )
        self.binding = binding
        self.events: list[tuple[str, object]] = []

    async def provision(
        self,
        runtime: GovernedRuntime,
        *,
        binding_id: str,
        operation_id: str,
    ) -> LifecycleConvergence:
        self.events.append(("provision", (runtime, binding_id, operation_id)))
        self.binding = replace(
            _binding(
                generation=runtime.runtime_generation,
                runtime_id=runtime.runtime_id,
                config_epoch=self.config.trust_manifest.config_epoch,
            ),
            binding_id=binding_id,
            owner_id=runtime.owner_id,
            agent_id=runtime.agent_id,
            population=runtime.population,
        )
        return LifecycleConvergence(
            protected=self.config.mode == "enforce",
            binding=self.binding,
        )

    async def close(
        self,
        *,
        owner_id: str,
        binding_id: str,
        operation_id: str,
    ) -> LifecycleConvergence:
        self.events.append(("close", (owner_id, binding_id, operation_id)))
        assert self.binding is not None
        self.binding = replace(
            self.binding,
            state=AuthorityBindingState.CLOSED,
            version=self.binding.version + 1,
        )
        return LifecycleConvergence(
            protected=self.config.mode == "enforce",
            binding=self.binding,
        )

    async def quiesce(self, **values: str) -> LifecycleConvergence:
        self.events.append(("quiesce", values))
        assert self.binding is not None
        self.binding = replace(
            self.binding,
            state=AuthorityBindingState.QUIESCENT,
            version=self.binding.version + 1,
        )
        return LifecycleConvergence(True, binding=self.binding)

    async def resume(self, **values: str) -> LifecycleConvergence:
        self.events.append(("resume", values))
        assert self.binding is not None
        self.binding = replace(
            self.binding,
            state=AuthorityBindingState.ACTIVE,
            version=self.binding.version + 1,
        )
        return LifecycleConvergence(True, binding=self.binding)

    async def renew(self, **values: str) -> LifecycleConvergence:
        self.events.append(("renew", values))
        assert self.binding is not None
        self.binding = replace(
            self.binding,
            lease_expires_at_ns=self.binding.lease_expires_at_ns + 10_000,
            version=self.binding.version + 1,
        )
        return LifecycleConvergence(True, binding=self.binding)

    async def revoke(
        self,
        *,
        owner_id: str,
        binding_id: str,
        operation_id: str,
        reason_code: str,
    ) -> LifecycleConvergence:
        self.events.append(
            ("revoke", (owner_id, binding_id, operation_id, reason_code))
        )
        assert self.binding is not None
        self.binding = replace(
            self.binding,
            state=AuthorityBindingState.REVOKED,
            version=self.binding.version + 1,
        )
        return LifecycleConvergence(True, binding=self.binding)


class RecordingCoordinator(GovernedLifecycleCoordinator):
    def latest_binding(self, **_values: object) -> AgentAuthorityBinding | None:
        service = self.service
        assert isinstance(service, RecordingLifecycleService)
        return service.binding


def _coordinator(
    service: RecordingLifecycleService,
) -> RecordingCoordinator:
    identities = iter(
        f"00000000-0000-4000-8000-{number:012d}" for number in range(1, 50)
    )
    return RecordingCoordinator(service, identifier_factory=lambda: next(identities))


class BlockingLookupCoordinator(RecordingCoordinator):
    """Hold the synchronous Plane lookup until the event loop advances."""

    def __init__(self, service: RecordingLifecycleService) -> None:
        super().__init__(service, identifier_factory=lambda: str(uuid.uuid4()))
        self.lookup_entered = threading.Event()
        self.lookup_release = threading.Event()
        self.lookup_thread_id: int | None = None
        self.released_before_return = False

    def latest_binding(self, **values: object) -> AgentAuthorityBinding | None:
        self.lookup_thread_id = threading.get_ident()
        self.lookup_entered.set()
        self.released_before_return = self.lookup_release.wait(timeout=2.0)
        return super().latest_binding(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller",
    ("admit_new_runtime", "admit_or_resume_runtime", "_current_or_none"),
)
async def test_every_async_latest_binding_caller_runs_off_event_loop(
    caller: str,
) -> None:
    service = RecordingLifecycleService(binding=None)
    coordinator = BlockingLookupCoordinator(service)
    event_loop_thread_id = threading.get_ident()
    ticker_thread_id: int | None = None

    async def release_after_loop_progress() -> None:
        nonlocal ticker_thread_id
        while not coordinator.lookup_entered.is_set():
            await asyncio.sleep(0)
        ticker_thread_id = threading.get_ident()
        coordinator.lookup_release.set()

    if caller == "admit_new_runtime":
        invocation = coordinator.admit_new_runtime(
            owner_id="owner-a",
            agent_id="agent-a",
            runtime_id="runtime-a",
            population=AuthorityPopulation.SERVER_DYNAMIC,
            declared_scopes=("tools:read",),
        )
    elif caller == "admit_or_resume_runtime":
        invocation = coordinator.admit_or_resume_runtime(
            GovernedRuntime(
                owner_id="owner-a",
                agent_id="agent-a",
                runtime_id="runtime-a",
                runtime_generation=1,
                population=AuthorityPopulation.SERVER_DYNAMIC,
                declared_scopes=("tools:read",),
            )
        )
    else:
        invocation = coordinator._current_or_none(
            "owner-a",
            "agent-a",
            AuthorityPopulation.SERVER_DYNAMIC,
        )

    await asyncio.wait_for(
        asyncio.gather(invocation, release_after_loop_progress()),
        timeout=5.0,
    )

    assert coordinator.released_before_return is True
    assert coordinator.lookup_thread_id is not None
    assert coordinator.lookup_thread_id != event_loop_thread_id
    assert ticker_thread_id == event_loop_thread_id


@pytest.mark.asyncio
async def test_new_runtime_closes_predecessor_and_advances_plane_generation() -> None:
    service = RecordingLifecycleService(binding=_binding())
    coordinator = _coordinator(service)

    result = await coordinator.admit_new_runtime(
        owner_id="owner-a",
        agent_id="agent-a",
        runtime_id="runtime-b",
        population=AuthorityPopulation.SERVER_DYNAMIC,
        declared_scopes=("tools:read",),
    )

    assert [event for event, _ in service.events] == ["close", "provision"]
    assert result.binding is not None
    assert result.binding.runtime_generation == 2
    assert result.binding.runtime_id == "runtime-b"
    assert result.binding.state is AuthorityBindingState.ACTIVE


@pytest.mark.asyncio
async def test_exact_quiesced_reconnect_resumes_same_generation() -> None:
    service = RecordingLifecycleService(
        binding=_binding(state=AuthorityBindingState.QUIESCENT)
    )
    coordinator = _coordinator(service)

    result = await coordinator.admit_or_resume_runtime(
        GovernedRuntime(
            owner_id="owner-a",
            agent_id="agent-a",
            runtime_id="runtime-a",
            runtime_generation=1,
            population=AuthorityPopulation.SERVER_DYNAMIC,
            declared_scopes=("tools:read",),
        )
    )

    assert [event for event, _ in service.events] == ["resume"]
    assert result.binding is not None
    assert result.binding.state is AuthorityBindingState.ACTIVE
    assert result.binding.runtime_generation == 1


@pytest.mark.asyncio
async def test_stale_or_epoch_mismatched_generation_never_reopens() -> None:
    service = RecordingLifecycleService(binding=_binding(config_epoch=6))
    coordinator = _coordinator(service)
    runtime = GovernedRuntime(
        owner_id="owner-a",
        agent_id="agent-a",
        runtime_id="runtime-a",
        runtime_generation=1,
        population=AuthorityPopulation.SERVER_DYNAMIC,
        declared_scopes=("tools:read",),
    )

    with pytest.raises(LetsLifecycleError, match="stale_runtime_generation"):
        await coordinator.admit_or_resume_runtime(runtime)

    assert service.events == []


@pytest.mark.asyncio
async def test_pause_renew_retire_and_compromise_map_to_exact_mutations() -> None:
    service = RecordingLifecycleService(binding=_binding(expires_at_ns=10_000))
    coordinator = _coordinator(service)

    not_due = await coordinator.renew_current_if_due(
        owner_id="owner-a",
        agent_id="agent-a",
        population=AuthorityPopulation.SERVER_DYNAMIC,
        now_ns=1_000,
        renewal_window_ns=1_000,
    )
    assert not_due.binding is service.binding
    assert service.events == []

    await coordinator.renew_current_if_due(
        owner_id="owner-a",
        agent_id="agent-a",
        population=AuthorityPopulation.SERVER_DYNAMIC,
        now_ns=9_500,
        renewal_window_ns=1_000,
    )
    await coordinator.quiesce_current(
        owner_id="owner-a",
        agent_id="agent-a",
        population=AuthorityPopulation.SERVER_DYNAMIC,
    )
    await coordinator.close_current(
        owner_id="owner-a",
        agent_id="agent-a",
        population=AuthorityPopulation.SERVER_DYNAMIC,
    )
    assert [event for event, _ in service.events] == ["renew", "quiesce", "close"]

    service.binding = _binding(generation=2, runtime_id="runtime-b")
    await coordinator.revoke_current(
        owner_id="owner-a",
        agent_id="agent-a",
        population=AuthorityPopulation.SERVER_DYNAMIC,
        reason_code="security_compromise",
    )
    assert service.events[-1][0] == "revoke"
    assert service.binding.state is AuthorityBindingState.REVOKED


@pytest.mark.asyncio
async def test_exact_retirement_never_closes_a_newer_successor() -> None:
    service = RecordingLifecycleService(
        binding=_binding(generation=2, runtime_id="runtime-b")
    )
    coordinator = _coordinator(service)

    superseded = await coordinator.close_runtime_generation(
        owner_id="owner-a",
        agent_id="agent-a",
        runtime_id="runtime-a",
        runtime_generation=1,
        population=AuthorityPopulation.SERVER_DYNAMIC,
    )

    assert superseded.binding is service.binding
    assert service.events == []

    await coordinator.close_runtime_generation(
        owner_id="owner-a",
        agent_id="agent-a",
        runtime_id="runtime-b",
        runtime_generation=2,
        population=AuthorityPopulation.SERVER_DYNAMIC,
    )
    assert [event for event, _ in service.events] == ["close"]


@pytest.mark.asyncio
async def test_exact_retirement_refuses_same_generation_identity_mismatch() -> None:
    service = RecordingLifecycleService(
        binding=_binding(generation=2, runtime_id="runtime-b")
    )
    coordinator = _coordinator(service)

    with pytest.raises(LetsLifecycleError, match="runtime_generation_mismatch"):
        await coordinator.close_runtime_generation(
            owner_id="owner-a",
            agent_id="agent-a",
            runtime_id="runtime-c",
            runtime_generation=2,
            population=AuthorityPopulation.SERVER_DYNAMIC,
        )

    assert service.events == []


@pytest.mark.asyncio
async def test_indeterminate_predecessor_is_left_for_same_id_reconciliation() -> None:
    service = RecordingLifecycleService(
        binding=_binding(state=AuthorityBindingState.RECONCILING)
    )
    coordinator = _coordinator(service)

    with pytest.raises(LetsLifecycleError, match="runtime_generation_reconciling"):
        await coordinator.admit_new_runtime(
            owner_id="owner-a",
            agent_id="agent-a",
            runtime_id="runtime-b",
            population=AuthorityPopulation.SERVER_DYNAMIC,
            declared_scopes=("tools:read",),
        )

    assert service.events == []


@pytest.mark.asyncio
async def test_off_mode_makes_no_lookup_no_id_and_no_lifecycle_call() -> None:
    service = RecordingLifecycleService(mode="off", binding=_binding())

    class NoLookupCoordinator(GovernedLifecycleCoordinator):
        def latest_binding(self, **_values: object) -> AgentAuthorityBinding | None:
            raise AssertionError("off mode must not query Plane")

    coordinator = NoLookupCoordinator(
        service,
        identifier_factory=lambda: (_ for _ in ()).throw(
            AssertionError("off mode must not allocate lifecycle IDs")
        ),
    )
    result = await coordinator.admit_new_runtime(
        owner_id="owner-a",
        agent_id="agent-a",
        runtime_id="runtime-b",
        population=AuthorityPopulation.SERVER_DYNAMIC,
        declared_scopes=("tools:read",),
    )

    assert result == LifecycleConvergence(protected=False)
    assert service.events == []


@pytest.mark.asyncio
async def test_byo_adapter_maps_admit_host_loss_reconnect_retire_and_revoke() -> None:
    service = RecordingLifecycleService(binding=None)
    coordinator = _coordinator(service)
    adapter = GovernedByoAgentLifecycle(coordinator)
    runtime = RuntimeInstanceRecord(
        fence=RuntimeFence(
            agent_id="agent-a",
            host_id="00000000-0000-4000-8000-000000000001",
            host_session_id="00000000-0000-4000-8000-000000000002",
            delivery_id="00000000-0000-4000-8000-000000000003",
            revision_id="00000000-0000-4000-8000-000000000004",
            runtime_instance_id="runtime-a",
            process_id="00000000-0000-4000-8000-000000000005",
            lifecycle_generation=1,
        ),
        operation_id=None,
        operation_execution_generation=0,
        state="online",
        is_authoritative=True,
        state_revision=0,
        created_at=datetime.now(UTC),
        started_at=None,
        registered_at=None,
        last_heartbeat_sequence=None,
        ready_at=None,
        last_liveness_at=None,
        terminal_at=None,
        failure_code=None,
    )

    await adapter.admit_or_resume(
        owner_user_id="owner-a",
        runtime=runtime,
        declared_scopes=("tools:read",),
        executor_conformant=True,
    )
    await adapter.host_lost(owner_user_id="owner-a", agent_id="agent-a")
    await adapter.admit_or_resume(
        owner_user_id="owner-a",
        runtime=runtime,
        declared_scopes=("tools:read",),
        executor_conformant=True,
    )
    await adapter.retire_runtime(owner_user_id="owner-a", agent_id="agent-a")
    service.binding = replace(
        _binding(generation=2, runtime_id="runtime-b"),
        population=AuthorityPopulation.BYO_USER,
    )
    await adapter.revoke_agent(
        owner_user_id="owner-a",
        agent_id="agent-a",
        reason_code="agent_deleted",
    )

    assert [event for event, _ in service.events] == [
        "provision",
        "quiesce",
        "resume",
        "close",
        "revoke",
    ]
