"""Counts identical failing tool calls within one chat turn so a loop is warned about, blocked, then stopped.
The orchestrator starts a guard per turn, refuses blocked calls on both dispatch paths and feeds results back after each round.
"""

from __future__ import annotations

import contextvars
import json
from typing import Dict, Optional, Tuple

from orchestrator.goal_progress import GoalProgress

WARN_AT = 2
BLOCK_AT = 3
STOP_AT = 6
STOP_NOTICE = "Stopped because the same failing action kept being repeated. Nothing further was run."


def warn_text(name: str) -> str:
    return (f"The last call to '{name}' failed twice with the same arguments. Do not send it again unchanged. "
            "Fix the arguments, use a different tool, or explain what is blocking you.")


def blocked_text(name: str) -> str:
    return (f"This exact call to '{name}' already failed twice and was not run again. "
            "Change the arguments or the approach.")


def _identity(tool_name: str, raw_arguments: object) -> Tuple[str, str]:
    if raw_arguments is None or (isinstance(raw_arguments, str) and not raw_arguments.strip()):
        return tool_name, "{}"
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments, strict=False)
        except ValueError:
            return tool_name, raw_arguments.strip()
    else:
        parsed = raw_arguments
    try:
        return tool_name, json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return tool_name, repr(parsed)


class RepeatGuard:
    def __init__(self) -> None:
        self._failures: Dict[Tuple[str, str], int] = {}

    def observe(self, tool_name: str, raw_arguments: object, failed: bool) -> str:
        key = _identity(tool_name, raw_arguments)
        if not failed:
            self._failures.pop(key, None)
            return "ok"
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        if count >= STOP_AT:
            return "stop"
        if count >= BLOCK_AT:
            return "block"
        if count >= WARN_AT:
            return "warn"
        return "ok"

    def blocked(self, tool_name: str, raw_arguments: object) -> bool:
        return self._failures.get(_identity(tool_name, raw_arguments), 0) >= WARN_AT


_CURRENT: contextvars.ContextVar[Optional[RepeatGuard]] = contextvars.ContextVar(
    "astral_repeat_guard", default=None)


def start_turn() -> RepeatGuard:
    guard = RepeatGuard()
    _CURRENT.set(guard)
    return guard


def current() -> Optional[RepeatGuard]:
    return _CURRENT.get()


def is_failure(result: object) -> bool:
    if result is None:
        return True
    if GoalProgress.is_approval_pending(result):
        return False
    return GoalProgress.has_error(result)
