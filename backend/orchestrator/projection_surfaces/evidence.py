"""Delivers transient evidence inspection through a fresh authenticated chrome read.
The current metadata operation supplies authority while navigation fences prevent stale source delivery.
"""

from copy import deepcopy
import json
import re
from types import SimpleNamespace
from uuid import UUID

from astralprims import Alert
from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
from orchestrator.context_authority import capture_context_authority, use_context_authority
from orchestrator.context_presentation import evidence_components, persistent_reference_components
from orchestrator.evidence_context import EvidenceContext, get_context
from orchestrator.human_request_authority import CurrentHumanCaller, _HumanSocketRequest, current_human_caller
from orchestrator.turn_guidance_authority import (
    bind_foreground_guidance, capture_turn_guidance_from_human, use_turn_guidance,
)
from orchestrator.work_admission import ExecutionFence, OperationRecord, OwnerScope
from persistent_agents.models import AssignmentError
from shared.protocol import ChromeRender, ChromeSurface, MCPResponse

TITLE = "Captured evidence"
NO_NAV = True
HANDLERS = {}
_REFERENCE = re.compile(r"obs_[A-Za-z0-9_-]{43}")
_VIEW = re.compile(r"view_[A-Za-z0-9_-]{43}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_COMPONENT_ID = re.compile(r"cc_[a-f0-9]{24}")
_IDENTITIES = {"submission_id", "request_generation", "connection_generation"}


def _refuse():
    raise AssignmentError("evidence_surface_unavailable", 503) from None


def _chat(value):
    try:
        identity = UUID(value)
        if type(value) is not str or identity.version != 4 or str(identity) != value:
            _refuse()
        return value
    except (TypeError, ValueError, AttributeError):
        _refuse()


def _params(value):
    if type(value) is not dict:
        _refuse()
    if set(value) == {"view_id"}:
        identity = value["view_id"]
        if type(identity) is not str or not _VIEW.fullmatch(identity):
            _refuse()
        return "inspect_context_view", {"view_id": identity}, "summary"
    if value == {} or value == {"kind": "usage"}:
        return "context_usage", {}, "usage"
    reference, offset, kind = value.get("reference"), value.get("offset", 0), value.get("kind")
    if (set(value) - {"kind", "reference", "offset"} or type(kind) is not str or kind not in {"source", "preview"}
            or type(reference) is not str or not _REFERENCE.fullmatch(reference)
            or type(offset) is not int or not 0 <= offset <= 8 * 1024 * 1024):
        _refuse()
    return "recall_observation", {"reference": reference, "offset": offset}, kind


def validate_payload(payload):
    if (type(payload) is not dict or payload.get("surface") != "evidence"
            or set(payload) - ({"surface", "params", "component_id"} | _IDENTITIES)):
        _refuse()
    if "component_id" in payload and (type(payload["component_id"]) is not str
            or not _COMPONENT_ID.fullmatch(payload["component_id"])):
        _refuse()
    return _params(payload.get("params", {}))


def invalidate_navigation(orch, websocket):
    previous = getattr(orch, "_evidence_navigation", {}).pop(id(websocket), None)
    if type(previous) is EvidenceNavigation:
        previous.close()


class EvidenceNavigation:
    def __init__(self, orch, pending, chat_id):
        self.orch, self.pending, self.chat_id, self.closed = orch, pending, chat_id, False
        self.request = deepcopy(validate_payload(pending.message.get("payload")))

    def __repr__(self):
        return "<EvidenceNavigation private>"

    def _local(self, orch, websocket):
        if (self.closed or self.orch is not orch or self.pending.websocket is not websocket
                or self.pending.boundary.orchestrator is not orch
                or self.pending.purpose != "metadata" or self.pending.method != "WS_READ"
                or self.pending.message.get("action") != "chrome_open"
                or getattr(orch, "_evidence_navigation", {}).get(id(websocket)) is not self
                or getattr(orch, "_ws_active_chat", {}).get(id(websocket)) != self.chat_id
                or validate_payload(self.pending.message.get("payload")) != self.request):
            _refuse()
        self.pending.assert_socket()

    def assert_current(self, orch, websocket, caller, request_generation):
        self._local(orch, websocket)
        context = _CONNECTION_OPERATION_CONTEXT.get()
        if (type(caller) is not CurrentHumanCaller or current_human_caller(expected_orchestrator=orch) is not caller
                or caller._binding.socket_request is not self.pending or caller.owner_id != self.pending.owner_id
                or self.pending.request_generation != request_generation or type(context) is not dict
                or context.get("human_request") is not self.pending or context.get("evidence_navigation") is not self):
            _refuse()
        operation, fence = context.get("operation"), context.get("execution_fence")
        if (type(operation) is not OperationRecord or type(fence) is not ExecutionFence
                or operation.operation_id != fence.operation_id or operation.operation_kind != "connection_frame"
                or operation.owner_scope is not OwnerScope.CONNECTION
                or operation.connection_scope_id != self.pending.connection.connection_scope_id
                or str(operation.connection_generation) != self.pending.connection_generation
                or str(operation.request_generation) != self.pending.request_generation
                or str(operation.chat_id) != self.chat_id
                or str(context.get("connection_generation")) != self.pending.connection_generation
                or str(context.get("request_generation")) != self.pending.request_generation):
            _refuse()
        caller._assert_local(orch)
        return context

    def close(self):
        self.closed = True
        table = getattr(self.orch, "_evidence_navigation", {})
        key = id(self.pending.websocket)
        if table.get(key) is self:
            table.pop(key)


def capture_navigation(orch, *, pending, chat_id):
    if (type(pending) is not _HumanSocketRequest or pending.boundary.orchestrator is not orch
            or pending.purpose != "metadata" or pending.method != "WS_READ"
            or pending.message.get("action") != "chrome_open"):
        _refuse()
    pending.assert_socket()
    selected = _chat(getattr(orch, "_ws_active_chat", {}).get(id(pending.websocket)))
    if chat_id != selected:
        _refuse()
    validate_payload(pending.message.get("payload"))
    invalidate_navigation(orch, pending.websocket)
    token = EvidenceNavigation(orch, pending, selected)
    if not hasattr(orch, "_evidence_navigation"):
        orch._evidence_navigation = {}
    orch._evidence_navigation[id(pending.websocket)] = token
    token._local(orch, pending.websocket)
    return token


def _components(response, arguments, kind):
    if response is None or (type(response) is MCPResponse and response.error):
        return evidence_components(state="blocked")
    if (type(response) is not MCPResponse or type(response.result) is not dict
            or type(response.ui_components) is not list
            or any(type(component) is not dict for component in response.ui_components)):
        _refuse()
    data = response.result
    if kind in {"source", "preview"} and data.get("status") != "unavailable":
        text, start, end, total = (data.get(key) for key in ("text", "start", "end", "total"))
        if (data.get("reference") != arguments["reference"] or data.get("untrusted") is not True
                or type(text) is not str or len(text.encode("utf-8")) > 16384
                or any(type(value) is not int for value in (start, end, total))
                or start != arguments["offset"] or not 0 <= start <= end <= total
                or end - start != len(text.encode("utf-8"))
                or type(data.get("digest")) is not str or not _DIGEST.fullmatch(data["digest"])):
            _refuse()
        if kind == "preview":
            preview = text.encode("utf-8")[:1024].decode("utf-8", errors="ignore")
            return evidence_components(state="preview", text=preview, reference=data["reference"],
                digest=data["digest"], start=start, end=start + len(preview.encode("utf-8")),
                total=total, next_offset=start, outcome=data.get("outcome"))
    elif kind == "summary" and data.get("status") != "unavailable":
        if (data.get("view") != "generated_summary" or data.get("view_id") != arguments["view_id"]
                or data.get("untrusted") is not True or type(data.get("text")) is not str
                or not data["text"] or len(data["text"].encode("utf-8")) > 16384
                or type(data.get("source_refs")) is not list or any(
                    type(reference) is not str or not _REFERENCE.fullmatch(reference) for reference in data["source_refs"])):
            _refuse()
    elif kind == "usage" and (type(data.get("usage")) is not dict or type(data.get("model_calls")) is not int):
        _refuse()
    return deepcopy(response.ui_components)


def _watch_components(response, arguments, kind):
    _components(response, arguments, kind)
    if response is None or response.error:
        kind = "blocked"
    elif response.result.get("status") == "unavailable":
        kind = "missing"
    elif kind == "preview":
        data = response.result
        metadata = {key: data[key] for key in ("reference", "digest", "start", "total", "outcome")}
        preview = data["text"].encode("utf-8")[:1024].decode("utf-8", errors="ignore")
        metadata["end"] = data["start"] + len(preview.encode("utf-8"))
        response = MCPResponse(result=metadata)
    components = persistent_reference_components(response, kind=kind,
        view_id=arguments.get("view_id"), watch=True)
    handoff = Alert(message="Inspect this temporary evidence view in this conversation on a phone or desktop.",
        variant="info").to_dict()
    if handoff not in components:
        components.append(handoff)
    return components


async def deliver(orch, websocket, user_id, payload, request_generation):
    from orchestrator.chrome_events import _note_open_surface
    from rote.adapter import ComponentAdapter
    from webrender import render
    from webrender.chrome import render_modal_shell

    context = _CONNECTION_OPERATION_CONTEXT.get() or {}
    token = context.get("evidence_navigation")
    origin = None
    try:
        caller = current_human_caller(expected_orchestrator=orch)
        if type(token) is not EvidenceNavigation:
            _refuse()
        operation_context = token.assert_current(orch, websocket, caller, request_generation)
        if user_id != caller.owner_id or token.pending.message.get("payload") != payload:
            _refuse()
        name, arguments, kind = token.request
        origin = await capture_turn_guidance_from_human(caller, expected_orchestrator=orch)
        token.assert_current(orch, websocket, caller, request_generation)
        binding = bind_foreground_guidance(origin, expected_orchestrator=orch, websocket=websocket,
            operation_context=operation_context, chat_id=token.chat_id)
        with use_turn_guidance(binding, expected_orchestrator=orch):
            lease = await capture_context_authority(orchestrator=orch, websocket=websocket, chat_id=token.chat_id)
            with use_context_authority(lease):
                token.assert_current(orch, websocket, caller, request_generation)
                service = get_context(orch)
                if type(service) is not EvidenceContext:
                    _refuse()
                with service.privacy_delivery(websocket=websocket, owner=caller.owner_id,
                                              chat=token.chat_id, request_generation=request_generation):
                    call = SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))
                    response = await orch.execute_single_tool(websocket, call, {name: "evidence-1"},
                        token.chat_id, user_id=caller.owner_id)
                    token.assert_current(orch, websocket, caller, request_generation)
                    await lease.verify(orchestrator=orch, websocket=websocket, chat_id=token.chat_id)
                    await caller.verify_delivery()
                    token.assert_current(orch, websocket, caller, request_generation)
                    response = await service.verify_delivery(response, websocket=websocket,
                        owner=caller.owner_id, chat=token.chat_id)
                    await lease.verify(orchestrator=orch, websocket=websocket, chat_id=token.chat_id)
                    token.assert_current(orch, websocket, caller, request_generation)
                    lease.assert_current(orchestrator=orch, websocket=websocket, chat_id=token.chat_id)
                    if getattr(orch, "_evidence_context", None) is not service:
                        _refuse()
                    profile = orch.rote.get_profile(websocket)
                    device = getattr(profile.device_type, "value", str(profile.device_type))
                    components = (_watch_components(response, arguments, kind) if device == "watch"
                        else _components(response, arguments, kind))
                    if device in {"browser", "mobile", "tablet"}:
                        frame = ChromeRender(html=render_modal_shell(TITLE, render(components, profile), "evidence"),
                            surface_key="evidence", request_generation=request_generation).to_json()
                    else:
                        adapted = ComponentAdapter.adapt(components, profile)
                        frame = ChromeSurface(surface_key="evidence", title=TITLE, components=adapted,
                            request_generation=request_generation).to_json()
                    token.assert_current(orch, websocket, caller, request_generation)
                    lease.assert_current(orchestrator=orch, websocket=websocket, chat_id=token.chat_id)
                    if response is not None and not response.error:
                        service.assert_delivery_current(response, websocket=websocket,
                            owner=caller.owner_id, chat=token.chat_id)
                    _note_open_surface(orch, websocket, "evidence")
                    if not await orch._safe_send(websocket, frame):
                        _refuse()
                    return True
    finally:
        if origin is not None:
            origin.close()
        if type(token) is EvidenceNavigation:
            token.close()
