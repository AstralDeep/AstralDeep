"""Classifies registered tool effects and binds model-proposed actions to owner review.
The host treats unknown or transformed source lineage as untrusted at effect time.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class Origin(str, Enum):
    HUMAN = "signed_human"
    TOOL = "tool_result"
    DERIVED = "derived"
    UNKNOWN = "unknown"


class Effect(str, Enum):
    READ = "read"
    MUTATION = "mutation_or_disclosure"
    PRIVILEGED = "privileged"


_EFFECT_BY_SCOPE = {
    "tools:read": Effect.READ,
    "tools:search": Effect.READ,
    "tools:write": Effect.MUTATION,
    "tools:files": Effect.MUTATION,
    "tools:execute": Effect.PRIVILEGED,
    "tools:system": Effect.PRIVILEGED,
}


@dataclass(frozen=True)
class ActionDeclaration:
    agent: str
    tool: str
    scope: str
    effect: Effect

    @property
    def needs_review(self) -> bool:
        return self.effect is not Effect.READ


def declaration(scopes: Mapping[str, str], agent: str, tool: str) -> ActionDeclaration:
    scope = scopes.get(tool)
    if not isinstance(scope, str) or scope not in _EFFECT_BY_SCOPE:
        raise ValueError(f"Tool {agent}/{tool} has no valid effect declaration")
    return ActionDeclaration(agent, tool, scope, _EFFECT_BY_SCOPE[scope])


def combine_origins(*origins: Origin) -> Origin:
    if not origins:
        return Origin.UNKNOWN
    if all(origin is Origin.HUMAN for origin in origins):
        return Origin.HUMAN
    if Origin.UNKNOWN in origins:
        return Origin.UNKNOWN
    return Origin.DERIVED


def action_digest(owner: str, chat: str, action: ActionDeclaration, args: Any) -> str:
    payload = json.dumps({
        "owner": owner, "chat": chat, "agent": action.agent,
        "tool": action.tool, "scope": action.scope, "effect": action.effect.value,
        "arguments": args,
    }, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def review_risks(action: ActionDeclaration) -> tuple[str, ...]:
    return ("source_bound", action.effect.value, action.scope)


def read_only_stream(scopes: Mapping[str, str], agent: str, tool: str) -> ActionDeclaration:
    action = declaration(scopes, agent, tool)
    if action.needs_review:
        raise ValueError("Effectful tools cannot use the stream transport")
    return action


def _strings(value: Any):
    if isinstance(value, str) and value:
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child)


class SourceLedger:
    def __init__(self, limit: int = 4096):
        self._limit = limit
        self._origins: OrderedDict[str, Origin] = OrderedDict()

    @staticmethod
    def _key(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def record_tool_output(self, *values: Any) -> None:
        for value in values:
            for text in _strings(value):
                key = self._key(text)
                self._origins[key] = Origin.TOOL
                self._origins.move_to_end(key)
                if len(self._origins) > self._limit:
                    self._origins.popitem(last=False)

    def origin_of(self, value: Any) -> Origin:
        strings = tuple(_strings(value))
        if not strings:
            return Origin.UNKNOWN
        origins = [self._origins.get(self._key(text), Origin.UNKNOWN)
                   for text in strings]
        if Origin.UNKNOWN in origins:
            return Origin.UNKNOWN
        return Origin.TOOL if len(origins) == 1 else Origin.DERIVED
