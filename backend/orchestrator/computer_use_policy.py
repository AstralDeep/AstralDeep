"""Single source of truth for computer-use verb tiers, destructiveness classification,
and limits, shared by the agent's mcp_tools.py and the remote_confirmation dispatch
gate so they cannot drift apart.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import PurePosixPath
import posixpath
import re
import shlex
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set
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
DE_ESCALATION_VERBS: FrozenSet[str] = frozenset({"end_session"})

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
    "run_command": 630.0, "read_output": 30.0, "interrupt_command": 5.0,
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


DEFAULT_COMMAND_TIMEOUT_S = 60
MAX_FOREGROUND_TIMEOUT_S = 600
MAX_BACKGROUND_TIMEOUT_S = 3600
MAX_COMMAND_CHARS = 4096
MAX_ARGV_ITEMS = 256
MAX_ARG_CHARS = 4096
MAX_ENV_VARS = 32
MAX_STDIN_BYTES = 16 * 1024

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
_LABEL_TEXT = 60


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
        return _destructive_program(words)

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


def _label_text(value: Any) -> str:
    text = " ".join(_CTRL_TEXT.sub("", str(value or "")).split())
    return text if len(text) <= _LABEL_TEXT else text[:_LABEL_TEXT - 1] + "…"


_CTRL_TEXT = re.compile(r"[\x00-\x1f\x7f]")


def application_name(bundle_id: Any) -> str:
    return _label_text(str(bundle_id or "").rsplit(".", 1)[-1]) or "this application"


def input_grant_label(app_name: str, window_title: Optional[str] = None) -> str:
    title = _label_text(window_title) if window_title else ""
    return f'Allow for this task in window "{title}"' if title else f"Allow for this task in {_label_text(app_name)}"


def window_grant_label(app_name: str, window_title: Optional[str]) -> str:
    if _label_text(window_title):
        return input_grant_label(app_name, window_title)
    return f"Allow for this task in this {_label_text(app_name)} window"


def command_grant_label(argv0: str, folder: str) -> str:
    return f"Allow {_label_text(argv0)} in {_label_text(folder)} for this task"


def _command_summary(args: Mapping[str, Any], host: str) -> str:
    argv = args.get("argv")
    shown = (f"Arguments: {json.dumps(argv, ensure_ascii=False)}" if isinstance(argv, list)
             else f"Command: {args.get('command')}")
    timeout = args.get("timeout_s", DEFAULT_COMMAND_TIMEOUT_S)
    env = args.get("env") if isinstance(args.get("env"), Mapping) else {}
    lines = [f"Run a command on {host}", shown, f"Folder: {args.get('cwd')}", f"Timeout: {timeout} s",
             "Environment: " + (", ".join(f"{key}={value}" for key, value in env.items()) if env else "none"),
             "Background: " + ("yes" if args.get("background") else "no")]
    if isinstance(args.get("stdin"), str):
        lines.append(f"Standard input: {len(args['stdin'])} characters")
    return "\n".join(lines)


def summary_for(tool_name: str, args: Mapping[str, Any], host_label: str) -> str:
    h = host_label or "your computer"
    if tool_name == "run_command":
        return _command_summary(args, h)
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
