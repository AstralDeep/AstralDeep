"""The versioned harness contract for Astral's routing and model runners: identity,
lifecycle/tool/provider/accounting seams, and fail-closed deployment validation;
adapters in this package bind the existing typesafe_routing and llm_config runners,
selection.py gates profile rollout, and conformance.py verifies adapters.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Mapping, Optional, Protocol, Tuple, runtime_checkable

from llm_config.log_scrub import _KEY_TOKEN_PATTERNS, _is_api_key_field

HARNESS_CONTRACT_VERSION = "1"

MAX_SURFACE_DESCRIPTION_CHARS = 200

MAX_USER_ID_CHARS = 256

_BOUNDED_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")

_ADAPTER_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


class HarnessContractError(ValueError):
    pass


class HarnessCancellationPending(HarnessContractError):
    pass


async def cancel_and_wait(task, *, timeout_seconds):
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 <= timeout_seconds <= 30
    ):
        raise HarnessContractError(
            "cancellation budget must be between zero and thirty seconds"
        )
    if not task.done() and task.cancelling() == 0:
        task.cancel()
    if not task.done():
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
        if not done:
            raise HarnessCancellationPending(
                "runner is still running after cancellation budget"
            )


class HarnessOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FALLBACK = "fallback"
    DENIED = "denied"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class HarnessIdentity:
    harness_id: str
    profile: str
    adapter_version: str
    contract_version: str = HARNESS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not _BOUNDED_ID_RE.fullmatch(self.harness_id):
            raise HarnessContractError(
                f"harness_id must be bounded lowercase snake_case: {self.harness_id!r}"
            )
        if not _BOUNDED_ID_RE.fullmatch(self.profile):
            raise HarnessContractError(
                f"profile must be bounded lowercase snake_case: {self.profile!r}"
            )
        if not _ADAPTER_VERSION_RE.fullmatch(self.adapter_version):
            raise HarnessContractError(
                f"adapter_version must be a bounded semantic version: "
                f"{self.adapter_version!r}"
            )
        if self.contract_version != HARNESS_CONTRACT_VERSION:
            raise HarnessContractError(
                f"deployment targets harness contract {self.contract_version!r} "
                f"but this runtime implements {HARNESS_CONTRACT_VERSION!r}"
            )


@dataclass(frozen=True, slots=True)
class HarnessAgentOption:
    agent_id: str
    name: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class HarnessToolOption:
    tool_name: str
    agent_id: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class HarnessToolSurface:
    agents: Tuple[HarnessAgentOption, ...]
    tools: Tuple[HarnessToolOption, ...]
    max_request_chars: int

    @property
    def tool_count(self) -> int:
        return len(self.tools)

    @property
    def agent_count(self) -> int:
        return len(self.agents)


_EMPTY_SURFACE = HarnessToolSurface(agents=(), tools=(), max_request_chars=0)


def empty_surface() -> HarnessToolSurface:
    return _EMPTY_SURFACE


@dataclass(frozen=True, slots=True)
class HarnessProviderFrame:
    provider_key: str
    model: str
    base_url: str
    credential_source: str
    local: bool
    keyless: bool

    def __post_init__(self) -> None:
        if not _BOUNDED_ID_RE.fullmatch(self.provider_key):
            raise HarnessContractError(
                f"provider frame provider_key must be bounded lowercase snake_case: "
                f"{self.provider_key!r}"
            )
        if len(self.model) > 256:
            raise HarnessContractError(
                f"provider frame model must be at most 256 chars: {self.model!r}"
            )
        if not self.base_url or len(self.base_url) > 2048:
            raise HarnessContractError(
                f"provider frame base_url must be 1..2048 chars: {self.base_url!r}"
            )
        if self.credential_source not in ("user", "system", "operator_default"):
            raise HarnessContractError(
                "provider frame credential_source must be one of "
                f"user/system/operator_default: {self.credential_source!r}"
            )


@dataclass(frozen=True, slots=True)
class HarnessTurnRequest:
    user_id: str
    message: str
    agents: Tuple[HarnessAgentOption, ...] = ()
    tools: Tuple[HarnessToolOption, ...] = ()
    history: Tuple[Mapping[str, Any], ...] = ()
    active_agent: Optional[str] = None
    selected_tools: Tuple[str, ...] = ()
    api_key: Optional[str] = field(default=None, repr=False)
    fingerprint: Optional[str] = field(default=None, repr=False)
    transport: Optional[Mapping[str, Any]] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.user_id or len(self.user_id) > MAX_USER_ID_CHARS:
            raise HarnessContractError(f"user_id must be 1..{MAX_USER_ID_CHARS} chars")
        if self.transport is not None and not isinstance(self.transport, Mapping):
            raise HarnessContractError("transport must be a mapping when supplied")


@dataclass
class HarnessExecution:
    identity: HarnessIdentity
    request: HarnessTurnRequest
    task: "asyncio.Task[Any]"
    started_monotonic: float
    runner_state: Any = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return not self.task.done()


@dataclass(frozen=True, slots=True)
class HarnessAccounting:
    attempts: int
    elapsed_ms: int
    outcome: HarnessOutcome
    credential_outcome: Optional[str] = None
    surface_agents: int = 0
    surface_tools: int = 0
    total_tokens: Optional[int] = None
    detail: Mapping[str, str] = field(default_factory=dict)
    usage_complete: bool = False

    def __post_init__(self) -> None:
        if type(self.attempts) is not int or self.attempts < 0:
            raise HarnessContractError("accounting attempts must not be negative")
        if type(self.elapsed_ms) is not int or self.elapsed_ms < 0:
            raise HarnessContractError("accounting elapsed_ms must not be negative")
        if self.total_tokens is not None and (
            type(self.total_tokens) is not int or self.total_tokens < 0
        ):
            raise HarnessContractError("accounting total_tokens must not be negative")
        if type(self.usage_complete) is not bool:
            raise HarnessContractError("accounting usage_complete must be boolean")
        assert_credential_free(self.detail)


@dataclass(frozen=True, slots=True)
class HarnessResult:
    outcome: HarnessOutcome
    accounting: HarnessAccounting
    decision: Optional[Any] = None
    notices: Tuple[Tuple[str, str], ...] = ()
    skip_reason: Optional[str] = None
    error_class: Optional[str] = None


def assert_credential_free(
    mapping: Mapping[str, Any],
    *,
    credential: Optional[str] = None,
) -> None:
    for key, value in mapping.items():
        if _is_api_key_field(key):
            raise HarnessContractError(
                f"accounting key {key!r} must never carry credential material"
            )
        serialized = str(value)
        if any(pattern.search(serialized) for pattern in _KEY_TOKEN_PATTERNS):
            raise HarnessContractError(
                f"accounting value for {key!r} must never carry credential material"
            )
        if credential and credential in serialized:
            raise HarnessContractError(
                f"accounting value for {key!r} must never echo the request credential"
            )


@runtime_checkable
class LifecycleSeam(Protocol):
    async def start(self, request: HarnessTurnRequest) -> HarnessExecution: ...

    async def await_result(self, execution: HarnessExecution) -> HarnessResult: ...

    async def cancel(self, execution: HarnessExecution) -> HarnessResult: ...


@runtime_checkable
class ToolSeam(Protocol):
    def surface(self, request: HarnessTurnRequest) -> HarnessToolSurface: ...


@runtime_checkable
class ProviderSeam(Protocol):
    def provider_frames(self) -> Tuple[HarnessProviderFrame, ...]: ...


@runtime_checkable
class AccountingSeam(Protocol):
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
    ) -> HarnessAccounting: ...


@dataclass(frozen=True, slots=True)
class HarnessDeployment:
    identity: HarnessIdentity
    lifecycle: LifecycleSeam
    tools: ToolSeam
    provider: ProviderSeam
    accounting: AccountingSeam
    default: bool = False

    def __post_init__(self) -> None:
        validate_deployment(self)


def validate_deployment(deployment: HarnessDeployment) -> None:
    identity = getattr(deployment, "identity", None)
    if not isinstance(identity, HarnessIdentity):
        raise HarnessContractError("deployment identity must be a HarnessIdentity")
    seams = (
        ("lifecycle", LifecycleSeam, ("start", "await_result", "cancel")),
        ("tools", ToolSeam, ("surface",)),
        ("provider", ProviderSeam, ("provider_frames",)),
        ("accounting", AccountingSeam, ("accounting",)),
    )
    for seam_name, protocol, members in seams:
        seam = getattr(deployment, seam_name, None)
        if seam is None:
            raise HarnessContractError(
                f"deployment {identity.harness_id!r} advertises no {seam_name} seam"
            )
        if not isinstance(seam, protocol):
            raise HarnessContractError(
                f"deployment {identity.harness_id!r} {seam_name} seam is missing "
                f"required members: {', '.join(members)}"
            )


__all__ = (
    "HARNESS_CONTRACT_VERSION",
    "MAX_SURFACE_DESCRIPTION_CHARS",
    "MAX_USER_ID_CHARS",
    "AccountingSeam",
    "HarnessAccounting",
    "HarnessAgentOption",
    "HarnessCancellationPending",
    "HarnessContractError",
    "HarnessDeployment",
    "HarnessExecution",
    "HarnessIdentity",
    "HarnessOutcome",
    "HarnessProviderFrame",
    "HarnessResult",
    "HarnessToolOption",
    "HarnessToolSurface",
    "HarnessTurnRequest",
    "LifecycleSeam",
    "ProviderSeam",
    "ToolSeam",
    "assert_credential_free",
    "empty_surface",
    "validate_deployment",
)
