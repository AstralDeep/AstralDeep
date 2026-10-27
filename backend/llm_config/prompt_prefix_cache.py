"""Opt-in, fail-closed eligibility policy for caching approved public static prompt
prefixes on the native Anthropic Messages API: it decides marker placement before the
request is sent, strips imported or model-supplied cache markers, parses native cache
usage, and emits revision-bound measurements. Dynamic owner, session, and clinical
content always stays outside the cached prefix.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger("LLMConfig.PrefixCache")

NATIVE_MESSAGES_PROFILE = "native_messages"
NATIVE_MESSAGES_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION_HEADER = "2023-06-01"

POLICY_SEMANTICS_VERSION = "prompt-prefix-cache-policy-v1"

MINIMUM_CACHEABLE_PREFIX_TOKENS = 1024
_OPUS_MODEL_MARKER = "claude-opus-4"
_OPUS_MINIMUM_CACHEABLE_PREFIX_TOKENS = 2048

CACHE_CONTROL_KEY = "cache_control"
EPHEMERAL_MARKER = {"type": "ephemeral"}

_MEASUREMENT_ENABLED_FIELDS = (
    "owner_id", "provider", "model", "profile", "policy_revision",
    "config_revision", "reason", "outcome",
)


def is_native_messages_profile(base_url: Any) -> bool:
    if type(base_url) is not str:
        return False
    return base_url.strip().rstrip("/") == NATIVE_MESSAGES_BASE_URL


def canonical_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovedPrefix:
    name: str
    digest: str


def _strip_markers(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_markers(item)
            for key, item in value.items()
            if key != CACHE_CONTROL_KEY
        }
    if isinstance(value, list):
        return [_strip_markers(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class PrefixCacheDecision:
    enabled: bool
    reason: str
    instructions: bool = False
    tools: bool = False
    static_core: Optional[str] = field(default=None, repr=False)
    policy_revision: str = ""
    prefix_digest: Optional[str] = None
    estimated_prefix_tokens: Optional[int] = None
    minimum_prefix_tokens: int = 0


def _model_minimum(model: Any) -> int:
    if type(model) is str and model.strip().lower().startswith(_OPUS_MODEL_MARKER):
        return _OPUS_MINIMUM_CACHEABLE_PREFIX_TOKENS
    return MINIMUM_CACHEABLE_PREFIX_TOKENS


def _tool_name(tool: Any) -> Optional[str]:
    if not isinstance(tool, dict):
        return None
    function = tool.get("function")
    if isinstance(function, dict) and type(function.get("name")) is str:
        return function["name"]
    if type(tool.get("name")) is str:
        return tool["name"]
    return None


class PromptCachePolicy:
    def __init__(
        self,
        approved_prefixes: tuple[ApprovedPrefix, ...] = (),
        *,
        minimum_prefix_tokens: Optional[int] = None,
    ) -> None:
        for entry in approved_prefixes:
            if (
                type(entry.name) is not str or not entry.name
                or type(entry.digest) is not str or len(entry.digest) != 64
            ):
                raise ValueError("malformed approved prefix")
        self._approved = frozenset(entry.digest for entry in approved_prefixes)
        self._names = tuple(entry.name for entry in approved_prefixes)
        self._minimum_override = minimum_prefix_tokens

    @property
    def policy_revision(self) -> str:
        entries = ",".join(
            f"{name}:{digest}"
            for name, digest in sorted(zip(self._names, self._approved))
        )
        return canonical_digest(f"{POLICY_SEMANTICS_VERSION}|{entries}")

    def _minimum(self, model: Any) -> int:
        if self._minimum_override is not None:
            return self._minimum_override
        return _model_minimum(model)

    def evaluate(
        self,
        *,
        enabled: bool,
        base_url: Any,
        model: Any,
        static_prefix: Any,
        system_content: Any,
        tool_defs: Any = None,
        static_tool_names: Any = frozenset(),
        prefix_token_estimator: Optional[Callable[[str, Any], int]] = None,
    ) -> PrefixCacheDecision:
        revision = self.policy_revision
        if not enabled:
            return PrefixCacheDecision(False, "policy_disabled", policy_revision=revision)
        if not is_native_messages_profile(base_url):
            return PrefixCacheDecision(False, "profile_not_native", policy_revision=revision)
        if type(static_prefix) is not str or not static_prefix:
            return PrefixCacheDecision(False, "no_approved_prefix", policy_revision=revision)
        digest = canonical_digest(static_prefix)
        if digest not in self._approved:
            return PrefixCacheDecision(
                False, "prefix_not_approved", policy_revision=revision, prefix_digest=digest,
            )
        if type(system_content) is not str or not system_content:
            return PrefixCacheDecision(
                False, "prefix_mismatch", policy_revision=revision, prefix_digest=digest,
            )
        if not system_content.startswith(static_prefix):
            return PrefixCacheDecision(
                False, "prefix_mismatch", policy_revision=revision, prefix_digest=digest,
            )

        tools_eligible = True
        if tool_defs:
            if not isinstance(tool_defs, (list, tuple)):
                return PrefixCacheDecision(
                    False, "prefix_mismatch", policy_revision=revision, prefix_digest=digest,
                )
            allowed = static_tool_names if isinstance(static_tool_names, (set, frozenset)) else frozenset()
            names = [_tool_name(tool) for tool in tool_defs]
            if any(name is None or name not in allowed for name in names):
                tools_eligible = False

        if not tools_eligible:
            return PrefixCacheDecision(
                False, "prefix_not_fully_approved", policy_revision=revision, prefix_digest=digest,
            )

        if prefix_token_estimator is None:
            estimated: Optional[int] = None
        else:
            try:
                estimated = int(prefix_token_estimator(static_prefix, tool_defs if tools_eligible else None))
            except (TypeError, ValueError, OverflowError):
                estimated = None
            if estimated is None or estimated < 0:
                return PrefixCacheDecision(
                    False, "admission_uncertain", policy_revision=revision, prefix_digest=digest,
                )
            minimum = self._minimum(model)
            if estimated < minimum:
                return PrefixCacheDecision(
                    False,
                    "below_minimum",
                    policy_revision=revision,
                    prefix_digest=digest,
                    estimated_prefix_tokens=estimated,
                    minimum_prefix_tokens=minimum,
                )

        return PrefixCacheDecision(
            True,
            "approved",
            instructions=True,
            tools=tools_eligible,
            static_core=static_prefix,
            policy_revision=revision,
            prefix_digest=digest,
            estimated_prefix_tokens=estimated,
            minimum_prefix_tokens=self._minimum(model),
        )


def apply_cache_markers(payload: Mapping[str, Any], decision: PrefixCacheDecision) -> dict[str, Any]:
    sanitized = _strip_markers(dict(payload))
    if not decision.enabled:
        return sanitized
    tools = sanitized.get("tools")
    if decision.tools and isinstance(tools, list) and tools:
        last = tools[-1]
        if isinstance(last, dict):
            last[CACHE_CONTROL_KEY] = dict(EPHEMERAL_MARKER)
    system = sanitized.get("system")
    if (
        decision.instructions
        and decision.static_core is not None
        and isinstance(system, list)
        and system
    ):
        first = system[0]
        if isinstance(first, dict) and first.get("text") == decision.static_core:
            first[CACHE_CONTROL_KEY] = dict(EPHEMERAL_MARKER)
    return sanitized


@dataclass(frozen=True, slots=True)
class NativeUsage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_creation_input_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None

    @property
    def total_tokens(self) -> Optional[int]:
        parts = (
            self.input_tokens,
            self.output_tokens,
            self.cache_creation_input_tokens,
            self.cache_read_input_tokens,
        )
        if any(part is None for part in parts):
            return None
        return sum(parts)


def _usage_value(usage: Any, name: str) -> Optional[int]:
    value = (
        usage.get(name)
        if isinstance(usage, Mapping)
        else getattr(usage, name, None)
    )
    return value if type(value) is int and not isinstance(value, bool) and value >= 0 else None


def parse_native_usage(usage: Any) -> NativeUsage:
    if usage is None:
        return NativeUsage()
    return NativeUsage(
        input_tokens=_usage_value(usage, "input_tokens"),
        output_tokens=_usage_value(usage, "output_tokens"),
        cache_creation_input_tokens=_usage_value(usage, "cache_creation_input_tokens"),
        cache_read_input_tokens=_usage_value(usage, "cache_read_input_tokens"),
    )


@dataclass(frozen=True, slots=True)
class CacheMeasurement:
    owner_id: str
    provider: str
    model: str
    profile: str
    policy_revision: str
    config_revision: str
    reason: str
    outcome: str
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    uncached_input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


def config_revision(base_url: Any, model: Any) -> str:
    base = base_url if type(base_url) is str else ""
    name = model if type(model) is str else ""
    return canonical_digest(f"{base}|{name}")


def record_measurement(
    measurement: CacheMeasurement,
    *,
    emit: Callable[[str], None] = logger.info,
) -> CacheMeasurement:
    fields = {name: getattr(measurement, name) for name in _MEASUREMENT_ENABLED_FIELDS}
    for name in ("cache_read_tokens", "cache_write_tokens", "uncached_input_tokens", "output_tokens"):
        value = getattr(measurement, name)
        if value is not None:
            fields[name] = value
    emit("prefix_cache_measurement " + json.dumps(fields, sort_keys=True, default=str))
    return measurement
