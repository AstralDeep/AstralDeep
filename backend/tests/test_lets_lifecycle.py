"""Crash-safe lifecycle convergence through Deep, Plane, and public LETS values."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from astralplane.authority import (
    AgentAuthorityBinding,
    AuthorityBindingState,
    AuthorityLifecycleOperation,
    AuthorityLifecycleStatus,
    AuthorityPopulation,
)
from lets.models import BranchRevocation, LeaseGrant, LeaseSnapshot, LeaseStatus

from orchestrator.lets_client import LetsClientBoundaryError
from orchestrator.lets_config import AuthenticatedTrustManifest, LetsHostConfig
from orchestrator.lets_lifecycle import (
    GovernedRuntime,
    LetsLifecycleError,
    LetsLifecycleService,
)
from orchestrator.lets_reconciler import LetsLifecycleReconciler


POLICY_DIGEST = "sha256:" + "1" * 64
MACHINE_DIGEST = "sha256:" + "2" * 64
ALLOCATION = (20, 20, 20, 20, 20, 20)


def _config(*, epoch: int = 7, mode: str = "enforce") -> LetsHostConfig:
    manifest = AuthenticatedTrustManifest(
        path=Path("C:/synthetic/manifest.json"),
        sha256="3" * 64,
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        config_epoch=epoch,
        warden_id="warden-a",
        policy_digest=POLICY_DIGEST,
        machine_digest=MACHINE_DIGEST,
        max_lease_ttl_ns=1_000_000,
    )
    return LetsHostConfig(
        master_enabled=True,
        mode=mode,  # type: ignore[arg-type]
        environment="test",
        governed_cohorts=("server_dynamic", "byo_user"),
        governed_agent_allowlist=(),
        warden_origin="https://warden.example",
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        policy_digest=POLICY_DIGEST,
        machine_digest=MACHINE_DIGEST,
        default_allocation=ALLOCATION,
        default_ttl_seconds=60,
        request_timeout_seconds=1.0,
        request_attempts=2,
        trust_manifest=manifest,
    )


def _runtime(
    agent_id: str = "agent-a",
    *,
    runtime_id: str | None = None,
    generation: int = 1,
    population: AuthorityPopulation = AuthorityPopulation.SERVER_DYNAMIC,
) -> GovernedRuntime:
    return GovernedRuntime(
        owner_id="owner-a",
        agent_id=agent_id,
        runtime_id=runtime_id or f"runtime-{agent_id}",
        runtime_generation=generation,
        population=population,
        declared_scopes=("tools:read",),
    )


class MemoryPlane:
    @contextmanager
    def transaction(self, **_options: object) -> Iterator[object]:
        yield object()


class MemoryAuthorityRepository:
    """Small CAS-accurate repository fake for orchestration contract tests."""

    def __init__(self) -> None:
        self.bindings: dict[tuple[str, str], AgentAuthorityBinding] = {}
        self.operations: dict[tuple[str, str], AuthorityLifecycleOperation] = {}

    def create_binding(
        self, _transaction: object, binding: AgentAuthorityBinding
    ) -> AgentAuthorityBinding:
        key = (binding.owner_id, binding.binding_id)
        current = self.bindings.get(key)
        if current is not None and current != binding:
            raise RuntimeError("binding conflict")
        self.bindings[key] = binding
        return binding

    def get_binding(
        self, _transaction: object, *, owner_id: str, binding_id: str
    ) -> AgentAuthorityBinding | None:
        return self.bindings.get((owner_id, binding_id))

    def transition_binding(
        self,
        _transaction: object,
        replacement: AgentAuthorityBinding,
        *,
        expected_state: AuthorityBindingState,
        expected_version: int,
    ) -> AgentAuthorityBinding:
        key = (replacement.owner_id, replacement.binding_id)
        current = self.bindings.get(key)
        assert current is not None
        assert current.state is expected_state
        assert current.version == expected_version
        assert replacement.version == expected_version + 1
        self.bindings[key] = replacement
        return replacement

    def activate_binding(
        self,
        transaction: object,
        replacement: AgentAuthorityBinding,
        *,
        expected_version: int,
    ) -> AgentAuthorityBinding:
        current = self.bindings[(replacement.owner_id, replacement.binding_id)]
        assert current.state is AuthorityBindingState.PROVISIONING
        return self.transition_binding(
            transaction,
            replacement,
            expected_state=current.state,
            expected_version=expected_version,
        )

    def abandon_provisioning_binding(
        self,
        transaction: object,
        replacement: AgentAuthorityBinding,
        *,
        expected_version: int,
    ) -> AgentAuthorityBinding:
        return self.transition_binding(
            transaction,
            replacement,
            expected_state=AuthorityBindingState.PROVISIONING,
            expected_version=expected_version,
        )

    def create_lifecycle_operation(
        self, _transaction: object, operation: AuthorityLifecycleOperation
    ) -> AuthorityLifecycleOperation:
        key = (operation.owner_id, operation.operation_id)
        current = self.operations.get(key)
        if current is not None and current != operation:
            raise RuntimeError("operation conflict")
        self.operations[key] = operation
        return operation

    def get_lifecycle_operation(
        self, _transaction: object, *, owner_id: str, operation_id: str
    ) -> AuthorityLifecycleOperation | None:
        return self.operations.get((owner_id, operation_id))

    def transition_lifecycle_operation(
        self,
        _transaction: object,
        replacement: AuthorityLifecycleOperation,
        *,
        expected_status: AuthorityLifecycleStatus,
        expected_version: int,
    ) -> AuthorityLifecycleOperation:
        key = (replacement.owner_id, replacement.operation_id)
        current = self.operations.get(key)
        assert current is not None
        assert current.status is expected_status
        assert current.version == expected_version
        assert replacement.version == expected_version + 1
        self.operations[key] = replacement
        return replacement

    def list_recoverable_lifecycle_operations(
        self,
        _transaction: object,
        *,
        owner_id: str,
        due_at: datetime,
        limit: int = 50,
    ) -> tuple[AuthorityLifecycleOperation, ...]:
        candidates = [
            operation
            for (owner, _operation_id), operation in self.operations.items()
            if owner == owner_id
            and operation.status
            in {
                AuthorityLifecycleStatus.PENDING,
                AuthorityLifecycleStatus.IN_FLIGHT,
                AuthorityLifecycleStatus.UNCERTAIN,
            }
            and operation.next_attempt_at is not None
            and operation.next_attempt_at <= due_at
        ]
        return tuple(
            sorted(
                candidates,
                key=lambda operation: (
                    operation.next_attempt_at,
                    operation.created_at,
                    operation.operation_id,
                ),
            )[:limit]
        )


class LifecycleClient:
    """Deterministic LETS boundary whose state survives service restarts."""

    def __init__(self) -> None:
        self.grants: dict[str, LeaseGrant] = {}
        self.snapshots: dict[str, LeaseSnapshot] = {}
        self.calls: list[tuple[str, str]] = []
        self.failures: dict[str, list[BaseException]] = {}

    def fail_once(self, method: str, error: BaseException) -> None:
        self.failures.setdefault(method, []).append(error)

    def _begin(self, method: str, operation_id: str) -> None:
        self.calls.append((method, operation_id))
        failures = self.failures.get(method, [])
        if failures:
            raise failures.pop(0)

    @staticmethod
    def _grant(
        *, agent_id: str, lease_id: str, parent_id: str | None, epoch: int = 7
    ) -> LeaseGrant:
        return LeaseGrant(
            tenant_id="tenant-a",
            envelope_id="envelope-a",
            config_epoch=epoch,
            lease_id=lease_id,
            lineage_id=f"lineage-{agent_id}",
            parent_id=parent_id,
            subject_id=agent_id,
            warden_id="warden-a",
            allocation=ALLOCATION,
            capabilities=frozenset({"astral.tools.read"}),
            policy_id="astral-policy",
            policy_version="1",
            policy_digest=POLICY_DIGEST,
            machine_digest=MACHINE_DIGEST,
            ancestor_path=() if parent_id is None else (parent_id,),
            branch_epoch=0,
            issued_at_ns=1,
            expires_at_ns=1_000_000,
            key_id="warden-a:key-1",
            signature="synthetic-signature",
        )

    def _store(self, grant: LeaseGrant) -> LeaseGrant:
        self.grants[grant.lease_id] = grant
        self.snapshots[grant.lease_id] = LeaseSnapshot(
            grant=grant,
            residual=grant.allocation,
            current_state="ready",
            status=LeaseStatus.ACTIVE,
            sequence=0,
            updated_at_ns=1,
        )
        return grant

    def provision_agent(
        self, *, operation_id: str, agent_id: str, declared_scopes: tuple[str, ...]
    ) -> LeaseGrant:
        self._begin("provision", operation_id)
        assert declared_scopes == ("tools:read",)
        lease_id = f"lease-{agent_id}"
        return self.grants.get(lease_id) or self._store(
            self._grant(agent_id=agent_id, lease_id=lease_id, parent_id=None)
        )

    def replicate_agent(
        self,
        *,
        operation_id: str,
        parent_lease_id: str,
        agent_id: str,
        declared_scopes: tuple[str, ...],
        expected_sequence: int,
    ) -> LeaseGrant:
        self._begin("spawn", operation_id)
        assert declared_scopes == ("tools:read",)
        assert self.snapshots[parent_lease_id].sequence == expected_sequence
        parent = self.snapshots[parent_lease_id]
        self.snapshots[parent_lease_id] = replace(
            parent,
            sequence=parent.sequence + 1,
            updated_at_ns=parent.updated_at_ns + 1,
        )
        lease_id = f"lease-{agent_id}"
        return self.grants.get(lease_id) or self._store(
            self._grant(
                agent_id=agent_id,
                lease_id=lease_id,
                parent_id=parent_lease_id,
            )
        )

    def _transition(
        self,
        method: str,
        operation_id: str,
        lease_id: str,
        status: LeaseStatus,
    ) -> LeaseSnapshot:
        self._begin(method, operation_id)
        snapshot = self.snapshots[lease_id]
        snapshot = replace(
            snapshot,
            status=status,
            sequence=snapshot.sequence + 1,
            updated_at_ns=snapshot.updated_at_ns + 1,
        )
        self.snapshots[lease_id] = snapshot
        return snapshot

    def renew(
        self,
        *,
        operation_id: str,
        lease_id: str,
        agent_id: str,
        expected_sequence: int,
    ) -> LeaseSnapshot:
        assert self.grants[lease_id].subject_id == agent_id
        assert self.snapshots[lease_id].sequence == expected_sequence
        return self._transition("renew", operation_id, lease_id, LeaseStatus.ACTIVE)

    def quiesce(
        self, *, operation_id: str, lease_id: str, agent_id: str
    ) -> LeaseSnapshot:
        assert self.grants[lease_id].subject_id == agent_id
        return self._transition("quiesce", operation_id, lease_id, LeaseStatus.QUIESCENT)

    def resume(
        self, *, operation_id: str, lease_id: str, agent_id: str
    ) -> LeaseSnapshot:
        assert self.grants[lease_id].subject_id == agent_id
        return self._transition("resume", operation_id, lease_id, LeaseStatus.ACTIVE)

    def close_lease(
        self, *, operation_id: str, lease_id: str, agent_id: str
    ) -> LeaseSnapshot:
        assert self.grants[lease_id].subject_id == agent_id
        return self._transition("close", operation_id, lease_id, LeaseStatus.CLOSED)

    def revoke(
        self, *, operation_id: str, lease_id: str, reason: str
    ) -> BranchRevocation:
        self._begin("revoke", operation_id)
        grant = self.grants[lease_id]
        return BranchRevocation(
            tenant_id=grant.tenant_id,
            envelope_id=grant.envelope_id,
            config_epoch=grant.config_epoch,
            branch_lease_id=lease_id,
            lineage_id=grant.lineage_id,
            epoch=1,
            issuer_warden=grant.warden_id,
            issued_at_ns=2,
            reason=reason,
            key_id=grant.key_id,
            signature="synthetic-signature",
        )

    def reconcile(self, *, lease_id: str, agent_id: str) -> LeaseSnapshot:
        self._begin("reconcile", lease_id)
        assert self.grants[lease_id].subject_id == agent_id
        return self.snapshots[lease_id]


def _service(
    repository: MemoryAuthorityRepository,
    client: LifecycleClient,
    *,
    epoch: int = 7,
) -> LetsLifecycleService:
    return LetsLifecycleService(
        config=_config(epoch=epoch),
        plane=MemoryPlane(),
        repository=repository,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
    )


async def _provision(
    service: LetsLifecycleService,
    runtime: GovernedRuntime | None = None,
    *,
    binding_id: str = "binding-a",
    operation_id: str = "provision-a",
):
    return await service.provision(
        _runtime() if runtime is None else runtime,
        binding_id=binding_id,
        operation_id=operation_id,
    )


async def test_crash_before_remote_response_leaves_recoverable_durable_intent() -> None:
    repository = MemoryAuthorityRepository()
    client = LifecycleClient()
    client.fail_once("provision", SystemExit("simulated-process-loss"))
    service = _service(repository, client)

    with pytest.raises(SystemExit, match="simulated-process-loss"):
        await _provision(service)

    binding = repository.bindings[("owner-a", "binding-a")]
    operation = repository.operations[("owner-a", "provision-a")]
    assert binding.state is AuthorityBindingState.PROVISIONING
    assert operation.status is AuthorityLifecycleStatus.IN_FLIGHT
    assert operation.attempt_count == 1
    assert operation.next_attempt_at is not None


async def test_restart_reconciles_crash_before_call_with_exact_operation_identity() -> None:
    repository = MemoryAuthorityRepository()
    client = LifecycleClient()
    client.fail_once("provision", SystemExit("simulated-process-loss"))
    first = _service(repository, client)
    with pytest.raises(SystemExit):
        await _provision(first)

    restarted = _service(repository, client)
    reconciler = LetsLifecycleReconciler(
        plane=restarted.plane,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        lifecycle=restarted,
    )
    batch = await reconciler.recover_owner(
        "owner-a",
        due_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    assert batch.selected == batch.claimed == batch.converged == 1
    assert repository.bindings[("owner-a", "binding-a")].state is AuthorityBindingState.ACTIVE
    assert repository.operations[("owner-a", "provision-a")].status is AuthorityLifecycleStatus.SUCCEEDED
    assert client.calls == [
        ("provision", "provision-a"),
        ("provision", "provision-a"),
    ]


async def test_commit_lost_response_reuses_request_and_converges_after_restart() -> None:
    repository = MemoryAuthorityRepository()
    client = LifecycleClient()
    original = client.provision_agent

    def committed_then_lost(**arguments: object) -> LeaseGrant:
        original(**arguments)  # type: ignore[arg-type]
        client.provision_agent = original  # type: ignore[method-assign]
        raise LetsClientBoundaryError("request_timeout", retryable=True)

    client.provision_agent = committed_then_lost  # type: ignore[method-assign]
    with pytest.raises(LetsLifecycleError, match="request_timeout"):
        await _provision(_service(repository, client))

    operation = repository.operations[("owner-a", "provision-a")]
    assert operation.status is AuthorityLifecycleStatus.UNCERTAIN
    result = await _provision(_service(repository, client))
    assert result.binding is not None
    assert result.binding.state is AuthorityBindingState.ACTIVE
    assert result.operation is not None
    assert result.operation.status is AuthorityLifecycleStatus.SUCCEEDED
    assert client.calls[-2:] == [
        ("provision", "provision-a"),
        ("provision", "provision-a"),
    ]


async def test_spawn_fences_parent_and_advances_its_sequence_atomically() -> None:
    repository = MemoryAuthorityRepository()
    client = LifecycleClient()
    service = _service(repository, client)
    root = await _provision(service)
    assert root.binding is not None

    child = await service.spawn(
        _runtime("agent-child"),
        parent_binding_id=root.binding.binding_id,
        binding_id="binding-child",
        operation_id="spawn-child",
    )

    assert child.binding is not None
    assert child.binding.state is AuthorityBindingState.ACTIVE
    parent = repository.bindings[("owner-a", "binding-a")]
    assert parent.state is AuthorityBindingState.ACTIVE
    assert parent.lease_sequence == 1
    repeated = await service.spawn(
        _runtime("agent-child"),
        parent_binding_id=parent.binding_id,
        binding_id="binding-child",
        operation_id="spawn-child",
    )
    assert repeated.result_sha256 == child.result_sha256
    assert [name for name, _operation in client.calls].count("spawn") == 1


async def test_renew_pause_reconnect_close_and_terminal_fence() -> None:
    repository = MemoryAuthorityRepository()
    client = LifecycleClient()
    service = _service(repository, client)
    binding = (await _provision(service)).binding
    assert binding is not None

    renewed = await service.renew(
        owner_id="owner-a", binding_id=binding.binding_id, operation_id="renew-a"
    )
    assert renewed.binding is not None and renewed.binding.lease_sequence == 1
    paused = await service.quiesce(
        owner_id="owner-a", binding_id=binding.binding_id, operation_id="pause-a"
    )
    assert paused.binding is not None
    assert paused.binding.state is AuthorityBindingState.QUIESCENT
    resumed = await service.resume(
        owner_id="owner-a", binding_id=binding.binding_id, operation_id="resume-a"
    )
    assert resumed.binding is not None
    assert resumed.binding.state is AuthorityBindingState.ACTIVE
    closed = await service.close(
        owner_id="owner-a", binding_id=binding.binding_id, operation_id="close-a"
    )
    assert closed.binding is not None
    assert closed.binding.state is AuthorityBindingState.CLOSED

    with pytest.raises(LetsLifecycleError, match="invalid_binding_lifecycle_state"):
        await service.renew(
            owner_id="owner-a",
            binding_id=binding.binding_id,
            operation_id="renew-after-close",
        )


async def test_reconcile_expiry_fails_closed_for_future_effect_lifecycle() -> None:
    repository = MemoryAuthorityRepository()
    client = LifecycleClient()
    service = _service(repository, client)
    binding = (await _provision(service)).binding
    assert binding is not None
    snapshot = client.snapshots[binding.lease_id]
    client.snapshots[binding.lease_id] = replace(
        snapshot,
        status=LeaseStatus.EXPIRED,
        sequence=snapshot.sequence + 1,
        updated_at_ns=snapshot.updated_at_ns + 1,
    )

    expired = await service.reconcile(
        owner_id="owner-a",
        binding_id=binding.binding_id,
        operation_id="expire-a",
    )
    assert expired.binding is not None
    assert expired.binding.state is AuthorityBindingState.EXPIRED
    with pytest.raises(LetsLifecycleError, match="invalid_binding_lifecycle_state"):
        await service.resume(
            owner_id="owner-a",
            binding_id=binding.binding_id,
            operation_id="resume-expired",
        )


async def test_revoke_converges_branch_and_never_reopens() -> None:
    repository = MemoryAuthorityRepository()
    client = LifecycleClient()
    service = _service(repository, client)
    binding = (await _provision(service)).binding
    assert binding is not None

    revoked = await service.revoke(
        owner_id="owner-a",
        binding_id=binding.binding_id,
        operation_id="revoke-a",
        reason_code="compromised",
    )
    assert revoked.binding is not None
    assert revoked.binding.state is AuthorityBindingState.REVOKED
    repeated = await service.revoke(
        owner_id="owner-a",
        binding_id=binding.binding_id,
        operation_id="revoke-a",
        reason_code="compromised",
    )
    assert repeated.result_sha256 == revoked.result_sha256
    assert [name for name, _operation in client.calls].count("revoke") == 1


async def test_epoch_rotation_requires_a_new_terminally_superseded_generation() -> None:
    repository = MemoryAuthorityRepository()
    client = LifecycleClient()
    first_service = _service(repository, client, epoch=7)
    first = (await _provision(first_service)).binding
    assert first is not None
    await first_service.close(
        owner_id="owner-a", binding_id=first.binding_id, operation_id="close-epoch-7"
    )

    rotated_runtime = _runtime(
        runtime_id="runtime-agent-a-epoch-8",
        generation=2,
    )
    second = await _provision(
        _service(repository, client, epoch=8),
        rotated_runtime,
        binding_id="binding-epoch-8",
        operation_id="provision-epoch-8",
    )

    assert repository.bindings[("owner-a", "binding-a")].state is AuthorityBindingState.CLOSED
    assert second.binding is not None
    assert second.binding.config_epoch == 8
    assert second.binding.runtime_generation == 2
    assert second.binding.binding_id != first.binding_id


async def test_definitive_provision_failure_closes_pending_intent() -> None:
    repository = MemoryAuthorityRepository()
    client = LifecycleClient()
    client.fail_once("provision", LetsClientBoundaryError("permission_denied"))

    with pytest.raises(LetsLifecycleError, match="permission_denied"):
        await _provision(_service(repository, client))

    binding = repository.bindings[("owner-a", "binding-a")]
    operation = repository.operations[("owner-a", "provision-a")]
    assert binding.state is AuthorityBindingState.CLOSED
    assert binding.lease_sequence == binding.lease_expires_at_ns == 0
    assert operation.status is AuthorityLifecycleStatus.FAILED
