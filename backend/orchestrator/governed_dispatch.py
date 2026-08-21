"""One final host adapter for every Astral tool actuator dispatch.

Astral's existing identity, delegation, permission, policy, security, taint,
PHI, egress, confirmation, rewrite, and credential gates remain upstream.  A
caller gives this adapter only the final rewritten argument mapping.  For a
governed runtime the adapter then resolves the exact Plane binding, obtains a
LETS permit, and carries that permit solely in MCP caller capabilities.

The adapter deliberately has an injectable runtime resolver and Plane/runtime
seam.  AstralDeep startup and lifecycle code can bind those dependencies once
the independently versioned AstralPlane runtime is ready without creating a
second database implementation here.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar, cast

from orchestrator.lets_gateway import (
    LETS_CALLER_CAPABILITY,
    IssuedPermit,
    LetsAuthorizationGateway,
    LetsGatewayError,
)
from orchestrator.protected_dispatch import build_protected_dispatch_context
from orchestrator.tool_retry import (
    ActuatorResult,
    EffectSemantics,
    ProtectedOutcomeStatus,
    ProtectedToolRetrier,
)

LetsMode = Literal["off", "shadow", "enforce"]
T = TypeVar("T")


class PlaneRuntime(Protocol):
    """Public AstralPlane transaction seam used for exact binding reads."""

    def transaction(self, **options: object): ...


class AuthorityRepository(Protocol):
    """Public AstralPlane authority query required by final dispatch."""

    def get_active_binding(
        self,
        transaction: object,
        *,
        owner_id: str,
        agent_id: str,
        runtime_id: str,
        runtime_generation: int,
    ) -> object | None: ...


@dataclass(frozen=True, slots=True)
class DispatchRuntime:
    """Exact host-owned runtime identity selected before final dispatch.

    ``runtime_id`` and ``runtime_generation`` may be absent only for an
    explicitly ungoverned population such as ``builtin`` or ``external``.
    Governed populations are validated before Plane is queried.
    """

    owner_id: str | None
    agent_id: str
    population: str
    runtime_id: str | None
    runtime_generation: int | None
    executor_audience: str | None
    executor_conformant: bool
    dispatch_posture: Literal["protected_executor", "dispatch_mediated_only"]


RuntimeResolver = Callable[
    [str, str | None], DispatchRuntime | Awaitable[DispatchRuntime]
]
Actuator = Callable[[dict[str, object]], T | Awaitable[T]]


class GovernedDispatchError(RuntimeError):
    """Stable, content-free final-dispatch refusal."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


