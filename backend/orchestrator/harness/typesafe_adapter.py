"""Bounded harness adapter for the existing TypeSafe jev-latest routing runner:
lifecycle delegates to typesafe_routing.runner with its circuit, budget, and flag
behavior unchanged, the tool seam vends the already-authorized bounded surface, and
accounting maps RoutingOutcome without exposing credentials.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any, Callable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from orchestrator.typesafe_routing.budget import (
    TURN_BUDGET_SECONDS,
    Outcome,
    UserCircuit,
)
from orchestrator.typesafe_routing.client import (
    ATTEMPT_TIMEOUT_MS,
    TYPESAFE_API_BASE,
    TYPESAFE_MODEL,
)
from orchestrator.typesafe_routing.questions import (
    ROUTING_MAX_REQUEST_CHARS,
    AgentOption,
    RoutingRequest,
    ToolOption,
)
from orchestrator.typesafe_routing.runner import (
    await_decision,
    start_routing,
)

from .contract import (
    MAX_SURFACE_DESCRIPTION_CHARS,
    AccountingSeam,
    HarnessAccounting,
    HarnessAgentOption,
    HarnessContractError,
    HarnessDeployment,
    HarnessExecution,
    HarnessIdentity,
    HarnessOutcome,
    HarnessProviderFrame,
    HarnessResult,
    HarnessToolOption,
    HarnessToolSurface,
    HarnessTurnRequest,
    LifecycleSeam,
    ProviderSeam,
    ToolSeam,
    assert_credential_free,
    cancel_and_wait,
)

_HARNESS_ID = "typesafe_jev_latest"

_PROFILE = "jev_latest"

_ADAPTER_VERSION = "1.0.0"

_TRANSPORT_KEYS = frozenset(
    {"client", "circuit", "flags", "budget_seconds", "attempt_timeout_ms"}
)


@dataclass
class TypeSafeRoutingHarness(LifecycleSeam, ToolSeam, ProviderSeam, AccountingSeam):
    client: Any = None
    circuit: Optional[UserCircuit] = None
    flags: Any = None
    clock: Callable[[], float] = time.monotonic
    sleep: Optional[Callable[[float], Any]] = None
    base_url: str = TYPESAFE_API_BASE
    model: str = TYPESAFE_MODEL
    budget_seconds: float = TURN_BUDGET_SECONDS
    attempt_timeout_ms: int = ATTEMPT_TIMEOUT_MS
    cancel_timeout_seconds: float = 5

    @property
    def identity(self) -> HarnessIdentity:
        return HarnessIdentity(
            harness_id=_HARNESS_ID,
            profile=_PROFILE,
            adapter_version=_ADAPTER_VERSION,
        )

    def _transport(self, request: HarnessTurnRequest) -> Mapping[str, Any]:
        transport = request.transport or {}
        unknown = set(transport) - _TRANSPORT_KEYS
        if unknown:
            raise HarnessContractError(
                f"unknown TypeSafe transport keys: {sorted(unknown)!r}"
            )
        return transport

    def _bounded_native_options(
        self, request: HarnessTurnRequest
    ) -> Tuple[Tuple[AgentOption, ...], Mapping[str, Tuple[ToolOption, ...]]]:
        tools_by_agent: dict[str, list[ToolOption]] = {}
        for option in request.tools:
            tools_by_agent.setdefault(option.agent_id, []).append(
                ToolOption(
                    option.tool_name,
                    (option.description or "")[:MAX_SURFACE_DESCRIPTION_CHARS],
                )
            )
        agents = tuple(
            AgentOption(
                option.agent_id,
                (option.name or option.agent_id)[:MAX_SURFACE_DESCRIPTION_CHARS],
                (option.description or "")[:MAX_SURFACE_DESCRIPTION_CHARS],
            )
            for option in request.agents
            if tools_by_agent.get(option.agent_id)
        )
        return agents, {agent: tuple(tools) for agent, tools in tools_by_agent.items()}

    def surface(self, request: HarnessTurnRequest) -> HarnessToolSurface:
        agents, tools_by_agent = self._bounded_native_options(request)
        bounded = RoutingRequest.build(
            current_request=request.message,
            history=request.history,
            active_agent=request.active_agent,
            selected_tools=request.selected_tools,
            agents=agents,
            tools_by_agent=tools_by_agent,
        )
        kept_agents = tuple(
            HarnessAgentOption(option.agent_id, option.name, option.description)
            for option in bounded.agents
        )
        kept_tools = tuple(
            HarnessToolOption(option.name, agent_id, option.description)
            for agent_id, options in bounded.tools_by_agent.items()
            for option in options
        )
        return HarnessToolSurface(
            agents=kept_agents,
            tools=kept_tools,
            max_request_chars=ROUTING_MAX_REQUEST_CHARS,
        )

    def _routing_request(
        self, request: HarnessTurnRequest, surface: HarnessToolSurface
    ) -> RoutingRequest:
        return RoutingRequest.build(
            current_request=request.message[: surface.max_request_chars],
            history=request.history,
            active_agent=request.active_agent,
            selected_tools=request.selected_tools,
            agents=[
                AgentOption(option.agent_id, option.name, option.description)
                for option in surface.agents
            ],
            tools_by_agent={
                agent_id: [
                    ToolOption(option.tool_name, option.description)
                    for option in surface.tools
                    if option.agent_id == agent_id
                ]
                for agent_id in {option.agent_id for option in surface.tools}
            },
        )

    def provider_frames(self) -> Tuple[HarnessProviderFrame, ...]:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            raise HarnessContractError(
                f"TypeSafe base URL must be an http(s) URL with a host: "
                f"{self.base_url!r}"
            )
        return (
            HarnessProviderFrame(
                provider_key=_HARNESS_ID,
                model=self.model,
                base_url=self.base_url,
                credential_source="user",
                local=False,
                keyless=False,
            ),
        )

    def accounting(
        self,
        *,
        outcome: HarnessOutcome,
        attempts: int,
        started_monotonic: float,
        surface: HarnessToolSurface,
        credential_outcome: Optional[str] = None,
        detail: Optional[Mapping[str, str]] = None,
        credential: Optional[str] = None,
        total_tokens: Optional[int] = None,
    ) -> HarnessAccounting:
        elapsed_ms = max(0, int((self.clock() - started_monotonic) * 1000))
        bounded_detail = dict(detail or {})
        assert_credential_free(bounded_detail, credential=credential)
        return HarnessAccounting(
            attempts=attempts,
            elapsed_ms=elapsed_ms,
            outcome=outcome,
            credential_outcome=credential_outcome,
            surface_agents=surface.agent_count,
            surface_tools=surface.tool_count,
            total_tokens=total_tokens,
            detail=bounded_detail,
        )

    async def start(self, request: HarnessTurnRequest) -> HarnessExecution:
        transport = self._transport(request)
        surface = self.surface(request)
        routing_request = self._routing_request(request, surface)
        task = await start_routing(
            user_id=request.user_id,
            request=routing_request,
            api_key=request.api_key,
            fingerprint=request.fingerprint,
            client=transport.get("client", self.client),
            circuit=transport.get("circuit", self.circuit),
            flags=transport.get("flags", self.flags),
            budget_seconds=transport.get("budget_seconds", self.budget_seconds),
            attempt_timeout_ms=transport.get(
                "attempt_timeout_ms", self.attempt_timeout_ms
            ),
            clock=self.clock,
            sleep=self.sleep,
        )
        return HarnessExecution(
            identity=self.identity,
            request=request,
            task=task,
            started_monotonic=self.clock(),
            runner_state=getattr(task, "astral_routing_progress", None),
        )

    async def await_result(self, execution: HarnessExecution) -> HarnessResult:
        task = execution.task
        transport = self._transport(execution.request)
        try:
            routing_outcome = await await_decision(
                task,
                user_id=execution.request.user_id,
                circuit=transport.get("circuit", self.circuit),
            )
        except asyncio.CancelledError:
            if task.cancelled() and asyncio.current_task().cancelling() == 0:
                return self._terminal_result(
                    execution,
                    HarnessOutcome.CANCELLED,
                    attempts=self._attempts(execution),
                    credential_outcome=None,
                    detail={"runner_outcome": "cancelled"},
                    decision=None,
                )
            raise
        if not task.done():
            await cancel_and_wait(task, timeout_seconds=self.cancel_timeout_seconds)
        return self._map_routing_outcome(execution, routing_outcome)

    async def cancel(self, execution: HarnessExecution) -> HarnessResult:
        task = execution.task
        await cancel_and_wait(task, timeout_seconds=self.cancel_timeout_seconds)
        if task.cancelled():
            return self._terminal_result(
                execution,
                HarnessOutcome.CANCELLED,
                attempts=self._attempts(execution),
                credential_outcome=None,
                detail={"runner_outcome": "cancelled"},
                decision=None,
            )
        return await self.await_result(execution)

    @staticmethod
    def _attempts(execution, fallback=0):
        return (
            execution.runner_state.attempts
            if execution.runner_state is not None
            else fallback
        )

    def _map_routing_outcome(
        self, execution: HarnessExecution, routing_outcome: Any
    ) -> HarnessResult:
        runner_outcome = routing_outcome.outcome
        if runner_outcome is Outcome.SUCCESS:
            outcome = HarnessOutcome.SUCCEEDED
        elif runner_outcome.value.startswith("fallback"):
            outcome = HarnessOutcome.FALLBACK
        elif runner_outcome.value.startswith("skipped"):
            outcome = HarnessOutcome.SKIPPED
        else:
            outcome = HarnessOutcome.CANCELLED
        return self._terminal_result(
            execution,
            outcome,
            attempts=self._attempts(execution, routing_outcome.attempts),
            credential_outcome=routing_outcome.credential_outcome,
            detail={"runner_outcome": runner_outcome.value},
            decision=routing_outcome.decision,
            skip_reason=(
                runner_outcome.value if outcome is HarnessOutcome.SKIPPED else None
            ),
        )

    def _terminal_result(
        self,
        execution: HarnessExecution,
        outcome: HarnessOutcome,
        *,
        attempts: int,
        credential_outcome: Optional[str],
        detail: Mapping[str, str],
        decision: Optional[Any] = None,
        skip_reason: Optional[str] = None,
    ) -> HarnessResult:
        surface = self.surface(execution.request)
        accounting = self.accounting(
            outcome=outcome,
            attempts=attempts,
            started_monotonic=execution.started_monotonic,
            surface=surface,
            credential_outcome=credential_outcome,
            detail=detail,
            credential=execution.request.api_key,
        )
        return HarnessResult(
            outcome=outcome,
            accounting=accounting,
            decision=decision,
            skip_reason=skip_reason,
        )

    def deployment(self, *, default: bool = False) -> HarnessDeployment:
        return HarnessDeployment(
            identity=self.identity,
            lifecycle=self,
            tools=self,
            provider=self,
            accounting=self,
            default=default,
        )


def typesafe_deployment(
    harness: Optional[TypeSafeRoutingHarness] = None, *, default: bool = False
) -> HarnessDeployment:
    return (harness or TypeSafeRoutingHarness()).deployment(default=default)


__all__ = (
    "TypeSafeRoutingHarness",
    "typesafe_deployment",
)
