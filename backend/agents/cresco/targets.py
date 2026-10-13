"""Bounded member-target grammar and operator-approved admission for the Cresco bridge:
mcp_server.py consults this module before any dial so discovery, reconnect, or fabric
content can never widen the actionable target set.
"""

from __future__ import annotations

import os
import re

MAX_TARGET_CHARS = 192
_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class TargetRefused(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def parse_target(raw: object) -> tuple[str, str] | tuple[str, str, str]:
    if not isinstance(raw, str) or not raw:
        raise TargetRefused("invalid_target", "A target of the form region_agent[_plugin] is required.")
    if len(raw) > MAX_TARGET_CHARS or any(ch.isspace() for ch in raw):
        raise TargetRefused("invalid_target", "The target is not a bounded region_agent[_plugin] identifier.")
    parts = raw.split("_")
    if len(parts) not in (2, 3) or not all(_COMPONENT.match(part) for part in parts):
        raise TargetRefused("invalid_target", "The target is not a bounded region_agent[_plugin] identifier.")
    return tuple(parts)  # type: ignore[return-value]


def target_text(parsed: tuple[str, ...]) -> str:
    return "_".join(parsed)


def approved_targets(env: dict[str, str] | None = None) -> frozenset[str]:
    source = os.environ if env is None else env
    approved = set()
    for entry in source.get("CRESCO_APPROVED_TARGETS", "").replace(";", ",").split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            approved.add(target_text(parse_target(candidate)))
        except TargetRefused:
            continue
    return frozenset(approved)


def require_approved(raw: object, env: dict[str, str] | None = None) -> tuple[str, ...]:
    parsed = parse_target(raw)
    if target_text(parsed) not in approved_targets(env):
        raise TargetRefused(
            "target_not_approved",
            "The target is not an operator-approved Cresco target; no connection was attempted.",
        )
    return parsed
