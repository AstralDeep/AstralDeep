#!/usr/bin/env python3
"""Terminal agent tools require current dispatch-owned authority for retained-session access.
Execution and input require an explicitly approved isolated SSH runtime and current human approval.
"""
from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Optional, Tuple

from astralprims import Alert, Card, Table, Text

from orchestrator.terminal_security import TerminalAuthority, require_terminal_enabled, require_terminal_execution
from orchestrator.terminal_sessions import (
    READ_WAIT_MAX_S,
    TerminalError,
)

_ORCH = None

_MAX_FIELD = 256
_MAX_SESSION_ID = 64
_MAX_TEXT_CHARS = 16_000
_DEFAULT_WAIT_S = 1.0

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\x1b]")

_SESSION_PROP = {
    "session_id": {
        "type": "string",
        "description": "Id of one of your open terminal sessions (from list_terminals).",
    }
}


class TerminalUnavailable(Exception):
    pass


def register_deps(orchestrator) -> None:
    global _ORCH
    _ORCH = orchestrator


def _manager():
    manager = getattr(_ORCH, "terminal_sessions", None)
    if manager is None:
        raise TerminalUnavailable()
    return manager


def _sanitize(value: Any, limit: int = _MAX_FIELD) -> str:
    if value is None:
        return ""
    text = _CTRL.sub("", str(value))
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _data(data: Dict[str, Any], components: Optional[List[Any]] = None) -> Dict[str, Any]:
    return {"_ui_components": [c.to_dict() for c in (components or [])], "_data": data}


def _fail(code: str, message: str, *, next_action: str = "", status_code: int = 422) -> Dict[str, Any]:
    text = f"{code}: {_sanitize(message, 400)}" + (f" — {next_action}" if next_action else "")
    return {"_ui_components": [Alert(message=text, variant="error").to_dict()],
            "_data": {"code": code, "message": message, "next_action": next_action, "status_code": status_code}}


def _card(title: str, components: List[Any], data: Dict[str, Any]) -> Dict[str, Any]:
    return _data(data, [Card(title=title, content=components)])


_ERROR_HINTS = {
    "terminal_disabled": ("Terminal operations are disabled on this server.", ""),
    "terminal_isolation_unavailable": (
        "No qualified isolated terminal executor is available; no command or input was executed.", ""),
    "terminal_authority_required": ("Use the normal authenticated terminal tool dispatch.", ""),
    "terminal_permission_denied": ("Current terminal permission is unavailable.", ""),
    "terminal_security_blocked": ("The terminal tool is blocked by system security policy.", ""),
    "terminal_authority_expired": ("Current owner authority has expired.", ""),
    "terminal_session_not_found": (
        "That session id is not one of your open terminals.",
        "Call list_terminals and use a live session_id.",
    ),
    "terminal_session_closed": (
        "That terminal has exited.",
        "Read any remaining output, then start a new terminal if you need one.",
    ),
    "terminal_session_limit_reached": (
        "You already have the maximum number of terminals open.",
        "Close one with close_terminal first.",
    ),
    "terminal_cols_invalid": (
        "cols must be an integer between 2 and 1000.",
        "",
    ),
    "terminal_target_invalid": (
        "target must be the approved machine id for this terminal runtime.",
        "Omit target to use the current approved runtime.",
    ),
    "terminal_target_changed": (
        "The approved terminal runtime no longer matches this request.",
        "Start a new terminal against the current approved runtime.",
    ),
    "terminal_directory_invalid": (
        "directory must be an absolute remote path without control characters.",
        "Use a path such as /home/owner/project, or omit directory.",
    ),
    "terminal_rows_invalid": (
        "rows must be an integer between 2 and 300.",
        "",
    ),
    "terminal_offset_invalid": (
        "offset must be an integer ≥ 0.",
        "Use a next_offset value returned by a previous read.",
    ),
    "terminal_wait_invalid": (
        "wait_seconds must be a number between 0 and 25.",
        "",
    ),
    "terminal_input_invalid": (
        "The keystroke payload was empty or too large.",
        "Send text of 1–8,000 characters.",
    ),
    "terminal_write_failed": (
        "The terminal refused the input.",
        "The session may have exited; read it to check.",
    ),
    "terminal_spawn_failed": (
        "The server could not start a shell session.",
        "Try again; if it persists the host may be out of resources.",
    ),
}


