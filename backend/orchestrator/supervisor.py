"""Runtime supervisor / intent-alignment gate — 033 Wave-4 (C-S5).

A pure, deterministic pre-send review of the model's drafted response and its
intended tool calls. Before anything reaches the user the supervisor can

    ALLOW    — let the drafted output / tool call proceed unchanged,
    REVISE   — the draft looks tainted (e.g. parrots an injection) and should
               be regenerated / sanitised,
    BLOCK    — the draft leaks a secret or PHI and must never be sent,
    ESCALATE — an intended tool call is destructive but the user never asked
               for that action; a human should confirm first.

Three independent, side-effect-free checks compose into the gate:

  * ``scan_ingress``  — flags injection markers in *untrusted* input so callers
    can datamark / quarantine it before it is trusted.
  * ``review_output`` — inspects the *drafted* output for leak markers, PHI
    (via an injected ``phi_check`` callable) and parroted injection markers.
  * ``intent_aligned`` — checks that a destructive tool call matches an intent
    actually expressed in the user's request.

The module is deliberately dependency-free: every external capability (the PHI
detector, the destructive-tool catalogue) is *injected* by the caller, so this
file imports nothing from the rest of the project and is trivially testable.

MCP channel (feature 064)
-------------------------
``intent_aligned`` assumes a natural-language request from which intent verbs
can be read. An MCP ``tools/call`` carries no such text — the caller names the
tool explicitly, and that naming *is* the intent — so the orchestrator passes
:func:`mcp_intent_text` (``"call tool <name>"``) as the request for
``mcp``-channel dispatches. The tool's own name contributes its verb, so a
destructive tool is aligned exactly when the caller asked for it by name, and
an LLM-planned call over chat still needs the user's words. This only changes
the supervisor's *input*; the permission gate, the feature-063 remote-compute
destructive refusal, taint and HITL gates run unchanged over MCP.
"""
from __future__ import annotations

import os
import re

# --------------------------------------------------------------------------- #
# Feature flag                                                                 #
# --------------------------------------------------------------------------- #


