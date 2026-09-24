"""Opt-in, byte-identical-by-default helpers for keeping the chat ReAct loop's prompt
cache-stable and lean: compose_system_prompt() stabilizes the prefix and
edit_context() tombstones stale tool output; used by orchestrator.py.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("Orchestrator.ContextEngineering")

FILE_CONTEXT_MARK = "%%ASTRAL_FILE_CONTEXT%%"
CANVAS_CONTEXT_MARK = "%%ASTRAL_CANVAS_CONTEXT%%"

TOMBSTONE = "[older tool output cleared to save context]"


def compose_system_prompt(
    template: str,
    *,
    file_context: str = "",
    canvas_context: str = "",
    cache_stable: bool = False,
) -> str:
    file_context = file_context or ""
    canvas_context = canvas_context or ""
    if not cache_stable:
        return template.replace(FILE_CONTEXT_MARK, file_context).replace(
            CANVAS_CONTEXT_MARK, canvas_context
        )
    core = template.replace(FILE_CONTEXT_MARK, "").replace(CANVAS_CONTEXT_MARK, "")
    trailing = [s.strip() for s in (file_context, canvas_context) if s and s.strip()]
    if trailing:
        core = core.rstrip() + "\n\n" + "\n\n".join(trailing) + "\n"
    return core


def _role_of(msg: Any) -> str:
    if isinstance(msg, dict):
        return msg.get("role", "") or ""
    return getattr(msg, "role", "") or ""


def _content_len(msg: Dict[str, Any]) -> int:
    content = msg.get("content")
    if isinstance(content, str):
        return len(content)
    if content is None:
        return 0
    try:
        return len(str(content))
    except Exception:  # pragma: no cover
        return 0


def edit_context(
    messages: List[Any],
    *,
    keep_last_tool_rounds: int = 3,
    min_tombstone_chars: int = 400,
    tombstone: str = TOMBSTONE,
) -> Tuple[List[Any], int]:
    if not isinstance(messages, list) or not messages:
        return messages, 0

    round_idx = 0
    tool_rounds: Dict[int, int] = {}
    max_tool_round = -1
    for i, msg in enumerate(messages):
        role = _role_of(msg)
        if role == "assistant":
            round_idx += 1
        elif role == "tool":
            tool_rounds[i] = round_idx
            if round_idx > max_tool_round:
                max_tool_round = round_idx

    if max_tool_round < 0:
        return messages, 0

    cutoff = max_tool_round - keep_last_tool_rounds
    if cutoff < 0:
        return messages, 0

    out: List[Any] = list(messages)
    n = 0
    for i, rnd in tool_rounds.items():
        if rnd > cutoff:
            continue
        msg = out[i]
        if not isinstance(msg, dict):
            continue
        if msg.get("content") == tombstone:
            continue
        if _content_len(msg) < min_tombstone_chars:
            continue
        edited = dict(msg)
        edited["content"] = tombstone
        out[i] = edited
        n += 1
    return out, n
