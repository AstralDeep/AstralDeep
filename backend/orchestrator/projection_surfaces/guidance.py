"""Current private notes through one authenticated, correlated delivery lifetime.

This adapter deliberately has no generic render/components callback. It retains
the exact opened notes through rendering and rechecks those values before send.
Navigation tokens carry no IAM authority: the original CurrentHumanCaller does.
"""
from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timezone
import re
from uuid import UUID, uuid4

from astralprojection.chrome.guidance import (
    NOTE_CATEGORY_LABELS,
    build_guidance_view,
)

from orchestrator.human_request_authority import (
    CurrentHumanCaller, _HumanSocketRequest, current_human_caller,
)
from personalization.explicit_note_service import ExplicitNoteCommand, ExplicitNoteService
from personalization.explicit_notes import normalize_note_value
from persistent_agents.models import AssignmentError

logger = logging.getLogger("Orchestrator.Chrome.Guidance")

TITLE = "Private notes"
HANDLERS = {}
_ACTIONS = frozenset({"chrome_note_search", "chrome_note_save", "chrome_note_toggle",
                      "chrome_note_forget", "chrome_turn_selection_set"})

#: The guidance view the composer's Advanced button opens. Absent, the
#: surface is the private-notes list it has always been.
SELECTION_VIEW = "selection"
#: Bounds on what one selection may name, mirroring the view builder's own.
MAX_SELECTION_SKILLS = 20
MAX_SELECTION_NOTES = 8
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


def _selection_reference(value, key):
    _require(type(value) is dict and set(value) == {key, "revision"})
    _identity(value[key])
    _revision(value["revision"])


def _selection_input(value):
    """The exact version-1 selection shape the client and Work both use.

    Validated as a shape only. Whether the things it names still exist, and
    still at those revisions, is decided by re-reading them — never by
    trusting the request.
    """
    _require(type(value) is dict and set(value) == {"version", "agent", "skills", "notes"})
    _require(value["version"] == 1)
    agent = value["agent"]
    if agent is not None:
        _require(type(agent) is dict and set(agent) == {"agent_id", "revision_id"})
        _identity(agent["agent_id"])
        _identity(agent["revision_id"])
    for key, maximum in (("skills", MAX_SELECTION_SKILLS), ("notes", MAX_SELECTION_NOTES)):
        entries = value[key]
        _require(type(entries) is list and len(entries) <= maximum)
        identity = "skill_id" if key == "skills" else "note_id"
        for entry in entries:
            _selection_reference(entry, identity)
        _require(len({entry[identity] for entry in entries}) == len(entries))
    return value


def _selection_payload(chosen) -> dict:
    """The version-1 selection shape ``work_submit._selected_ids`` accepts."""
    return {"version": 1,
            "agent": None if chosen["agent"] is None else dict(chosen["agent"]),
            "skills": [dict(item) for item in chosen["skills"]],
            "notes": [dict(item) for item in chosen["notes"]]}


def _is_selection(action, payload) -> bool:
    """Is this request about the per-chat selection rather than the notes?"""
    if action == "chrome_turn_selection_set":
        return True
    return (action == "chrome_open"
            and (payload.get("params") or {}).get("view") == SELECTION_VIEW)


def _request(action, payload):
    """Snapshot all command fields before any read, IAM, audit or rendering wait."""
    try:
        _require(type(payload) is dict and type(action) is str)
        payload = deepcopy(payload)
        if action == "chrome_open":
            _require(set(payload) <= {"surface", "params"} and payload.get("surface") == "guidance")
            params = payload.get("params", {})
            _require(type(params) is dict)
            if params.get("view") == SELECTION_VIEW:
                # Nothing but the view. The 088 open contract is that this
                # request carries no client state at all -- not a session id,
                # not the binding the composer is holding -- so the picker
                # opens from what the server can see for itself.
                _require(set(params) == {"view"})
                return payload
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
        elif action == "chrome_turn_selection_set":
            _selection_input(payload)
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


