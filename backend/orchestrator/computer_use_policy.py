"""Single source of truth for computer-use verb tiers, destructiveness classification,
and limits, shared by the agent's mcp_tools.py and the remote_confirmation dispatch
gate so they cannot drift apart.
"""

from __future__ import annotations

from pathlib import PurePosixPath
import re
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
    "write_file", "delete_path",
})
SESSION_VERBS: FrozenSet[str] = frozenset({
    "list_computers", "start_session", "end_session", "resume_session", "confirm_action",
})

HOST_VERBS: FrozenSet[str] = OBSERVE_VERBS | INPUT_VERBS | CONSEQUENTIAL_VERBS

ALL_VERBS: FrozenSet[str] = HOST_VERBS | SESSION_VERBS

TIERS: Dict[str, str] = {
    **{v: "observe" for v in OBSERVE_VERBS},
    **{v: "input" for v in INPUT_VERBS},
    **{v: "consequential" for v in CONSEQUENTIAL_VERBS},
    **{v: "session" for v in SESSION_VERBS},
}

SCOPES: Dict[str, str] = {
    **{v: "tools:read" for v in OBSERVE_VERBS},
    **{v: "tools:write" for v in INPUT_VERBS},
    "write_file": "tools:files",
    "delete_path": "tools:files",
    "list_computers": "tools:read",
    "start_session": "tools:write",
    "end_session": "tools:write",
    "resume_session": "tools:write",
    "confirm_action": "tools:write",
}

DESTRUCTIVE_CLASSIFICATION: Dict[str, Any] = {
    "write_file": "always",
    "delete_path": "always",
    "confirm_action": "always",
    "open_app": {"by_shell_app": True},
}

SHELL_APPS: FrozenSet[str] = frozenset({
    "powershell", "pwsh", "cmd", "command prompt", "wt", "windows terminal", "windowsterminal",
    "terminal", "conhost", "bash", "sh", "zsh", "wsl", "ubuntu", "git-bash", "git bash", "mintty",
    "cygwin", "cygwin64 terminal", "python", "python3", "ipython", "node", "irb", "psql",
    "iterm", "iterm2", "warp", "ghostty", "kitty", "alacritty", "wezterm", "com.github.wez.wezterm",
    "com.apple.terminal", "com.googlecode.iterm2", "dev.warp.warp-stable",
    "com.mitchellh.ghostty", "net.kovidgoyal.kitty", "org.alacritty",
})
MAC_TERMINAL_BUNDLES = frozenset({"com.apple.Terminal", "com.googlecode.iterm2", "com.mitchellh.ghostty",
    "dev.warp.Warp-Stable", "net.kovidgoyal.kitty", "org.alacritty", "com.github.wez.wezterm"})


def mac_application(app: Any, args: Any) -> str:
    if not isinstance(app, str) or not app or app != app.strip() or len(app) > 1024 or args != []:
        raise ValueError("Use a Mac application name, bundle identifier or absolute .app path without arguments")
    if re.fullmatch(r"[\w .+\-/]+", app) is None:
        raise ValueError("Invalid Mac application target")
    if "/" in app:
        path = PurePosixPath(app)
        if not app.startswith("/") or str(path) != app or ".." in path.parts or not app.endswith(".app"):
            raise ValueError("Use an absolute canonical .app path")
        return app
    aliases = {"terminal": "com.apple.Terminal", "terminal.app": "com.apple.Terminal",
               "iterm": "com.googlecode.iterm2", "iterm.app": "com.googlecode.iterm2",
               "iterm2": "com.googlecode.iterm2", "iterm2.app": "com.googlecode.iterm2"}
    for name, bundle in {"ghostty": "com.mitchellh.ghostty", "warp": "dev.warp.Warp-Stable",
                         "kitty": "net.kovidgoyal.kitty", "alacritty": "org.alacritty",
                         "wezterm": "com.github.wez.wezterm"}.items():
        aliases[name] = aliases[f"{name}.app"] = bundle
    return aliases.get(app.lower(), app)


