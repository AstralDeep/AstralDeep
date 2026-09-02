"""Feature 076 — the single source of truth for computer-use verb policy.

Everything that decides *how* a `computer-use-1` verb is gated lives here so the
agent registry, the dispatch gate (``remote_confirmation``), the host, and the
contract test cannot drift from one another (spec contracts/verbs.md):

- ``TIERS``: which verbs merely observe, which inject input, which are
  consequential (always confirmed on the phone), and which manage the session.
- ``DESTRUCTIVE_CLASSIFICATION``: the 063-shaped classification consumed by the
  shared confirmation gate — every consequential verb is ``"always"``.
- ``UNATTENDED_ALLOWED``: the only verbs a machine-class turn may run.
- ``SESSION_REQUIRED``: verbs that need an *active* (not paused) session.
- Card copy for the approval proposal.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, Mapping

AGENT_ID = "computer-use-1"

OBSERVE_VERBS: FrozenSet[str] = frozenset({
    "screenshot", "list_windows", "get_clipboard", "read_file", "list_dir", "wait",
})
INPUT_VERBS: FrozenSet[str] = frozenset({
    "click", "double_click", "right_click", "move", "drag", "scroll", "type_text",
    "press_keys", "focus_window", "open_app", "set_clipboard",
})
CONSEQUENTIAL_VERBS: FrozenSet[str] = frozenset({
    "run_command", "write_file", "delete_path",
})
SESSION_VERBS: FrozenSet[str] = frozenset({
    "list_computers", "start_session", "end_session", "resume_session", "confirm_action",
})

#: Verbs the host executes (everything the orchestrator relays). Session verbs
#: are orchestrator-side only and never reach the host as a request.
HOST_VERBS: FrozenSet[str] = OBSERVE_VERBS | INPUT_VERBS | CONSEQUENTIAL_VERBS

ALL_VERBS: FrozenSet[str] = HOST_VERBS | SESSION_VERBS

TIERS: Dict[str, str] = {
    **{v: "observe" for v in OBSERVE_VERBS},
    **{v: "input" for v in INPUT_VERBS},
    **{v: "consequential" for v in CONSEQUENTIAL_VERBS},
    **{v: "session" for v in SESSION_VERBS},
}

#: Scope declared per verb (tool_permissions vocabulary).
SCOPES: Dict[str, str] = {
    **{v: "tools:read" for v in OBSERVE_VERBS},
    **{v: "tools:write" for v in INPUT_VERBS},
    "run_command": "tools:execute",
    "write_file": "tools:files",
    "delete_path": "tools:files",
    "list_computers": "tools:read",
    "start_session": "tools:write",
    "end_session": "tools:write",
    "resume_session": "tools:write",
    "confirm_action": "tools:write",
}

#: 063-shaped classification consumed by ``remote_confirmation`` (FR-011).
#: ``confirm_action`` is "always" on purpose: the model's own request for a
#: consequential UI step rides the same durable, single-use proposal card.
DESTRUCTIVE_CLASSIFICATION: Dict[str, Any] = {
    "run_command": "always",
    "write_file": "always",
    "delete_path": "always",
    "confirm_action": "always",
}

#: The only verbs a machine-class (scheduled / background / MCP) turn may run.
UNATTENDED_ALLOWED: FrozenSet[str] = frozenset({"list_computers"})

#: Verbs that require an ACTIVE session (paused ⇒ typed `paused`).
SESSION_REQUIRED: FrozenSet[str] = HOST_VERBS

#: Per-verb round-trip deadline (seconds) the orchestrator waits for the host.
TIMEOUTS: Dict[str, float] = {
    "screenshot": 15.0, "list_windows": 10.0, "get_clipboard": 5.0, "read_file": 10.0,
    "list_dir": 10.0, "wait": 12.0,
    "click": 10.0, "double_click": 10.0, "right_click": 10.0, "move": 5.0, "drag": 10.0,
    "scroll": 5.0, "type_text": 30.0, "press_keys": 5.0, "focus_window": 5.0,
    "open_app": 15.0, "set_clipboard": 5.0,
    "run_command": 310.0, "write_file": 10.0, "delete_path": 10.0,
    "list_computers": 5.0, "start_session": 10.0, "end_session": 5.0,
    "resume_session": 5.0, "confirm_action": 5.0,
}

#: Bounds enforced orchestrator-side before a request is built (defence in
#: depth — the host re-validates).
MAX_TEXT_CHARS = 4000
MAX_COMMAND_CHARS = 2000
MAX_CLIPBOARD_CHARS = 16 * 1024
MAX_WRITE_BYTES = 256 * 1024
MAX_READ_BYTES = 262_144
DEFAULT_READ_BYTES = 65_536
MAX_SUMMARY_CHARS = 400
MAX_WAIT_SECONDS = 10.0
MIN_SCREENSHOT_WIDTH = 320
MAX_SCREENSHOT_WIDTH = 1920
DEFAULT_SCREENSHOT_WIDTH = 1280
MAX_SCROLL_NOTCHES = 20
MAX_COMMAND_TIMEOUT_S = 300

CARD_TITLE = "Confirm an action on {host}"
CARD_CAPTION = ("This runs on your computer exactly as shown and I can't undo it. "
                "Approve to continue, or decline.")


def summary_for(tool_name: str, args: Mapping[str, Any], host_label: str) -> str:
    """Plain-language description shown on the approval card (never raw args
    beyond what the user must see to decide)."""
    h = host_label or "your computer"
    if tool_name == "run_command":
        cmd = str(args.get("command") or "")[:200]
        return f"Run on {h}: {cmd}"
    if tool_name == "write_file":
        mode = "overwrite" if args.get("if_exists") == "overwrite" else "create"
        return f"Write file on {h} ({mode}): {str(args.get('path') or '')[:200]}"
    if tool_name == "delete_path":
        return f"Delete on {h}: {str(args.get('path') or '')[:200]}"
    if tool_name == "confirm_action":
        return f"On {h}: {str(args.get('summary') or 'the next step')[:MAX_SUMMARY_CHARS]}"
    return f"{tool_name} on {h}"


def is_unattended_allowed(tool_name: str) -> bool:
    return tool_name in UNATTENDED_ALLOWED
