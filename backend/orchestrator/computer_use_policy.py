"""Single source of truth for computer-use verb tiers, destructiveness classification,
and limits, shared by the agent's mcp_tools.py and the remote_confirmation dispatch
gate so they cannot drift apart.
"""

from __future__ import annotations

import ipaddress
import math
from pathlib import PurePosixPath
import posixpath
import re
import shlex
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set
import unicodedata
from urllib.parse import urlsplit, urlunsplit

AGENT_ID = "computer-use-1"

COMMAND_VERBS: FrozenSet[str] = frozenset({"run_command", "read_output", "interrupt_command", "project_preview"})
BROWSER_VERBS: FrozenSet[str] = frozenset({
    "browser_snapshot", "browser_get_text", "browser_screenshot", "browser_wait",
    "browser_navigate", "browser_click", "browser_fill", "browser_press", "browser_scroll",
    "browser_dialog", "browser_find", "browser_hover", "browser_zoom", "browser_console",
    "browser_network", "browser_evaluate", "browser_resize", "browser_tabs",
    "browser_request_login", "browser_fill_login",
})
ACCESSIBILITY_VERBS: FrozenSet[str] = frozenset({"list_apps", "ax_tree", "ax_find", "ax_action", "menu", "zoom"})

OBSERVE_VERBS: FrozenSet[str] = frozenset({
    "screenshot", "list_windows", "get_clipboard", "read_file", "list_dir", "wait",
    "browser_snapshot", "browser_get_text", "browser_screenshot", "browser_wait",
    "read_output",
    "list_apps", "ax_tree", "ax_find", "zoom",
    "browser_find", "browser_zoom", "browser_console", "browser_network",
})
INPUT_VERBS: FrozenSet[str] = frozenset({
    "click", "double_click", "right_click", "move", "drag", "scroll", "type_text",
    "press_keys", "focus_window", "open_app", "set_clipboard",
    "browser_navigate", "browser_click", "browser_fill", "browser_press", "browser_scroll",
    "browser_dialog", "interrupt_command",
    "ax_action", "menu", "browser_hover", "browser_resize", "browser_tabs", "browser_batch",
    "browser_request_login",
})
CONSEQUENTIAL_VERBS: FrozenSet[str] = frozenset({
    "write_file", "delete_path", "run_command", "project_preview", "browser_evaluate",
    "browser_fill_login",
})
SESSION_VERBS: FrozenSet[str] = frozenset({
    "list_computers", "start_session", "end_session", "resume_session",
})
DE_ESCALATION_VERBS: FrozenSet[str] = frozenset({"end_session"})

COMPOSITE_VERBS: FrozenSet[str] = frozenset({"project_preview", "browser_batch"})
BROWSER_COMPOSITE_VERBS: FrozenSet[str] = frozenset({"browser_batch"})
HOST_VERBS: FrozenSet[str] = (OBSERVE_VERBS | INPUT_VERBS | CONSEQUENTIAL_VERBS) - COMPOSITE_VERBS

ALL_VERBS: FrozenSet[str] = HOST_VERBS | SESSION_VERBS | COMPOSITE_VERBS

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
    "project_preview": "tools:execute",
    "write_file": "tools:files",
    "delete_path": "tools:files",
    "browser_evaluate": "tools:write",
    "browser_fill_login": "tools:write",
    "list_computers": "tools:read",
    "start_session": "tools:write",
    "end_session": "tools:write",
    "resume_session": "tools:write",
}