async def _resolve(value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


class GovernedFinalDispatch:
    """Authorize exactly one possible physical tool invocation.

    Existing Astral retry behavior remains outside this class.  Each entry to
    :meth:`execute` represents one possible physical attempt and therefore
    uses a one-attempt :class:`ProtectedToolRetrier`, which allocates a fresh
    operation ID and nonce.  A later legacy retry re-enters this adapter and
    necessarily receives different authority.
    """

    def __init__(
        self,
        *,
        mode: LetsMode,
        governed_populations: tuple[str, ...] = (),
        governed_agent_allowlist: tuple[str, ...] = (),
        gateway: LetsAuthorizationGateway | None = None,
        plane: PlaneRuntime | None = None,
        authority_repository: AuthorityRepository | None = None,
        runtime_resolver: RuntimeResolver | None = None,
    ) -> None:
        if mode not in {"off", "shadow", "enforce"}:
            raise ValueError("invalid LETS dispatch mode")
        if len(set(governed_populations)) != len(governed_populations):
            raise ValueError("governed populations must be unique")
        if len(set(governed_agent_allowlist)) != len(governed_agent_allowlist):
            raise ValueError("governed agent allowlist must be unique")
        self.mode = mode
        self.governed_populations = tuple(governed_populations)
        self.governed_agent_allowlist = tuple(governed_agent_allowlist)
        self._gateway = gateway
        self._plane = plane
        self._repository = authority_repository
        self._runtime_resolver = runtime_resolver

    @classmethod
    def off(cls) -> "GovernedFinalDispatch":
        """Return the exact no-LETS adapter used by flag-off deployments."""

        return cls(mode="off")

    @classmethod
    def unavailable(cls, mode: LetsMode) -> "GovernedFinalDispatch":
        """Represent active configuration whose injected runtime is not ready.

        Enforce mode refuses before an actuator.  Shadow remains observational
        and therefore preserves the existing Astral decision.
        """

        return cls(mode=mode)

    @classmethod
    def active(
        cls,
        *,
        gateway: LetsAuthorizationGateway,
        plane: PlaneRuntime,
        authority_repository: AuthorityRepository,
        runtime_resolver: RuntimeResolver,
    ) -> "GovernedFinalDispatch":
        config = gateway.config
        return cls(
            mode=config.mode,
            governed_populations=tuple(config.governed_cohorts),
            governed_agent_allowlist=tuple(config.governed_agent_allowlist),
            gateway=gateway,
            plane=plane,
            authority_repository=authority_repository,
            runtime_resolver=runtime_resolver,
        )

    @property
    def ready(self) -> bool:
        if self.mode == "off":
            return True
        return all(
            value is not None
            for value in (
                self._gateway,
                self._plane,
                self._repository,
                self._runtime_resolver,
            )
        )

    async def execute(
        self,
        *,
        owner_id: str | None,
        agent_id: str,
        tool_id: str,
        scope: str,
        channel: str,
        audit_correlation_id: str,
        final_arguments: dict[str, object],
        invoke: Actuator[T],
        authorized_effect: Mapping[str, object] | None = None,
        actor_user_id: str | None = None,
        auth_principal: str | None = None,
        conversation_id: str | None = None,
    ) -> T:
        """Run one final actuator attempt under the selected rollout mode.

        Off mode calls no resolver, Plane query, context builder, retrier, or
        gateway and fabricates no caller capability.  Shadow mode attempts the
        same authorization but never converts a LETS/Plane failure into an
        Astral denial.  Enforce mode fails closed before ``invoke``.
        """

        if self.mode == "off":
            return await _resolve(invoke({}))

        if not self.ready:
            if self.mode == "shadow":
                return await _resolve(invoke({}))
            raise GovernedDispatchError("governed_dispatch_unavailable", retryable=True)

        assert self._runtime_resolver is not None
        try:
            runtime = await _resolve(self._runtime_resolver(agent_id, owner_id))
        except Exception:
            if self.mode == "shadow":
                return await _resolve(invoke({}))
            raise GovernedDispatchError("runtime_identity_unavailable", retryable=True) from None

        try:
            self._validate_runtime_identity(runtime, agent_id=agent_id)
        except GovernedDispatchError:
            if self.mode == "shadow":
                return await _resolve(invoke({}))
            raise
        if runtime.population not in self.governed_populations:
            # External/builtin populations remain ordinary Astral-mediated
            # dispatch and never receive a misleading protected permit.
            return await _resolve(invoke({}))
        if (
            self.governed_agent_allowlist
            and agent_id not in self.governed_agent_allowlist
        ):
            # The optional agent allowlist narrows a governed population; an
            # excluded agent retains ordinary Astral-mediated dispatch and is
            # not misrepresented as an enforcement failure.
            return await _resolve(invoke({}))

        try:
            self._validate_governed_runtime(runtime, owner_id=owner_id)
        except GovernedDispatchError:
            if self.mode == "shadow":
                return await _resolve(invoke({}))
            raise

        if not runtime.executor_conformant:
            if self.mode == "shadow":
                return await _resolve(invoke({}))
            raise GovernedDispatchError("executor_not_conformant")

        if actor_user_id is not None and actor_user_id != owner_id:
            if self.mode == "shadow":
                return await _resolve(invoke({}))
            raise GovernedDispatchError("audit_actor_owner_mismatch")
        from orchestrator.lets_audit import LetsAuditObserver

        observer = LetsAuditObserver(
            actor_user_id=actor_user_id or cast(str, owner_id),
            auth_principal=auth_principal or actor_user_id or cast(str, owner_id),
            agent_id=agent_id,
            conversation_id=conversation_id,
            strict=self.mode == "enforce",
        )

        effect = (
            dict(authorized_effect)
            if authorized_effect is not None
            else {"effect_class": scope.removeprefix("tools:")}
        )
        retrier: ProtectedToolRetrier[IssuedPermit, T] = ProtectedToolRetrier(
            physical_attempts=1,
            authorization_transport_attempts=1,
        )

        async def authorize(attempt) -> IssuedPermit:
            try:
                binding = await asyncio.to_thread(self._active_binding, runtime)
                context = build_protected_dispatch_context(
                    operation_id=attempt.operation_id,
                    agent_id=agent_id,
                    runtime_id=cast(str, runtime.runtime_id),
                    tool_id=tool_id,
                    scope=scope,
                    executor_audience=cast(str, runtime.executor_audience),
                    channel=channel,
                    audit_correlation_id=audit_correlation_id,
                    expected_sequence=(
                        0 if binding is None else int(binding.lease_sequence)
                    ),
                    final_arguments=final_arguments,
                    authorized_effect=effect,
                    nonce=attempt.nonce,
                )
                assert self._gateway is not None
                permit = await self._gateway.authorize(
                    binding=binding,
                    population=runtime.population,
                    executor_conformant=runtime.executor_conformant,
                    context=context,
                    final_arguments=final_arguments,
                    authorized_effect=effect,
                    observer=observer,
                )
            except LetsGatewayError as exc:
                raise GovernedDispatchError(
                    exc.code,
                    retryable=exc.retryable,
                ) from None
            except GovernedDispatchError:
                raise
            except Exception:
                raise GovernedDispatchError(
                    "protected_authorization_failed",
                    retryable=True,
                ) from None
            if self.mode == "enforce" and not permit.enforced:
                permit.release()
                raise GovernedDispatchError("protected_permit_not_enforced")
            return permit

        actuator_started = False

        async def actuate(_attempt, permit: IssuedPermit) -> ActuatorResult[T]:
            nonlocal actuator_started
            capabilities = permit.caller_capabilities()
            if self.mode == "enforce":
                if set(capabilities) != {LETS_CALLER_CAPABILITY}:
                    permit.release()
                    raise GovernedDispatchError("protected_permit_missing")
            elif capabilities:
                permit.release()
                raise GovernedDispatchError("shadow_permit_must_not_be_carried")
            try:
                actuator_started = True
                value = await _resolve(invoke(capabilities))
            finally:
                permit.release()
            return ActuatorResult(value=value)

        try:
            outcome = await retrier.execute(
                semantics=EffectSemantics.NON_IDEMPOTENT,
                authorize=authorize,
                invoke=actuate,
            )
        except GovernedDispatchError:
            if self.mode == "shadow" and not actuator_started:
                return await _resolve(invoke({}))
            raise
        except Exception:
            if self.mode == "shadow" and not actuator_started:
                return await _resolve(invoke({}))
            if actuator_started:
                raise
            raise GovernedDispatchError("protected_dispatch_failed", retryable=True) from None

        if outcome.status is not ProtectedOutcomeStatus.SUCCEEDED:
            if self.mode == "shadow":
                return await _resolve(invoke({}))
            raise GovernedDispatchError(
                outcome.error_code or "protected_dispatch_failed",
                retryable=outcome.status is ProtectedOutcomeStatus.OUTCOME_UNCERTAIN,
            )
        return cast(T, outcome.value)

    def _active_binding(self, runtime: DispatchRuntime) -> object | None:
        assert self._plane is not None
        assert self._repository is not None
        with self._plane.transaction() as transaction:
            return self._repository.get_active_binding(
                transaction,
                owner_id=cast(str, runtime.owner_id),
                agent_id=runtime.agent_id,
                runtime_id=cast(str, runtime.runtime_id),
                runtime_generation=cast(int, runtime.runtime_generation),
            )

    @staticmethod
    def _validate_runtime_identity(
        runtime: DispatchRuntime,
        *,
        agent_id: str,
    ) -> None:
        if not isinstance(runtime, DispatchRuntime) or runtime.agent_id != agent_id:
            raise GovernedDispatchError("runtime_identity_mismatch")
        if not isinstance(runtime.population, str) or not runtime.population:
            raise GovernedDispatchError("runtime_population_invalid")
        if type(runtime.executor_conformant) is not bool:
            raise GovernedDispatchError("executor_conformance_invalid")
        expected_posture = (
            "protected_executor"
            if runtime.executor_conformant
            else "dispatch_mediated_only"
        )
        if runtime.dispatch_posture != expected_posture:
            raise GovernedDispatchError("executor_posture_mismatch")

    @staticmethod
    def _validate_governed_runtime(
        runtime: DispatchRuntime,
        *,
        owner_id: str | None,
    ) -> None:
        # A host binding cannot substitute for the authenticated invocation
        # owner.  Every governed channel (including unattended work) must
        # carry its owner into final dispatch; otherwise a transport that
        # skipped identity/owner gates could borrow an agent's durable binding.
        if owner_id is None:
            raise GovernedDispatchError("dispatch_owner_unavailable")
        if runtime.owner_id != owner_id:
            raise GovernedDispatchError("runtime_owner_mismatch")
        for value, code in (
            (runtime.owner_id, "runtime_owner_unavailable"),
            (runtime.runtime_id, "runtime_identity_unavailable"),
            (runtime.executor_audience, "executor_audience_unavailable"),
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise GovernedDispatchError(code)
        if (
            type(runtime.runtime_generation) is not int
            or runtime.runtime_generation < 1
        ):
            raise GovernedDispatchError("runtime_generation_unavailable")


__all__ = (
    "DispatchRuntime",
    "GovernedDispatchError",
    "GovernedFinalDispatch",
    "RuntimeResolver",
)
