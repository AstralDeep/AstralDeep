"""Current private notes through one authenticated, correlated delivery lifetime.

This adapter deliberately has no generic render/components callback. It retains
the exact opened notes through rendering and rechecks those values before send.
Navigation tokens carry no IAM authority: the original CurrentHumanCaller does.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import re
from uuid import UUID, uuid4

from astralprojection.chrome.guidance import NOTE_CATEGORY_LABELS, build_notes_view

from orchestrator.human_request_authority import (
    CurrentHumanCaller, _HumanSocketRequest, current_human_caller,
)
from personalization.explicit_note_service import ExplicitNoteCommand, ExplicitNoteService
from personalization.explicit_notes import normalize_note_value
from persistent_agents.models import AssignmentError

TITLE = "Private notes"
HANDLERS = {}
_ACTIONS = frozenset({"chrome_note_search", "chrome_note_save", "chrome_note_toggle", "chrome_note_forget"})
_MAX_REVISION = 2**53 - 2
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_UTC_DATE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z", re.ASCII)


def _require(condition):
    if not condition:
        raise ValueError


def _identity(value):
    _require(type(value) is str)
    parsed = UUID(value)
    _require(parsed.version == 4 and str(parsed) == value)


def _revision(value, *, create=False):
    _require(type(value) is int and (0 if create else 1) <= value <= _MAX_REVISION)


def _search(value):
    _require(type(value) is str and len(value.encode("utf-8")) <= 256)
    if value.strip():
        normalize_note_value(value)


def _date(value):
    _require(type(value) is str and _UTC_DATE.fullmatch(value) is not None)
    delta = datetime.fromisoformat(value.replace("Z", "+00:00")) - _EPOCH
    result = delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
    _require(result > 0)
    return result


def _request(action, payload):
    """Snapshot all command fields before any read, IAM, audit or rendering wait."""
    try:
        _require(type(payload) is dict and type(action) is str)
        payload = deepcopy(payload)
        if action == "chrome_open":
            _require(set(payload) <= {"surface", "params"} and payload.get("surface") == "guidance")
            params = payload.get("params", {})
            _require(type(params) is dict)
            mode = params.get("mode", "list")
            _require(type(mode) is str and mode in {"list", "new", "edit", "forget"})
            allowed = ({"mode", "search", "after_id"} if mode == "list" else {"mode"}
                       if mode == "new" else {"mode", "note_id", "expected_revision"})
            _require(set(params) <= allowed)
            if mode in {"edit", "forget"}:
                _identity(params.get("note_id"))
                _revision(params.get("expected_revision"))
            if "search" in params:
                _search(params["search"])
            if "after_id" in params:
                _identity(params["after_id"])
        elif action == "chrome_note_search":
            _require(set(payload) == {"fields"} and type(payload["fields"]) is dict
                     and set(payload["fields"]) == {"search"})
            _search(payload["fields"]["search"])
        else:
            _require(action in _ACTIONS)
            expected = {"note_id", "expected_revision"}
            if action == "chrome_note_save":
                expected.add("fields")
            elif action == "chrome_note_toggle":
                expected.add("enabled")
            _require(set(payload) == expected)
            _identity(payload["note_id"])
            _revision(payload["expected_revision"], create=action == "chrome_note_save")
            if action == "chrome_note_toggle":
                _require(type(payload["enabled"]) is bool)
            elif action == "chrome_note_save":
                fields = payload["fields"]
                _require(type(fields) is dict and {"category", "value", "enabled", "expiry"}
                         <= set(fields) <= {"category", "value", "enabled", "expiry", "expiry_date"})
                _require(type(fields["category"]) is str and fields["category"] in NOTE_CATEGORY_LABELS.values())
                normalize_note_value(fields["value"])
                _require(type(fields["enabled"]) is bool)
                choices = {"No expiry", "Set a date"}
                if payload["expected_revision"] > 0:
                    choices.add("Keep current expiry")
                _require(type(fields["expiry"]) is str and fields["expiry"] in choices)
                _require(type(fields.get("expiry_date", "")) is str and len(fields.get("expiry_date", "")) <= 32)
                if fields["expiry"] == "Set a date":
                    _date(fields.get("expiry_date"))
        return payload
    except (ValueError, TypeError, AttributeError, UnicodeError, KeyError, RecursionError):
        raise AssignmentError("explicit_note_request_invalid", 422) from None


def invalidate_navigation(orch, websocket):
    previous = getattr(orch, "_guidance_navigation", {}).pop(id(websocket), None)
    if type(previous) is GuidanceNavigation:
        previous.close()


def _payload(pending):
    """Normalize only envelope duplicates already bound by the shared capture."""
    pending.assert_socket()
    payload = deepcopy(pending.message.get("payload"))
    if type(payload) is not dict:
        raise AssignmentError("explicit_note_request_invalid", 422)
    for key in ("submission_id", "request_generation", "connection_generation"):
        if key in payload:
            value = payload[key]
            if (type(value) is not str or value != getattr(pending, key)
                    or pending.message.get(key) != value):
                raise AssignmentError("explicit_note_request_invalid", 422)
            del payload[key]
    return payload


class GuidanceNavigation:
    """Host-private latest request token, never a substitute for human authority."""

    def __init__(self, orch, pending):
        self.orch, self.pending, self.closed = orch, pending, False

    def __repr__(self):
        return "<GuidanceNavigation private>"

    def assert_current(self, orch, websocket, caller, request_generation):
        if (self.closed or self.orch is not orch or type(caller) is not CurrentHumanCaller
                or caller._binding.socket_request is not self.pending
                or self.pending.websocket is not websocket
                or self.pending.request_generation != request_generation
                or getattr(orch, "_guidance_navigation", {}).get(id(websocket)) is not self):
            raise AssignmentError("explicit_note_navigation_changed", 409)
        caller._assert_local(orch)

    def close(self):
        self.closed = True
        table = getattr(self.orch, "_guidance_navigation", {})
        key = id(self.pending.websocket)
        if table.get(key) is self:
            table.pop(key)


def capture_navigation(orch, *, pending):
    """Ingress calls before the first session/admission await; invalid attempts retire old reads."""
    if type(pending) is not _HumanSocketRequest:
        raise AssignmentError("explicit_note_navigation_unavailable", 503)
    invalidate_navigation(orch, pending.websocket)
    message = pending.message
    if (pending.boundary.orchestrator is not orch or pending.purpose != "metadata"
            or pending.method not in {"WS_READ", "WS_WRITE"}):
        raise AssignmentError("explicit_note_navigation_unavailable", 503)
    pending.assert_socket()
    _request(message.get("action"), _payload(pending))
    token = GuidanceNavigation(orch, pending)
    if not hasattr(orch, "_guidance_navigation"):
        orch._guidance_navigation = {}
    orch._guidance_navigation[id(pending.websocket)] = token
    return token


def _snapshot(note):
    metadata = note.metadata
    return {"note_id": metadata.note_id, "revision": metadata.revision,
            "category": metadata.category, "value": note.value, "enabled": metadata.enabled,
            "expires_at": metadata.expires_at}


async def _expected(service, caller, payload):
    note = await service.get(caller=caller, note_id=payload["note_id"])
    if note.metadata.revision != payload["expected_revision"]:
        raise AssignmentError("explicit_note_changed", 409)
    return note


def _service_current(orch, service, caller):
    if getattr(orch, "explicit_notes", None) is not service:
        raise AssignmentError("explicit_note_service_unavailable", 503)
    service._current(caller)


async def _state(service, caller, action, payload):
    notice = None
    if action == "chrome_open":
        params = payload.get("params", {})
    elif action == "chrome_note_search":
        params = {"search": payload["fields"]["search"]}
    else:
        caller.require_write()
        common = {"note_id": payload["note_id"], "expected_revision": payload["expected_revision"]}
        if action == "chrome_note_save":
            fields = payload["fields"]
            expires_at = None
            if fields["expiry"] == "Keep current expiry":
                expires_at = (await _expected(service, caller, payload)).metadata.expires_at
            elif fields["expiry"] == "Set a date":
                expires_at = _date(fields["expiry_date"])
            category = next(key for key, label in NOTE_CATEGORY_LABELS.items() if label == fields["category"])
            command = ExplicitNoteCommand(command="save", **common, category=category,
                value=fields["value"], enabled=fields["enabled"], expires_at=expires_at)
            notice = "saved"
        elif action == "chrome_note_toggle":
            command = ExplicitNoteCommand(command="set_enabled", **common, enabled=payload["enabled"])
            notice = "enabled" if payload["enabled"] else "disabled"
        else:
            command = ExplicitNoteCommand(command="forget", **common)
            notice = "forgotten"
        await service.command(caller=caller, body=command)
        params = {}
    mode = params.get("mode", "list")
    state = {"status": "ready", "mode": mode}
    if notice is not None:
        state["notice"] = notice
    if mode == "list":
        page = await service.list(caller=caller, after_id=params.get("after_id"), search=params.get("search", ""))
        state.update(notes=tuple(_snapshot(note) for note in page.notes),
                     search=params.get("search", ""), next_cursor=page.next_cursor)
        return state, page.notes
    if mode == "new":
        state["note_id"] = str(uuid4())
        return state, ()
    note = await _expected(service, caller, params)
    state["note"] = _snapshot(note)
    return state, (note,)


async def deliver(orch, websocket, user_id, action, payload, request_generation, *, guidance_navigation=None):
    """Complete correlated render/send. No current token means no read or mutation."""
    from astralprojection.chrome import render_html
    from astralprojection.models import LayoutView
    from orchestrator.chrome_events import _device_type, _note_open_surface
    from rote.adapter import ComponentAdapter
    from shared.protocol import ChromeRender, ChromeSurface
    from webrender.chrome import render_modal_shell

    token = guidance_navigation
    try:
        caller = current_human_caller(expected_orchestrator=orch)
        if type(token) is not GuidanceNavigation:
            raise AssignmentError("explicit_note_navigation_unavailable", 503)
        token.assert_current(orch, websocket, caller, request_generation)
        pending = token.pending
        if (user_id != caller.owner_id or pending.message.get("action") != action
                or pending.message.get("payload") != payload
                or type(pending.captured.get("_client_capabilities")) is not list
                or "guidance_notes_v1" not in pending.captured["_client_capabilities"]):
            raise AssignmentError("explicit_note_surface_unavailable", 503)
        service = getattr(orch, "explicit_notes", None)
        if type(service) is not ExplicitNoteService:
            raise AssignmentError("explicit_note_service_unavailable", 503)
        request = _request(action, _payload(pending))
        state, notes = await _state(service, caller, action, request)
        _service_current(orch, service, caller)
        token.assert_current(orch, websocket, caller, request_generation)
        device = _device_type(orch, websocket)
        view = build_notes_view(state, layout=LayoutView(mode="watch" if device == "watch" else "standard"))
        if device == "browser":
            frame = ChromeRender(html=render_modal_shell(view.title, render_html(view), "guidance"),
                surface_key="guidance", request_generation=request_generation).to_json()
        else:
            components = ComponentAdapter.adapt_guidance_surface(state, orch.rote.get_profile(websocket))
            frame = ChromeSurface(surface_key="guidance", title=view.title, components=components,
                                  request_generation=request_generation).to_json()
        await service.verify_snapshot(caller=caller, notes=notes)
        try:
            async with asyncio.timeout_at(caller._deadline):
                _service_current(orch, service, caller)
                token.assert_current(orch, websocket, caller, request_generation)
                _note_open_surface(orch, websocket, "guidance")
                if not await orch._safe_send(websocket, frame):
                    raise AssignmentError("explicit_note_delivery_failed", 503)
        except TimeoutError:
            raise AssignmentError("explicit_note_delivery_timeout", 408) from None
        return True
    finally:
        if type(token) is GuidanceNavigation:
            token.close()
