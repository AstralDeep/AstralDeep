"""Pure, deterministic pre-send gate reviewing a drafted response and its intended tool
call for injection, leaked secrets/PHI, and intent mismatch, returning
allow/revise/block/escalate. Called from orchestrator.py and turn_hooks.py.
"""

from __future__ import annotations

import os
import re


def supervisor_enabled() -> bool:
    return os.getenv("FF_RUNTIME_SUPERVISOR", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


ALLOW, REVISE, BLOCK, ESCALATE = "allow", "revise", "block", "escalate"

_SEVERITY = {ALLOW: 0, REVISE: 1, ESCALATE: 2, BLOCK: 3}


_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous instructions",
    "disregard the above",
    "you are now",
    "system prompt",
    "new instructions:",
    "reveal your instructions",
    "print your system",
)

_LEAK_MARKERS = (
    "api_key",
    "database_url",
    "-----begin",
    "secret_key",
    "bearer ",
)

_DESTRUCTIVE_NAME_PATTERN = re.compile(
    r"(?:^|[_\-/])(?:delete|drop|wipe|purge|send|transfer|pay)(?:_|\b)",
    re.IGNORECASE,
)

_INTENT_VERBS = (
    "delete",
    "remove",
    "wipe",
    "purge",
    "drop",
    "send",
    "email",
    "mail",
    "transfer",
    "pay",
    "wire",
)


def scan_ingress(untrusted_text: str) -> list:
    if not untrusted_text:
        return []
    low = untrusted_text.lower()
    return [marker for marker in _INJECTION_MARKERS if marker in low]


def review_output(draft_text: str, *, phi_check=None) -> tuple:
    reasons: list = []
    verdict = ALLOW
    text = draft_text or ""
    low = text.lower()

    leaks = [marker for marker in _LEAK_MARKERS if marker in low]
    for marker in leaks:
        reasons.append(f"leak marker in draft: {marker!r}")
    if leaks:
        verdict = _max_verdict(verdict, BLOCK)

    if phi_check is not None:
        try:
            is_phi = bool(phi_check(text))
        # Any detector error fails closed as PHI-positive
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"phi_check error (fail-closed): {exc}")
            verdict = _max_verdict(verdict, BLOCK)
        else:
            if is_phi:
                reasons.append("PHI detected in draft")
                verdict = _max_verdict(verdict, BLOCK)

    injections = [marker for marker in _INJECTION_MARKERS if marker in low]
    for marker in injections:
        reasons.append(f"injection marker in draft: {marker!r}")
    if injections:
        verdict = _max_verdict(verdict, REVISE)

    return verdict, reasons


def mcp_intent_text(tool_name: str) -> str:
    name = (tool_name or "").strip()
    words = re.sub(r"[_\-.:/]+", " ", name).strip()
    return f"call tool {name} {words}".strip()


def intent_aligned(request: str, tool_name: str, *, destructive_tools=None) -> bool:
    name = tool_name or ""
    destructive = _is_destructive(name, destructive_tools)
    if not destructive:
        return True

    low_request = (request or "").lower()
    return any(verb in low_request for verb in _INTENT_VERBS)


def supervise(
    request: str,
    draft_text: str,
    intended_tool: str = None,
    *,
    phi_check=None,
    destructive_tools=None,
) -> tuple:
    reasons: list = []
    verdict = ALLOW

    if intended_tool and not intent_aligned(
        request, intended_tool, destructive_tools=destructive_tools
    ):
        reasons.append(f"intent mismatch: {intended_tool}")
        verdict = _max_verdict(verdict, ESCALATE)

    out_verdict, out_reasons = review_output(draft_text, phi_check=phi_check)
    reasons.extend(out_reasons)
    verdict = _max_verdict(verdict, out_verdict)

    return verdict, reasons


def _max_verdict(a: str, b: str) -> str:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _is_destructive(tool_name: str, destructive_tools=None) -> bool:
    if destructive_tools and tool_name in destructive_tools:
        return True
    return bool(_DESTRUCTIVE_NAME_PATTERN.search(tool_name or ""))
