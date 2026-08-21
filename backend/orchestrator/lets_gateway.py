"""LETS authorization transport and final protected-executor gateway.

This module keeps two security boundaries explicit:

* :class:`LetsAuthorizationGateway` asks the independently deployed warden for
  authority after Astral's existing gates and final argument rewrites; and
* :class:`ReceiptExecutorGateway` performs host binding checks and calls the
  public LETS ``ReceiptVerifier.verify_and_claim`` immediately before an
  actuator.

The signed receipt is carried in MCP/A2A/internal envelope metadata under the
``astraldeep.lets/v1`` key.  It is never inserted into tool arguments.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol, TypeVar

from lets.canonical import canonical_digest, strict_json_loads
from lets.crypto import PublicKeyRegistry
from lets.errors import (
    ClockUncertainError,
    PolicyError,
    ReplayError,
    SignatureError,
    StorageError,
    ValidationError,
)
from lets.executor import (
    ExecutorPolicy,
    ReceiptVerifier,
    SQLiteReceiptReplayStore,
    executor_replay_identity,
)
from lets.executor_authority import ProcessFileExecutorAuthorityAnchor
from lets.manifest import ClusterManifest
from lets.models import Receipt

from orchestrator.lets_client import LetsClientBoundaryError, LetsWardenClient
from orchestrator.lets_config import LetsHostConfig
from orchestrator.lets_scope_profile import RESOURCE_DIMENSIONS
from orchestrator.protected_dispatch import (
    ProtectedDispatchContext,
    ProtectedDispatchError,
    canonical_wire_arguments_sha256,
    recompute_effect_sha256_from_evidence,
)


LETS_CALLER_CAPABILITY: Final = "astraldeep.lets/v1"
PERMIT_TYPE: Final = "astraldeep.protected-permit/v1"
_ACTIVE_BINDING_STATE: Final = "active"
_READY_STATE: Final = "ready"

T = TypeVar("T")


class AuthorityBinding(Protocol):
    """Host-neutral fields required from an AstralPlane authority binding."""

    binding_id: str
    owner_id: str
    agent_id: str
    runtime_id: str
    runtime_generation: int
    population: object
    tenant_id: str
    envelope_id: str
    warden_id: str
    lease_id: str
    lineage_id: str
    subject_id: str
    policy_digest: str
    machine_digest: str
    config_epoch: int
    capabilities: tuple[str, ...]
    lease_sequence: int
    lease_expires_at_ns: int
    state: object


class GatewayEvidenceObserver(Protocol):
    def __call__(
        self,
        event: str,
        evidence: Mapping[str, str | int | bool | None],
    ) -> object | Awaitable[object]: ...


class DurableEffectCoordinator(Protocol):
    """Deep-owned Plane checkpoint seam used without exposing Plane internals."""

    def prepare_authorization(
        self,
        *,
        binding: object,
        context: ProtectedDispatchContext,
    ) -> object: ...

    def record_receipt(
        self,
        *,
        binding: object,
        context: ProtectedDispatchContext,
        envelope: "ProtectedPermitEnvelope",
    ) -> object: ...

    def claim_for_execution(
        self,
        *,
        envelope: "ProtectedPermitEnvelope",
        replay_status: object,
    ) -> object: ...

    def fail_before_execution(
        self,
        *,
        owner_id: str,
        operation_id: str,
        error_code: str,
        denied: bool = False,
    ) -> object: ...

    def record_outcome(
        self,
        *,
        owner_id: str,
        operation_id: str,
        outcome: str,
        error_code: str | None = None,
    ) -> object: ...


class LetsGatewayError(RuntimeError):
    """Typed, content-free refusal safe for user and operator projection."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def _exact_digest(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LetsGatewayError(f"invalid_{label}")
    return value


def _binding_state(binding: AuthorityBinding) -> str:
    value = getattr(binding.state, "value", binding.state)
    return value if isinstance(value, str) else ""


