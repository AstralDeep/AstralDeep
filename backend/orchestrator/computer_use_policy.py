"""Single source of truth for computer-use verb tiers, destructiveness classification,
and limits, shared by the agent's mcp_tools.py and the remote_confirmation dispatch
gate so they cannot drift apart.
"""

from __future__ import annotations

import ipaddress
from pathlib import PurePosixPath
import re
from typing import Any, Dict, FrozenSet, Mapping, Optional
import unicodedata
from urllib.parse import urlsplit, urlunsplit

AGENT_ID = "computer-use-1"

COMMAND_VERBS: FrozenSet[str] = frozenset({"run_command", "read_output", "interrupt_command"})
BROWSER_VERBS: FrozenSet[str] = frozenset({
    "browser_snapshot", "browser_get_text", "browser_screenshot", "browser_wait",
    "browser_navigate", "browser_click", "browser_fill", "browser_press", "browser_scroll",
    "browser_dialog",
})

OBSERVE_VERBS: FrozenSet[str] = frozenset({
    "screenshot", "list_windows", "get_clipboard", "read_file", "list_dir", "wait",
    "browser_snapshot", "browser_get_text", "browser_screenshot", "browser_wait",
    "read_output",
})
INPUT_VERBS: FrozenSet[str] = frozenset({
    "click", "double_click", "right_click", "move", "drag", "scroll", "type_text",
    "press_keys", "focus_window", "open_app", "set_clipboard",
    "browser_navigate", "browser_click", "browser_fill", "browser_press", "browser_scroll",
    "browser_dialog", "interrupt_command",
})
CONSEQUENTIAL_VERBS: FrozenSet[str] = frozenset({
    "write_file", "delete_path", "run_command",
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
    "run_command": "tools:execute",
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
    "run_command": "always",
    "browser_navigate": "always",
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
    "run_command": 600.0, "read_output": 30.0, "interrupt_command": 5.0,
    "browser_snapshot": 5.0, "browser_get_text": 5.0, "browser_screenshot": 5.0,
    "browser_wait": 15.0, "browser_navigate": 15.0, "browser_click": 5.0, "browser_fill": 5.0,
    "browser_press": 5.0, "browser_scroll": 5.0, "browser_dialog": 5.0,
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


MAX_BROWSER_URL_CHARS = 2048
_BROWSER_HOST = re.compile(r"[a-z0-9_][a-z0-9._-]*")


def canonical_browser_url(raw: Any) -> Optional[str]:
    if (not isinstance(raw, str) or not raw or len(raw) > MAX_BROWSER_URL_CHARS or not raw.isascii()
            or any(ord(ch) < 33 or ord(ch) == 127 or ch == "\\" for ch in raw)):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    scheme, host = parts.scheme.lower(), parts.hostname
    if scheme not in ("http", "https") or not host or "@" in parts.netloc or port == 0:
        return None
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return None
        if "%" in host:
            return None
        netloc = f"[{host}]"
    elif _BROWSER_HOST.fullmatch(host):
        netloc = host
    else:
        return None
    if port is not None and port != (80 if scheme == "http" else 443):
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


def _origin_label(url: Any) -> str:
    canonical = canonical_browser_url(url)
    if canonical is None:
        return ""
    parts = urlsplit(canonical)
    return f"{parts.scheme}://{parts.netloc}"


def _one_line(value: Any, limit: int) -> str:
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else "" if unicodedata.category(ch) == "Cf"
                   else ch for ch in str(value or ""))
    return " ".join(text.split())[:limit]


def summary_for(tool_name: str, args: Mapping[str, Any], host_label: str) -> str:
    h = host_label or "your computer"
    if tool_name == "browser_navigate":
        if args.get("url") is None:
            action = args.get("action")
            move = {"back": "Go back", "forward": "Go forward", "reload": "Reload the page"}.get(
                action if isinstance(action, str) else "", "Navigate")
            return f"{move} in the Astral browser pane on {h}"
        canonical = canonical_browser_url(args["url"])
        if canonical is None:
            return (f"Navigate the Astral browser pane on {h} to an address that will be refused; "
                    "nothing will load.")
        parts = urlsplit(canonical)
        rest = _one_line(parts.path, 120) + ("?…" if parts.query else "")
        return (f"Open {_origin_label(canonical)}{rest} in the Astral browser pane on {h}. Approving this site "
                "lets the pane load its pages; passwords and payment fields are never filled.")
    if (tool_name in ("browser_click", "browser_fill") and isinstance(args.get("confirm_element"), str)
            and _one_line(args["confirm_element"], 1)):
        element = _one_line(args["confirm_element"], 200)
        verb = "Click" if tool_name == "browser_click" else "Fill in"
        return (f"{verb} {element} in the Astral browser pane on {h}. This looks like a purchase, payment, "
                "deletion or message, and I can't undo it.")
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