def supervisor_enabled() -> bool:
    """Return True when the runtime supervisor is enabled via env flag.

    Reads ``FF_RUNTIME_SUPERVISOR``; truthy values are ``1/true/yes/on``
    (case-insensitive, surrounding whitespace ignored). Defaults to off.
    """
    return os.getenv("FF_RUNTIME_SUPERVISOR", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# --------------------------------------------------------------------------- #
# Verdicts                                                                     #
# --------------------------------------------------------------------------- #

ALLOW, REVISE, BLOCK, ESCALATE = "allow", "revise", "block", "escalate"

#: Severity ordering used to pick the most severe verdict when several checks
#: fire. Higher number == more severe. BLOCK > ESCALATE > REVISE > ALLOW.
_SEVERITY = {ALLOW: 0, REVISE: 1, ESCALATE: 2, BLOCK: 3}


# --------------------------------------------------------------------------- #
# Marker catalogues                                                            #
# --------------------------------------------------------------------------- #

#: Substrings that signal a prompt-injection / jailbreak attempt in untrusted
#: ingress or a draft that is parroting one. Matched case-insensitively.
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

#: Substrings that strongly suggest a secret / credential leak in a draft.
#: Matched case-insensitively.
_LEAK_MARKERS = (
    "api_key",
    "database_url",
    "-----begin",
    "secret_key",
    "bearer ",
)

#: Verb patterns that name destructive tool intents. A tool whose name matches
#: one of these prefixes/fragments is treated as destructive.
_DESTRUCTIVE_NAME_PATTERN = re.compile(
    r"(?:^|[_\-/])(?:delete|drop|wipe|purge|send|transfer|pay)(?:_|\b)",
    re.IGNORECASE,
)

#: Words that, when present in a request, express an intent to perform a
#: destructive / outbound action. Matched as whole-ish words (substring after
#: lowercasing) so "delete", "removed", "emailing", "transferred", ... all hit.
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


# --------------------------------------------------------------------------- #
# Ingress scan                                                                 #
# --------------------------------------------------------------------------- #


def scan_ingress(untrusted_text: str) -> list:
    """Return the injection markers present in ``untrusted_text``.

    The text is lowercased and each entry of :data:`_INJECTION_MARKERS` is
    checked as a substring. The returned list preserves marker-catalogue order
    and is empty for clean input. Callers use the result to datamark / flag
    untrusted ingress before it is trusted by the model.
    """
    if not untrusted_text:
        return []
    low = untrusted_text.lower()
    return [marker for marker in _INJECTION_MARKERS if marker in low]


# --------------------------------------------------------------------------- #
# Output review                                                                #
# --------------------------------------------------------------------------- #


def review_output(draft_text: str, *, phi_check=None) -> tuple:
    """Review a drafted model response before it is sent to the user.

    Returns ``(verdict, reasons)`` where ``reasons`` is a list of human-readable
    strings explaining every concern found:

      * **BLOCK** if the draft contains a leak marker (:data:`_LEAK_MARKERS`) or
        ``phi_check`` is provided and reports PHI. ``phi_check`` raising is
        treated as PHI (fail-closed → BLOCK).
      * **REVISE** if the draft contains an injection marker — the model may be
        parroting an injection back to the user.
      * **ALLOW** otherwise.

    All concerns are collected, so the reasons list may describe several issues;
    the returned verdict is the most severe one (BLOCK > REVISE > ALLOW).
    """
    reasons: list = []
    verdict = ALLOW
    text = draft_text or ""
    low = text.lower()

    # --- leak markers -> BLOCK --------------------------------------------- #
    leaks = [marker for marker in _LEAK_MARKERS if marker in low]
    for marker in leaks:
        reasons.append(f"leak marker in draft: {marker!r}")
    if leaks:
        verdict = _max_verdict(verdict, BLOCK)

    # --- PHI (injected detector, fail-closed) -> BLOCK --------------------- #
    if phi_check is not None:
        try:
            is_phi = bool(phi_check(text))
        except Exception as exc:  # noqa: BLE001 — fail closed on detector error
            reasons.append(f"phi_check error (fail-closed): {exc}")
            verdict = _max_verdict(verdict, BLOCK)
        else:
            if is_phi:
                reasons.append("PHI detected in draft")
                verdict = _max_verdict(verdict, BLOCK)

    # --- parroted injection markers -> REVISE ------------------------------ #
    injections = [marker for marker in _INJECTION_MARKERS if marker in low]
    for marker in injections:
        reasons.append(f"injection marker in draft: {marker!r}")
    if injections:
        verdict = _max_verdict(verdict, REVISE)

    return verdict, reasons


# --------------------------------------------------------------------------- #
# Intent alignment                                                             #
# --------------------------------------------------------------------------- #


def mcp_intent_text(tool_name: str) -> str:
    """Synthesized request text for an MCP-channel call (see module docstring).

    The tool name is spelled twice — once verbatim and once with separators
    replaced by spaces — so both ``delete_records`` and ``delete-records`` expose
    their action verb to :func:`intent_aligned`.
    """
    name = (tool_name or "").strip()
    words = re.sub(r"[_\-.:/]+", " ", name).strip()
    return f"call tool {name} {words}".strip()


def intent_aligned(request: str, tool_name: str, *, destructive_tools=None) -> bool:
    """Return whether ``tool_name`` is aligned with the user's ``request``.

    Heuristic, deterministic intent check:

      * A **destructive** tool — its name matches ``delete_/drop_/wipe_/purge_/
        send_/transfer_/pay_`` (see :data:`_DESTRUCTIVE_NAME_PATTERN`) or it is
        listed in the injected ``destructive_tools`` collection — is aligned
        **only** when the request expresses that intent, i.e. contains one of
        the action verbs in :data:`_INTENT_VERBS` (delete / remove / send /
        email / transfer / pay / ...).
      * A non-destructive / read tool is **always** aligned.

    Returns ``True`` (aligned) or ``False`` (a destructive action the user did
    not ask for).
    """
    name = tool_name or ""
    destructive = _is_destructive(name, destructive_tools)
    if not destructive:
        return True

    low_request = (request or "").lower()
    return any(verb in low_request for verb in _INTENT_VERBS)


# --------------------------------------------------------------------------- #
# Combined gate                                                                #
# --------------------------------------------------------------------------- #


def supervise(
    request: str,
    draft_text: str,
    intended_tool: str = None,
    *,
    phi_check=None,
    destructive_tools=None,
) -> tuple:
    """Run the combined pre-send supervisor gate.

    Returns ``(verdict, reasons)``:

      * If ``intended_tool`` is given and not intent-aligned with ``request``
        (a destructive action the user did not ask for) the verdict folds in
        **ESCALATE** with reason ``"intent mismatch: {tool}"`` — a human should
        confirm before the action runs.
      * The drafted output is always reviewed via :func:`review_output` and its
        verdict / reasons are folded in too.

    Verdict precedence is **BLOCK > ESCALATE > REVISE > ALLOW**: the most severe
    verdict among the checks is returned. ``reasons`` aggregates every concern.
    """
    reasons: list = []
    verdict = ALLOW

    # --- intent alignment of the intended tool call ------------------------ #
    if intended_tool and not intent_aligned(
        request, intended_tool, destructive_tools=destructive_tools
    ):
        reasons.append(f"intent mismatch: {intended_tool}")
        verdict = _max_verdict(verdict, ESCALATE)

    # --- output review (leaks / PHI / parroted injection) ------------------ #
    out_verdict, out_reasons = review_output(draft_text, phi_check=phi_check)
    reasons.extend(out_reasons)
    verdict = _max_verdict(verdict, out_verdict)

    return verdict, reasons


# --------------------------------------------------------------------------- #
# Internal helpers                                                             #
# --------------------------------------------------------------------------- #


def _max_verdict(a: str, b: str) -> str:
    """Return the more severe of two verdicts per :data:`_SEVERITY`."""
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _is_destructive(tool_name: str, destructive_tools=None) -> bool:
    """Return True when ``tool_name`` is destructive by pattern or by override."""
    if destructive_tools and tool_name in destructive_tools:
        return True
    return bool(_DESTRUCTIVE_NAME_PATTERN.search(tool_name or ""))