def _binding_population(binding: AuthorityBinding) -> str:
    value = getattr(binding.population, "value", binding.population)
    return value if isinstance(value, str) else ""


def _receipt_digest(receipt: Receipt) -> str:
    return canonical_digest(receipt.to_dict()).removeprefix("sha256:")


def _safe_evidence(
    *,
    context: ProtectedDispatchContext,
    binding_id: str | None,
    code: str | None = None,
    enforced: bool,
) -> Mapping[str, str | int | bool | None]:
    return {
        "operation_id": context.operation_id,
        "audit_correlation_id": context.audit_correlation_id,
        "agent_id": context.agent_id,
        "runtime_id": context.runtime_id,
        "tool_id": context.tool_id,
        "scope": context.scope,
        "channel": context.channel,
        "binding_id": binding_id,
        "enforced": enforced,
        "code": code,
    }


@dataclass(frozen=True, slots=True)
class ProtectedPermitEnvelope:
    """Strict receipt envelope carried outside ordinary tool arguments."""

    binding_id: str
    owner_id: str
    runtime_generation: int
    context: Mapping[str, str | int]
    expected_sequence: int
    nonce: str = field(repr=False)
    wire_arguments_sha256: str = field(repr=False)
    receipt: Receipt = field(repr=False)

    def to_metadata(self) -> dict[str, object]:
        return {
            "type": PERMIT_TYPE,
            "binding_id": self.binding_id,
            "owner_id": self.owner_id,
            "runtime_generation": self.runtime_generation,
            "context": dict(self.context),
            "expected_sequence": self.expected_sequence,
            "nonce": self.nonce,
            "wire_arguments_sha256": self.wire_arguments_sha256,
            "receipt": self.receipt.to_dict(),
        }

    @classmethod
    def from_metadata(cls, value: object) -> "ProtectedPermitEnvelope":
        if not isinstance(value, Mapping):
            raise LetsGatewayError("missing_protected_permit")
        expected = {
            "type",
            "binding_id",
            "owner_id",
            "runtime_generation",
            "context",
            "expected_sequence",
            "nonce",
            "wire_arguments_sha256",
            "receipt",
        }
        if set(value) != expected or value.get("type") != PERMIT_TYPE:
            raise LetsGatewayError("invalid_protected_permit")
        context = value.get("context")
        if not isinstance(context, Mapping) or any(
            not isinstance(key, str) or not isinstance(item, (str, int))
            or isinstance(item, bool)
            for key, item in context.items()
        ):
            raise LetsGatewayError("invalid_protected_context")
        generation = value.get("runtime_generation")
        if type(generation) is not int or generation < 1:
            raise LetsGatewayError("invalid_runtime_generation")
        expected_sequence = value.get("expected_sequence")
        if type(expected_sequence) is not int or expected_sequence < 0:
            raise LetsGatewayError("invalid_expected_sequence")
        nonce = value.get("nonce")
        if not isinstance(nonce, str) or not 16 <= len(nonce) <= 256:
            raise LetsGatewayError("invalid_nonce")
        try:
            receipt = Receipt.from_dict(dict(value["receipt"]))  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError, ValidationError):
            raise LetsGatewayError("invalid_receipt") from None
        binding_id = value.get("binding_id")
        owner_id = value.get("owner_id")
        if not isinstance(binding_id, str) or not binding_id:
            raise LetsGatewayError("invalid_binding_id")
        if not isinstance(owner_id, str) or not owner_id:
            raise LetsGatewayError("invalid_owner_id")
        wire_digest = _exact_digest(
            value.get("wire_arguments_sha256"),  # type: ignore[arg-type]
            "wire_arguments_digest",
        )
        return cls(
            binding_id=binding_id,
            owner_id=owner_id,
            runtime_generation=generation,
            context=dict(context),
            expected_sequence=expected_sequence,
            nonce=nonce,
            wire_arguments_sha256=wire_digest,
            receipt=receipt,
        )


