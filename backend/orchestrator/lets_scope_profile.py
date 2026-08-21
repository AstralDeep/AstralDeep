"""Fixed Astral-to-LETS tool authority profile.

The profile is reviewed source rather than operator configuration. Unknown or
ambiguous mappings fail closed before any LETS request is constructed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Final

SCOPE_PROFILE_VERSION: Final = "astral.tools/v1"
RESOURCE_DIMENSIONS: Final = 6


class ScopeProfileError(ValueError):
    """A tool effect cannot be mapped to one exact LETS authority entry."""


@dataclass(frozen=True, slots=True)
class ScopeBinding:
    scope: str
    capability: str
    transition: str
    resource_dimension: int

    def unit_cost(self) -> tuple[int, ...]:
        values = [0] * RESOURCE_DIMENSIONS
        values[self.resource_dimension] = 1
        return tuple(values)


SCOPE_BINDINGS: Final = (
    ScopeBinding("tools:read", "astral.tools.read", "tool_read", 0),
    ScopeBinding("tools:write", "astral.tools.write", "tool_write", 1),
    ScopeBinding("tools:search", "astral.tools.search", "tool_search", 2),
    ScopeBinding("tools:system", "astral.tools.system", "tool_system", 3),
    ScopeBinding("tools:files", "astral.tools.files", "tool_files", 4),
    ScopeBinding("tools:execute", "astral.tools.execute", "tool_execute", 5),
)

_BY_SCOPE: Final = MappingProxyType({entry.scope: entry for entry in SCOPE_BINDINGS})
_BY_TRANSITION: Final = MappingProxyType(
    {entry.transition: entry for entry in SCOPE_BINDINGS}
)


def _canonical_profile_bytes(entries: Sequence[ScopeBinding]) -> bytes:
    document = {
        "entries": [asdict(entry) for entry in entries],
        "profile": SCOPE_PROFILE_VERSION,
    }
    return json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def profile_sha256(entries: Sequence[ScopeBinding] = SCOPE_BINDINGS) -> str:
    """Return the canonical profile digest for evidence and drift checks."""

    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise ScopeProfileError("scope profile entries must be an ordered sequence")
    if not entries or any(not isinstance(entry, ScopeBinding) for entry in entries):
        raise ScopeProfileError("scope profile entries must be ScopeBinding values")
    return hashlib.sha256(_canonical_profile_bytes(entries)).hexdigest()


SCOPE_PROFILE_SHA256: Final = profile_sha256()


def binding_for_scope(scope: str) -> ScopeBinding:
    if not isinstance(scope, str) or not scope or scope != scope.strip():
        raise ScopeProfileError("tool scope must be one canonical non-empty string")
    try:
        return _BY_SCOPE[scope]
    except KeyError as exc:
        raise ScopeProfileError(f"unknown LETS tool scope: {scope}") from exc


def binding_for_transition(transition: str) -> ScopeBinding:
    if not isinstance(transition, str) or not transition:
        raise ScopeProfileError("LETS transition must be a non-empty string")
    try:
        return _BY_TRANSITION[transition]
    except KeyError as exc:
        raise ScopeProfileError(f"unknown LETS transition: {transition}") from exc


def binding_for_tool(
    tool_id: str,
    tool_scope_map: Mapping[str, str],
) -> ScopeBinding:
    """Resolve one registered tool to one exact profile entry."""

    if (
        not isinstance(tool_id, str)
        or not tool_id
        or tool_id != tool_id.strip()
        or any(character.isspace() for character in tool_id)
    ):
        raise ScopeProfileError("tool ID must be one canonical non-empty string")
    if not isinstance(tool_scope_map, Mapping):
        raise ScopeProfileError("tool scope map must be a mapping")
    if tool_id not in tool_scope_map:
        raise ScopeProfileError(f"tool has no registered scope: {tool_id}")
    return binding_for_scope(tool_scope_map[tool_id])


def validate_allocation(allocation: Sequence[int]) -> tuple[int, ...]:
    """Validate the exact six-dimensional non-negative allocation."""

    if (
        not isinstance(allocation, Sequence)
        or isinstance(allocation, (str, bytes))
        or len(allocation) != RESOURCE_DIMENSIONS
    ):
        raise ScopeProfileError(
            f"LETS allocation must contain exactly {RESOURCE_DIMENSIONS} dimensions"
        )
    normalized: list[int] = []
    for value in allocation:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ScopeProfileError(
                "LETS allocation dimensions must be non-negative integers"
            )
        normalized.append(value)
    return tuple(normalized)


def require_single_audience(audiences: Iterable[str]) -> str:
    """Return one exact executor audience or reject ambiguity."""

    if isinstance(audiences, (str, bytes)) or not isinstance(audiences, Iterable):
        raise ScopeProfileError("executor audiences must be an iterable of strings")
    values = tuple(audiences)
    if len(values) != 1:
        raise ScopeProfileError("exactly one executor audience is required")
    audience = values[0]
    if (
        not isinstance(audience, str)
        or not audience
        or audience != audience.strip()
        or any(character.isspace() for character in audience)
    ):
        raise ScopeProfileError("executor audience must be one canonical string")
    return audience


__all__ = (
    "RESOURCE_DIMENSIONS",
    "SCOPE_BINDINGS",
    "SCOPE_PROFILE_SHA256",
    "SCOPE_PROFILE_VERSION",
    "ScopeBinding",
    "ScopeProfileError",
    "binding_for_scope",
    "binding_for_tool",
    "binding_for_transition",
    "profile_sha256",
    "require_single_audience",
    "validate_allocation",
)
