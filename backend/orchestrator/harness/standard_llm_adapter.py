"""Bounded harness adapter for the existing BYO/local provider runner: the provider
seam adapts llm_config's server-owned preset catalog and lexical local-endpoint
classification, and the lifecycle seam wraps the orchestrator-owned model invocation
with denial, failure, cancellation, and usage accounting.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any, Awaitable, Callable, Mapping, Optional, Tuple

from llm_config.local_endpoint import classify_endpoint
from llm_config.providers import CUSTOM_PROVIDER_KEY, all_presets, resolve_base_url

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
)

_HARNESS_ID = "standard_openai_compatible"

_PROFILE = "standard"

_ADAPTER_VERSION = "1.0.0"

_TRANSPORT_KEYS = frozenset({"invoke"})

ModelInvoke = Callable[[], Awaitable[Tuple[Optional[Any], Optional[Any]]]]


def _usage_tokens(usage: Optional[Any]) -> Optional[int]:
    if usage is None:
        return None
    if isinstance(usage, Mapping):
        value = usage.get("total_tokens")
    else:
        value = getattr(usage, "total_tokens", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclass
class StandardLLMHarness(LifecycleSeam, ToolSeam, ProviderSeam, AccountingSeam):
    invoke: Optional[ModelInvoke] = None
    clock: Callable[[], float] = time.monotonic

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
                f"unknown standard-LLM transport keys: {sorted(unknown)!r}"
            )
        return transport

    def _invoke_for(self, request: HarnessTurnRequest) -> ModelInvoke:
        invoke = self._transport(request).get("invoke", self.invoke)
        if not callable(invoke):
            raise HarnessContractError(
                "the standard-LLM harness requires an invoke callable from its "
                "constructor or the turn transport"
            )
        return invoke

    def surface(self, request: HarnessTurnRequest) -> HarnessToolSurface:
        return HarnessToolSurface(
            agents=tuple(
                HarnessAgentOption(
                    option.agent_id,
                    (option.name or option.agent_id)[:MAX_SURFACE_DESCRIPTION_CHARS],
                    (option.description or "")[:MAX_SURFACE_DESCRIPTION_CHARS],
                )
                for option in request.agents
            ),
            tools=tuple(
                HarnessToolOption(
                    option.tool_name,
                    option.agent_id,
                    (option.description or "")[:MAX_SURFACE_DESCRIPTION_CHARS],
                )
                for option in request.tools
            ),
            max_request_chars=0,
        )

    def provider_frames(self) -> Tuple[HarnessProviderFrame, ...]:
        frames = []
        for preset in all_presets():
            base_url = resolve_base_url(preset.key)
            if preset.key == CUSTOM_PROVIDER_KEY or base_url is None:
                continue
            verdict = classify_endpoint(base_url)
            frames.append(
                HarnessProviderFrame(
                    provider_key=preset.key,
                    model="",
                    base_url=base_url,
                    credential_source="user",
                    local=verdict.local,
                    keyless=not preset.key_required,
                )
            )
        return tuple(frames)

    def frame_for_provider(
        self,
        provider_key: str,
        submitted_base_url: str = "",
        *,
        key_present: bool = False,
        model: str = "",
    ) -> Optional[HarnessProviderFrame]:
        base_url = resolve_base_url(provider_key, submitted_base_url)
        if base_url is None:
            return None
        verdict = classify_endpoint(base_url)
        return HarnessProviderFrame(
            provider_key=provider_key,
            model=model,
            base_url=base_url,
            credential_source="user",
            local=verdict.local,
            keyless=not key_present,
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
        invoke = self._invoke_for(request)
        task = asyncio.ensure_future(invoke())
        return HarnessExecution(
            identity=self.identity,
            request=request,
            task=task,
            started_monotonic=self.clock(),
        )

    async def await_result(self, execution: HarnessExecution) -> HarnessResult:
        task = execution.task
        try:
            message, usage = await task
        except asyncio.CancelledError:
            if task.cancelled() and asyncio.current_task().cancelling() == 0:
                return self._terminal_result(
                    execution,
                    HarnessOutcome.CANCELLED,
                    attempts=0,
                    detail={"runner_outcome": "cancelled"},
                )
            raise
        except Exception as error:
            return self._terminal_result(
                execution,
                HarnessOutcome.FAILED,
                attempts=1,
                detail={"runner_outcome": "failed"},
                error_class=type(error).__name__,
            )
        if message is None:
            return self._terminal_result(
                execution,
                HarnessOutcome.DENIED,
                attempts=1,
                credential_outcome="unavailable",
                detail={"runner_outcome": "denied_no_credentials"},
            )
        return self._terminal_result(
            execution,
            HarnessOutcome.SUCCEEDED,
            attempts=1,
            detail={"runner_outcome": "succeeded"},
            total_tokens=_usage_tokens(usage),
            decision=(message, usage),
        )

    async def cancel(self, execution: HarnessExecution) -> HarnessResult:
        task = execution.task
        if not task.done():
            task.cancel()
        if not task.done():
            await asyncio.wait({task})
        if task.cancelled():
            return self._terminal_result(
                execution,
                HarnessOutcome.CANCELLED,
                attempts=0,
                detail={"runner_outcome": "cancelled"},
            )
        return await self.await_result(execution)

    def _terminal_result(
        self,
        execution: HarnessExecution,
        outcome: HarnessOutcome,
        *,
        attempts: int,
        detail: Mapping[str, str],
        credential_outcome: Optional[str] = None,
        total_tokens: Optional[int] = None,
        decision: Optional[Any] = None,
        error_class: Optional[str] = None,
    ) -> HarnessResult:
        surface = self.surface(execution.request)
        accounting = self.accounting(
            outcome=outcome,
            attempts=attempts,
            started_monotonic=execution.started_monotonic,
            surface=surface,
            credential_outcome=credential_outcome,
            detail=detail,
            total_tokens=total_tokens,
        )
        return HarnessResult(
            outcome=outcome,
            accounting=accounting,
            decision=decision,
            error_class=error_class,
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


def standard_deployment(
    harness: Optional[StandardLLMHarness] = None, *, default: bool = False
) -> HarnessDeployment:
    return (harness or StandardLLMHarness()).deployment(default=default)


__all__ = (
    "ModelInvoke",
    "StandardLLMHarness",
    "standard_deployment",
)
