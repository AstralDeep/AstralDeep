"""Classifies registered tool effects and binds model-proposed actions to owner review.
The host treats unknown or transformed source lineage as untrusted at effect time.
"""

from __future__ import annotations

import hashlib
import json
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
