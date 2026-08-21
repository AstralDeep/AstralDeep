"""Durable Astral-to-LETS lifecycle convergence.

AstralPlane owns neutral persistence and LETS owns remote finite authority.  This
module is the product-policy adapter between them: it commits an owner-scoped
intent before every remote mutation, reuses the exact request identity only for
the same canonical intent, and leaves ambiguous results recoverable and
fail-closed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Callable, Final, Protocol

from astralplane.authority import (
    AgentAuthorityBinding,
    AuthorityBindingState,
    AuthorityLifecycleKind,
    AuthorityLifecycleOperation,
    AuthorityLifecycleStatus,
    AuthorityPopulation,
    AuthorityRepository,
)
from lets.models import BranchRevocation, LeaseGrant, LeaseSnapshot, LeaseStatus

from orchestrator.lets_client import LetsClientBoundaryError, LetsWardenClient
from orchestrator.lets_config import LetsHostConfig
from orchestrator.lets_scope_profile import SCOPE_BINDINGS, binding_for_scope


_RECOVERY_DELAY: Final = timedelta(seconds=15)
_AMBIGUOUS_REMOTE_CODES: Final = frozenset(
    {
        "request_timeout",
        "transport_unavailable",
        "remote_unavailable",
        "invalid_response",
        "response_binding_mismatch",
        "client_failure",
        "lifecycle_client_failure",
    }
)
_ACTIVE: Final = AuthorityBindingState.ACTIVE
_QUIESCENT: Final = AuthorityBindingState.QUIESCENT


class PlaneAuthorityRuntime(Protocol):
    """The small AstralPlane runtime seam required by this adapter."""

    def transaction(self, **options: object): ...


class LetsLifecycleError(RuntimeError):
    """Stable, content-free lifecycle refusal."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GovernedRuntime:
    """Owner- and generation-fenced Astral identity admitted to LETS."""

    owner_id: str
    agent_id: str
    runtime_id: str
    runtime_generation: int
    population: AuthorityPopulation
    declared_scopes: tuple[str, ...]
    executor_conformant: bool = True


@dataclass(frozen=True, slots=True)
class LifecycleConvergence:
    """Redacted local result of one lifecycle convergence attempt."""

    protected: bool
    binding: AgentAuthorityBinding | None = None
    operation: AuthorityLifecycleOperation | None = None
    result_sha256: str | None = None
    error_code: str | None = None
    would_deny: bool = False