async def _offered_agents(orch, caller):
    """The user's own declarative agents that have an active revision."""
    try:
        heads = await orch.declarative_agents.list_heads(caller=caller)
    except Exception:
        logger.debug("selection: declarative agents unavailable", exc_info=True)
        return ()
    out = []
    for head in heads:
        revision_id = getattr(head, "selected_definition_revision_id", None)
        if getattr(head, "status", "") != "active" or not revision_id:
            continue
        out.append({"agent_id": head.agent_id, "revision_id": revision_id,
                    "display_name": str(head.display_name or head.agent_id)[:120]})
        if len(out) >= MAX_SELECTION_SKILLS:
            break
    return tuple(out)


async def _offered_skills(orch, caller):
    """The user's own skills, current revision each."""
    try:
        from orchestrator import user_skills

        store = user_skills.store_for(orch)
        if store is None:
            return ()
        skills = await store.list(caller=caller)
    except Exception:
        logger.debug("selection: skills unavailable", exc_info=True)
        return ()
    out = []
    for skill in skills:
        skill_id = getattr(skill, "skill_id", "")
        revision = getattr(skill, "revision", 0)
        if not skill_id or not isinstance(revision, int) or revision < 1:
            continue
        out.append({"skill_id": skill_id, "revision": revision,
                    "name": str(skill.name or "")[:60],
                    "command": str(getattr(skill, "command", "") or ""),
                    "enabled": bool(skill.enabled)})
        if len(out) >= MAX_SELECTION_SKILLS:
            break
    return tuple(out)


def _offered_notes(notes):
    """The note rows the picker offers, from the same read the notes list uses."""
    out = []
    for note in notes:
        metadata = note.metadata
        if metadata.category not in NOTE_CATEGORY_LABELS:
            continue
        out.append({"note_id": metadata.note_id, "revision": metadata.revision,
                    "category": metadata.category, "enabled": bool(metadata.enabled)})
        if len(out) >= MAX_SELECTION_NOTES:
            break
    return tuple(out)


def _narrow_selection(selection, agents, skills, notes):
    """Keep only what is still on offer at exactly the revision named.

    A skill deleted, or a note edited, since the selection was made is simply
    no longer selected — the alternative is a picker that refuses to draw
    because of something the person cannot see.
    """
    chosen = {"agent": None, "skills": [], "notes": []}
    if not isinstance(selection, dict):
        return chosen
    agent = selection.get("agent")
    if isinstance(agent, dict):
        for entry in agents:
            if (entry["agent_id"] == agent.get("agent_id")
                    and entry["revision_id"] == agent.get("revision_id")):
                chosen["agent"] = {"agent_id": entry["agent_id"],
                                   "revision_id": entry["revision_id"]}
                break
    for kind, offered, identity in (("skills", skills, "skill_id"), ("notes", notes, "note_id")):
        current = {entry[identity]: entry["revision"] for entry in offered}
        seen = set()
        for entry in selection.get(kind) or []:
            if not isinstance(entry, dict):
                continue
            key = entry.get(identity)
            if key in seen or current.get(key) != entry.get("revision"):
                continue
            seen.add(key)
            chosen[kind].append({identity: key, "revision": entry["revision"]})
    return chosen