@dataclass(slots=True)
class IssuedPermit:
    """One authorization result while its binding-order lock is held."""

    operation_id: str
    enforced: bool
    shadow: bool
    envelope: ProtectedPermitEnvelope | None = field(default=None, repr=False)
    would_deny_code: str | None = None
    _lock: asyncio.Lock | None = field(default=None, repr=False)
    _released: bool = field(default=False, repr=False)

    def caller_capabilities(self) -> dict[str, object]:
        if not self.enforced or self.envelope is None:
            return {}
        return {LETS_CALLER_CAPABILITY: self.envelope.to_metadata()}

    def release(self) -> None:
        if not self._released and self._lock is not None:
            self._lock.release()
            self._released = True

    async def __aenter__(self) -> "IssuedPermit":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.release()


class LetsAuthorizationGateway:
    """Authorize protected effects and serialize one runtime through claim."""

    def __init__(
        self,
        config: LetsHostConfig,
        client: LetsWardenClient | None,
        *,
        observer: GatewayEvidenceObserver | None = None,
        effect_coordinator: DurableEffectCoordinator | None = None,
    ) -> None:
        if not isinstance(config, LetsHostConfig):
            raise TypeError("LETS host config is required")
        if config.mode != "off" and client is None:
            raise LetsGatewayError("lets_client_unavailable")
        if config.mode == "enforce" and effect_coordinator is None:
            raise LetsGatewayError("plane_effect_coordinator_unavailable")
        self.config = config
        self.client = client
        self._observer = observer
        self._effect_coordinator = effect_coordinator
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._locks_guard = threading.Lock()

    def governs(
        self,
        *,
        population: str,
        agent_id: str,
        executor_conformant: bool,
    ) -> bool:
        if self.config.mode == "off":
            return False
        if population not in self.config.governed_cohorts:
            return False
        allowlist = self.config.governed_agent_allowlist
        if allowlist and agent_id not in allowlist:
            return False
        if population == "byo_user" and not executor_conformant:
            if self.config.mode == "enforce":
                raise LetsGatewayError("executor_not_conformant")
            return True
        return True

    async def authorize(
        self,
        *,
        binding: AuthorityBinding | None,
        population: str,
        executor_conformant: bool,
        context: ProtectedDispatchContext,
        final_arguments: Mapping[str, object],
        authorized_effect: Mapping[str, object],
        observer: GatewayEvidenceObserver | None = None,
    ) -> IssuedPermit:
        """Return a permit without ever placing it in ``final_arguments``."""

        context.assert_snapshot_matches(
            final_arguments=final_arguments,
            authorized_effect=authorized_effect,
        )
        if not self.governs(
            population=population,
            agent_id=context.agent_id,
            executor_conformant=executor_conformant,
        ):
            await self._observe(
                "off_or_ungoverned",
                _safe_evidence(
                    context=context,
                    binding_id=None if binding is None else binding.binding_id,
                    enforced=False,
                ),
                observer=observer,
            )
            return IssuedPermit(
                operation_id=context.operation_id,
                enforced=False,
                shadow=False,
            )

        if binding is None:
            return await self._deny_or_shadow(
                context,
                None,
                "binding_unavailable",
                observer=observer,
            )

        try:
            self._validate_binding(binding, population=population, context=context)
        except LetsGatewayError as exc:
            return await self._deny_or_shadow(
                context,
                binding.binding_id,
                exc.code,
                retryable=exc.retryable,
                observer=observer,
            )

        key = (binding.warden_id, binding.lease_id, context.executor_audience)
        lock = self._lock_for(key)
        await lock.acquire()
        coordinator = self._effect_coordinator
        if self.config.mode == "enforce":
            assert coordinator is not None  # Constructor fail-closed fence.
            try:
                await asyncio.to_thread(
                    coordinator.prepare_authorization,
                    binding=binding,
                    context=context,
                )
            except Exception as exc:
                lock.release()
                code, retryable = self._coordinator_error(
                    exc,
                    default="effect_persistence_unavailable",
                )
                return await self._deny_or_shadow(
                    context,
                    binding.binding_id,
                    code,
                    retryable=retryable,
                    observer=observer,
                )
        try:
            assert self.client is not None  # Active mode constructor fence.
            receipt = await asyncio.to_thread(
                self.client.authorize_tool,
                operation_id=context.operation_id,
                lease_id=binding.lease_id,
                agent_id=context.agent_id,
                declared_scope=context.scope,
                executor_audience=context.executor_audience,
                nonce=context.nonce,
                evidence=dict(context.lets_evidence()),
                expected_state=_READY_STATE,
                expected_sequence=context.expected_sequence,
            )
            self._validate_receipt(receipt, binding=binding, context=context)
        except LetsClientBoundaryError as exc:
            if self.config.mode == "enforce" and not exc.retryable:
                await self._checkpoint_failure(
                    coordinator,
                    binding=binding,
                    context=context,
                    code=exc.code,
                    denied=True,
                )
            lock.release()
            return await self._deny_or_shadow(
                context,
                binding.binding_id,
                exc.code,
                retryable=exc.retryable,
                observer=observer,
            )
        except LetsGatewayError as exc:
            if self.config.mode == "enforce":
                await self._checkpoint_failure(
                    coordinator,
                    binding=binding,
                    context=context,
                    code=exc.code,
                )
            lock.release()
            return await self._deny_or_shadow(
                context,
                binding.binding_id,
                exc.code,
                retryable=exc.retryable,
                observer=observer,
            )
        except Exception:
            # The warden may have committed while its response was lost. Keep
            # the exact LETS_PENDING intent recoverable under the same request
            # identity; never invent a new operation here.
            lock.release()
            return await self._deny_or_shadow(
                context,
                binding.binding_id,
                "authorization_outcome_uncertain",
                retryable=True,
                observer=observer,
            )

        if self.config.mode == "shadow":
            lock.release()
            await self._observe(
                "shadow_authorized",
                _safe_evidence(
                    context=context,
                    binding_id=binding.binding_id,
                    enforced=False,
                ),
                observer=observer,
            )
            return IssuedPermit(
                operation_id=context.operation_id,
                enforced=False,
                shadow=True,
            )

        envelope = ProtectedPermitEnvelope(
            binding_id=binding.binding_id,
            owner_id=binding.owner_id,
            runtime_generation=binding.runtime_generation,
            context=dict(context.lets_evidence()),
            expected_sequence=context.expected_sequence,
            nonce=context.nonce,
            wire_arguments_sha256=context.wire_arguments_sha256,
            receipt=receipt,
        )
        try:
            assert coordinator is not None
            await asyncio.to_thread(
                coordinator.record_receipt,
                binding=binding,
                context=context,
                envelope=envelope,
            )
        except Exception as exc:
            lock.release()
            code, retryable = self._coordinator_error(
                exc,
                default="effect_persistence_unavailable",
            )
            return await self._deny_or_shadow(
                context,
                binding.binding_id,
                code,
                retryable=retryable,
                observer=observer,
            )
        try:
            await self._observe(
                "receipt_received",
                {
                    **_safe_evidence(
                        context=context,
                        binding_id=binding.binding_id,
                        enforced=True,
                    ),
                    "receipt_sha256": _receipt_digest(receipt),
                    "resulting_sequence": receipt.resulting_sequence,
                },
                observer=observer,
            )
        except Exception:
            # An enforce audit append is part of the authorization boundary.
            # Release the per-runtime lock before refusing so an unavailable
            # recorder cannot deadlock every later attempt for this lease.
            lock.release()
            raise LetsGatewayError("audit_append_failed", retryable=True) from None
        return IssuedPermit(
            operation_id=context.operation_id,
            enforced=True,
            shadow=False,
            envelope=envelope,
            _lock=lock,
        )

    async def _checkpoint_failure(
        self,
        coordinator: DurableEffectCoordinator | None,
        *,
        binding: AuthorityBinding,
        context: ProtectedDispatchContext,
        code: str,
        denied: bool = False,
    ) -> None:
        if coordinator is None:
            return
        try:
            await asyncio.to_thread(
                coordinator.fail_before_execution,
                owner_id=binding.owner_id,
                operation_id=context.operation_id,
                error_code=code,
                denied=denied,
            )
        except Exception:
            # The original refusal remains authoritative. Recovery will find
            # the nonterminal Plane intent; never allow an audit write failure
            # to turn a denial into execution.
            return

    @staticmethod
    def _coordinator_error(
        error: Exception,
        *,
        default: str,
    ) -> tuple[str, bool]:
        code = getattr(error, "code", default)
        retryable = getattr(error, "retryable", True)
        if not isinstance(code, str) or not code:
            code = default
        return code, bool(retryable)

    async def _deny_or_shadow(
        self,
        context: ProtectedDispatchContext,
        binding_id: str | None,
        code: str,
        *,
        retryable: bool = False,
        observer: GatewayEvidenceObserver | None = None,
    ) -> IssuedPermit:
        await self._observe(
            "would_deny" if self.config.mode == "shadow" else "denied",
            _safe_evidence(
                context=context,
                binding_id=binding_id,
                code=code,
                enforced=self.config.mode == "enforce",
            ),
            observer=observer,
        )
        if self.config.mode == "enforce":
            raise LetsGatewayError(code, retryable=retryable)
        return IssuedPermit(
            operation_id=context.operation_id,
            enforced=False,
            shadow=True,
            would_deny_code=code,
        )

    def _lock_for(self, key: tuple[str, str, str]) -> asyncio.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    @staticmethod
    def _validate_binding(
        binding: AuthorityBinding,
        *,
        population: str,
        context: ProtectedDispatchContext,
    ) -> None:
        expected = (
            binding.agent_id,
            binding.runtime_id,
            _binding_population(binding),
            _binding_state(binding),
            binding.lease_sequence,
        )
        actual = (
            context.agent_id,
            context.runtime_id,
            population,
            _ACTIVE_BINDING_STATE,
            context.expected_sequence,
        )
        if expected != actual:
            raise LetsGatewayError("binding_mismatch")
        if context.capability not in binding.capabilities:
            raise LetsGatewayError("capability_not_bound")

    @staticmethod
    def _validate_receipt(
        receipt: Receipt,
        *,
        binding: AuthorityBinding,
        context: ProtectedDispatchContext,
    ) -> None:
        evidence_digest = canonical_digest(dict(context.lets_evidence()))
        dimension = context.resource_dimension
        if (
            type(dimension) is not int
            or dimension < 0
            or dimension >= RESOURCE_DIMENSIONS
        ):
            raise LetsGatewayError("receipt_binding_mismatch")
        expected_cost = tuple(
            1 if index == dimension else 0
            for index in range(RESOURCE_DIMENSIONS)
        )
        checks = (
            (receipt.request_id, context.operation_id),
            (receipt.tenant_id, binding.tenant_id),
            (receipt.envelope_id, binding.envelope_id),
            (receipt.warden_id, binding.warden_id),
            (receipt.lease_id, binding.lease_id),
            (receipt.lineage_id, binding.lineage_id),
            (receipt.subject_id, binding.subject_id),
            (receipt.policy_digest, binding.policy_digest),
            (receipt.machine_digest, binding.machine_digest),
            (receipt.config_epoch, binding.config_epoch),
            (receipt.executor_audience, context.executor_audience),
            (receipt.transition, context.transition),
            (tuple(receipt.cost), expected_cost),
            (receipt.nonce, context.nonce),
            (receipt.evidence_digest, evidence_digest),
            (receipt.source_state, _READY_STATE),
            (receipt.target_state, _READY_STATE),
            (receipt.resulting_sequence, context.expected_sequence + 1),
        )
        if any(actual != expected for actual, expected in checks):
            raise LetsGatewayError("receipt_binding_mismatch")

    async def _observe(
        self,
        event: str,
        evidence: Mapping[str, str | int | bool | None],
        *,
        observer: GatewayEvidenceObserver | None = None,
    ) -> None:
        selected = observer or self._observer
        if selected is None:
            return
        value = selected(event, evidence)
        if isinstance(value, Awaitable):
            await value