@dataclass(frozen=True, slots=True)
class LifecycleRecoveryContext:
    """Deep-owned context not represented by Plane's neutral operation model."""

    parent_binding_id: str | None = None
    revocation_reason: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical_sha256(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _wire_sha256(value: object) -> str:
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        raise LetsLifecycleError("invalid_lifecycle_response")
    document = to_dict()
    if not isinstance(document, Mapping):
        raise LetsLifecycleError("invalid_lifecycle_response")
    return _canonical_sha256(document)


def _capabilities(scopes: Sequence[str]) -> tuple[str, ...]:
    if isinstance(scopes, (str, bytes)) or not scopes:
        raise LetsLifecycleError("invalid_declared_scopes")
    try:
        values = tuple(sorted({binding_for_scope(item).capability for item in scopes}))
    except (TypeError, ValueError):
        raise LetsLifecycleError("invalid_declared_scopes") from None
    if len(values) != len(scopes):
        raise LetsLifecycleError("invalid_declared_scopes")
    return values


def _scopes(capabilities: Sequence[str]) -> tuple[str, ...]:
    by_capability = {entry.capability: entry.scope for entry in SCOPE_BINDINGS}
    if isinstance(capabilities, (str, bytes)) or not capabilities:
        raise LetsLifecycleError("invalid_binding_capabilities")
    try:
        scopes = tuple(by_capability[item] for item in capabilities)
    except (KeyError, TypeError):
        raise LetsLifecycleError("invalid_binding_capabilities") from None
    if len(scopes) != len(set(scopes)):
        raise LetsLifecycleError("invalid_binding_capabilities")
    return scopes


def _snapshot_state(snapshot: LeaseSnapshot) -> AuthorityBindingState:
    states = {
        LeaseStatus.PROVISIONED: AuthorityBindingState.ACTIVE,
        LeaseStatus.ACTIVE: AuthorityBindingState.ACTIVE,
        LeaseStatus.QUIESCENT: AuthorityBindingState.QUIESCENT,
        LeaseStatus.CLOSED: AuthorityBindingState.CLOSED,
        LeaseStatus.REVOKED: AuthorityBindingState.REVOKED,
        LeaseStatus.EXPIRED: AuthorityBindingState.EXPIRED,
    }
    try:
        return states[snapshot.status]
    except (KeyError, TypeError):
        raise LetsLifecycleError("unknown_remote_lease_state") from None


class LetsLifecycleService:
    """Converge governed agent lifecycle through Plane and public LETS APIs."""

    def __init__(
        self,
        *,
        config: LetsHostConfig,
        plane: PlaneAuthorityRuntime | None,
        repository: AuthorityRepository | None,
        client: LetsWardenClient | None,
    ) -> None:
        if not isinstance(config, LetsHostConfig):
            raise TypeError("LETS host config is required")
        if config.mode != "off" and (plane is None or repository is None or client is None):
            raise LetsLifecycleError("lifecycle_runtime_unavailable")
        self.config = config
        self.plane = plane
        self.repository = repository
        self.client = client

    def governs(self, runtime: GovernedRuntime) -> bool:
        population = runtime.population.value
        if self.config.mode == "off" or population not in self.config.governed_cohorts:
            return False
        allowlist = self.config.governed_agent_allowlist
        if allowlist and runtime.agent_id not in allowlist:
            return False
        if population == AuthorityPopulation.BYO_USER.value and not runtime.executor_conformant:
            if self.config.mode == "enforce":
                raise LetsLifecycleError("executor_not_conformant")
        return True

    async def provision(
        self,
        runtime: GovernedRuntime,
        *,
        binding_id: str,
        operation_id: str,
    ) -> LifecycleConvergence:
        """Issue one governed root after its local intent is durable."""

        if not self.governs(runtime):
            return LifecycleConvergence(protected=False)
        try:
            capabilities = _capabilities(runtime.declared_scopes)
        except LetsLifecycleError as exc:
            return self._deny(exc.code)
        fingerprint = self._fingerprint(
            AuthorityLifecycleKind.PROVISION,
            runtime=runtime,
            binding_id=binding_id,
            capabilities=capabilities,
        )
        try:
            binding, operation, _parent, terminal = await asyncio.to_thread(
                self._prepare_new_binding,
                runtime,
                binding_id,
                operation_id,
                AuthorityLifecycleKind.PROVISION,
                fingerprint,
                capabilities,
                None,
                None,
            )
        except LetsLifecycleError as exc:
            return self._deny(exc.code, retryable=exc.retryable)
        except Exception:
            return self._deny("lifecycle_persistence_failure", retryable=True)
        if terminal is not None:
            return terminal
        try:
            assert self.client is not None
            grant = await asyncio.to_thread(
                self.client.provision_agent,
                operation_id=operation.operation_id,
                agent_id=runtime.agent_id,
                declared_scopes=runtime.declared_scopes,
            )
            return await asyncio.to_thread(
                self._finish_grant,
                binding,
                operation,
                grant,
            )
        except LetsClientBoundaryError as exc:
            return await self._finish_remote_error(binding, operation, exc)
        except LetsLifecycleError as exc:
            return await self._finish_remote_error(
                binding,
                operation,
                LetsClientBoundaryError(exc.code, retryable=True),
            )
        except Exception:
            return await self._finish_remote_error(
                binding,
                operation,
                LetsClientBoundaryError("lifecycle_client_failure"),
            )

    async def spawn(
        self,
        runtime: GovernedRuntime,
        *,
        parent_binding_id: str,
        binding_id: str,
        operation_id: str,
    ) -> LifecycleConvergence:
        """Spawn a distinct child/subtask agent under an active parent lease.

        Feature 074 deliberately serializes generations for one ``agent_id``;
        a concurrent child therefore has its own governed child agent identity.
        """

        if not self.governs(runtime):
            return LifecycleConvergence(protected=False)
        try:
            capabilities = _capabilities(runtime.declared_scopes)
        except LetsLifecycleError as exc:
            return self._deny(exc.code)
        try:
            parent = await asyncio.to_thread(
                self._get_binding,
                runtime.owner_id,
                parent_binding_id,
            )
        except Exception:
            return self._deny("lifecycle_persistence_failure", retryable=True)
        if parent is None or parent.state not in {
            _ACTIVE,
            AuthorityBindingState.RECONCILING,
        }:
            return self._deny("parent_binding_unavailable")
        if parent.agent_id == runtime.agent_id:
            return self._deny("spawn_requires_distinct_child_agent")
        try:
            existing_operation = await asyncio.to_thread(
                self._get_operation,
                runtime.owner_id,
                operation_id,
            )
        except Exception:
            return self._deny("lifecycle_persistence_failure", retryable=True)
        parent_sequence = (
            parent.lease_sequence
            if existing_operation is None
            else existing_operation.expected_lease_sequence
        )
        if parent_sequence is None:
            return self._deny("spawn_sequence_unavailable")
        fingerprint = self._fingerprint(
            AuthorityLifecycleKind.SPAWN,
            runtime=runtime,
            binding_id=binding_id,
            capabilities=capabilities,
            extra={
                "parent_binding_id": parent.binding_id,
                "parent_lease_id": parent.lease_id,
                "parent_sequence": parent_sequence,
            },
        )
        try:
            binding, operation, parent, terminal = await asyncio.to_thread(
                self._prepare_new_binding,
                runtime,
                binding_id,
                operation_id,
                AuthorityLifecycleKind.SPAWN,
                fingerprint,
                capabilities,
                parent_sequence,
                parent,
            )
        except LetsLifecycleError as exc:
            return self._deny(exc.code, retryable=exc.retryable)
        except Exception:
            return self._deny("lifecycle_persistence_failure", retryable=True)
        if terminal is not None:
            return terminal
        if parent is None:  # pragma: no cover - guarded by the SPAWN preparation path.
            raise LetsLifecycleError("parent_binding_unavailable")
        try:
            assert self.client is not None
            grant = await asyncio.to_thread(
                self.client.replicate_agent,
                operation_id=operation.operation_id,
                parent_lease_id=parent.lease_id,
                agent_id=runtime.agent_id,
                declared_scopes=runtime.declared_scopes,
                expected_sequence=operation.expected_lease_sequence,
            )
            if grant.parent_id != parent.lease_id:
                raise LetsLifecycleError("spawn_parent_mismatch")
            return await asyncio.to_thread(
                self._finish_spawn_grant,
                binding,
                operation,
                parent,
                grant,
            )
        except LetsClientBoundaryError as exc:
            return await self._finish_remote_error(
                binding,
                operation,
                exc,
                spawn_parent=parent,
            )
        except LetsLifecycleError as exc:
            return await self._finish_remote_error(
                binding,
                operation,
                LetsClientBoundaryError(exc.code, retryable=True),
                spawn_parent=parent,
            )
        except Exception:
            return await self._finish_remote_error(
                binding,
                operation,
                LetsClientBoundaryError("lifecycle_client_failure"),
                spawn_parent=parent,
            )

    async def renew(self, *, owner_id: str, binding_id: str, operation_id: str):
        return await self._mutate_existing(
            owner_id=owner_id,
            binding_id=binding_id,
            operation_id=operation_id,
            kind=AuthorityLifecycleKind.RENEW,
        )

    async def quiesce(self, *, owner_id: str, binding_id: str, operation_id: str):
        return await self._mutate_existing(
            owner_id=owner_id,
            binding_id=binding_id,
            operation_id=operation_id,
            kind=AuthorityLifecycleKind.QUIESCE,
        )

    async def resume(self, *, owner_id: str, binding_id: str, operation_id: str):
        return await self._mutate_existing(
            owner_id=owner_id,
            binding_id=binding_id,
            operation_id=operation_id,
            kind=AuthorityLifecycleKind.RESUME,
        )

    async def close(self, *, owner_id: str, binding_id: str, operation_id: str):
        return await self._mutate_existing(
            owner_id=owner_id,
            binding_id=binding_id,
            operation_id=operation_id,
            kind=AuthorityLifecycleKind.CLOSE,
        )

    async def revoke(
        self,
        *,
        owner_id: str,
        binding_id: str,
        operation_id: str,
        reason_code: str,
    ):
        return await self._mutate_existing(
            owner_id=owner_id,
            binding_id=binding_id,
            operation_id=operation_id,
            kind=AuthorityLifecycleKind.REVOKE,
            reason_code=reason_code,
        )

    async def reconcile(
        self,
        *,
        owner_id: str,
        binding_id: str,
        operation_id: str,
    ):
        return await self._mutate_existing(
            owner_id=owner_id,
            binding_id=binding_id,
            operation_id=operation_id,
            kind=AuthorityLifecycleKind.RECONCILE,
        )

    async def resume_claimed(
        self,
        *,
        binding: AgentAuthorityBinding,
        operation: AuthorityLifecycleOperation,
        context: LifecycleRecoveryContext | None = None,
    ) -> LifecycleConvergence:
        """Resume one operation already claimed durably by the reconciler.

        Plane deliberately stores only neutral request fingerprints. Deep's
        durable agent graph supplies the parent binding for SPAWN and the
        reviewed reason for REVOKE through ``LifecycleRecoveryContext``. No
        value is guessed when that context is unavailable.
        """

        if self.config.mode == "off":
            return LifecycleConvergence(protected=False)
        if (
            not isinstance(binding, AgentAuthorityBinding)
            or not isinstance(operation, AuthorityLifecycleOperation)
            or operation.owner_id != binding.owner_id
            or operation.binding_id != binding.binding_id
            or operation.status is not AuthorityLifecycleStatus.IN_FLIGHT
        ):
            raise LetsLifecycleError("invalid_recovery_claim")
        recovery = LifecycleRecoveryContext() if context is None else context
        if not isinstance(recovery, LifecycleRecoveryContext):
            raise LetsLifecycleError("invalid_recovery_context")

        parent: AgentAuthorityBinding | None = None
        try:
            assert self.client is not None
            scopes = _scopes(binding.capabilities)
            if operation.kind is AuthorityLifecycleKind.PROVISION:
                result = await asyncio.to_thread(
                    self.client.provision_agent,
                    operation_id=operation.operation_id,
                    agent_id=binding.agent_id,
                    declared_scopes=scopes,
                )
                return await asyncio.to_thread(
                    self._finish_grant,
                    binding,
                    operation,
                    result,
                )
            if operation.kind is AuthorityLifecycleKind.SPAWN:
                if recovery.parent_binding_id is None:
                    raise LetsLifecycleError("spawn_recovery_context_unavailable")
                parent = await asyncio.to_thread(
                    self._get_binding,
                    binding.owner_id,
                    recovery.parent_binding_id,
                )
                if (
                    parent is None
                    or parent.state is not AuthorityBindingState.RECONCILING
                    or parent.lease_sequence != operation.expected_lease_sequence
                ):
                    raise LetsLifecycleError("spawn_parent_fence_mismatch")
                result = await asyncio.to_thread(
                    self.client.replicate_agent,
                    operation_id=operation.operation_id,
                    parent_lease_id=parent.lease_id,
                    agent_id=binding.agent_id,
                    declared_scopes=scopes,
                    expected_sequence=operation.expected_lease_sequence,
                )
                return await asyncio.to_thread(
                    self._finish_spawn_grant,
                    binding,
                    operation,
                    parent,
                    result,
                )
            if operation.kind is AuthorityLifecycleKind.RENEW:
                result = await asyncio.to_thread(
                    self.client.renew,
                    operation_id=operation.operation_id,
                    lease_id=binding.lease_id,
                    agent_id=binding.agent_id,
                    expected_sequence=operation.expected_lease_sequence,
                )
            elif operation.kind is AuthorityLifecycleKind.QUIESCE:
                result = await asyncio.to_thread(
                    self.client.quiesce,
                    operation_id=operation.operation_id,
                    lease_id=binding.lease_id,
                    agent_id=binding.agent_id,
                )
            elif operation.kind is AuthorityLifecycleKind.RESUME:
                result = await asyncio.to_thread(
                    self.client.resume,
                    operation_id=operation.operation_id,
                    lease_id=binding.lease_id,
                    agent_id=binding.agent_id,
                )
            elif operation.kind is AuthorityLifecycleKind.CLOSE:
                result = await asyncio.to_thread(
                    self.client.close_lease,
                    operation_id=operation.operation_id,
                    lease_id=binding.lease_id,
                    agent_id=binding.agent_id,
                )
            elif operation.kind is AuthorityLifecycleKind.REVOKE:
                if recovery.revocation_reason is None:
                    raise LetsLifecycleError("revoke_recovery_context_unavailable")
                result = await asyncio.to_thread(
                    self.client.revoke,
                    operation_id=operation.operation_id,
                    lease_id=binding.lease_id,
                    reason=recovery.revocation_reason,
                )
            elif operation.kind is AuthorityLifecycleKind.RECONCILE:
                result = await asyncio.to_thread(
                    self.client.reconcile,
                    lease_id=binding.lease_id,
                    agent_id=binding.agent_id,
                )
            else:  # pragma: no cover - enum exhaustiveness guard.
                raise LetsLifecycleError("unknown_lifecycle_operation")
            return await asyncio.to_thread(
                self._finish_existing,
                binding,
                operation,
                result,
            )
        except LetsClientBoundaryError as exc:
            return await self._finish_remote_error(
                binding,
                operation,
                LetsClientBoundaryError(exc.code, retryable=True),
                spawn_parent=parent,
            )
        except LetsLifecycleError as exc:
            return await self._finish_remote_error(
                binding,
                operation,
                LetsClientBoundaryError(exc.code, retryable=True),
                spawn_parent=parent,
            )
        except Exception:
            return await self._finish_remote_error(
                binding,
                operation,
                LetsClientBoundaryError("lifecycle_recovery_failure", retryable=True),
                spawn_parent=parent,
            )

    async def _mutate_existing(
        self,
        *,
        owner_id: str,
        binding_id: str,
        operation_id: str,
        kind: AuthorityLifecycleKind,
        reason_code: str | None = None,
    ) -> LifecycleConvergence:
        if self.config.mode == "off":
            return LifecycleConvergence(protected=False)
        if kind is AuthorityLifecycleKind.REVOKE and not reason_code:
            return self._deny("missing_revocation_reason")
        try:
            binding = await asyncio.to_thread(self._get_binding, owner_id, binding_id)
        except Exception:
            return self._deny("lifecycle_persistence_failure", retryable=True)
        if binding is None:
            return self._deny("binding_unavailable")
        if not self._binding_is_governed(binding):
            return LifecycleConvergence(protected=False)
        try:
            existing_operation = await asyncio.to_thread(
                self._get_operation,
                owner_id,
                operation_id,
            )
        except Exception:
            return self._deny("lifecycle_persistence_failure", retryable=True)
        fingerprint_binding = binding
        if (
            existing_operation is not None
            and existing_operation.expected_lease_sequence is not None
        ):
            fingerprint_binding = replace(
                binding,
                lease_sequence=existing_operation.expected_lease_sequence,
            )
        fingerprint = self._fingerprint(
            kind,
            binding=fingerprint_binding,
            extra={} if reason_code is None else {"reason_code": reason_code},
        )
        confirmed_binding = binding
        try:
            binding, operation, terminal = await asyncio.to_thread(
                self._prepare_existing_operation,
                binding,
                operation_id,
                kind,
                fingerprint,
            )
        except LetsLifecycleError as exc:
            return self._deny(exc.code, retryable=exc.retryable)
        except Exception:
            return self._deny("lifecycle_persistence_failure", retryable=True)
        if terminal is not None:
            return terminal
        try:
            assert self.client is not None
            if kind is AuthorityLifecycleKind.RENEW:
                result = await asyncio.to_thread(
                    self.client.renew,
                    operation_id=operation.operation_id,
                    lease_id=binding.lease_id,
                    agent_id=binding.agent_id,
                    expected_sequence=operation.expected_lease_sequence,
                )
            elif kind is AuthorityLifecycleKind.QUIESCE:
                result = await asyncio.to_thread(
                    self.client.quiesce,
                    operation_id=operation.operation_id,
                    lease_id=binding.lease_id,
                    agent_id=binding.agent_id,
                )
            elif kind is AuthorityLifecycleKind.RESUME:
                result = await asyncio.to_thread(
                    self.client.resume,
                    operation_id=operation.operation_id,
                    lease_id=binding.lease_id,
                    agent_id=binding.agent_id,
                )
            elif kind is AuthorityLifecycleKind.CLOSE:
                result = await asyncio.to_thread(
                    self.client.close_lease,
                    operation_id=operation.operation_id,
                    lease_id=binding.lease_id,
                    agent_id=binding.agent_id,
                )
            elif kind is AuthorityLifecycleKind.REVOKE:
                assert reason_code is not None
                result = await asyncio.to_thread(
                    self.client.revoke,
                    operation_id=operation.operation_id,
                    lease_id=binding.lease_id,
                    reason=reason_code,
                )
            elif kind is AuthorityLifecycleKind.RECONCILE:
                result = await asyncio.to_thread(
                    self.client.reconcile,
                    lease_id=binding.lease_id,
                    agent_id=binding.agent_id,
                )
            else:  # pragma: no cover - callers enumerate the closed set above.
                raise LetsLifecycleError("unknown_lifecycle_operation")
            return await asyncio.to_thread(
                self._finish_existing,
                binding,
                operation,
                result,
            )
        except LetsClientBoundaryError as exc:
            return await self._finish_remote_error(
                binding,
                operation,
                exc,
                restore_state=self._restorable_state(kind, confirmed_binding.state),
            )
        except LetsLifecycleError as exc:
            return await self._finish_remote_error(
                binding,
                operation,
                LetsClientBoundaryError(exc.code, retryable=True),
                restore_state=self._restorable_state(kind, confirmed_binding.state),
            )
        except Exception:
            return await self._finish_remote_error(
                binding,
                operation,
                LetsClientBoundaryError("lifecycle_client_failure"),
                restore_state=self._restorable_state(kind, confirmed_binding.state),
            )

    def _binding_is_governed(self, binding: AgentAuthorityBinding) -> bool:
        population = binding.population.value
        allowlist = self.config.governed_agent_allowlist
        return (
            population in self.config.governed_cohorts
            and (not allowlist or binding.agent_id in allowlist)
        )

    def _fingerprint(
        self,
        kind: AuthorityLifecycleKind,
        *,
        runtime: GovernedRuntime | None = None,
        binding_id: str | None = None,
        capabilities: tuple[str, ...] | None = None,
        binding: AgentAuthorityBinding | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> str:
        document: dict[str, object] = {
            "type": "astral.lets-lifecycle-intent/v1",
            "kind": kind.value,
            "tenant_id": self.config.tenant_id or "off",
            "envelope_id": self.config.envelope_id or "off",
            "policy_digest": self.config.policy_digest or "off",
            "machine_digest": self.config.machine_digest or "off",
            "config_epoch": (
                0 if self.config.trust_manifest is None else self.config.trust_manifest.config_epoch
            ),
        }
        if runtime is not None:
            document.update(
                {
                    "owner_id": runtime.owner_id,
                    "agent_id": runtime.agent_id,
                    "runtime_id": runtime.runtime_id,
                    "runtime_generation": runtime.runtime_generation,
                    "population": runtime.population.value,
                    "binding_id": binding_id,
                    "capabilities": list(capabilities or ()),
                }
            )
        if binding is not None:
            document.update(
                {
                    "owner_id": binding.owner_id,
                    "agent_id": binding.agent_id,
                    "runtime_id": binding.runtime_id,
                    "runtime_generation": binding.runtime_generation,
                    "population": binding.population.value,
                    "binding_id": binding.binding_id,
                    "lease_id": binding.lease_id,
                    "expected_lease_sequence": binding.lease_sequence,
                }
            )
        document.update(dict(extra or {}))
        return _canonical_sha256(document)

    def _prepare_new_binding(
        self,
        runtime: GovernedRuntime,
        binding_id: str,
        operation_id: str,
        kind: AuthorityLifecycleKind,
        fingerprint: str,
        capabilities: tuple[str, ...],
        expected_sequence: int | None,
        initial_parent: AgentAuthorityBinding | None,
    ) -> tuple[
        AgentAuthorityBinding,
        AuthorityLifecycleOperation,
        AgentAuthorityBinding | None,
        LifecycleConvergence | None,
    ]:
        plane, repository = self._active_plane()
        now = _now()
        with plane.transaction() as transaction:
            binding = repository.get_binding(
                transaction,
                owner_id=runtime.owner_id,
                binding_id=binding_id,
            )
            operation = repository.get_lifecycle_operation(
                transaction,
                owner_id=runtime.owner_id,
                operation_id=operation_id,
            )
            parent: AgentAuthorityBinding | None = None
            if kind is AuthorityLifecycleKind.SPAWN:
                if initial_parent is None:
                    raise LetsLifecycleError("parent_binding_unavailable")
                parent = repository.get_binding(
                    transaction,
                    owner_id=runtime.owner_id,
                    binding_id=initial_parent.binding_id,
                )
                if (
                    parent is None
                    or parent.owner_id != runtime.owner_id
                    or parent.lease_id != initial_parent.lease_id
                    or parent.lease_sequence != initial_parent.lease_sequence
                ):
                    raise LetsLifecycleError("parent_binding_changed", retryable=True)
            if binding is None and operation is None:
                if parent is not None and parent.state is not _ACTIVE:
                    raise LetsLifecycleError("parent_binding_unavailable", retryable=True)
                manifest = self.config.trust_manifest
                if (
                    manifest is None
                    or self.config.tenant_id is None
                    or self.config.envelope_id is None
                    or self.config.policy_digest is None
                    or self.config.machine_digest is None
                ):
                    raise LetsLifecycleError("lifecycle_config_unavailable")
                binding = AgentAuthorityBinding.provisioning_intent(
                    binding_id=binding_id,
                    owner_id=runtime.owner_id,
                    agent_id=runtime.agent_id,
                    runtime_id=runtime.runtime_id,
                    runtime_generation=runtime.runtime_generation,
                    population=runtime.population,
                    tenant_id=self.config.tenant_id,
                    envelope_id=self.config.envelope_id,
                    policy_digest=self.config.policy_digest,
                    machine_digest=self.config.machine_digest,
                    config_epoch=manifest.config_epoch,
                    capabilities=capabilities,
                    created_at=now,
                )
                binding = repository.create_binding(transaction, binding)
                operation = AuthorityLifecycleOperation(
                    operation_id=operation_id,
                    owner_id=runtime.owner_id,
                    binding_id=binding_id,
                    kind=kind,
                    expected_binding_version=binding.version,
                    expected_lease_sequence=expected_sequence,
                    request_fingerprint=fingerprint,
                    status=AuthorityLifecycleStatus.PENDING,
                    remote_request_id=operation_id,
                    result_digest=None,
                    error_code=None,
                    attempt_count=0,
                    next_attempt_at=now,
                    last_attempt_at=None,
                    reconciled_at=None,
                    reconciliation_digest=None,
                    created_at=now,
                    updated_at=now,
                    version=0,
                )
                operation = repository.create_lifecycle_operation(transaction, operation)
                if parent is not None:
                    replacement = replace(
                        parent,
                        state=AuthorityBindingState.RECONCILING,
                        updated_at=now,
                        version=parent.version + 1,
                    )
                    parent = repository.transition_binding(
                        transaction,
                        replacement,
                        expected_state=parent.state,
                        expected_version=parent.version,
                    )
            elif binding is None or operation is None:
                raise LetsLifecycleError("incomplete_lifecycle_intent")
            self._require_same_operation(operation, kind=kind, fingerprint=fingerprint)
            terminal = self._terminal_convergence(binding, operation)
            if terminal is not None:
                if parent is not None:
                    expected_parent_sequence = operation.expected_lease_sequence
                    if expected_parent_sequence is None:
                        raise LetsLifecycleError("spawn_sequence_unavailable")
                    required_sequence = expected_parent_sequence + (
                        1
                        if operation.status
                        in {
                            AuthorityLifecycleStatus.SUCCEEDED,
                            AuthorityLifecycleStatus.RECONCILED,
                        }
                        else 0
                    )
                    if parent.lease_sequence != required_sequence:
                        raise LetsLifecycleError("spawn_parent_fence_mismatch")
                return binding, operation, parent, terminal
            if parent is not None:
                if (
                    parent.state is not AuthorityBindingState.RECONCILING
                    or parent.lease_sequence != operation.expected_lease_sequence
                ):
                    raise LetsLifecycleError("parent_binding_not_fenced", retryable=True)
            operation = self._start_operation(transaction, repository, operation, now)
            return binding, operation, parent, None

    def _prepare_existing_operation(
        self,
        initial: AgentAuthorityBinding,
        operation_id: str,
        kind: AuthorityLifecycleKind,
        fingerprint: str,
    ) -> tuple[
        AgentAuthorityBinding,
        AuthorityLifecycleOperation,
        LifecycleConvergence | None,
    ]:
        plane, repository = self._active_plane()
        now = _now()
        with plane.transaction() as transaction:
            binding = repository.get_binding(
                transaction,
                owner_id=initial.owner_id,
                binding_id=initial.binding_id,
            )
            if binding is None:
                raise LetsLifecycleError("binding_unavailable")
            if binding != initial:
                raise LetsLifecycleError("binding_changed", retryable=True)
            operation = repository.get_lifecycle_operation(
                transaction,
                owner_id=binding.owner_id,
                operation_id=operation_id,
            )
            if operation is None:
                self._require_operation_state(binding, kind)
                operation = AuthorityLifecycleOperation(
                    operation_id=operation_id,
                    owner_id=binding.owner_id,
                    binding_id=binding.binding_id,
                    kind=kind,
                    expected_binding_version=binding.version,
                    expected_lease_sequence=binding.lease_sequence,
                    request_fingerprint=fingerprint,
                    status=AuthorityLifecycleStatus.PENDING,
                    remote_request_id=operation_id,
                    result_digest=None,
                    error_code=None,
                    attempt_count=0,
                    next_attempt_at=now,
                    last_attempt_at=None,
                    reconciled_at=None,
                    reconciliation_digest=None,
                    created_at=now,
                    updated_at=now,
                    version=0,
                )
                operation = repository.create_lifecycle_operation(transaction, operation)
                intent_state = self._intent_state(kind)
                if intent_state is not None and intent_state is not binding.state:
                    replacement = replace(
                        binding,
                        state=intent_state,
                        updated_at=now,
                        version=binding.version + 1,
                    )
                    binding = repository.transition_binding(
                        transaction,
                        replacement,
                        expected_state=binding.state,
                        expected_version=binding.version,
                    )
            self._require_same_operation(operation, kind=kind, fingerprint=fingerprint)
            terminal = self._terminal_convergence(binding, operation)
            if terminal is not None:
                return binding, operation, terminal
            operation = self._start_operation(transaction, repository, operation, now)
            return binding, operation, None

    @staticmethod
    def _start_operation(
        transaction: object,
        repository: AuthorityRepository,
        operation: AuthorityLifecycleOperation,
        now: datetime,
    ) -> AuthorityLifecycleOperation:
        if operation.status is AuthorityLifecycleStatus.IN_FLIGHT:
            raise LetsLifecycleError("lifecycle_operation_in_flight", retryable=True)
        if operation.status not in {
            AuthorityLifecycleStatus.PENDING,
            AuthorityLifecycleStatus.UNCERTAIN,
        }:
            raise LetsLifecycleError("lifecycle_operation_not_retryable")
        started = replace(
            operation,
            status=AuthorityLifecycleStatus.IN_FLIGHT,
            result_digest=None,
            error_code=None,
            attempt_count=operation.attempt_count + 1,
            next_attempt_at=now + _RECOVERY_DELAY,
            last_attempt_at=now,
            updated_at=now,
            version=operation.version + 1,
        )
        return repository.transition_lifecycle_operation(
            transaction,  # type: ignore[arg-type]
            started,
            expected_status=operation.status,
            expected_version=operation.version,
        )

    def _finish_grant(
        self,
        initial_binding: AgentAuthorityBinding,
        initial_operation: AuthorityLifecycleOperation,
        grant: LeaseGrant,
    ) -> LifecycleConvergence:
        if not isinstance(grant, LeaseGrant):
            raise LetsLifecycleError("invalid_lifecycle_response")
        plane, repository = self._active_plane()
        now = _now()
        digest = _wire_sha256(grant)
        with plane.transaction() as transaction:
            binding, operation = self._reload(
                transaction,
                repository,
                initial_binding,
                initial_operation,
            )
            replacement = replace(
                binding,
                warden_id=grant.warden_id,
                lease_id=grant.lease_id,
                lineage_id=grant.lineage_id,
                subject_id=grant.subject_id,
                lease_sequence=0,
                lease_expires_at_ns=grant.expires_at_ns,
                state=AuthorityBindingState.ACTIVE,
                updated_at=now,
                version=binding.version + 1,
            )
            binding = repository.activate_binding(
                transaction,
                replacement,
                expected_version=binding.version,
            )
            operation = self._succeed_operation(
                transaction,
                repository,
                operation,
                digest,
                now,
            )
        return LifecycleConvergence(
            protected=self.config.mode == "enforce",
            binding=binding,
            operation=operation,
            result_sha256=digest,
        )

    def _finish_spawn_grant(
        self,
        initial_binding: AgentAuthorityBinding,
        initial_operation: AuthorityLifecycleOperation,
        initial_parent: AgentAuthorityBinding,
        grant: LeaseGrant,
    ) -> LifecycleConvergence:
        """Activate a child and advance/unfence its parent in one Plane commit.

        LETS atomically increments the parent sequence when it creates a child.
        Keeping the parent in ``RECONCILING`` from durable intent through this
        commit prevents any physical effect from using its now-stale sequence.
        """

        if not isinstance(grant, LeaseGrant):
            raise LetsLifecycleError("invalid_lifecycle_response")
        if grant.parent_id != initial_parent.lease_id:
            raise LetsLifecycleError("spawn_parent_mismatch")
        plane, repository = self._active_plane()
        now = _now()
        digest = _wire_sha256(grant)
        with plane.transaction() as transaction:
            binding, operation = self._reload(
                transaction,
                repository,
                initial_binding,
                initial_operation,
            )
            parent = repository.get_binding(
                transaction,
                owner_id=initial_parent.owner_id,
                binding_id=initial_parent.binding_id,
            )
            if (
                parent is None
                or parent.state is not AuthorityBindingState.RECONCILING
                or parent.lease_id != initial_parent.lease_id
                or parent.lease_sequence != operation.expected_lease_sequence
            ):
                raise LetsLifecycleError("spawn_parent_fence_mismatch", retryable=True)
            parent_replacement = replace(
                parent,
                lease_sequence=parent.lease_sequence + 1,
                state=AuthorityBindingState.ACTIVE,
                updated_at=now,
                version=parent.version + 1,
            )
            repository.transition_binding(
                transaction,
                parent_replacement,
                expected_state=parent.state,
                expected_version=parent.version,
            )
            replacement = replace(
                binding,
                warden_id=grant.warden_id,
                lease_id=grant.lease_id,
                lineage_id=grant.lineage_id,
                subject_id=grant.subject_id,
                lease_sequence=0,
                lease_expires_at_ns=grant.expires_at_ns,
                state=AuthorityBindingState.ACTIVE,
                updated_at=now,
                version=binding.version + 1,
            )
            binding = repository.activate_binding(
                transaction,
                replacement,
                expected_version=binding.version,
            )
            operation = self._succeed_operation(
                transaction,
                repository,
                operation,
                digest,
                now,
            )
        return LifecycleConvergence(
            protected=self.config.mode == "enforce",
            binding=binding,
            operation=operation,
            result_sha256=digest,
        )

    def _finish_existing(
        self,
        initial_binding: AgentAuthorityBinding,
        initial_operation: AuthorityLifecycleOperation,
        result: LeaseSnapshot | BranchRevocation,
    ) -> LifecycleConvergence:
        if not isinstance(result, (LeaseSnapshot, BranchRevocation)):
            raise LetsLifecycleError("invalid_lifecycle_response")
        plane, repository = self._active_plane()
        now = _now()
        digest = _wire_sha256(result)
        with plane.transaction() as transaction:
            binding, operation = self._reload(
                transaction,
                repository,
                initial_binding,
                initial_operation,
            )
            if isinstance(result, LeaseSnapshot):
                if (
                    result.grant.lease_id != binding.lease_id
                    or result.grant.subject_id != binding.subject_id
                    or result.grant.lineage_id != binding.lineage_id
                ):
                    raise LetsLifecycleError("lifecycle_response_binding_mismatch")
                target_state = _snapshot_state(result)
                expected_states = {
                    AuthorityLifecycleKind.RENEW: {_ACTIVE, _QUIESCENT},
                    AuthorityLifecycleKind.QUIESCE: {_QUIESCENT},
                    AuthorityLifecycleKind.RESUME: {_ACTIVE},
                    AuthorityLifecycleKind.CLOSE: {AuthorityBindingState.CLOSED},
                }
                if (
                    operation.kind in expected_states
                    and target_state not in expected_states[operation.kind]
                ):
                    raise LetsLifecycleError("lifecycle_response_state_mismatch")
                replacement = replace(
                    binding,
                    lease_sequence=result.sequence,
                    lease_expires_at_ns=result.grant.expires_at_ns,
                    state=target_state,
                    updated_at=now,
                    version=binding.version + 1,
                )
            else:
                if (
                    result.branch_lease_id != binding.lease_id
                    or result.lineage_id != binding.lineage_id
                ):
                    raise LetsLifecycleError("lifecycle_response_binding_mismatch")
                replacement = replace(
                    binding,
                    state=AuthorityBindingState.REVOKED,
                    updated_at=now,
                    version=binding.version + 1,
                )
            binding = repository.transition_binding(
                transaction,
                replacement,
                expected_state=binding.state,
                expected_version=binding.version,
            )
            operation = self._succeed_operation(
                transaction,
                repository,
                operation,
                digest,
                now,
            )
        return LifecycleConvergence(
            protected=self.config.mode == "enforce",
            binding=binding,
            operation=operation,
            result_sha256=digest,
        )

    async def _finish_remote_error(
        self,
        binding: AgentAuthorityBinding,
        operation: AuthorityLifecycleOperation,
        error: LetsClientBoundaryError,
        *,
        spawn_parent: AgentAuthorityBinding | None = None,
        restore_state: AuthorityBindingState | None = None,
    ) -> LifecycleConvergence:
        uncertain = error.retryable or error.code in _AMBIGUOUS_REMOTE_CODES
        try:
            result = await asyncio.to_thread(
                self._record_remote_error,
                binding,
                operation,
                error.code,
                uncertain,
                spawn_parent,
                restore_state,
            )
        except Exception:
            if self.config.mode == "enforce":
                raise LetsLifecycleError(
                    "lifecycle_persistence_failure",
                    retryable=True,
                ) from None
            return LifecycleConvergence(
                protected=False,
                binding=binding,
                operation=operation,
                error_code="lifecycle_persistence_failure",
                would_deny=self.config.mode == "shadow",
            )
        if self.config.mode == "enforce":
            raise LetsLifecycleError(error.code, retryable=uncertain)
        return replace(result, would_deny=True)

    def _record_remote_error(
        self,
        initial_binding: AgentAuthorityBinding,
        initial_operation: AuthorityLifecycleOperation,
        code: str,
        uncertain: bool,
        initial_spawn_parent: AgentAuthorityBinding | None,
        restore_state: AuthorityBindingState | None,
    ) -> LifecycleConvergence:
        plane, repository = self._active_plane()
        now = _now()
        with plane.transaction() as transaction:
            binding, operation = self._reload(
                transaction,
                repository,
                initial_binding,
                initial_operation,
            )
            status = (
                AuthorityLifecycleStatus.UNCERTAIN
                if uncertain
                else AuthorityLifecycleStatus.FAILED
            )
            replacement = replace(
                operation,
                status=status,
                result_digest=None,
                error_code=code,
                next_attempt_at=(now + _RECOVERY_DELAY if uncertain else None),
                updated_at=now,
                version=operation.version + 1,
            )
            operation = repository.transition_lifecycle_operation(
                transaction,
                replacement,
                expected_status=operation.status,
                expected_version=operation.version,
            )
            if (
                not uncertain
                and binding.state is AuthorityBindingState.PROVISIONING
                and operation.kind
                in {AuthorityLifecycleKind.PROVISION, AuthorityLifecycleKind.SPAWN}
            ):
                abandoned = replace(
                    binding,
                    state=AuthorityBindingState.CLOSED,
                    updated_at=now,
                    version=binding.version + 1,
                )
                binding = repository.abandon_provisioning_binding(
                    transaction,
                    abandoned,
                    expected_version=binding.version,
                )
                if initial_spawn_parent is not None:
                    parent = repository.get_binding(
                        transaction,
                        owner_id=initial_spawn_parent.owner_id,
                        binding_id=initial_spawn_parent.binding_id,
                    )
                    if (
                        parent is None
                        or parent.state is not AuthorityBindingState.RECONCILING
                        or parent.lease_id != initial_spawn_parent.lease_id
                        or parent.lease_sequence
                        != initial_operation.expected_lease_sequence
                    ):
                        raise LetsLifecycleError("spawn_parent_fence_mismatch")
                    parent_replacement = replace(
                        parent,
                        state=AuthorityBindingState.ACTIVE,
                        updated_at=now,
                        version=parent.version + 1,
                    )
                    repository.transition_binding(
                        transaction,
                        parent_replacement,
                        expected_state=parent.state,
                        expected_version=parent.version,
                    )
            elif not uncertain and restore_state is not None:
                restored = replace(
                    binding,
                    state=restore_state,
                    updated_at=now,
                    version=binding.version + 1,
                )
                binding = repository.transition_binding(
                    transaction,
                    restored,
                    expected_state=binding.state,
                    expected_version=binding.version,
                )
        return LifecycleConvergence(
            protected=False,
            binding=binding,
            operation=operation,
            error_code=code,
        )

    @staticmethod
    def _succeed_operation(
        transaction: object,
        repository: AuthorityRepository,
        operation: AuthorityLifecycleOperation,
        digest: str,
        now: datetime,
    ) -> AuthorityLifecycleOperation:
        if operation.kind is AuthorityLifecycleKind.RECONCILE:
            replacement = replace(
                operation,
                status=AuthorityLifecycleStatus.RECONCILED,
                result_digest=digest,
                error_code=None,
                next_attempt_at=None,
                reconciled_at=now,
                reconciliation_digest=digest,
                updated_at=now,
                version=operation.version + 1,
            )
        else:
            replacement = replace(
                operation,
                status=AuthorityLifecycleStatus.SUCCEEDED,
                result_digest=digest,
                error_code=None,
                next_attempt_at=None,
                updated_at=now,
                version=operation.version + 1,
            )
        return repository.transition_lifecycle_operation(
            transaction,  # type: ignore[arg-type]
            replacement,
            expected_status=operation.status,
            expected_version=operation.version,
        )

    @staticmethod
    def _reload(
        transaction: object,
        repository: AuthorityRepository,
        initial_binding: AgentAuthorityBinding,
        initial_operation: AuthorityLifecycleOperation,
    ) -> tuple[AgentAuthorityBinding, AuthorityLifecycleOperation]:
        binding = repository.get_binding(
            transaction,  # type: ignore[arg-type]
            owner_id=initial_binding.owner_id,
            binding_id=initial_binding.binding_id,
        )
        operation = repository.get_lifecycle_operation(
            transaction,  # type: ignore[arg-type]
            owner_id=initial_operation.owner_id,
            operation_id=initial_operation.operation_id,
        )
        if binding is None or operation is None:
            raise LetsLifecycleError("lifecycle_evidence_unavailable")
        if operation.status is not AuthorityLifecycleStatus.IN_FLIGHT:
            raise LetsLifecycleError("lifecycle_operation_not_in_flight")
        return binding, operation

    @staticmethod
    def _require_same_operation(
        operation: AuthorityLifecycleOperation,
        *,
        kind: AuthorityLifecycleKind,
        fingerprint: str,
    ) -> None:
        if operation.kind is not kind or operation.request_fingerprint != fingerprint:
            raise LetsLifecycleError("lifecycle_request_conflict")

    def _terminal_convergence(
        self,
        binding: AgentAuthorityBinding,
        operation: AuthorityLifecycleOperation,
    ) -> LifecycleConvergence | None:
        if operation.status in {
            AuthorityLifecycleStatus.SUCCEEDED,
            AuthorityLifecycleStatus.RECONCILED,
        }:
            return LifecycleConvergence(
                protected=self.config.mode == "enforce",
                binding=binding,
                operation=operation,
                result_sha256=operation.result_digest,
                error_code=operation.error_code,
            )
        if operation.status is AuthorityLifecycleStatus.FAILED:
            return self._deny(operation.error_code or "lifecycle_failed", binding, operation)
        return None

    @staticmethod
    def _require_operation_state(
        binding: AgentAuthorityBinding,
        kind: AuthorityLifecycleKind,
    ) -> None:
        allowed = {
            AuthorityLifecycleKind.RENEW: {_ACTIVE, _QUIESCENT},
            AuthorityLifecycleKind.QUIESCE: {_ACTIVE},
            AuthorityLifecycleKind.RESUME: {_QUIESCENT},
            AuthorityLifecycleKind.CLOSE: {_ACTIVE, _QUIESCENT},
            AuthorityLifecycleKind.REVOKE: {
                _ACTIVE,
                _QUIESCENT,
                AuthorityBindingState.RECONCILING,
                AuthorityBindingState.CLOSING,
            },
            AuthorityLifecycleKind.RECONCILE: {
                _ACTIVE,
                _QUIESCENT,
                AuthorityBindingState.RECONCILING,
                AuthorityBindingState.CLOSING,
                AuthorityBindingState.REVOKING,
            },
        }
        if binding.state not in allowed.get(kind, set()):
            raise LetsLifecycleError("invalid_binding_lifecycle_state")

    @staticmethod
    def _intent_state(kind: AuthorityLifecycleKind) -> AuthorityBindingState | None:
        return {
            AuthorityLifecycleKind.RENEW: AuthorityBindingState.RECONCILING,
            AuthorityLifecycleKind.QUIESCE: AuthorityBindingState.RECONCILING,
            AuthorityLifecycleKind.RESUME: AuthorityBindingState.RECONCILING,
            AuthorityLifecycleKind.CLOSE: AuthorityBindingState.CLOSING,
            AuthorityLifecycleKind.REVOKE: AuthorityBindingState.REVOKING,
            AuthorityLifecycleKind.RECONCILE: AuthorityBindingState.RECONCILING,
        }.get(kind)

    @staticmethod
    def _restorable_state(
        kind: AuthorityLifecycleKind,
        state: AuthorityBindingState,
    ) -> AuthorityBindingState | None:
        """Return a known pre-call stable state safe to restore on a hard denial."""

        if kind is AuthorityLifecycleKind.RECONCILE:
            return None
        if state in {_ACTIVE, _QUIESCENT}:
            return state
        return None

    def _get_binding(
        self,
        owner_id: str,
        binding_id: str,
    ) -> AgentAuthorityBinding | None:
        plane, repository = self._active_plane()
        with plane.transaction() as transaction:
            return repository.get_binding(
                transaction,
                owner_id=owner_id,
                binding_id=binding_id,
            )

    def _get_operation(
        self,
        owner_id: str,
        operation_id: str,
    ) -> AuthorityLifecycleOperation | None:
        plane, repository = self._active_plane()
        with plane.transaction() as transaction:
            return repository.get_lifecycle_operation(
                transaction,
                owner_id=owner_id,
                operation_id=operation_id,
            )

    def _active_plane(self) -> tuple[PlaneAuthorityRuntime, AuthorityRepository]:
        if self.plane is None or self.repository is None:
            raise LetsLifecycleError("lifecycle_runtime_unavailable")
        return self.plane, self.repository

    def _deny(
        self,
        code: str,
        binding: AgentAuthorityBinding | None = None,
        operation: AuthorityLifecycleOperation | None = None,
        *,
        retryable: bool = False,
    ) -> LifecycleConvergence:
        if self.config.mode == "enforce":
            raise LetsLifecycleError(code, retryable=retryable)
        return LifecycleConvergence(
            protected=False,
            binding=binding,
            operation=operation,
            error_code=code,
            would_deny=self.config.mode == "shadow",
        )


class GovernedLifecycleCoordinator:
    """Map committed Astral runtime events onto one current LETS binding.

    The low-level service above deliberately requires explicit durable request
    identities.  Host lifecycle code should not duplicate the rules for
    locating the current owner-scoped binding, advancing runtime generations,
    or deciding whether a reconnect may resume an existing lease.  This bridge
    centralizes those rules while still allocating a fresh operation identity
    for every *new* physical lifecycle mutation.

    Ambiguous mutations are never replaced with a new request: they leave the
    binding in a fenced state for :class:`LetsLifecycleReconciler` to resume
    using the already-persisted operation identity.
    """

    def __init__(
        self,
        service: LetsLifecycleService,
        *,
        identifier_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(service, LetsLifecycleService):
            raise TypeError("LETS lifecycle service is required")
        self.service = service
        self._identifier_factory = identifier_factory or (lambda: str(uuid.uuid4()))

    def latest_binding(
        self,
        *,
        owner_id: str,
        agent_id: str,
        population: AuthorityPopulation,
    ) -> AgentAuthorityBinding | None:
        """Return the latest owner-scoped generation through Plane's public API."""

        if self.service.config.mode == "off" or not self._cohort_enabled(
            agent_id,
            population,
        ):
            return None
        plane, repository = self.service._active_plane()
        with plane.transaction() as transaction:
            return repository.get_latest_binding(
                transaction,
                owner_id=owner_id,
                agent_id=agent_id,
                population=population,
            )

    async def admit_new_runtime(
        self,
        *,
        owner_id: str,
        agent_id: str,
        runtime_id: str,
        population: AuthorityPopulation,
        declared_scopes: Sequence[str],
        executor_conformant: bool = True,
    ) -> LifecycleConvergence:
        """Close the prior generation and issue a root for one new runtime.

        Runtime generations are derived from Plane rather than process-local
        counters, so a host restart cannot accidentally reuse an old receipt
        generation.  A nonterminal predecessor must close successfully before
        the successor intent can be created.
        """

        probe = GovernedRuntime(
            owner_id=owner_id,
            agent_id=agent_id,
            runtime_id=runtime_id,
            runtime_generation=1,
            population=population,
            declared_scopes=tuple(declared_scopes),
            executor_conformant=executor_conformant,
        )
        if not self.service.governs(probe):
            return LifecycleConvergence(protected=False)
        try:
            latest = await asyncio.to_thread(
                self.latest_binding,
                owner_id=owner_id,
                agent_id=agent_id,
                population=population,
            )
        except Exception:
            return self._deny("lifecycle_persistence_failure", retryable=True)
        generation = 1 if latest is None else latest.runtime_generation + 1
        runtime = GovernedRuntime(
            owner_id=owner_id,
            agent_id=agent_id,
            runtime_id=runtime_id,
            runtime_generation=generation,
            population=population,
            declared_scopes=tuple(declared_scopes),
            executor_conformant=executor_conformant,
        )
        predecessor = await self._close_predecessor(latest)
        if predecessor is not None:
            return predecessor
        return await self.service.provision(
            runtime,
            binding_id=self._new_identifier(),
            operation_id=self._new_identifier(),
        )

    async def admit_or_resume_runtime(
        self,
        runtime: GovernedRuntime,
    ) -> LifecycleConvergence:
        """Resume an exact quiesced generation or admit a newer generation."""

        if not self.service.governs(runtime):
            return LifecycleConvergence(protected=False)
        try:
            latest = await asyncio.to_thread(
                self.latest_binding,
                owner_id=runtime.owner_id,
                agent_id=runtime.agent_id,
                population=runtime.population,
            )
        except Exception:
            return self._deny("lifecycle_persistence_failure", retryable=True)
        if latest is None:
            return await self.service.provision(
                runtime,
                binding_id=self._new_identifier(),
                operation_id=self._new_identifier(),
            )

        exact_runtime = (
            latest.runtime_id == runtime.runtime_id
            and latest.runtime_generation == runtime.runtime_generation
        )
        current_epoch = self._current_config_epoch()
        exact_epoch = current_epoch is None or latest.config_epoch == current_epoch
        if exact_runtime and exact_epoch:
            if latest.state is AuthorityBindingState.ACTIVE:
                return self._existing(latest)
            if latest.state is AuthorityBindingState.QUIESCENT:
                return await self.service.resume(
                    owner_id=runtime.owner_id,
                    binding_id=latest.binding_id,
                    operation_id=self._new_identifier(),
                )
            if latest.state.terminal:
                return self._deny("terminal_runtime_generation", latest)
            return self._deny("runtime_generation_reconciling", latest, retryable=True)

        if runtime.runtime_generation <= latest.runtime_generation:
            return self._deny("stale_runtime_generation", latest)
        predecessor = await self._close_predecessor(latest)
        if predecessor is not None:
            return predecessor
        return await self.service.provision(
            runtime,
            binding_id=self._new_identifier(),
            operation_id=self._new_identifier(),
        )

    async def quiesce_current(
        self,
        *,
        owner_id: str,
        agent_id: str,
        population: AuthorityPopulation,
    ) -> LifecycleConvergence:
        """Fence dispatch for pause, disconnect, or host loss."""

        binding = await self._current_or_none(owner_id, agent_id, population)
        if binding is None:
            return LifecycleConvergence(protected=False)
        if binding.state is AuthorityBindingState.QUIESCENT or binding.state.terminal:
            return self._existing(binding)
        if binding.state is not AuthorityBindingState.ACTIVE:
            return self._deny("runtime_generation_reconciling", binding, retryable=True)
        return await self.service.quiesce(
            owner_id=owner_id,
            binding_id=binding.binding_id,
            operation_id=self._new_identifier(),
        )

    async def close_current(
        self,
        *,
        owner_id: str,
        agent_id: str,
        population: AuthorityPopulation,
    ) -> LifecycleConvergence:
        """Drain and terminalize the current runtime generation."""

        binding = await self._current_or_none(owner_id, agent_id, population)
        if binding is None:
            return LifecycleConvergence(protected=False)
        if binding.state.terminal:
            return self._existing(binding)
        if binding.state not in {_ACTIVE, _QUIESCENT}:
            return self._deny("runtime_generation_reconciling", binding, retryable=True)
        return await self.service.close(
            owner_id=owner_id,
            binding_id=binding.binding_id,
            operation_id=self._new_identifier(),
        )

    async def close_runtime_generation(
        self,
        *,
        owner_id: str,
        agent_id: str,
        runtime_id: str,
        runtime_generation: int,
        population: AuthorityPopulation,
    ) -> LifecycleConvergence:
        """Close only the exact current runtime generation.

        Successor admission closes its predecessor before provisioning the new
        binding.  Cleanup of that predecessor can run later, after the
        successor is already current; it must therefore never translate into
        ``close_current`` and accidentally close the successor.  A request for
        an older generation is an idempotent no-op, while a same/newer but
        mismatched identity remains a fail-closed lifecycle conflict.
        """

        if not isinstance(runtime_id, str) or not runtime_id:
            raise ValueError("runtime_id must be non-empty")
        if type(runtime_generation) is not int or runtime_generation < 1:
            raise ValueError("runtime_generation must be positive")
        binding = await self._current_or_none(owner_id, agent_id, population)
        if binding is None:
            return LifecycleConvergence(protected=False)
        if runtime_generation < binding.runtime_generation:
            return self._existing(binding)
        exact_runtime = (
            binding.runtime_id == runtime_id
            and binding.runtime_generation == runtime_generation
        )
        if not exact_runtime:
            return self._deny("runtime_generation_mismatch", binding)
        if binding.state.terminal:
            return self._existing(binding)
        if binding.state not in {_ACTIVE, _QUIESCENT}:
            return self._deny("runtime_generation_reconciling", binding, retryable=True)
        return await self.service.close(
            owner_id=owner_id,
            binding_id=binding.binding_id,
            operation_id=self._new_identifier(),
        )

    async def revoke_current(
        self,
        *,
        owner_id: str,
        agent_id: str,
        population: AuthorityPopulation,
        reason_code: str,
    ) -> LifecycleConvergence:
        """Revoke the current branch for deletion, compromise, or ownership loss."""

        binding = await self._current_or_none(owner_id, agent_id, population)
        if binding is None:
            return LifecycleConvergence(protected=False)
        if binding.state.terminal:
            return self._existing(binding)
        return await self.service.revoke(
            owner_id=owner_id,
            binding_id=binding.binding_id,
            operation_id=self._new_identifier(),
            reason_code=reason_code,
        )

    async def renew_current_if_due(
        self,
        *,
        owner_id: str,
        agent_id: str,
        population: AuthorityPopulation,
        now_ns: int,
        renewal_window_ns: int,
    ) -> LifecycleConvergence:
        """Renew one active/quiescent lease only inside a bounded due window."""

        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("now_ns must be non-negative")
        if type(renewal_window_ns) is not int or renewal_window_ns < 0:
            raise ValueError("renewal_window_ns must be non-negative")
        binding = await self._current_or_none(owner_id, agent_id, population)
        if binding is None:
            return LifecycleConvergence(protected=False)
        if binding.state not in {_ACTIVE, _QUIESCENT}:
            if binding.state.terminal:
                return self._existing(binding)
            return self._deny("runtime_generation_reconciling", binding, retryable=True)
        if binding.lease_expires_at_ns > now_ns + renewal_window_ns:
            return self._existing(binding)
        return await self.service.renew(
            owner_id=owner_id,
            binding_id=binding.binding_id,
            operation_id=self._new_identifier(),
        )

    async def _close_predecessor(
        self,
        binding: AgentAuthorityBinding | None,
    ) -> LifecycleConvergence | None:
        if binding is None or binding.state.terminal:
            return None
        if binding.state not in {_ACTIVE, _QUIESCENT}:
            return self._deny("runtime_generation_reconciling", binding, retryable=True)
        result = await self.service.close(
            owner_id=binding.owner_id,
            binding_id=binding.binding_id,
            operation_id=self._new_identifier(),
        )
        if result.binding is not None and result.binding.state.terminal:
            return None
        if result.error_code is not None:
            return result
        return self._deny("predecessor_close_incomplete", result.binding, retryable=True)

    async def _current_or_none(
        self,
        owner_id: str,
        agent_id: str,
        population: AuthorityPopulation,
    ) -> AgentAuthorityBinding | None:
        if self.service.config.mode == "off" or not self._cohort_enabled(
            agent_id,
            population,
        ):
            return None
        try:
            return await asyncio.to_thread(
                self.latest_binding,
                owner_id=owner_id,
                agent_id=agent_id,
                population=population,
            )
        except Exception:
            if self.service.config.mode == "enforce":
                raise LetsLifecycleError(
                    "lifecycle_persistence_failure",
                    retryable=True,
                ) from None
            return None

    def _cohort_enabled(
        self,
        agent_id: str,
        population: AuthorityPopulation,
    ) -> bool:
        if not isinstance(population, AuthorityPopulation):
            raise TypeError("authority population is required")
        config = self.service.config
        if population.value not in config.governed_cohorts:
            return False
        return not config.governed_agent_allowlist or agent_id in config.governed_agent_allowlist

    def _current_config_epoch(self) -> int | None:
        manifest = self.service.config.trust_manifest
        return None if manifest is None else manifest.config_epoch

    def _new_identifier(self) -> str:
        value = self._identifier_factory()
        if not isinstance(value, str) or not value:
            raise LetsLifecycleError("invalid_lifecycle_operation_identity")
        return value

    def _existing(self, binding: AgentAuthorityBinding) -> LifecycleConvergence:
        return LifecycleConvergence(
            protected=self.service.config.mode == "enforce",
            binding=binding,
        )

    def _deny(
        self,
        code: str,
        binding: AgentAuthorityBinding | None = None,
        *,
        retryable: bool = False,
    ) -> LifecycleConvergence:
        if self.service.config.mode == "enforce":
            raise LetsLifecycleError(code, retryable=retryable)
        return LifecycleConvergence(
            protected=False,
            binding=binding,
            error_code=code,
            would_deny=self.service.config.mode == "shadow",
        )


__all__ = (
    "GovernedLifecycleCoordinator",
    "GovernedRuntime",
    "LifecycleRecoveryContext",
    "LetsLifecycleError",
    "LetsLifecycleService",
    "LifecycleConvergence",
    "PlaneAuthorityRuntime",
)