DESTRUCTIVE_CLASSIFICATION: Dict[str, Any] = {
    "write_file": "always",
    "delete_path": "always",
    "open_app": {"by_shell_app": True},
    "run_command": "always",
    "project_preview": "always",
    "browser_navigate": "always",
    "browser_evaluate": "always",
    "browser_tabs": "always",
    "browser_fill_login": "always",
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

AX_REF_POINTER_VERBS: FrozenSet[str] = frozenset({"click", "double_click", "right_click", "move", "drag"})
AX_INPUT_VERBS: FrozenSet[str] = frozenset({"ax_action", "menu"})
AX_ACTIONS = ("press", "focus", "raise", "show_menu", "confirm", "cancel", "increment", "decrement", "set_value",
              "pick")
AX_ACTING = frozenset({"press", "confirm", "pick", "set_value"})
AX_REF = re.compile(r"a[0-9]{1,9}")
AX_SNAPSHOT = re.compile(r"x[0-9]{1,9}")
MAX_CONFIRM_ELEMENT_CHARS = 200
MAX_BATCH_STEPS = 20
BATCH_REVIEWED_STEPS: FrozenSet[str] = frozenset({"browser_navigate", "browser_evaluate"})
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_CONSEQUENTIAL_WORDS = re.compile(
    r"(?:^|[^a-z0-9])(?:buy|pay|purchase|order|checkout|delete|remove|send|transfer|confirm\s+payment|quit|"
    r"force\s+quit|erase|empty\s+trash|shut\s+down|restart|log\s+out|sign\s+out|revert|discard)(?=$|[^a-z0-9])")


def consequential_label(text: Any) -> bool:
    folded = _CAMEL.sub(r"\1 \2", _one_line(text, 400)).lower()
    return bool(_CONSEQUENTIAL_WORDS.search(folded))


def confirm_element(args: Mapping[str, Any]) -> str:
    value = args.get("confirm_element")
    return _one_line(value, MAX_CONFIRM_ELEMENT_CHARS) if isinstance(value, str) else ""


def menu_path(value: Any) -> Optional[List[str]]:
    if (not isinstance(value, list) or not 1 <= len(value) <= 6
            or not all(isinstance(item, str) and item.strip() and len(item) <= 100 and "\x00" not in item
                       for item in value)):
        return None
    return list(value)


def is_consequential_input(tool: str, args: Mapping[str, Any]) -> bool:
    if tool not in AX_INPUT_VERBS:
        return False
    if confirm_element(args):
        return True
    if tool == "ax_action":
        value = args.get("value")
        return args.get("action") in AX_ACTING and isinstance(value, str) and consequential_label(value)
    path = menu_path(args.get("path"))
    return args.get("list") is not True and path is not None and consequential_label(path[-1])


def is_terminal_bundle(bundle: Any) -> bool:
    folded = str(bundle or "").casefold()
    return bool(folded) and (is_shell_app(bundle) or any(item.casefold() == folded for item in MAC_TERMINAL_BUNDLES))


def ax_addressed(tool: str, args: Mapping[str, Any]) -> bool:
    if tool in AX_REF_POINTER_VERBS:
        return args.get("ref") is not None
    return tool == "ax_action" or tool == "menu" and args.get("pid") is not None


def bind_ax_target(tool: str, args: Dict[str, Any], target: Optional[Mapping[str, Any]]) -> Optional[CwdRefusal]:
    if not ax_addressed(tool, args):
        return None
    if target is None:
        if tool == "menu":
            return None
        return CwdRefusal("stale_ref", "That accessibility reference belongs to a snapshot that is no longer "
                                       "current. Call ax_find or ax_tree again and use its snapshot_id.")
    bundle = target.get("bundle_id")
    if is_astral_app(bundle):
        return CwdRefusal("out_of_range", "Astral's own windows cannot be operated by computer tools.")
    if tool in AX_INPUT_VERBS and is_terminal_bundle(bundle):
        return CwdRefusal("terminal_target", "Terminal windows are not driven by accessibility actions. "
                                             "Run programs with run_command instead.")
    hwnd = target.get("hwnd")
    if type(hwnd) is not int:
        return None
    if "hwnd" in args and args["hwnd"] != hwnd:
        return CwdRefusal("out_of_range", "hwnd does not match the window of that accessibility snapshot; "
                                          "omit hwnd for reference actions.")
    args["hwnd"] = hwnd
    return None


def batch_steps(value: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_BATCH_STEPS:
        return None
    steps = []
    for step in value:
        if not isinstance(step, Mapping) or not set(step) <= {"tool", "args"} or not isinstance(step.get("tool"), str):
            return None
        args = step.get("args", {})
        if not isinstance(args, Mapping):
            return None
        steps.append({"tool": step["tool"], "args": dict(args)})
    return steps


def batch_needs_review(steps: Any) -> bool:
    checked = batch_steps(steps)
    if checked is None:
        return True
    for step in checked:
        tool, args = step["tool"], step["args"]
        if (tool in BATCH_REVIEWED_STEPS or tool not in BROWSER_VERBS or confirm_element(args)
                or tool == "browser_tabs" and args.get("url") is not None):
            return True
    return False


UNATTENDED_ALLOWED: FrozenSet[str] = frozenset({"list_computers"})

SESSION_REQUIRED: FrozenSet[str] = HOST_VERBS | COMPOSITE_VERBS

TIMEOUTS: Dict[str, float] = {
    "screenshot": 15.0, "list_windows": 10.0, "get_clipboard": 5.0, "read_file": 10.0,
    "list_dir": 10.0, "wait": 12.0,
    "click": 10.0, "double_click": 10.0, "right_click": 10.0, "move": 5.0, "drag": 10.0,
    "scroll": 5.0, "type_text": 30.0, "press_keys": 5.0, "focus_window": 5.0,
    "open_app": 15.0, "set_clipboard": 5.0,
    "write_file": 10.0, "delete_path": 10.0,
    "run_command": 630.0, "read_output": 30.0, "interrupt_command": 5.0,
    "project_preview": 630.0,
    "browser_snapshot": 5.0, "browser_get_text": 5.0, "browser_screenshot": 5.0,
    "browser_wait": 15.0, "browser_navigate": 15.0, "browser_click": 5.0, "browser_fill": 5.0,
    "browser_press": 5.0, "browser_scroll": 5.0, "browser_dialog": 5.0,
    "list_apps": 5.0, "ax_tree": 8.0, "ax_find": 8.0, "ax_action": 10.0, "menu": 10.0, "zoom": 15.0,
    "browser_find": 5.0, "browser_hover": 5.0, "browser_zoom": 5.0, "browser_console": 5.0,
    "browser_network": 5.0, "browser_evaluate": 15.0, "browser_resize": 5.0, "browser_tabs": 15.0,
    "browser_request_login": 630.0, "browser_fill_login": 15.0,
    "browser_batch": 120.0,
    "list_computers": 5.0, "start_session": 10.0, "end_session": 5.0,
    "resume_session": 5.0,
}

MAX_TEXT_CHARS = 4000
MAX_CLIPBOARD_CHARS = 16 * 1024
MAX_WRITE_BYTES = 256 * 1024
MAX_READ_BYTES = 262_144
DEFAULT_READ_BYTES = 65_536
MAX_WAIT_SECONDS = 10.0
RESUME_SETTLE_SECONDS = 1.5
MIN_SCREENSHOT_WIDTH = 320
MAX_SCREENSHOT_WIDTH = 1920
DEFAULT_SCREENSHOT_WIDTH = 1280
MAX_SCROLL_NOTCHES = 20

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


WAIT_ON_OWNER_VERBS: FrozenSet[str] = frozenset({"browser_request_login"})
LOCAL_CONFIRM_VERBS: FrozenSet[str] = frozenset({"browser_fill_login"})
FILL_LOGIN_ORIGIN_CHARS = MAX_BROWSER_URL_CHARS
MAX_FILL_LOGIN_FIELDS = 20
FILL_LOGIN_ALLOWED_ARGS = frozenset({"computer", "origin", "refs", "snapshot_id", "user_id", "session_id"})


def fill_login_origin(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > FILL_LOGIN_ORIGIN_CHARS:
        return None
    canonical = canonical_browser_url(value)
    if canonical is None:
        return None
    parts = urlsplit(canonical)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def fill_login_preflight(args: Any) -> str:
    if not isinstance(args, Mapping):
        return "browser_fill_login takes only origin, refs and snapshot_id and never a value"
    extra = sorted(str(key) for key in args
                   if not str(key).startswith("_") and str(key) not in FILL_LOGIN_ALLOWED_ARGS)
    if extra:
        return ("browser_fill_login takes only origin, refs and snapshot_id; refused arguments: "
                + ", ".join(extra[:8]))
    if fill_login_origin(args.get("origin")) is None:
        return ("origin must be the bare http or https origin of the page in the browser pane, "
                "such as https://example.org")
    refs = args.get("refs")
    if (not isinstance(refs, list) or not 1 <= len(refs) <= MAX_FILL_LOGIN_FIELDS
            or any(not isinstance(ref, str) or not re.fullmatch(r"e[0-9]{1,9}", ref) for ref in refs)
            or len(set(refs)) != len(refs)):
        return f"refs must be 1-{MAX_FILL_LOGIN_FIELDS} unique field references from browser_snapshot, such as e12"
    if not isinstance(args.get("snapshot_id"), str) or not re.fullmatch(r"s[0-9]{1,9}", args["snapshot_id"]):
        return "snapshot_id must be the snapshot_id of the browser_snapshot the references came from"
    return ""


def _one_line(value: Any, limit: int) -> str:
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else "" if unicodedata.category(ch) == "Cf"
                   else ch for ch in str(value or ""))
    return " ".join(text.split())[:limit]


DEFAULT_COMMAND_TIMEOUT_S = 60
MAX_FOREGROUND_TIMEOUT_S = 600
MAX_BACKGROUND_TIMEOUT_S = 3600
MAX_COMMAND_CHARS = 4096
MAX_ARGV_ITEMS = 256
MAX_ARG_CHARS = 4096
MAX_ENV_VARS = 32
MAX_STDIN_BYTES = 16 * 1024
MAX_ALLOWED_DOMAINS = 64
MAX_DOMAIN_CHARS = 253
SANDBOX_MODES = ("default", "escalated")


def validate_allowed_domains(value: Any) -> List[str]:
    if not isinstance(value, list) or len(value) > MAX_ALLOWED_DOMAINS:
        raise ValueError(f"allowed_domains must be a list of at most {MAX_ALLOWED_DOMAINS} hostnames")
    out: List[str] = []
    seen = set()
    for item in value:
        if (not isinstance(item, str) or not item.strip() or "\\x00" in item
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in item)):
            raise ValueError("allowed_domains entries must be printable hostnames like example.com or *.example.com")
        lowered = item.strip().lower()
        if len(lowered) > MAX_DOMAIN_CHARS:
            raise ValueError(f"each allowed domain is at most {MAX_DOMAIN_CHARS} characters")
        body = lowered[2:] if lowered.startswith("*.") else lowered
        if (not body or body.startswith(".") or body.endswith(".") or ".." in body
                or any(label == "" for label in body.split("."))):
            raise ValueError(f"{item[:80]} is not a valid hostname or *.wildcard")
        labels = body.split(".")
        if len(labels) < 2 or any(len(label) > 63 for label in labels):
            raise ValueError(f"{item[:80]} is not a valid hostname or *.wildcard")
        for label in labels:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label):
                raise ValueError(f"{item[:80]} is not a valid hostname or *.wildcard")
        try:
            ipaddress.ip_address(body)
        except ValueError:
            pass
        else:
            raise ValueError("allowed_domains entries cannot be IP addresses")
        if "localhost" in labels:
            raise ValueError("allowed_domains entries cannot name localhost")
        if lowered not in seen:
            seen.add(lowered)
            out.append(lowered)
    return out


