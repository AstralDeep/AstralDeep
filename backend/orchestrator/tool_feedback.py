"""Builds short, deterministic tool-failure notices for chat, deduplicated per turn, so
provider bodies/exception strings/URLs never reach the UI though the fuller
diagnostic stays available to audit. Used by coordinator.py and orchestrator.py.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from astralprims import Alert

_SEARCH_KEY = "Add a search provider API key in agent settings for higher limits."
PUBLIC_ERRORS = {
    "SEARCH_BLOCKED": f"Keyless search is blocked. {_SEARCH_KEY}",
    "SEARCH_AUTH_FAILED": "Search credentials were rejected. Check the provider API key in agent settings.",
    "SEARCH_EGRESS_BLOCKED": "The search provider is blocked by network policy. Check its URL in agent settings.",
    "SEARCH_RATE_LIMITED": "Search has reached its provider limit. Try later or check your API plan in agent settings.",
    "SEARCH_UNAVAILABLE": f"Search is unavailable. Try later. {_SEARCH_KEY}",
    "UPSTREAM_NOT_FOUND": "This page was not found. Check the link or choose another source.",
    "UPSTREAM_ACCESS_DENIED": "This page requires access that is unavailable here. Choose a public source.",
    "UPSTREAM_BLOCKED": "This page is blocked by network policy. Choose another source.",
    "UPSTREAM_TOO_LARGE": "This page exceeds the size limit. Choose a smaller source.",
    "UPSTREAM_UNAVAILABLE": "This page could not be retrieved. Try later or choose another source.",
    "ARXIV_UNAVAILABLE": "arXiv search is unavailable. Try again later.",
    "RESEARCH_SUMMARY_UNAVAILABLE": "The research summary could not be completed. Try again later.",
}
_NOTICES: ContextVar[set[str] | None] = ContextVar("tool_failure_notices", default=None)
_MAX_NOTICES = 64


@contextmanager
def turn_tool_notices():
    token = _NOTICES.set(set())
    try:
        yield
    finally:
        _NOTICES.reset(token)


def tool_failure_message(tool_name: object, error: object) -> str:
    code = error.get("code") if isinstance(error, Mapping) else None
    message = PUBLIC_ERRORS.get(code) if isinstance(code, str) else None
    if message is None:
        label = "Tool"
        if isinstance(tool_name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", tool_name):
            label = tool_name.replace("_", " ").capitalize()
        message = f"{label} could not complete. Try again."
    return message


def tool_failure_content(tool_name: object, error: object) -> str:
    error = error if isinstance(error, Mapping) else {}
    code = error.get("code")
    code = code if isinstance(code, str) and code in PUBLIC_ERRORS else "TOOL_FAILED"
    return json.dumps({
        "status": "error",
        "code": code,
        "message": tool_failure_message(tool_name, error),
        "retryable": error.get("retryable") is True,
    })


def tool_failure_notice(tool_name: object, error: object) -> dict | None:
    message = tool_failure_message(tool_name, error)
    notices = _NOTICES.get()
    if notices is not None:
        if message in notices:
            return None
        if len(notices) < _MAX_NOTICES:
            notices.add(message)
    return Alert(
        variant="error",
        message=message,
        css={"padding": "6px 10px", "margin": "4px 0"},
    ).to_dict()