def _terminal_fail(exc: TerminalError) -> Dict[str, Any]:
    message, next_action = _ERROR_HINTS.get(
        exc.code, ("The terminal refused the operation.", ""))
    return _fail(exc.code, message, next_action=next_action, status_code=exc.status_code)


def _guarded(kwargs: Dict[str, Any], tool_name: str) -> Tuple[Any, Optional[str], Optional[Dict[str, Any]]]:
    try:
        require_terminal_enabled()
        authority = kwargs.get("_terminal_authority")
        if not isinstance(authority, TerminalAuthority) or not authority.matches(_ORCH):
            raise TerminalError("terminal_authority_required", 403)
        authority.assert_current(tool_name)
        if kwargs.get("user_id") != authority.owner_id:
            raise TerminalError("terminal_owner_required", 403)
        manager = _manager()
    except TerminalError as exc:
        return None, None, _terminal_fail(exc)
    except TerminalUnavailable:
        return None, None, _fail(
            "terminal_unavailable", "Terminal sessions are not available on this server.", status_code=503)
    return manager, authority.owner_id, None


def _session_ref(kwargs: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    session_id = kwargs.get("session_id")
    if not (isinstance(session_id, str) and 0 < len(session_id) <= _MAX_SESSION_ID):
        return None, _fail(
            "terminal_session_invalid",
            "session_id must be the id of one of your open terminals.",
            next_action="Call list_terminals to see your live sessions.")
    return session_id, None


def _describe_session(info: Dict[str, Any]) -> Text:
    return Text(
        content=(f"Session {_sanitize(info.get('session_id'), 48)} — "
                 f"{info.get('cols')}×{info.get('rows')}, "
                 f"idle {_sanitize(info.get('idle_seconds'), 20)}s, "
                 f"output bytes {_sanitize(info.get('ring_start'), 20)}"
                 f"..{_sanitize(info.get('ring_end'), 20)}."),
        variant="body")


def list_terminals(**kwargs) -> Dict[str, Any]:
    manager, user_id, refusal = _guarded(kwargs, "list_terminals")
    if refusal:
        return refusal
    sessions = manager.list_sessions(user_id)
    if not sessions:
        return _card("Terminal sessions",
                     [Text(content="No open terminals. Start one with start_terminal.",
                           variant="body")],
                     {"sessions": []})
    rows = [[_sanitize(s.get("session_id"), 48),
             f"{s.get('cols')}×{s.get('rows')}",
             _sanitize(s.get("idle_seconds"), 20),
             f"{s.get('ring_start')}..{s.get('ring_end')}"]
            for s in sessions]
    return _card("Terminal sessions",
                 [Table(headers=["Session", "Size", "Idle s", "Output bytes"],
                        rows=rows)],
                 {"sessions": sessions})


def start_terminal(**kwargs) -> Dict[str, Any]:
    manager, user_id, refusal = _guarded(kwargs, "start_terminal")
    if refusal:
        return refusal
    try:
        require_terminal_execution(manager, user_id)
        info = manager.create(user_id, cols=kwargs.get("cols", 80), rows=kwargs.get("rows", 24),
                              command=kwargs.get("command"), target=kwargs.get("target"),
                              directory=kwargs.get("directory"), authority=kwargs["_terminal_authority"],
                              runtime_binding=kwargs.get("runtime_binding"))
    except TerminalError as exc:
        return _terminal_fail(exc)
    return _card("Terminal started", [_describe_session(info)], {"session": info})


def terminal_read(**kwargs) -> Dict[str, Any]:
    manager, user_id, refusal = _guarded(kwargs, "terminal_read")
    if refusal:
        return refusal
    session_id, bad = _session_ref(kwargs)
    if bad:
        return bad
    offset = kwargs.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return _fail("terminal_offset_invalid",
                     "offset must be an integer ≥ 0.",
                     next_action="Use a next_offset value from a previous read.")
    wait_seconds = kwargs.get("wait_seconds", _DEFAULT_WAIT_S)
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) \
            or not 0 <= float(wait_seconds) <= READ_WAIT_MAX_S:
        return _fail("terminal_wait_invalid",
                     f"wait_seconds must be a number between 0 and {READ_WAIT_MAX_S:.0f}.")
    try:
        payload = manager.read(user_id, session_id, offset=offset,
                               wait_seconds=float(wait_seconds), authority=kwargs["_terminal_authority"])
    except TerminalError as exc:
        return _terminal_fail(exc)
    _manager_value, _user_id, refusal = _guarded(kwargs, "terminal_read")
    if refusal:
        return refusal
    chunk = base64.b64decode(payload.get("data_b64") or "")
    text = _sanitize(chunk.decode("utf-8", "replace"), _MAX_TEXT_CHARS)
    components: List[Any] = []
    if text:
        components.append(Text(content=text, variant="body"))
    if payload.get("truncated"):
        components.append(Alert(
            message="The requested offset is older than the terminal's retained "
                    "output; this chunk starts at the oldest retained byte.",
            variant="warning"))
    if payload.get("eof") and not text:
        components.append(Text(
            content="The terminal has exited and has no further output.",
            variant="body"))
    if not components:
        components.append(Text(content="(no new output yet)", variant="body"))
    return _card(f"Terminal output — {_sanitize(session_id, 48)}", components, {
        "session_id": session_id,
        "offset": payload.get("offset"),
        "next_offset": payload.get("next_offset"),
        "bytes": payload.get("bytes"),
        "truncated": payload.get("truncated"),
        "eof": payload.get("eof"),
        "exit_code": payload.get("exit_code"),
        "close_reason": payload.get("close_reason"),
        "text": text,
        "data_b64": payload.get("data_b64"),
        "session": payload.get("session"),
    })


