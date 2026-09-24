"""Classifies a chat turn into a minimal, safe-by-construction flow (read-only,
multi-tool plan-then-execute, or parser map-reduce) and returns the tool-call
constraints that pattern enforces; used by turn_hooks.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def flow_patterns_enabled() -> bool:
    return os.getenv("FF_FLOW_PATTERNS", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


READ_ONLY, MULTI_TOOL, PARSER, DEFAULT = "read_only", "multi_tool", "parser", "default"

_PARSER_KEYWORDS = (
    "parse",
    "extract",
    "read file",
    "read-file",
    "read the file",
    "read this file",
    "read my file",
    "read attachment",
    "read the attachment",
)

_LOOKUP_LEADERS = (
    "what",
    "who",
    "when",
    "where",
    "why",
    "how",
    "is",
    "are",
    "does",
    "list",
    "show",
    "find",
)

_MULTI_STEP_KEYWORDS = (
    "then",
    "and then",
    "after that",
    "finally",
    "steps",
)


def _first_token(text: str) -> str:
    for raw in text.strip().split():
        token = "".join(ch for ch in raw if ch.isalpha()).lower()
        if token:
            return token
    return ""


def classify_flow(
    request: str,
    *,
    tool_count: int = 0,
    has_attachment: bool = False,
) -> str:
    text = (request or "").strip()
    low = text.lower()

    if has_attachment or any(kw in low for kw in _PARSER_KEYWORDS):
        return PARSER

    is_lookup = _first_token(text) in _LOOKUP_LEADERS or low.endswith("?")
    is_multi_step = tool_count >= 2 or any(kw in low for kw in _MULTI_STEP_KEYWORDS)

    if is_multi_step:
        return MULTI_TOOL

    if is_lookup and tool_count <= 1:
        return READ_ONLY

    return DEFAULT


@dataclass(frozen=True)
class FlowConstraints:
    pattern: str
    allow_free_tool_calls: bool
    requires_plan: bool
    max_tools: int


_CONSTRAINTS = {
    READ_ONLY: FlowConstraints(
        pattern=READ_ONLY,
        allow_free_tool_calls=False,
        requires_plan=False,
        max_tools=1,
    ),
    MULTI_TOOL: FlowConstraints(
        pattern=MULTI_TOOL,
        allow_free_tool_calls=False,
        requires_plan=True,
        max_tools=12,
    ),
    PARSER: FlowConstraints(
        pattern=PARSER,
        allow_free_tool_calls=False,
        requires_plan=False,
        max_tools=4,
    ),
    DEFAULT: FlowConstraints(
        pattern=DEFAULT,
        allow_free_tool_calls=True,
        requires_plan=False,
        max_tools=8,
    ),
}


def constraints_for(pattern: str) -> FlowConstraints:
    known = _CONSTRAINTS.get(pattern)
    if known is not None:
        return known
    base = _CONSTRAINTS[DEFAULT]
    return FlowConstraints(
        pattern=pattern,
        allow_free_tool_calls=base.allow_free_tool_calls,
        requires_plan=base.requires_plan,
        max_tools=base.max_tools,
    )


# Empty plan refuses all — never treat as unrestricted
def refuse_out_of_plan(planned_tools: list, called_tool: str) -> bool:
    target = (called_tool or "").strip().lower()
    allowed = {
        (str(name) or "").strip().lower()
        for name in (planned_tools or [])
    }
    return target not in allowed


def within_tool_budget(pattern: str, tools_used: int) -> bool:
    return tools_used <= constraints_for(pattern).max_tools