class ReceiptExecutorGateway:
    """Verify exact host context and durably claim immediately before effect."""

    def __init__(
        self,
        verifier: ReceiptVerifier,
        *,
        replay_status: Callable[[], object] | None = None,
        effect_coordinator: DurableEffectCoordinator | None = None,
    ) -> None:
        if not isinstance(verifier, ReceiptVerifier):
            raise TypeError("public LETS receipt verifier is required")
        if (replay_status is None) != (effect_coordinator is None):
            raise TypeError(
                "replay status and durable effect coordinator must be configured together"
            )
        self._verifier = verifier
        self._replay_status = replay_status
        self._effect_coordinator = effect_coordinator
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def verify_and_claim(
        self,
        *,
        metadata: object,
        final_arguments: Mapping[str, object],
        owner_id: str,
        binding_id: str,
        lease_id: str,
        lineage_id: str,
        agent_id: str,
        runtime_id: str,
        runtime_generation: int,
        tool_id: str,
        executor_audience: str,
    ) -> ProtectedPermitEnvelope:
        envelope = ProtectedPermitEnvelope.from_metadata(metadata)
        context = envelope.context
        required_context = {
            "type",
            "operation_id",
            "agent_id",
            "runtime_id",
            "tool_id",
            "scope",
            "capability",
            "transition",
            "resource_dimension",
            "executor_audience",
            "channel",
            "audit_correlation_id",
            "scope_profile_sha256",
            "authorized_effect_sha256",
            "effect_sha256",
        }
        if set(context) != required_context:
            raise LetsGatewayError("invalid_protected_context")
        host_checks = (
            (envelope.owner_id, owner_id),
            (envelope.binding_id, binding_id),
            (envelope.receipt.lease_id, lease_id),
            (envelope.receipt.lineage_id, lineage_id),
            (context["agent_id"], agent_id),
            (context["runtime_id"], runtime_id),
            (envelope.runtime_generation, runtime_generation),
            (context["tool_id"], tool_id),
            (context["executor_audience"], executor_audience),
            (envelope.receipt.request_id, context["operation_id"]),
            (envelope.receipt.subject_id, agent_id),
            (envelope.receipt.executor_audience, executor_audience),
            (envelope.receipt.transition, context["transition"]),
            (envelope.receipt.nonce, envelope.nonce),
            (
                envelope.receipt.resulting_sequence,
                envelope.expected_sequence + 1,
            ),
        )
        if any(actual != expected for actual, expected in host_checks):
            raise LetsGatewayError("executor_host_binding_mismatch")
        dimension = context["resource_dimension"]
        cost = tuple(envelope.receipt.cost)
        expected_cost = (
            tuple(
                1 if index == dimension else 0
                for index in range(RESOURCE_DIMENSIONS)
            )
            if type(dimension) is int and 0 <= dimension < RESOURCE_DIMENSIONS
            else ()
        )
        if (
            type(dimension) is not int
            or dimension < 0
            or dimension >= RESOURCE_DIMENSIONS
            or cost != expected_cost
        ):
            raise LetsGatewayError("executor_cost_mismatch")
        try:
            recomputed_effect = recompute_effect_sha256_from_evidence(
                context,
                expected_sequence=envelope.expected_sequence,
                nonce=envelope.nonce,
            )
        except ProtectedDispatchError:
            raise LetsGatewayError("invalid_protected_context") from None
        if not hmac.compare_digest(
            recomputed_effect,
            str(context["effect_sha256"]),
        ):
            raise LetsGatewayError("executor_effect_digest_mismatch")
        wire_digest = canonical_wire_arguments_sha256(final_arguments)
        if not hmac.compare_digest(wire_digest, envelope.wire_arguments_sha256):
            raise LetsGatewayError("executor_arguments_mutated")
        if envelope.receipt.evidence_digest != canonical_digest(dict(context)):
            raise LetsGatewayError("executor_evidence_mismatch")

        lock = self._binding_lock(envelope.binding_id)
        with lock:
            try:
                self._verifier.verify_and_claim(envelope.receipt)
            except ReplayError:
                raise LetsGatewayError("receipt_replayed") from None
            except ClockUncertainError:
                raise LetsGatewayError("clock_uncertain", retryable=True) from None
            except SignatureError:
                raise LetsGatewayError("receipt_signature_invalid") from None
            except PolicyError:
                raise LetsGatewayError("receipt_policy_invalid") from None
            except StorageError:
                raise LetsGatewayError("replay_store_unavailable", retryable=True) from None
            except (ValidationError, TypeError, ValueError):
                raise LetsGatewayError("receipt_invalid") from None
            if self._effect_coordinator is not None:
                try:
                    assert self._replay_status is not None
                    status = self._replay_status()
                    self._effect_coordinator.claim_for_execution(
                        envelope=envelope,
                        replay_status=status,
                    )
                except Exception as exc:
                    code, retryable = LetsAuthorizationGateway._coordinator_error(
                        exc,
                        default="plane_claim_unavailable",
                    )
                    raise LetsGatewayError(code, retryable=retryable) from None
        return envelope

    def claim_and_invoke(
        self,
        *,
        metadata: object,
        final_arguments: Mapping[str, object],
        owner_id: str,
        binding_id: str,
        lease_id: str,
        lineage_id: str,
        agent_id: str,
        runtime_id: str,
        runtime_generation: int,
        tool_id: str,
        executor_audience: str,
        actuator: Callable[[], T],
        failure_is_uncertain: bool = True,
    ) -> T:
        envelope = self.verify_and_claim(
            metadata=metadata,
            final_arguments=final_arguments,
            owner_id=owner_id,
            binding_id=binding_id,
            lease_id=lease_id,
            lineage_id=lineage_id,
            agent_id=agent_id,
            runtime_id=runtime_id,
            runtime_generation=runtime_generation,
            tool_id=tool_id,
            executor_audience=executor_audience,
        )
        try:
            result = actuator()
        except Exception:
            self.record_outcome(
                envelope,
                outcome=("outcome_uncertain" if failure_is_uncertain else "effect_failed"),
                error_code=(
                    "effect_result_lost" if failure_is_uncertain else "effect_failed"
                ),
            )
            raise
        self.record_outcome(envelope, outcome="succeeded")
        return result

    def record_outcome(
        self,
        envelope: ProtectedPermitEnvelope,
        *,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        """Persist a redacted result when this executor owns Plane evidence."""

        if self._effect_coordinator is None:
            return
        try:
            self._effect_coordinator.record_outcome(
                owner_id=envelope.owner_id,
                operation_id=envelope.receipt.request_id,
                outcome=outcome,
                error_code=error_code,
            )
        except Exception as exc:
            code, retryable = LetsAuthorizationGateway._coordinator_error(
                exc,
                default="effect_persistence_unavailable",
            )
            raise LetsGatewayError(code, retryable=retryable) from None

    def _binding_lock(self, binding_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(binding_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[binding_id] = lock
            return lock


@dataclass(slots=True)
class ExecutorGatewayRuntime:
    gateway: ReceiptExecutorGateway
    replay_store: SQLiteReceiptReplayStore = field(repr=False)
    authority_anchor: ProcessFileExecutorAuthorityAnchor | None = field(
        default=None,
        repr=False,
    )

    def close(self) -> None:
        if self.authority_anchor is not None:
            self.authority_anchor.close()


def create_executor_gateway(
    config: LetsHostConfig,
    *,
    effect_coordinator: DurableEffectCoordinator | None = None,
) -> ExecutorGatewayRuntime:
    """Build/reopen the pinned public LETS verifier from authenticated config."""

    if not isinstance(config, LetsHostConfig) or config.mode == "off":
        raise LetsGatewayError("executor_not_configured")
    manifest_ref = config.trust_manifest
    if (
        manifest_ref is None
        or config.executor_db_root is None
        or config.executor_instance_id is None
        or config.tenant_id is None
        or config.envelope_id is None
        or config.policy_digest is None
        or config.machine_digest is None
    ):
        raise LetsGatewayError("executor_not_configured")
    try:
        raw = manifest_ref.path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest_ref.sha256:
            raise LetsGatewayError("trust_manifest_changed")
        document = strict_json_loads(raw)
        if not isinstance(document, Mapping):
            raise LetsGatewayError("trust_manifest_invalid")
        manifest = ClusterManifest.from_dict(
            dict(document),
            allow_insecure_http=config.environment in {"development", "dev", "test"},
        )
        warden = manifest.warden(manifest_ref.warden_id)
        policies = [item for item in manifest.policies if item.digest == config.policy_digest]
        if len(policies) != 1:
            raise LetsGatewayError("trust_manifest_policy_mismatch")
        policy_spec = policies[0]
        if policy_spec.machine.digest != config.machine_digest:
            raise LetsGatewayError("trust_manifest_machine_mismatch")
    except LetsGatewayError:
        raise
    except Exception:
        raise LetsGatewayError("trust_manifest_invalid") from None

    registry = PublicKeyRegistry()
    for key in warden.keys:
        registry.register(
            warden.warden_id,
            key.key_id,
            key.public_key,
            not_before_ns=key.not_before_ns,
            not_after_ns=key.not_after_ns,
        )
    policy = ExecutorPolicy(
        audience=config.executor_instance_id,
        tenant_id=config.tenant_id,
        envelope_id=config.envelope_id,
        config_epoch=manifest_ref.config_epoch,
        allowed_policy_digests=frozenset({config.policy_digest}),
        allowed_machine_digests=frozenset({config.machine_digest}),
        trusted_wardens=frozenset({manifest_ref.warden_id}),
        max_clock_uncertainty_ns=policy_spec.max_clock_uncertainty_ns,
    )
    database_path = config.executor_db_root / f"{config.executor_instance_id}.sqlite3"
    authority_anchor: ProcessFileExecutorAuthorityAnchor | None = None
    try:
        if config.executor_authority_root is not None:
            authority_anchor = ProcessFileExecutorAuthorityAnchor(
                config.executor_authority_root
                / f"{config.executor_instance_id}.anchor"
            )
        if database_path.exists():
            replay_store = SQLiteReceiptReplayStore(
                database_path,
                authority_anchor=authority_anchor,
                allow_unanchored=authority_anchor is None,
            )
        else:
            replay_store = SQLiteReceiptReplayStore.initialize(
                database_path,
                authority_anchor=authority_anchor,
                allow_unanchored=authority_anchor is None,
                identity=executor_replay_identity(policy, registry),
            )
        verifier = ReceiptVerifier(registry, replay_store, policy)
    except Exception:
        if authority_anchor is not None:
            authority_anchor.close()
        raise LetsGatewayError("executor_initialization_failed") from None
    return ExecutorGatewayRuntime(
        gateway=ReceiptExecutorGateway(
            verifier,
            replay_status=(
                None if effect_coordinator is None else replay_store.status
            ),
            effect_coordinator=effect_coordinator,
        ),
        replay_store=replay_store,
        authority_anchor=authority_anchor,
    )


def extract_lets_metadata(caller_capabilities: object) -> object | None:
    if not isinstance(caller_capabilities, Mapping):
        return None
    return caller_capabilities.get(LETS_CALLER_CAPABILITY)


__all__ = (
    "DurableEffectCoordinator",
    "ExecutorGatewayRuntime",
    "IssuedPermit",
    "LETS_CALLER_CAPABILITY",
    "LetsAuthorizationGateway",
    "LetsGatewayError",
    "PERMIT_TYPE",
    "ProtectedPermitEnvelope",
    "ReceiptExecutorGateway",
    "create_executor_gateway",
    "extract_lets_metadata",
)