def terminal_write(**kwargs) -> Dict[str, Any]:
    manager, user_id, refusal = _guarded(kwargs, "terminal_write")
    if refusal:
        return refusal
    try:
        require_terminal_execution(manager, user_id)
        session_id, bad = _session_ref(kwargs)
        if bad:
            return bad
        data = kwargs.get("data")
        encoded = kwargs.get("data_b64")
        if (data is None) == (encoded is None):
            raise TerminalError("terminal_input_invalid", 422)
        if data is not None:
            if not isinstance(data, str):
                raise TerminalError("terminal_input_invalid", 422)
            encoded = base64.b64encode(data.encode("utf-8")).decode("ascii")
        info = manager.write(user_id, session_id, encoded,
                             authority=kwargs["_terminal_authority"], runtime_binding=kwargs.get("runtime_binding"))
    except TerminalError as exc:
        return _terminal_fail(exc)
    return _card("Terminal input sent", [_describe_session(info)], {"session": info})


def terminal_resize(**kwargs) -> Dict[str, Any]:
    manager, user_id, refusal = _guarded(kwargs, "terminal_resize")
    if refusal:
        return refusal
    session_id, bad = _session_ref(kwargs)
    if bad:
        return bad
    try:
        info = manager.resize(user_id, session_id,
                              cols=kwargs.get("cols", 80), rows=kwargs.get("rows", 24),
                              authority=kwargs["_terminal_authority"])
    except TerminalError as exc:
        return _terminal_fail(exc)
    return _card(f"Terminal resized — {_sanitize(session_id, 48)}",
                 [_describe_session(info)], {"session": info})


def terminal_interrupt(**kwargs) -> Dict[str, Any]:
    manager, user_id, refusal = _guarded(kwargs, "terminal_interrupt")
    if refusal:
        return refusal
    session_id, bad = _session_ref(kwargs)
    if bad:
        return bad
    try:
        info = manager.interrupt(user_id, session_id, authority=kwargs["_terminal_authority"])
    except TerminalError as exc:
        return _terminal_fail(exc)
    return _card(f"Interrupted — {_sanitize(session_id, 48)}",
                 [Text(content="Sent Ctrl-C (SIGINT) to the terminal's foreground "
                            "process.", variant="body"),
                  _describe_session(info)],
                 {"session": info})


def close_terminal(**kwargs) -> Dict[str, Any]:
    manager, user_id, refusal = _guarded(kwargs, "close_terminal")
    if refusal:
        return refusal
    session_id, bad = _session_ref(kwargs)
    if bad:
        return bad
    try:
        outcome = manager.close(user_id, session_id)
    except TerminalError as exc:
        return _terminal_fail(exc)
    reason = _sanitize(outcome.get("reason"), 60) or "owner_close"
    exit_code = outcome.get("exit_code")
    detail = f"Closed ({reason})."
    if type(exit_code) is int:
        detail = f"Closed ({reason}); exit code {exit_code}."
    return _card(f"Terminal closed — {_sanitize(session_id, 48)}",
                 [Text(content=detail, variant="body")],
                 {"session_id": session_id, "closed": True, "reason": reason, "exit_code": exit_code})


