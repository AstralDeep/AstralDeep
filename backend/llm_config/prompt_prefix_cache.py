"""Offline eligibility and marker validation for explicitly approved public prompt prefixes. Native provider support remains unqualified, so the orchestrator sends ordinary uncached requests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

NATIVE_MESSAGES_PROFILE = "native_messages"
NATIVE_MESSAGES_BASE_URL = "https://api.anthropic.com/v1"
POLICY_SEMANTICS_VERSION = "prompt-prefix-cache-policy-v2"
MINIMUM_CACHEABLE_PREFIX_TOKENS = 1024
CACHE_CONTROL_KEY = "cache_control"
EPHEMERAL_MARKER = {"type": "ephemeral"}
MAXIMUM_TOOL_PREFIX_BYTES = 65_536



def canonical_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _json_value(value: Any, depth: int = 0) -> bool:
    if depth > 32:
        return False
    if type(value) in (str, int, float, bool) or value is None:
        return True
    if type(value) is list:
        return all(_json_value(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and key != CACHE_CONTROL_KEY and _json_value(item, depth + 1)
            for key, item in value.items()
        )
    return False


def canonical_tools_digest(tools: Any) -> Optional[str]:
    if tools is None:
        tools = []
    if type(tools) not in (list, tuple):
        return None
    names = set()
    for tool in tools:
        if type(tool) is not dict or not _json_value(tool):
            return None
        definition = tool.get("function", tool)
        if type(definition) is not dict:
            return None
        name = definition.get("name")
        schema = definition.get("parameters", definition.get("input_schema"))
        if (type(name) is not str or not name or name in names
                or type(schema) is not dict or schema.get("type") != "object"
                or ("properties" in schema and type(schema["properties"]) is not dict)):
            return None
        names.add(name)
    try:
        encoded = json.dumps(list(tools), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    if len(encoded.encode("utf-8", errors="surrogatepass")) > MAXIMUM_TOOL_PREFIX_BYTES:
        return None
    return canonical_digest(encoded)


_EMPTY_TOOLS_DIGEST = canonical_digest("[]")


@dataclass(frozen=True, slots=True)
class ApprovedPrefix:
    name: str
    digest: str
    tools_digest: str = _EMPTY_TOOLS_DIGEST


def _strip_markers(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_markers(item) for key, item in value.items() if key != CACHE_CONTROL_KEY}
    if isinstance(value, (list, tuple)):
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
    tools_digest: Optional[str] = None
    model: str = ""
    estimated_prefix_tokens: Optional[int] = None
    minimum_prefix_tokens: int = 0


def _valid_digest(value: Any) -> bool:
    return type(value) is str and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


class PromptCachePolicy:
    def __init__(
        self,
        approved_prefixes: tuple[ApprovedPrefix, ...] = (),
        *,
        minimum_prefix_tokens: Optional[int] = None,
    ) -> None:
        names = set()
        for entry in approved_prefixes:
            if (not isinstance(entry, ApprovedPrefix) or type(entry.name) is not str
                    or not entry.name or entry.name in names or not _valid_digest(entry.digest)
                    or not _valid_digest(entry.tools_digest)):
                raise ValueError("malformed approved prefix")
            names.add(entry.name)
        if minimum_prefix_tokens is not None and (
                type(minimum_prefix_tokens) is not int or minimum_prefix_tokens < 0):
            raise ValueError("malformed minimum prefix tokens")
        self._entries = tuple(sorted((entry.name, entry.digest, entry.tools_digest) for entry in approved_prefixes))
        self._approved = frozenset((entry.digest, entry.tools_digest) for entry in approved_prefixes)
        self._minimum_override = minimum_prefix_tokens

    @property
    def policy_revision(self) -> str:
        return canonical_digest(json.dumps(
            [POLICY_SEMANTICS_VERSION, self._entries, self._minimum_override], separators=(",", ":"),
        ))

    def evaluate(
        self,
        *,
        enabled: bool,
        base_url: Any,
        model: Any,
        static_prefix: Any,
        system_content: Any,
        tool_defs: Any = None,
        prefix_token_estimator: Optional[Callable[[str, Any], int]] = None,
    ) -> PrefixCacheDecision:
        revision = self.policy_revision
        decision = {"policy_revision": revision}
        if enabled is not True:
            return PrefixCacheDecision(False, "policy_disabled", **decision)
        if base_url != NATIVE_MESSAGES_BASE_URL or type(model) is not str or not model:
            return PrefixCacheDecision(False, "profile_not_native", **decision)
        if type(static_prefix) is not str or not static_prefix:
            return PrefixCacheDecision(False, "no_approved_prefix", **decision)
        digest = canonical_digest(static_prefix)
        decision["prefix_digest"] = digest
        if type(system_content) is not str or not system_content.startswith(static_prefix):
            return PrefixCacheDecision(False, "prefix_mismatch", **decision)
        tools_digest = canonical_tools_digest(tool_defs)
        if tools_digest is None or (digest, tools_digest) not in self._approved:
            return PrefixCacheDecision(False, "prefix_not_fully_approved", **decision)
        if prefix_token_estimator is None:
            return PrefixCacheDecision(False, "admission_uncertain", **decision)
        try:
            estimated = prefix_token_estimator(static_prefix, tool_defs)
        except Exception:
            return PrefixCacheDecision(False, "admission_uncertain", **decision)
        if type(estimated) is not int or estimated < 0:
            return PrefixCacheDecision(False, "admission_uncertain", **decision)
        minimum = self._minimum_override
        if minimum is None:
            minimum = 2048 if model.startswith("claude-opus-4") else MINIMUM_CACHEABLE_PREFIX_TOKENS
        if estimated < minimum:
            return PrefixCacheDecision(False, "below_minimum", estimated_prefix_tokens=estimated,
                                       minimum_prefix_tokens=minimum, **decision)
        return PrefixCacheDecision(
            True, "approved", instructions=True, tools=bool(tool_defs), static_core=static_prefix,
            tools_digest=tools_digest, model=model, estimated_prefix_tokens=estimated,
            minimum_prefix_tokens=minimum, **decision,
        )

    def validates(self, decision: PrefixCacheDecision) -> bool:
        return (decision.enabled and decision.policy_revision == self.policy_revision
                and (decision.prefix_digest, decision.tools_digest) in self._approved)


def apply_cache_markers(
    payload: Mapping[str, Any], decision: PrefixCacheDecision, *, policy: Optional[PromptCachePolicy] = None,
) -> dict[str, Any]:
    sanitized = _strip_markers(dict(payload))
    if (policy is None or not policy.validates(decision)
            or type(decision.static_core) is not str):
        return sanitized
    system = sanitized.get("system")
    tools = sanitized.get("tools")
    if (not isinstance(system, list) or not system
            or system[0] != {"type": "text", "text": decision.static_core}
            or payload.get("model") != decision.model
            or canonical_digest(decision.static_core) != decision.prefix_digest
            or canonical_tools_digest(tools) != decision.tools_digest):
        return sanitized
    if decision.tools and tools:
        tools[-1][CACHE_CONTROL_KEY] = dict(EPHEMERAL_MARKER)
    if decision.instructions:
        system[0][CACHE_CONTROL_KEY] = dict(EPHEMERAL_MARKER)
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