def command_domains(args: Mapping[str, Any]) -> tuple:
    value = args.get("allowed_domains")
    return tuple(value) if isinstance(value, list) else ()


def sandbox_mode(args: Mapping[str, Any]) -> str:
    value = args.get("sandbox")
    return value if value in SANDBOX_MODES else "default"


def sandbox_exact_review_required(args: Mapping[str, Any], approved: Any = None) -> bool:
    if sandbox_mode(args) == "escalated":
        return True
    domains = command_domains(args)
    if not domains:
        return False
    return True if approved is None else not set(domains) <= set(approved)


EGRESS_DENIED_CIDRS: tuple = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "255.255.255.255/32",
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b:1::/48", "100::/64",
    "2001:db8::/32", "fc00::/7", "fe80::/10", "ff00::/8",
)


def proxy_policy(domains: Sequence[str]) -> Dict[str, Any]:
    return {"allowed_domains": list(domains), "denied_cidrs": list(EGRESS_DENIED_CIDRS)}


COMMAND_DEADLINE_MARGIN_S = 10
BACKGROUND_START_S = 30.0
HOST_DEADLINE_GRACE_S = 15.0
DEFAULT_HOST_DEADLINE_S = 30.0


def host_deadline(verb: str, args: Mapping[str, Any]) -> float:
    if verb != "run_command":
        return float(TIMEOUTS.get(verb, DEFAULT_HOST_DEADLINE_S))
    if args.get("background") is True:
        return BACKGROUND_START_S
    timeout = args.get("timeout_s", DEFAULT_COMMAND_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        timeout = DEFAULT_COMMAND_TIMEOUT_S
    return float(min(timeout, MAX_FOREGROUND_TIMEOUT_S) + COMMAND_DEADLINE_MARGIN_S)


def dispatch_timeout(verb: str, args: Mapping[str, Any]) -> float:
    return host_deadline(verb, args) + HOST_DEADLINE_GRACE_S

_MAX_NESTING = 4
_SEPARATORS = frozenset({"|", "||", "&&", ";", "&", "(", ")", "|&", ";;", ";&", ";;&"})
_WRITE_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>", ">&"})
_READ_REDIRECTS = frozenset({"<", "<<", "<<<", "<&"})
_HARMLESS_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_SUBSTITUTIONS = (re.compile(r"\$\(([^()]*)\)"), re.compile(r"`([^`]*)`"))
_ALWAYS_DESTRUCTIVE = frozenset({
    "sudo", "su", "doas", "rmdir", "diskutil", "dd", "launchctl", "security", "osascript", "killall",
    "shutdown", "reboot", "halt",
})
_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "time", "nice", "xargs", "timeout", "caffeinate",
                       "stdbuf", "builtin"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"})
_INTERPRETERS = frozenset({"perl", "ruby", "node", "osascript", "php"})
_DOWNLOADERS = frozenset({"curl", "wget"})


def _program(word: str) -> str:
    return posixpath.basename(word).lower()


def _short_flags(words: Sequence[str]) -> str:
    return "".join(w[1:] for w in words if w.startswith("-") and not w.startswith("--"))


def _in_venv(word: str) -> bool:
    parts = PurePosixPath(word).parts
    return len(parts) >= 3 and parts[-2] == "bin" and parts[-3] in {".venv", "venv", "env", ".env"}


def _git_subcommand(words: Sequence[str]) -> tuple:
    index = 1
    while index < len(words) and words[index].startswith("-"):
        index += 2 if words[index] in {"-C", "-c"} else 1
    return (words[index], list(words[index + 1:])) if index < len(words) else ("", [])


def _first_operand(words: Sequence[str]) -> str:
    return next((w for w in words[1:] if not w.startswith("-")), "")


def _destructive_program(words: Sequence[str]) -> bool:
    program = _program(words[0])
    rest = list(words[1:])
    flags = _short_flags(rest)
    if program in _ALWAYS_DESTRUCTIVE or program.startswith("mkfs"):
        return True
    if program == "rm":
        targets = [w for w in rest if not w.startswith("-")]
        return ("r" in flags or "R" in flags or "--recursive" in rest
                or any(t in {"/", "/*", "~", "~/"} for t in targets))
    if program in {"chmod", "chown", "chgrp"}:
        return "R" in flags or "--recursive" in rest
    if program == "defaults":
        return _first_operand(words) in {"write", "delete", "import", "rename"}
    if program == "git":
        sub, tail = _git_subcommand(words)
        tail_flags = _short_flags(tail)
        if sub == "push":
            return ("f" in tail_flags or any(w.startswith("--force") for w in tail)
                    or any(w.startswith("+") for w in tail))
        if sub == "reset":
            return "--hard" in tail
        if sub == "clean":
            return "f" in tail_flags or "--force" in tail
        return False
    if program in {"npm", "yarn", "pnpm"}:
        return _first_operand(words) == "publish"
    if re.fullmatch(r"pip[0-9.]*", program):
        return _first_operand(words) == "install" and not _in_venv(words[0])
    if program.startswith("python"):
        return ("-m" in rest and rest[rest.index("-m") + 1:rest.index("-m") + 3] == ["pip", "install"]
                and not _in_venv(words[0]))
    if program == "brew":
        return _first_operand(words) in {"install", "reinstall", "upgrade", "uninstall", "remove", "rm"}
    return False


class _CommandScan:
    check = staticmethod(_destructive_program)

    def __init__(self, cwd: Optional[str], roots: Sequence[str]):
        self.cwd = cwd
        self.roots = list(roots) or ([cwd] if cwd else [])
        self.programs: Set[str] = set()

    def _inside(self, target: str) -> bool:
        if target in _HARMLESS_TARGETS:
            return True
        if any(mark in target for mark in ("$", "`", "*", "?")) or target.startswith("~"):
            return False
        if not target.startswith("/"):
            if not self.cwd:
                return False
            target = posixpath.join(self.cwd, target)
        resolved = posixpath.normpath(target)
        return any(resolved == root or resolved.startswith(root.rstrip("/") + "/") for root in self.roots)

    def invocation(self, words: Sequence[str], depth: int) -> bool:
        if depth > _MAX_NESTING:
            return True
        words = list(words)
        while words and _ASSIGNMENT.fullmatch(words[0]):
            words.pop(0)
        if not words:
            return False
        program = _program(words[0])
        self.programs.add(program)
        if program in _WRAPPERS:
            return any(self.invocation(words[index:], depth + 1) for index in range(1, len(words)))
        if program in _SHELLS:
            for index, word in enumerate(words[1:], start=1):
                if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
                    script = next((w for w in words[index + 1:] if not w.startswith("-")), None)
                    return script is not None and self.text(script, depth + 1)
            return False
        return self.check(words)

    def text(self, command: str, depth: int) -> bool:
        if depth > _MAX_NESTING:
            return True
        for pattern in _SUBSTITUTIONS:
            for inner in pattern.findall(command):
                if self.text(inner, depth + 1):
                    return True
        try:
            lexer = shlex.shlex(command.replace("\r", "\n").replace("\n", " ; "), posix=True,
                                punctuation_chars=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            return True
        segment: List[str] = []
        for index, token in enumerate(tokens):
            if token in _SEPARATORS:
                if segment and self.invocation(segment, depth):
                    return True
                segment = []
                continue
            if token in _WRITE_REDIRECTS or token in _READ_REDIRECTS:
                continue
            previous = tokens[index - 1] if index else ""
            if previous in _WRITE_REDIRECTS:
                if not (previous == ">&" and (token.isdigit() or token == "-")) and not self._inside(token):
                    return True
                continue
            if previous in _READ_REDIRECTS:
                continue
            segment.append(token)
        return bool(segment) and self.invocation(segment, depth)


def _command_shape(args: Mapping[str, Any]):
    argv, command = args.get("argv"), args.get("command")
    if (argv is None) == (command is None):
        return None
    if argv is not None:
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            return None
        return "argv", argv
    if not isinstance(command, str) or not command.strip():
        return None
    return "command", command


def _absolute_cwd(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        return None
    return posixpath.normpath(value)


def _root_paths(roots: Any) -> List[Dict[str, str]]:
    out = []
    for root in roots or ():
        if isinstance(root, Mapping):
            path = _absolute_cwd(root.get("path"))
            if path is not None and path != "/":
                out.append({"name": str(root.get("name") or posixpath.basename(path)), "path": path})
    return out


class CwdRefusal(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_HOME = re.compile(r"(/(?:Users|home)/[^/]+)(?:/|$)")
_CWD_FORMS = "Pass an absolute folder inside one, <folder name>/sub/path, or ~/sub/path."


def _folders_hint(folders: Sequence[Mapping[str, str]]) -> str:
    listed = "; ".join(f"{root['name']} = {root['path']}" for root in folders)
    return f"Shared folders: {listed[:1800]}. {_CWD_FORMS}"


def resolve_cwd(value: Any, roots: Any, host_name: str) -> str:
    folders = _root_paths(roots)
    if not folders:
        raise CwdRefusal("cwd_outside_workspace", f"{host_name} shares no workspace folder; ask the user to "
                                                  "select one in Computer controls")
    hint = _folders_hint(folders)
    if (not isinstance(value, str) or not value.strip() or len(value) > 1024 or "\x00" in value
            or ".." in value.split("/")):
        raise CwdRefusal("out_of_range", f"cwd must name a shared folder. {hint}")
    if value.startswith("/"):
        path = value
    elif value == "~" or value.startswith("~/"):
        homes = {match.group(1) for root in folders if (match := _HOME.match(root["path"]))}
        if len(homes) != 1:
            raise CwdRefusal("out_of_range", f"~ cannot be resolved from the shared folders. {hint}")
        path = homes.pop() + value[1:]
    else:
        named = [root for root in folders if value == root["name"] or value.startswith(root["name"] + "/")]
        longest = max((len(root["name"]) for root in named), default=0)
        named = [root for root in named if len(root["name"]) == longest]
        if len(named) != 1:
            raise CwdRefusal("out_of_range", f"cwd must start with /, ~/ or the name of one shared folder. {hint}")
        path = named[0]["path"] + value[longest:]
    folder = "/" + posixpath.normpath(path).lstrip("/")
    if not any(folder == root["path"] or folder.startswith(root["path"] + "/") for root in folders):
        raise CwdRefusal("cwd_outside_workspace", f"cwd {folder[:200]} is outside every folder the user shared. {hint}")
    return folder


def resolve_workspace_path(value: Any, roots: Any, host_name: str, *, primary: str | None = None) -> str:
    folders = _root_paths(roots)
    if not folders:
        raise CwdRefusal("path_outside_workspace", f"{host_name} shares no workspace folder; ask the user to "
                                                  "select a project folder in Computer controls")
    hint = _folders_hint(folders)
    if not isinstance(value, str) or not value.strip() or len(value) > 1024 or "\x00" in value or "\\" in value:
        raise CwdRefusal("out_of_range", f"path must name a file or directory in a shared folder. {hint}")
    if value.startswith("/"):
        path = value
    elif primary is not None and any(root["path"] == primary for root in folders):
        path = posixpath.join(primary, value)
    elif len(folders) == 1:
        path = posixpath.join(folders[0]["path"], value)
    else:
        raise CwdRefusal("path_outside_workspace", f"Use an absolute path when multiple folders are shared. {hint}")
    path = "/" + posixpath.normpath(path).lstrip("/")
    if not any(path == root["path"] or path.startswith(root["path"] + "/") for root in folders):
        raise CwdRefusal("path_outside_workspace", f"The path is outside every folder shared by {host_name}. {hint}")
    return path


_ESCALATING = frozenset({"sudo", "sudoedit", "su", "doas", "pkexec", "visudo", "dseditgroup", "sysadminctl"})
_TRUST_EDITS = frozenset({"authorizationdb", "add-trusted-cert", "remove-trusted-cert", "trust-settings-import"})
_SETID_MODE = re.compile(r"[ugoa]*[+=][rwxXt]*s[rwxXst]*|0?[2467][0-7]{3}")


def _escalating_program(words: Sequence[str]) -> bool:
    program, rest = _program(words[0]), list(words[1:])
    if program in _ESCALATING:
        return True
    if program == "launchctl":
        return any(word.startswith("system") or "/Library/LaunchDaemons" in word for word in rest)
    if program == "osascript":
        return any("administrator privileges" in word.lower() for word in rest)
    if program == "security":
        return _first_operand(words) in _TRUST_EDITS
    if program == "dscl":
        return any("admin" in word.lower() for word in rest)
    if program == "chmod":
        return any(_SETID_MODE.fullmatch(word) for word in rest if not word.startswith("-"))
    return False


class _EscalationScan(_CommandScan):
    check = staticmethod(_escalating_program)

    def _inside(self, target: str) -> bool:
        return True


def is_privilege_escalation(args: Mapping[str, Any]) -> bool:
    shape = _command_shape(args)
    if shape is None:
        return True
    scan = _EscalationScan(None, [])
    kind, value = shape
    return scan.invocation(value, 0) if kind == "argv" else scan.text(value, 0)


ASTRAL_BUNDLE_PREFIX = "com.personalailabs.astraldeep"
_ASTRAL_NAMES = frozenset({"astral", "astraldeep", "astral deep"})
_ASTRAL_TEXT = re.compile(r"com\.personalailabs\.astraldeep|astral ?(?:deep)?\.app", re.IGNORECASE)
SECURE_INPUT_BUNDLES: FrozenSet[str] = frozenset({
    "com.apple.securityagent", "com.apple.loginwindow", "com.apple.keychainaccess", "com.apple.passwords",
    "com.apple.systempreferences", "com.apple.systemsettings", "com.bitwarden.desktop", "org.keepassxc.keepassxc",
    "com.lastpass.lastpass", "in.sinew.enpass-desktop", "com.dashlane.dashlanephonefinal", "me.proton.pass.electron",
})
_SECURE_INPUT_PREFIXES = ("com.1password.", "com.agilebits.onepassword")
KEYBOARD_VERBS: FrozenSet[str] = frozenset({"type_text", "press_keys"})
WINDOW_INPUT_VERBS: FrozenSet[str] = frozenset({
    "click", "double_click", "right_click", "move", "drag", "scroll", "type_text", "press_keys", "focus_window",
})
BYPASS_VERBS: FrozenSet[str] = (OBSERVE_VERBS | INPUT_VERBS | CONSEQUENTIAL_VERBS
                                | frozenset({"list_computers", "start_session", "end_session", "resume_session"}))


def is_astral_app(value: Any) -> bool:
    text = str(value or "").strip().lower()
    stem = text.replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".exe", ".app"):
        stem = stem.removesuffix(suffix)
    return bool(text) and (text.startswith(ASTRAL_BUNDLE_PREFIX) or stem in _ASTRAL_NAMES)


def is_secure_input_app(bundle: Any) -> bool:
    folded = str(bundle or "").casefold()
    return folded in SECURE_INPUT_BUNDLES or folded.startswith(_SECURE_INPUT_PREFIXES)


def bypass_floor(tool: str, args: Mapping[str, Any], window_bundle: Optional[str],
                 approved: Any = None) -> bool:
    if "terminal_app" in args and tool in WINDOW_INPUT_VERBS:
        try:
            terminal = mac_terminal_application(args["terminal_app"])
        except ValueError:
            return True
        if tool not in KEYBOARD_VERBS or not window_bundle or terminal.casefold() != window_bundle.casefold():
            return True
    if tool == "run_command":
        argv = args.get("argv")
        text = " ".join(str(word) for word in argv) if isinstance(argv, list) else str(args.get("command") or "")
        return (is_privilege_escalation(args) or bool(_ASTRAL_TEXT.search(text))
                or sandbox_exact_review_required(args, approved))
    if tool in {"write_file", "delete_path"}:
        return bool(_ASTRAL_TEXT.search(str(args.get("path") or "")))
    if tool == "open_app":
        return is_astral_app(args.get("app"))
    if tool in AX_INPUT_VERBS:
        if is_astral_app(window_bundle) or is_terminal_bundle(window_bundle):
            return True
        return (tool == "ax_action" and args.get("action") == "set_value"
                and (not window_bundle or is_secure_input_app(window_bundle)))
    if tool in WINDOW_INPUT_VERBS:
        if not window_bundle or is_astral_app(window_bundle):
            return True
        return tool in KEYBOARD_VERBS and (is_secure_input_app(window_bundle)
                                          or is_shell_app(window_bundle) and "terminal_app" not in args)
    return False


def is_destructive_command(args: Mapping[str, Any], roots: Any = ()) -> bool:
    shape = _command_shape(args)
    if shape is None:
        return True
    scan = _CommandScan(_absolute_cwd(args.get("cwd")), [root["path"] for root in _root_paths(roots)])
    kind, value = shape
    destructive = scan.invocation(value, 0) if kind == "argv" else scan.text(value, 0)
    downloads_into_shell = scan.programs & _DOWNLOADERS and (
        scan.programs & (_SHELLS | _INTERPRETERS) or any(p.startswith("python") for p in scan.programs))
    return bool(destructive or downloads_into_shell)


_GIT_READ_ONLY = frozenset({"status", "log", "diff", "show", "blame", "describe", "rev-parse", "ls-files",
                            "shortlog"})
_GIT_LISTING = {"branch": frozenset({"--list", "-a", "-r", "-v"}), "tag": frozenset({"-l", "--list"}),
                "remote": frozenset({"-v"})}
_GIT_UNSAFE_OPTIONS = ("--output", "--ext-diff", "--exec", "--upload-pack", "--receive-pack", "--textconv")
_FIND_UNSAFE = frozenset({"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"})
_EXEC_OPTIONS = {
    "tar": ("--to-command", "--use-compress-program", "-I", "--checkpoint-action", "--info-script", "-F",
            "--new-volume-script"),
    "rsync": ("-e", "--rsh", "--rsync-path"),
}


def _grantable_git(words: Sequence[str]) -> bool:
    if len(words) < 2 or words[1].startswith("-"):
        return False
    sub, tail = words[1], list(words[2:])
    if any(word.startswith(_GIT_UNSAFE_OPTIONS) for word in tail):
        return False
    if sub in _GIT_READ_ONLY:
        return True
    if sub in _GIT_LISTING:
        return all(word in _GIT_LISTING[sub] for word in tail)
    return sub == "stash" and tail == ["list"]


def _grantable_invocation(words: Sequence[str]) -> bool:
    program = _program(words[0])
    if program == "git":
        return _grantable_git(words)
    if program == "find":
        return not _FIND_UNSAFE & set(words)
    if program in _EXEC_OPTIONS:
        return not any(word == option or word.startswith(option + "=") or (
            len(option) == 2 and word.startswith("-") and not word.startswith("--") and option[1] in word[1:])
            for word in words[1:] for option in _EXEC_OPTIONS[program])
    return True


def command_grant(args: Mapping[str, Any], roots: Any = ()) -> Optional[Dict[str, str]]:
    from orchestrator.computer_grants import is_grantable_program

    shape = _command_shape(args)
    cwd = _absolute_cwd(args.get("cwd"))
    if shape is None or cwd is None or args.get("env") or shape[0] != "argv":
        return None
    words = shape[1]
    candidates = [root for root in _root_paths(roots) if cwd == root["path"] or cwd.startswith(root["path"] + "/")]
    if not candidates:
        return None
    root = max(candidates, key=lambda item: len(item["path"]))
    if (not words or "=" in words[0] or not is_grantable_program(words[0])
            or not _grantable_invocation(words) or is_destructive_command(args, roots)):
        return None
    return {"argv0": words[0], "root": root["path"], "folder": root["name"]}


def is_unattended_allowed(tool_name: str) -> bool:
    return tool_name in UNATTENDED_ALLOWED