def _entry(fn, description: str, input_schema: Dict[str, Any], scope: str, timeout: float, destructive: str = "never"):
    return {
        "function": fn,
        "description": description,
        "input_schema": input_schema,
        "scope": scope,
        "destructive": destructive,
        "retryable": False,
        "timeout": timeout,
    }


TOOL_REGISTRY = {
    "list_terminals": _entry(
        list_terminals,
        "List your open interactive terminal sessions on the server with their ids, "
        "sizes, idle time, and retained output ranges. Read-only.",
        {"type": "object", "properties": {}},
        "tools:read", 10.0),
    "start_terminal": _entry(
        start_terminal,
        "Requests arbitrary command execution on the operator-approved isolated SSH runtime; "
        "requires system permission and explicit approval.",
        {"type": "object", "properties": {
            "runtime_binding": {"type": "string", "pattern": "^[a-f0-9]{64}$",
                                "description": "Server-selected runtime binding retained by the approval."},
            "command": {"type": "string",
                        "description": ("Optional command to run instead of an "
                                        "interactive shell, e.g. 'df -h'.")},
            "target": {"type": "string", "maxLength": 128,
                       "description": "Optional approved machine id for this runtime."},
            "directory": {"type": "string", "maxLength": 4096,
                          "description": "Optional absolute starting directory on the approved runtime."},
            "cols": {"type": "integer", "minimum": 2, "maximum": 1000},
            "rows": {"type": "integer", "minimum": 2, "maximum": 300},
        }},
        "tools:system", 20.0, "always"),
    "terminal_read": _entry(
        terminal_read,
        "Read new output from one of your terminal sessions since a byte offset; "
        "returns the text plus next_offset to continue from. Read-only.",
        {"type": "object", "properties": {
            **_SESSION_PROP,
            "offset": {"type": "integer", "minimum": 0,
                       "description": "Byte offset to read from (0 or a previous "
                                      "next_offset). Default 0."},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": 25,
                             "description": ("Wait up to this long for new output "
                                             "(default 1).")},
        }, "required": ["session_id"]},
        "tools:read", 30.0),
    "terminal_write": _entry(
        terminal_write,
        "Requests arbitrary command execution through input on the approved isolated runtime; "
        "requires system permission and explicit approval.",
        {"type": "object", "properties": {
            **_SESSION_PROP,
            "data": {"type": "string",
                     "description": "Text to send, e.g. 'ls -la\\n' or 'y'."},
            "runtime_binding": {"type": "string", "pattern": "^[a-f0-9]{64}$",
                                "description": "Server-selected runtime binding retained by the approval."},
            "data_b64": {"type": "string", "maxLength": 21848,
                         "description": "Base64 input bytes, instead of data."},
        }, "required": ["session_id"], "oneOf": [{"required": ["data"]}, {"required": ["data_b64"]}]},
        "tools:system", 15.0, "always"),
    "terminal_resize": _entry(
        terminal_resize,
        "Resize one of your terminal sessions (columns × rows). Not destructive.",
        {"type": "object", "properties": {
            **_SESSION_PROP,
            "cols": {"type": "integer", "minimum": 2, "maximum": 1000},
            "rows": {"type": "integer", "minimum": 2, "maximum": 300},
        }, "required": ["session_id", "cols", "rows"]},
        "tools:write", 10.0),
    "terminal_interrupt": _entry(
        terminal_interrupt,
        "Send Ctrl-C (SIGINT) to the foreground process of one of your terminal "
        "sessions, e.g. to stop a long-running command. Not destructive.",
        {"type": "object", "properties": {
            **_SESSION_PROP,
        }, "required": ["session_id"]},
        "tools:write", 10.0),
    "close_terminal": _entry(
        close_terminal,
        "Close one of your terminal sessions and end its shell. The session's output "
        "is discarded; start a new one when needed.",
        {"type": "object", "properties": {
            **_SESSION_PROP,
        }, "required": ["session_id"]},
        "tools:write", 10.0),
}
