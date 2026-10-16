"""Deterministic conformance for harness deployments: the same lifecycle, denial,
cancellation, failure-recovery, accounting-redaction, tool-surface, and
provider-frame scenarios run against every adapter, with scripted transports and no
network, database, or credentials.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Tuple

from llm_config.invocation import InvocationResult

from .contract import (
    MAX_SURFACE_DESCRIPTION_CHARS,
    HarnessAgentOption,
    HarnessContractError,
    HarnessDeployment,
    HarnessOutcome,
    HarnessToolOption,
    HarnessTurnRequest,
)

Scenario = Callable[[HarnessDeployment], Awaitable[None]]

USER = "harness-conformance-user"

MESSAGE = "conformance routing message"

AGENT_ID = "conformance-agent"

TOOL_NAME = "conformance-agent__lookup"

CREDENTIAL = "conformance-canary-credential"

_TYPESAFE_ID = "typesafe_jev_latest"

_STANDARD_ID = "standard_openai_compatible"


def _base_options(long_description: bool = False) -> Tuple[Any, Any]:
    description = "looks up live conditions" + (
        " with an intentionally long tail" * 20 if long_description else ""
    )
    agents = (
        HarnessAgentOption(AGENT_ID, "Conformance Agent", "answers conformance turns"),
    )
    tools = (HarnessToolOption(TOOL_NAME, AGENT_ID, description),)
    return agents, tools


async def _block_forever(seconds: float) -> None:
    del seconds
    await asyncio.Event().wait()


def _typesafe_request(script: str) -> HarnessTurnRequest:
    from orchestrator.typesafe_routing.budget import UserCircuit
    from tests.fakes.typesafe_fake import (
        FakeTypeSafeClient,
        TypeSafeAPITimeoutError,
        TypeSafeAuthenticationError,
        high_confidence,
    )

    agents, tools = _base_options()
    transport: dict[str, Any] = {"circuit": UserCircuit()}
    api_key: Optional[str] = CREDENTIAL
    if script == "success":
        transport["client"] = FakeTypeSafeClient(
            default=high_confidence(AGENT_ID, TOOL_NAME)
        )
    elif script == "denied":
        api_key = None
    elif script == "slow":
        transport["client"] = FakeTypeSafeClient(
            default=high_confidence(AGENT_ID, TOOL_NAME),
            latency=0.05,
            sleep=_block_forever,
        )
    elif script == "transient_failure":
        transport["client"] = FakeTypeSafeClient(
            default=high_confidence(AGENT_ID, TOOL_NAME),
            faults=[TypeSafeAPITimeoutError] * 3,
        )
    elif script == "auth_failure":
        transport["client"] = FakeTypeSafeClient(
            default=high_confidence(AGENT_ID, TOOL_NAME),
            faults=[TypeSafeAuthenticationError],
        )
    else:
        raise HarnessContractError(f"unknown TypeSafe conformance script: {script!r}")
    return HarnessTurnRequest(
        user_id=USER,
        message=MESSAGE,
        agents=agents,
        tools=tools,
        api_key=api_key,
        fingerprint="conformance-fingerprint",
        transport=transport,
    )


def _standard_request(script: str) -> HarnessTurnRequest:
    agents, tools = _base_options()

    async def _succeed():
        return InvocationResult(
            "succeeded", 1, "conformance completion", {"total_tokens": 7}, 7, True
        )

    async def _deny():
        return InvocationResult("denied", 0, credential_outcome="unavailable")

    async def _transient():
        return InvocationResult("failed", 3, error_class="TimeoutError")

    async def _auth_failure():
        return InvocationResult(
            "failed", 1, credential_outcome="rejected", error_class="PermissionError"
        )

    async def _slow():
        await asyncio.Event().wait()

    scripts = {
        "success": _succeed,
        "denied": _deny,
        "slow": _slow,
        "transient_failure": _transient,
        "auth_failure": _auth_failure,
    }
    if script not in scripts:
        raise HarnessContractError(
            f"unknown standard-LLM conformance script: {script!r}"
        )
    return HarnessTurnRequest(
        user_id=USER,
        message=MESSAGE,
        agents=agents,
        tools=tools,
        api_key=CREDENTIAL,
        transport={"invoke": scripts[script]},
    )


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    identity: Any
    results: Tuple[ScenarioResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def failures(self) -> Tuple[str, ...]:
        return tuple(
            f"{result.name}: {result.detail}"
            for result in self.results
            if not result.passed
        )


def _request(deployment: HarnessDeployment, script: str) -> HarnessTurnRequest:
    harness_id = deployment.identity.harness_id
    if harness_id == _TYPESAFE_ID:
        return _typesafe_request(script)
    if harness_id == _STANDARD_ID:
        return _standard_request(script)
    raise HarnessContractError(
        f"no conformance scripting registered for harness {harness_id!r}"
    )


async def _scenario_lifecycle_success(deployment: HarnessDeployment) -> None:
    request = _request(deployment, "success")
    surface = deployment.tools.surface(request)
    assert surface.agent_count >= 1, "a tooled turn must expose its authorized agents"
    assert surface.tool_count >= 1, "a tooled turn must expose its authorized tools"
    execution = await deployment.lifecycle.start(request)
    result = await deployment.lifecycle.await_result(execution)
    assert result.outcome is HarnessOutcome.SUCCEEDED, (
        f"expected a succeeded lifecycle, got {result.outcome}"
    )
    assert result.decision is not None, "a succeeded lifecycle must carry its decision"
    assert not execution.running, "an awaited execution must be terminal"
    accounting = result.accounting
    assert accounting.attempts >= 1, "succeeded accounting must record attempts"
    assert accounting.elapsed_ms >= 0, "succeeded accounting must record elapsed time"
    assert accounting.surface_tools == surface.tool_count, (
        "accounting must record the tool surface it ran with"
    )
    assert accounting.outcome is HarnessOutcome.SUCCEEDED


async def _scenario_denial_without_credentials(deployment: HarnessDeployment) -> None:
    execution = await deployment.lifecycle.start(_request(deployment, "denied"))
    result = await deployment.lifecycle.await_result(execution)
    assert result.outcome in (HarnessOutcome.SKIPPED, HarnessOutcome.DENIED), (
        f"an uncredentialed turn must be skipped or denied, got {result.outcome}"
    )
    assert result.decision is None, "a denied turn must not carry a decision"
    assert result.accounting.outcome is result.outcome
    if result.outcome is HarnessOutcome.SKIPPED:
        assert result.skip_reason, "a skipped turn must record its skip reason"


async def _scenario_cancellation_midflight(deployment: HarnessDeployment) -> None:
    execution = await deployment.lifecycle.start(_request(deployment, "slow"))
    await asyncio.sleep(0)
    cancelled = await deployment.lifecycle.cancel(execution)
    assert cancelled.outcome is HarnessOutcome.CANCELLED, (
        f"cancelling a running execution must terminate it as cancelled, "
        f"got {cancelled.outcome}"
    )
    assert cancelled.decision is None, "a cancelled execution must not emit a decision"
    recovery = await deployment.lifecycle.await_result(
        await deployment.lifecycle.start(_request(deployment, "success"))
    )
    assert recovery.outcome is HarnessOutcome.SUCCEEDED, (
        "a deployment must accept a fresh execution after a cancellation"
    )


async def _scenario_transient_failure_recovery(deployment: HarnessDeployment) -> None:
    failed = await deployment.lifecycle.await_result(
        await deployment.lifecycle.start(_request(deployment, "transient_failure"))
    )
    assert failed.outcome in (HarnessOutcome.FAILED, HarnessOutcome.FALLBACK), (
        f"a transient failure must end failed or in fallback, got {failed.outcome}"
    )
    assert failed.accounting.attempts >= 1, (
        "a failed execution must account at least one attempt"
    )
    recovered = await deployment.lifecycle.await_result(
        await deployment.lifecycle.start(_request(deployment, "success"))
    )
    assert recovered.outcome is HarnessOutcome.SUCCEEDED, (
        "a deployment must recover on a fresh execution after a transient failure"
    )


async def _scenario_nontransient_failure(deployment: HarnessDeployment) -> None:
    result = await deployment.lifecycle.await_result(
        await deployment.lifecycle.start(_request(deployment, "auth_failure"))
    )
    assert result.outcome in (HarnessOutcome.FAILED, HarnessOutcome.FALLBACK), (
        f"an auth failure must end failed or in fallback, got {result.outcome}"
    )
    assert result.accounting.attempts >= 1
    if result.outcome is HarnessOutcome.FALLBACK:
        assert result.accounting.credential_outcome == "rejected", (
            "an auth fallback must record the rejected credential disposition"
        )
    else:
        assert result.error_class, (
            "an auth failure must record the upstream error class"
        )


async def _scenario_accounting_redaction(deployment: HarnessDeployment) -> None:
    execution = await deployment.lifecycle.start(_request(deployment, "success"))
    result = await deployment.lifecycle.await_result(execution)
    serialized = repr(result.accounting) + repr(result.accounting.detail)
    assert CREDENTIAL not in serialized, (
        "accounting records must never contain request credential material"
    )
    assert "api_key" not in repr(result.accounting.detail).lower()
    assert result.accounting.outcome is HarnessOutcome.SUCCEEDED


async def _scenario_tool_surface_bounded(deployment: HarnessDeployment) -> None:
    request = _request(deployment, "success")
    long_agents, _ = _base_options()
    long_request = HarnessTurnRequest(
        user_id=USER,
        message=MESSAGE,
        agents=long_agents,
        tools=(
            HarnessToolOption(
                TOOL_NAME,
                AGENT_ID,
                "looks up live conditions" + " with an intentionally long tail" * 20,
            ),
        ),
    )
    for bounded_request in (request, long_request):
        surface = deployment.tools.surface(bounded_request)
        requested_tools = {option.tool_name for option in bounded_request.tools}
        requested_agents = {option.agent_id for option in bounded_request.agents}
        for option in surface.tools:
            assert option.tool_name in requested_tools, (
                "the tool seam must never widen the authorized tool set"
            )
            assert option.agent_id in requested_agents
            assert len(option.description) <= MAX_SURFACE_DESCRIPTION_CHARS, (
                "surface descriptions must stay within the contract bound"
            )
        for option in surface.agents:
            assert option.agent_id in requested_agents, (
                "the tool seam must never widen the authorized agent set"
            )
            assert len(option.description) <= MAX_SURFACE_DESCRIPTION_CHARS
    empty = deployment.tools.surface(HarnessTurnRequest(user_id=USER, message=MESSAGE))
    assert empty.tool_count == 0 and empty.agent_count == 0, (
        "an optionless turn must produce an empty surface"
    )


async def _scenario_provider_frames_declared(deployment: HarnessDeployment) -> None:
    frames = deployment.provider.provider_frames()
    assert frames, "a deployment must declare at least one provider frame"
    for frame in frames:
        assert len(frame.model) <= 256
        assert frame.base_url, "every provider frame must carry a base URL"
        assert frame.credential_source in ("user", "system", "operator_default")
        assert frame.base_url.startswith(("http://", "https://")), (
            "provider frames must describe http(s) endpoints, not credentials"
        )


SCENARIOS: Tuple[Tuple[str, Scenario], ...] = (
    ("lifecycle_success", _scenario_lifecycle_success),
    ("denial_without_credentials", _scenario_denial_without_credentials),
    ("cancellation_midflight", _scenario_cancellation_midflight),
    ("transient_failure_recovery", _scenario_transient_failure_recovery),
    ("nontransient_failure", _scenario_nontransient_failure),
    ("accounting_redaction", _scenario_accounting_redaction),
    ("tool_surface_bounded", _scenario_tool_surface_bounded),
    ("provider_frames_declared", _scenario_provider_frames_declared),
)


async def run_conformance(deployment: HarnessDeployment) -> ConformanceReport:
    results = []
    for name, scenario in SCENARIOS:
        try:
            await scenario(deployment)
        except AssertionError as error:
            results.append(ScenarioResult(name, False, str(error)))
        except Exception as error:
            results.append(
                ScenarioResult(name, False, f"{type(error).__name__}: {error}")
            )
        else:
            results.append(ScenarioResult(name, True, "passed"))
    return ConformanceReport(identity=deployment.identity, results=tuple(results))


__all__ = (
    "AGENT_ID",
    "CREDENTIAL",
    "MESSAGE",
    "ConformanceReport",
    "SCENARIOS",
    "TOOL_NAME",
    "USER",
    "ScenarioResult",
    "run_conformance",
)