async def _selection_state(orch, service, caller, action, payload):
    """The selection picker's state, and the notes the read is verified against."""
    page = await service.list(caller=caller, after_id=None, search="")
    notes = page.notes
    agents = await _offered_agents(orch, caller)
    skills = await _offered_skills(orch, caller)
    offered_notes = _offered_notes(notes)
    if action == "chrome_turn_selection_set":
        incoming = payload
        notice = "cleared" if (payload["agent"] is None and not payload["skills"]
                               and not payload["notes"]) else "saved"
    else:
        # An open render shows nothing selected. The per-chat binding lives in
        # the browser and the open request may not carry it, so the server has
        # nothing to show as chosen until it is told -- and a render that
        # claimed an empty selection would then be adopted and wipe the real
        # one, which is why only a selection command stamps below.
        incoming = None
        notice = None
    state = {
        "view": SELECTION_VIEW, "status": "ready",
        "agents": agents, "skills": skills, "notes": offered_notes,
        "selected": _narrow_selection(incoming, agents, skills, offered_notes),
    }
    if notice is not None:
        state["notice"] = notice
    return state, notes


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
    import json

    from astralprojection.chrome import render_html
    from astralprojection.models import LayoutView
    from orchestrator.chrome_events import _device_type, _note_open_surface
    from webrender import esc
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
        capabilities = pending.captured.get("_client_capabilities")
        selection = _is_selection(action, payload if type(payload) is dict else {})
        required = "guidance_selection_v1" if selection else "guidance_notes_v1"
        if (user_id != caller.owner_id or pending.message.get("action") != action
                or pending.message.get("payload") != payload
                or type(capabilities) is not list or required not in capabilities):
            raise AssignmentError("explicit_note_surface_unavailable", 503)
        service = getattr(orch, "explicit_notes", None)
        if type(service) is not ExplicitNoteService:
            raise AssignmentError("explicit_note_service_unavailable", 503)
        request = _request(action, _payload(pending))
        if selection:
            state, notes = await _selection_state(orch, service, caller, action, request)
        else:
            state, notes = await _state(service, caller, action, request)
        _service_current(orch, service, caller)
        token.assert_current(orch, websocket, caller, request_generation)
        device = _device_type(orch, websocket)
        view = build_guidance_view(state, layout=LayoutView(mode="watch" if device == "watch" else "standard"))
        if device == "browser":
            body = render_html(view)
            if action == "chrome_turn_selection_set":
                # Stamp what was rendered as selected on the surface root. The
                # composer adopts exactly this, so what it will send with the
                # next turn is what the picker just showed, at these revisions
                # -- not whatever the browser happened to be holding.
                body = ('<div data-chrome-surface="guidance" data-astral-selection="'
                        + esc(json.dumps(_selection_payload(state["selected"])))
                        + '">' + body + "</div>")
            nav_html = ""
            if not selection:
                try:
                    from orchestrator.auth import _extract_roles
                    from orchestrator.chrome_events import _session_identity, _settings_nav_html
                    roles = _extract_roles(caller.claims) if hasattr(caller, "claims") else ()
                    nav_html = _settings_nav_html(roles, "guidance", _session_identity(orch, websocket, roles))
                except Exception:
                    logger.debug("chrome: settings rail unavailable for guidance", exc_info=True)
                    nav_html = ""
            frame = ChromeRender(html=render_modal_shell(view.title, body, "guidance", nav_html=nav_html),
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


# ---------------------------------------------------------------------------
# 088 T032 — declarative-agent metadata handlers, registered HERE
# ---------------------------------------------------------------------------
# ``_h_declarative_view``/``_h_declarative_command`` are DEFINED in
# ``authoring.py`` (they operate on ``authoring.SURFACE_KEY`` state and
# ``DeclarativeAgentService``) but are registered in THIS module's HANDLERS,
# not authoring's own: a pinned contract test
# (test_declarative_agent_definition_088.py) asserts ``authoring.HANDLERS``
# names no "declarative" action, because the declarative *definition* parsing
# contract is a distinct, inert surface from the metadata-lifecycle dispatch
# added by T032/T037. ``chrome_events.collect_handlers()`` aggregates every
# projection-surface module's HANDLERS by action name alone, and
# ``human_request_authority`` classifies WS_READ/WS_WRITE by the action name
# itself — so which module registers a handler is transparent to dispatch;
# only the handler's own returned ``SURFACE_KEY`` decides what re-renders.
from orchestrator.projection_surfaces import authoring as _declarative_authoring  # noqa: E402

HANDLERS["chrome_declarative_view"] = _declarative_authoring._h_declarative_view
HANDLERS["chrome_declarative_command"] = _declarative_authoring._h_declarative_command
