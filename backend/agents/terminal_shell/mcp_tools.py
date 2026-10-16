#!/usr/bin/env python3
"""Governed shell/session verbs over the orchestrator's owner-scoped terminal session
manager (orchestrator/terminal_sessions.py): each verb binds to the dispatch-injected
owner identity — never a model-supplied one — and returns truthful machine codes as
error alerts for the normal tool-result rendering path.
"""
from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Optional, Tuple

from astralprims import Alert, Card, Table, Text

from orchestrator.terminal_sessions import (
    INPUT_CHUNK_MAX,
    READ_WAIT_MAX_S,
    TerminalError,
)

_ORCH = None

_MAX_FIELD = 256
_MAX_SESSION_ID = 64
_MAX_TEXT_CHARS = 16_000
_MAX_COMMAND_CHARS = 512
_MAX_WRITE_CHARS = 8_000
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


def _fail(code: str, message: str, *, next_action: str = "") -> Dict[str, Any]:
    text = f"{code}: {_sanitize(message, 400)}" + (f" — {next_action}" if next_action else "")
    return {"_ui_components": [Alert(message=text, variant="error").to_dict()],
            "_data": {"code": code, "message": message, "next_action": next_action}}


def _card(title: str, components: List[Any], data: Dict[str, Any]) -> Dict[str, Any]:
    return _data(data, [Card(title=title, content=components)])


_ERROR_HINTS = {
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
    return _fail(exc.code, message, next_action=next_action)


def _owner(kwargs: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    user_id = kwargs.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        return None, _fail(
            "terminal_owner_required",
            "Terminal sessions belong to a signed-in owner.",
            next_action="Sign in and dispatch this tool through your own session.")
    return user_id, None


def _guarded(kwargs: Dict[str, Any]) -> Tuple[Any, Optional[str], Optional[Dict[str, Any]]]:
    try:
        manager = _manager()
    except TerminalUnavailable:
        return None, None, _fail(
            "terminal_unavailable",
            "Terminal sessions are not available on this server.")
    user_id, refusal = _owner(kwargs)
    if refusal:
        return None, None, refusal
    return manager, user_id, None


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
    manager, user_id, refusal = _guarded(kwargs)
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
    manager, user_id, refusal = _guarded(kwargs)
    if refusal:
        return refusal
    cols = kwargs.get("cols", 80)
    rows = kwargs.get("rows", 24)
    command = kwargs.get("command")
    if command is not None and not (isinstance(command, str) and command.strip()):
        return _fail("terminal_command_invalid",
                     "command must be non-empty text when given.")
    if isinstance(command, str) and len(command) > _MAX_COMMAND_CHARS:
        return _fail("terminal_command_invalid",
                     f"command is too long (max {_MAX_COMMAND_CHARS} characters).")
    try:
        info = manager.create(user_id, cols=cols, rows=rows, command=command)
    except TerminalError as exc:
        return _terminal_fail(exc)
    except OSError:
        return _fail("terminal_spawn_failed",
                     "The server could not start a shell session.")
    title = "Terminal command finished" if command else "Terminal ready"
    body = [Text(
        content=(f"Ran: {_sanitize(command, 200)}" if command
                 else "An interactive shell is running. Send keystrokes with "
                      "terminal_write and read output with terminal_read."),
        variant="body"),
        _describe_session(info)]
    return _card(title, body, {"session": info, "command": command})


def terminal_read(**kwargs) -> Dict[str, Any]:
    manager, user_id, refusal = _guarded(kwargs)
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
                               wait_seconds=float(wait_seconds))
    except TerminalError as exc:
        return _terminal_fail(exc)
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
        "text": text,
    })


def terminal_write(**kwargs) -> Dict[str, Any]:
    manager, user_id, refusal = _guarded(kwargs)
    if refusal:
        return refusal
    session_id, bad = _session_ref(kwargs)
    if bad:
        return bad
    data = kwargs.get("data")
    if not isinstance(data, str) or not data:
        return _fail("terminal_input_invalid",
                     "data must be the text to send, e.g. 'ls -la\\n'.")
    if len(data) > _MAX_WRITE_CHARS:
        return _fail("terminal_input_invalid",
                     f"data must be at most {_MAX_WRITE_CHARS} characters.")
    encoded = data.encode("utf-8", "ignore")
    if not encoded or len(encoded) > INPUT_CHUNK_MAX:
        return _fail("terminal_input_invalid",
                     f"data must encode to 1–{INPUT_CHUNK_MAX} bytes.")
    try:
        info = manager.write(user_id, session_id,
                             base64.b64encode(encoded).decode("ascii"))
    except TerminalError as exc:
        return _terminal_fail(exc)
    return _card(f"Sent to terminal — {_sanitize(session_id, 48)}",
                 [_describe_session(info)],
                 {"session": info, "sent_bytes": len(encoded)})


def terminal_resize(**kwargs) -> Dict[str, Any]:
    manager, user_id, refusal = _guarded(kwargs)
    if refusal:
        return refusal
    session_id, bad = _session_ref(kwargs)
    if bad:
        return bad
    try:
        info = manager.resize(user_id, session_id,
                              cols=kwargs.get("cols", 80), rows=kwargs.get("rows", 24))
    except TerminalError as exc:
        return _terminal_fail(exc)
    return _card(f"Terminal resized — {_sanitize(session_id, 48)}",
                 [_describe_session(info)], {"session": info})


def terminal_interrupt(**kwargs) -> Dict[str, Any]:
    manager, user_id, refusal = _guarded(kwargs)
    if refusal:
        return refusal
    session_id, bad = _session_ref(kwargs)
    if bad:
        return bad
    try:
        info = manager.interrupt(user_id, session_id)
    except TerminalError as exc:
        return _terminal_fail(exc)
    return _card(f"Interrupted — {_sanitize(session_id, 48)}",
                 [Text(content="Sent Ctrl-C (SIGINT) to the terminal's foreground "
                            "process.", variant="body"),
                  _describe_session(info)],
                 {"session": info})


def close_terminal(**kwargs) -> Dict[str, Any]:
    manager, user_id, refusal = _guarded(kwargs)
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
    return _card(f"Terminal closed — {_sanitize(session_id, 48)}",
                 [Text(content=f"Closed ({reason}).", variant="body")],
                 {"session_id": session_id, "closed": True, "reason": reason})


def _entry(fn, description: str, input_schema: Dict[str, Any], scope: str, timeout: float):
    return {
        "function": fn,
        "description": description,
        "input_schema": input_schema,
        "scope": scope,
        "destructive": "never",
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
        "Open a new interactive shell terminal owned by the signed-in user, or run one "
        "command to completion when 'command' is given. Creates new work; not "
        "destructive. Read its output with terminal_read.",
        {"type": "object", "properties": {
            "command": {"type": "string",
                        "description": ("Optional command to run instead of an "
                                        "interactive shell, e.g. 'df -h'.")},
            "cols": {"type": "integer", "minimum": 2, "maximum": 1000},
            "rows": {"type": "integer", "minimum": 2, "maximum": 300},
        }},
        "tools:write", 20.0),
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
        "Send keystrokes (text) to one of your terminal sessions, exactly as a user "
        "would type them; include a trailing '\\n' to run the line. Not destructive "
        "by itself — the shell executes what the session runs.",
        {"type": "object", "properties": {
            **_SESSION_PROP,
            "data": {"type": "string",
                     "description": "Text to send, e.g. 'ls -la\\n' or 'y'."},
        }, "required": ["session_id", "data"]},
        "tools:write", 15.0),
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