def mac_terminal_application(app: Any, *, allow_path=False) -> str:
    target = mac_application(app, [])
    if "/" in target and allow_path:
        target = mac_application(PurePosixPath(target).name, [])
    terminal = next((bundle for bundle in MAC_TERMINAL_BUNDLES if bundle.lower() == target.lower()), None)
    if terminal is None:
        raise ValueError("terminal_app must name a recognized Mac terminal application or bundle identifier")
    return terminal


def is_shell_app(app: Any) -> bool:
    text = str(app or "").strip().lower()
    if not text:
        return False
    stem = text.replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".exe", ".lnk", ".bat", ".cmd", ".app"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem in SHELL_APPS or text in SHELL_APPS


def is_destructive(tool_name: str, args: Mapping[str, Any]) -> bool:
    classification = DESTRUCTIVE_CLASSIFICATION.get(tool_name)
    if classification is None or classification == "never":
        return False
    if classification == "always":
        return True
    if isinstance(classification, dict) and classification.get("by_shell_app"):
        return is_shell_app(args.get("app"))
    return True

UNATTENDED_ALLOWED: FrozenSet[str] = frozenset({"list_computers"})

SESSION_REQUIRED: FrozenSet[str] = HOST_VERBS

TIMEOUTS: Dict[str, float] = {
    "screenshot": 15.0, "list_windows": 10.0, "get_clipboard": 5.0, "read_file": 10.0,
    "list_dir": 10.0, "wait": 12.0,
    "click": 10.0, "double_click": 10.0, "right_click": 10.0, "move": 5.0, "drag": 10.0,
    "scroll": 5.0, "type_text": 30.0, "press_keys": 5.0, "focus_window": 5.0,
    "open_app": 15.0, "set_clipboard": 5.0,
    "write_file": 10.0, "delete_path": 10.0,
    "list_computers": 5.0, "start_session": 10.0, "end_session": 5.0,
    "resume_session": 5.0, "confirm_action": 5.0,
}

MAX_TEXT_CHARS = 4000
MAX_CLIPBOARD_CHARS = 16 * 1024
MAX_WRITE_BYTES = 256 * 1024
MAX_READ_BYTES = 262_144
DEFAULT_READ_BYTES = 65_536
MAX_SUMMARY_CHARS = 400
MAX_WAIT_SECONDS = 10.0
RESUME_SETTLE_SECONDS = 1.5
APPROVAL_RETRY_GRACE_S = 180.0
MIN_SCREENSHOT_WIDTH = 320
MAX_SCREENSHOT_WIDTH = 1920
DEFAULT_SCREENSHOT_WIDTH = 1280
MAX_SCROLL_NOTCHES = 20
TERMINAL_GRANT_S = 180

REFUSAL_TEXT = (
    "confirmation_required: the user has been asked to approve this action on their device. "
    "STOP NOW: end your turn with one short sentence telling the user what needs their approval. "
    "Do not call this or any other tool, do not retry, and do not ask again — the task resumes "
    "automatically after they tap Approve."
)

CARD_TITLE = "Confirm an action on {host}"
CARD_CAPTION = ("This runs on your computer exactly as shown and I can't undo it. "
                "Approve to continue, or decline.")


def summary_for(tool_name: str, args: Mapping[str, Any], host_label: str) -> str:
    h = host_label or "your computer"
    if tool_name == "write_file":
        mode = "overwrite" if args.get("if_exists") == "overwrite" else "create"
        return f"Write file on {h} ({mode}): {str(args.get('path') or '')[:200]}"
    if tool_name == "delete_path":
        return f"Delete on {h}: {str(args.get('path') or '')[:200]}"
    if tool_name == "confirm_action":
        return f"On {h}: {str(args.get('summary') or 'the next step')[:MAX_SUMMARY_CHARS]}"
    if tool_name == "open_app":
        return f"Open a terminal on {h}: {str(args.get('app') or '')[:120]}"
    return f"{tool_name} on {h}"


def is_unattended_allowed(tool_name: str) -> bool:
    return tool_name in UNATTENDED_ALLOWED
