"""Tests for clinical_diagnostic.dispatcher: prompt shaping, bounded WebSocket frame
collection, terminal/denial handling, owner-isolation probes, HTTP failures, and safe
token handling.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
import urllib.error

import pytest

from clinical_diagnostic.dispatcher import (
    DispatchedAnswer,
    DispatcherDenied,
    DispatcherError,
    DispatcherProducer,
    DispatcherTimeout,
    IsolationProbe,
    WebSocketDispatcher,
    _SyncWebsocketTransport,
    _http_request,
    render_dispatcher_prompt,
)
from clinical_diagnostic.models import (
    ClinicalCase,
    ConversationMessage,
    FailureCode,
    Partition,
    RubricItem,
    Setting,
)


def case():
    return ClinicalCase(
        case_id="case-1",
        partition=Partition.DEVELOPMENT,
        setting=Setting.CONSULTATION,
        specialty="primary care",
        conversation=(
            ConversationMessage("user", "Synthetic first question?"),
            ConversationMessage("assistant", "Synthetic prior answer."),
            ConversationMessage("user", "Synthetic final question?"),
        ),
        rubric_items=(RubricItem("c1", "Criterion.", 1.0),),
    )


class Response:
    def __init__(self, status, payload):
        self.status = status
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


class FakeTransport:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.closed = False
        self.recv_timeout = None

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout):
        self.recv_timeout = timeout
        if not self.frames:
            raise DispatcherTimeout()
        frame = self.frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        if isinstance(frame, (str, bytes)):
            return frame
        return json.dumps(frame)

    def close(self):
        self.closed = True


def empty_chat_http(method, url, _headers, _body):
    if method == "POST":
        return Response(201, {"chat_id": "chat-1"})
    if url.endswith("/components"):
        return Response(200, {"components": []})
    return Response(200, {"chat": {"id": "chat-1", "messages": []}})


def dispatcher(frames, http=None, **over):
    transport = FakeTransport(frames)
    fields = dict(
        token="secret-token",
        ws_uri="ws://127.0.0.1:8001/ws",
        http_uri="http://127.0.0.1:8001",
        transport_factory=lambda: transport,
        http=http or empty_chat_http,
        clock=lambda: 0.0,
    )
    fields.update(over)
    return WebSocketDispatcher(**fields), transport


def test_prompt_shapes_roles_and_final_request():
    prompt = render_dispatcher_prompt(case())
    assert "user: Synthetic first question?" in prompt
    assert "assistant: Synthetic prior answer." in prompt
    assert prompt.rstrip().endswith("user: Synthetic final question?")


def test_constructor_validates_required_configuration():
    with pytest.raises(ValueError, match="bearer token"):
        WebSocketDispatcher(token="", ws_uri="ws://x/ws", http_uri="http://x")
    with pytest.raises(ValueError, match="absolute"):
        WebSocketDispatcher(token="t", ws_uri="relative", http_uri="http://x")
    with pytest.raises(ValueError, match="timeout_seconds"):
        WebSocketDispatcher(token="t", ws_uri="ws://x/ws", http_uri="http://x",
                            timeout_seconds=0)
    with pytest.raises(ValueError, match="max_frames"):
        WebSocketDispatcher(token="t", ws_uri="ws://x/ws", http_uri="http://x",
                            max_frames=0)
    with pytest.raises(ValueError, match="max_message_bytes"):
        WebSocketDispatcher(token="t", ws_uri="ws://x/ws", http_uri="http://x",
                            max_message_bytes=10)
    adapter, _ = dispatcher([])
    assert "secret-token" not in repr(adapter)


def test_answer_registers_chats_and_collects_committed_components():
    frames = [
        scoped_frame(type="audit_append", event={"event_id": "audit-1"}),
        committed_answer(text="Required fact.", canvas=[
            {"type": "card", "title": "Safety action.", "content": [
                {"type": "text", "content": "Cannot determine remotely."}]}]),
        completed_turn(),
    ]
    adapter, transport = scoped_dispatcher(frames)
    answer = adapter.answer(case())
    assert answer.text == "Required fact.\nSafety action.\nCannot determine remotely."
    assert answer.chat_id == "chat-1"
    assert answer.frame_types == ("rote_config", "audit_append", "conversation_snapshot", "operation_status")
    assert answer.audit_event_ids == ("audit-1",)
    assert transport.sent[0]["type"] == "register_ui"
    assert transport.sent[0]["token"] == "secret-token"
    assert transport.sent[1]["action"] == "chat_message"
    assert transport.sent[1]["payload"]["chat_id"] == "chat-1"
    assert transport.closed


def test_answer_ignores_history_html_and_transient_assistant_messages():
    adapter, _ = scoped_dispatcher([
        {"type": "ui_render", "target": "history", "html": "<p>Old history.</p>"},
        {"type": "assistant_message", "role": "assistant", "content": "Tool preview."},
        committed_answer(), completed_turn(),
    ])
    assert adapter.answer(case()).text == "Final answer."


def test_answer_requires_matching_operation_terminal_after_chat_status():
    adapter, _ = scoped_dispatcher([
        committed_answer(), {"type": "chat_status", "status": "done"},
        completed_turn(state="cancelled"),
    ])
    with pytest.raises(DispatcherDenied):
        adapter.answer(case())


def test_answer_refuses_missing_terminal_and_missing_text():
    adapter, _ = scoped_dispatcher([committed_answer()], max_frames=2)
    with pytest.raises(DispatcherError, match="terminal"):
        adapter.answer(case())
    adapter, _ = scoped_dispatcher([committed_answer(text=""), completed_turn()])
    with pytest.raises(DispatcherError) as caught:
        adapter.answer(case())
    assert caught.value.code is FailureCode.MALFORMED_ANSWER


def test_answer_refuses_denials_failed_terminals_and_bad_frames():
    adapter, _ = scoped_dispatcher([scoped_frame(type="error", message="authentication required")])
    with pytest.raises(DispatcherDenied):
        adapter.answer(case())
    adapter, _ = scoped_dispatcher([completed_turn(state="failed")])
    with pytest.raises(DispatcherDenied):
        adapter.answer(case())
    adapter, _ = dispatcher(["not json"])
    with pytest.raises(DispatcherError, match="invalid JSON"):
        adapter.answer(case())
    adapter, _ = dispatcher([42])
    with pytest.raises(DispatcherError, match="not an object"):
        adapter.answer(case())
    adapter, _ = dispatcher([{"type": "ui_render", "target": "chat",
                              "components": [{"type": "text", "text": "x" * 2048}]}],
                            max_message_bytes=1024)
    with pytest.raises(DispatcherError, match="size bound"):
        adapter.answer(case())


def test_answer_marks_committed_tool_denial_alerts_without_false_positives():
    adapter, _ = scoped_dispatcher([
        committed_answer(canvas=[{"type": "alert", "message": "Tool is restricted for this agent."}]),
        completed_turn(),
    ])
    assert adapter.answer(case()).tool_denials
    adapter, _ = scoped_dispatcher([
        committed_answer(text="The note is not authorized for sharing."), completed_turn(),
    ])
    assert adapter.answer(case()).tool_denials == ()


def test_answer_enforces_the_frame_budget():
    frames = [{"type": "ui_render", "target": "chat",
               "components": [{"type": "text", "text": "Fact."}]}] * 20
    adapter, _ = dispatcher(frames, max_frames=2)
    with pytest.raises(DispatcherError, match="terminal"):
        adapter.answer(case())


def test_create_chat_refuses_denial_and_missing_identity():
    adapter, _ = dispatcher([], http=lambda *_a, **_k: Response(403, {}))
    with pytest.raises(DispatcherDenied):
        adapter.create_chat()
    adapter, _ = dispatcher([], http=lambda *_a, **_k: Response(200, {"other": 1}))
    with pytest.raises(DispatcherError, match="no chat identity"):
        adapter.create_chat()
    adapter, _ = dispatcher([], http=lambda *_a, **_k: Response(500, b"nope"))
    with pytest.raises(DispatcherError, match="status 500"):
        adapter.create_chat()


def test_probe_foreign_chat_reports_denial_and_leakage():
    adapter, _ = dispatcher([], http=lambda *_a, **_k: Response(403, {}))
    assert adapter.probe_foreign_chat("chat/1") == IsolationProbe(403, True, "foreign chat is not readable")
    adapter, _ = dispatcher([], http=lambda *_a, **_k: Response(200, {"id": "chat"}))
    assert adapter.probe_foreign_chat("chat/1").denied is False
    adapter, _ = dispatcher([], http=lambda *_a, **_k: Response(401, {}))
    with pytest.raises(DispatcherDenied):
        adapter.probe_foreign_chat("chat/1")
    adapter, _ = dispatcher([], http=lambda *_a, **_k: Response(500, {}))
    with pytest.raises(DispatcherError, match="status 500"):
        adapter.probe_foreign_chat("chat/1")
    adapter, _ = dispatcher([])
    with pytest.raises(ValueError, match="chat_id"):
        adapter.probe_foreign_chat("")


def test_dispatched_answer_and_producer_contract():
    with pytest.raises(ValueError, match="non-empty"):
        DispatchedAnswer("", "chat")
    with pytest.raises(ValueError, match="chat identity"):
        DispatchedAnswer("text", "")
    with pytest.raises(ValueError, match="status"):
        IsolationProbe(True, True, "x")

    class Adapter:
        identity = "adapter"

        def answer(self, _case):
            return DispatchedAnswer("Answer.", "chat-1")

        def probe_foreign_chat(self, _chat):
            return IsolationProbe(403, True, "denied")

    class AuditedAdapter(Adapter):
        def answer(self, _case):
            return DispatchedAnswer("Answer.", "chat-1", audit_event_ids=("audit-1",))

    answer = DispatcherProducer(AuditedAdapter()).produce(case())
    assert answer.text == "Answer."
    assert answer.source.value == "astral_dispatcher"
    assert answer.audit_event_ids == ("audit-1",)


def test_sync_transport_maps_timeouts_and_closures():
    class Connection:
        def __init__(self, failure):
            self.failure = failure
            self.sent = []
            self.closed = False

        def send(self, text):
            self.sent.append(text)

        def recv(self, timeout=None):
            raise self.failure

        def close(self):
            self.closed = True

    connection = Connection(TimeoutError())
    transport = _SyncWebsocketTransport(connection)
    with pytest.raises(DispatcherTimeout):
        transport.recv(1)
    connection = Connection(RuntimeError("close"))
    transport = _SyncWebsocketTransport(connection)
    transport.close()
    assert connection.closed


def test_http_request_success_http_error_and_transport_error(monkeypatch):
    class Opened:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"chat_id": "c"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: Opened())
    response = _http_request("POST", "http://x/api/chats", {}, b"{}")
    assert response.status == 201
    assert response.body == b'{"chat_id": "c"}'

    def http_error(*_a, **_k):
        raise urllib.error.HTTPError("http://x", 403, "forbidden", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr("urllib.request.urlopen", http_error)
    assert _http_request("GET", "http://x", {}, None).status == 403

    def url_error(*_a, **_k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", url_error)
    with pytest.raises(DispatcherError) as caught:
        _http_request("GET", "http://x", {}, None)
    assert caught.value.retryable is True


def test_default_transport_uses_the_lazy_websocket_client(monkeypatch):
    from websockets.sync import client as sync_client

    connection = ScopedTransport([committed_answer(), completed_turn()])
    monkeypatch.setattr(sync_client, "connect", lambda *_a, **_k: connection)
    adapter, _ = dispatcher([])
    adapter._transport_factory = adapter._default_transport
    assert adapter.answer(case()).text == "Final answer."


def test_dispatched_answer_rejects_oversized_text_and_empty_audit_ids():
    with pytest.raises(ValueError, match="size bound"):
        DispatchedAnswer("x" * (64 * 1024 + 1), "chat")
    with pytest.raises(ValueError, match="audit ids"):
        DispatchedAnswer("text", "chat", audit_event_ids=("",))


def test_prompt_rejects_an_empty_conversation():
    with pytest.raises(ValueError, match="empty"):
        render_dispatcher_prompt(SimpleNamespace(conversation=()))


def test_create_chat_rejects_non_mapping_oversized_and_invalid_bodies():
    adapter, _ = dispatcher([], http=lambda *_a, **_k: Response(200, b"[]"))
    with pytest.raises(DispatcherError, match="no mapping"):
        adapter.create_chat()
    adapter, _ = dispatcher([], http=lambda *_a, **_k: Response(200, b"x" * (1024 * 1024 + 1)))
    with pytest.raises(DispatcherError, match="size bound"):
        adapter.create_chat()
    adapter, _ = dispatcher([], http=lambda *_a, **_k: Response(200, b"{"))
    with pytest.raises(DispatcherError, match="valid JSON"):
        adapter.create_chat()


def test_answer_accepts_byte_frames_with_committed_text():
    snapshot = committed_answer()
    adapter, _ = scoped_dispatcher([
        lambda request: json.dumps(snapshot(request)).encode(), completed_turn(),
    ])
    assert adapter.answer(case()).text == "Final answer."


def test_answer_refuses_non_text_frames():
    class RawTransport(FakeTransport):
        def recv(self, timeout):
            return 42

    adapter, _ = dispatcher([], transport_factory=lambda: RawTransport([]))
    with pytest.raises(DispatcherError, match="non-text"):
        adapter.answer(case())


def test_answer_rejects_snapshot_without_final_assistant():
    frame = committed_answer()(dict(payload={"chat_id": "chat-1"},
        connection_generation="c", request_generation="r"))
    frame["transcript"] = frame["transcript"][:1]
    adapter, _ = scoped_dispatcher([scoped_frame(**{key: value for key, value in frame.items()
        if key not in {"chat_id", "request_generation", "connection_generation"}})])
    with pytest.raises(DispatcherError, match="final assistant"):
        adapter.answer(case())


def test_denial_text_falls_back_when_no_message_is_present():
    adapter, _ = scoped_dispatcher([scoped_frame(type="error")])
    with pytest.raises(DispatcherDenied) as caught:
        adapter.answer(case())
    assert caught.value.detail == "dispatcher refused the turn"


def test_sync_transport_maps_a_closed_connection(monkeypatch):
    class ConnectionClosed(Exception):
        pass

    class Connection:
        def send(self, _text):
            return None

        def recv(self, timeout=None):
            raise ConnectionClosed()

        def close(self):
            raise RuntimeError("close failed")

    import websockets.exceptions as exceptions

    monkeypatch.setattr(exceptions, "ConnectionClosed", ConnectionClosed)
    transport = _SyncWebsocketTransport(Connection())
    with pytest.raises(DispatcherError, match="closed"):
        transport.recv(1)
    transport.close()


def scoped_frame(**fields):
    def build(request):
        return {
            "chat_id": request["payload"]["chat_id"],
            "connection_generation": request["connection_generation"],
            "request_generation": request["request_generation"],
            **fields,
        }
    return build


def committed_answer(*, text="Final answer.", canvas=(), **fields):
    return scoped_frame(
        type="conversation_snapshot", schema_version=1,
        snapshot_id="f2b1ba2d-f16f-4d5e-a91f-708984f19381",
        committed_at="2026-10-20T12:00:00Z", snapshot_purpose="commit", render_revision=1,
        transcript=[
            {"message_id": "1", "role": "user", "created_at": "2026-10-20T12:00:00Z",
             "attachments": [], "parts": [{"type": "text", "text": render_dispatcher_prompt(case())}]},
            {"message_id": "2", "role": "assistant", "created_at": "2026-10-20T12:00:01Z",
             "attachments": [], "parts": [{"type": "text", "text": text}]},
        ],
        canvas={"target": "canvas", "components": list(canvas)},
        **fields,
    )


def completed_turn(**fields):
    return scoped_frame(**dict(type="operation_status", action="chat_message", terminal=True,
                               state="completed", surface="chat", sequence=2, phase="completed", label="Completed",
        operation_id="0f919b79-ec24-4a97-b927-27770be54639", retryable=False, error=None,
        retry_after_ms=None, updated_at="2026-10-20T12:00:02Z") | fields)


class ScopedTransport(FakeTransport):
    def __init__(self, frames):
        super().__init__([{"type": "rote_config"}, *frames])

    def recv(self, timeout):
        if self.frames and callable(self.frames[0]):
            self.frames[0] = self.frames[0](self.sent[1])
        return super().recv(timeout)


def scoped_dispatcher(frames, **over):
    transport = ScopedTransport(frames)
    adapter, _ = dispatcher([], transport_factory=lambda: transport, **over)
    return adapter, transport


def test_current_committed_answer_includes_full_canvas_artifact_without_tool_preview():
    full = "Synthetic full clinical answer. " * 100
    adapter, transport = scoped_dispatcher([
        scoped_frame(type="ui_render", target="chat", components=[
            {"type": "text", "content": "Tool preview that must be excluded."}]),
        committed_answer(text="Brief lead.", canvas=[
            {"type": "card", "component_id": "final-doc", "title": "Final document",
             "content": [{"type": "text", "content": full}]},
            {"type": "card", "_source_tool": "lookup", "content": [
                {"type": "text", "content": "Tool material that must be excluded."}]},
        ]),
        completed_turn(),
    ])
    answer = adapter.answer(case())
    assert full.strip() in answer.text
    assert "Brief lead." in answer.text
    assert "Tool preview" not in answer.text
    assert "Tool material" not in answer.text
    assert transport.closed


@pytest.mark.parametrize("field,value", [
    ("chat_id", "foreign-chat"), ("request_generation", "stale-request"),
    ("connection_generation", "stale-connection"),
])
def test_stale_snapshot_and_terminal_cannot_complete_current_request(field, value):
    adapter, _ = scoped_dispatcher([
        committed_answer(text="Stale answer.", **{field: value}),
        completed_turn(**{field: value}),
        {"type": "chat_status", "status": "done"},
        committed_answer(text="Current final answer."), completed_turn(),
    ])
    answer = adapter.answer(case())
    assert answer.text == "Current final answer."


def test_uncommitted_chat_lead_cannot_be_scored_as_final_answer():
    adapter, transport = scoped_dispatcher([
        scoped_frame(type="ui_render", target="chat", components=[
            {"type": "text", "content": "Incomplete lead."}]), completed_turn(),
    ])
    with pytest.raises(DispatcherError, match="committed"):
        adapter.answer(case())
    assert transport.closed


def test_dispatcher_nested_json_is_bounded_protocol_failure():
    adapter, transport = scoped_dispatcher(["[" * 4001 + "0" + "]" * 4001])
    with pytest.raises(DispatcherError, match="nesting"):
        adapter.answer(case())
    assert transport.closed


@pytest.mark.parametrize("problem", ["messages", "canvas", "foreign", "denied", "unavailable", "shape"])
def test_answer_refuses_nonempty_or_unverified_initial_chat(problem):
    def http(method, url, headers, body):
        if method == "POST":
            return empty_chat_http(method, url, headers, body)
        if problem == "denied":
            return Response(403, {})
        if problem == "unavailable":
            return Response(503, {})
        if problem == "shape":
            return Response(200, [])
        if url.endswith("/components"):
            return Response(200, {"components": [{}] if problem == "canvas" else []})
        return Response(200, {"chat": {"id": "foreign" if problem == "foreign" else "chat-1",
            "messages": [{}] if problem == "messages" else []}})

    adapter, transport = scoped_dispatcher([], http=http)
    with pytest.raises(DispatcherError):
        adapter.answer(case())
    assert transport.sent == []


@pytest.mark.parametrize("state", ["failed", "cancelled", "retryable", "unknown"])
def test_current_terminal_failure_never_scores_committed_answer(state):
    adapter, transport = scoped_dispatcher([committed_answer(), completed_turn(state=state)])
    with pytest.raises(DispatcherDenied):
        adapter.answer(case())
    assert transport.closed


def test_terminal_for_another_action_cannot_complete_the_current_turn():
    adapter, _ = scoped_dispatcher([
        committed_answer(), completed_turn(action="chrome_open"),
        completed_turn(state="failed"),
    ])
    with pytest.raises(DispatcherDenied):
        adapter.answer(case())


def test_prior_hydration_and_wrong_commit_cannot_supply_final_text():
    snapshot = committed_answer()
    def hydration(request):
        return dict(snapshot(request), snapshot_purpose="hydration")
    adapter, _ = scoped_dispatcher([hydration,
        committed_answer(request_generation="another-request"), completed_turn()])
    with pytest.raises(DispatcherError, match="committed"):
        adapter.answer(case())


def test_snapshot_requires_exact_current_user_prompt_and_only_one_request():
    for problem in ("different-prompt", "multiple-users", "missing-assistant", "invalid-message"):
        snapshot = committed_answer()
        def invalid(request):
            frame = snapshot(request)
            if problem == "different-prompt":
                frame["transcript"][0]["parts"][0]["text"] = "Foreign request."
            elif problem == "multiple-users":
                frame["transcript"].insert(0, {"role": "user", "parts": []})
            elif problem == "missing-assistant":
                frame["transcript"][-1]["role"] = "tool"
            else:
                frame["transcript"].append(42)
            return frame
        adapter, _ = scoped_dispatcher([invalid, completed_turn()])
        with pytest.raises(DispatcherError, match="transcript"):
            adapter.answer(case())


@pytest.mark.parametrize("change", ["revision", "transcript", "canvas", "target", "parts", "part", "structured"])
def test_malformed_committed_payload_is_an_infrastructure_failure(change):
    snapshot = committed_answer()
    def invalid(request):
        frame = snapshot(request)
        if change == "revision":
            frame["render_revision"] = True
        elif change in ("transcript", "canvas"):
            frame[change] = None
        elif change == "target":
            frame["canvas"]["target"] = "foreign"
        elif change == "parts":
            frame["transcript"][-1]["parts"] = None
        elif change == "part":
            frame["transcript"][-1]["parts"] = [42]
        else:
            frame["transcript"][-1]["parts"] = [{"type": "structured", "plain_text": "Ambiguous"}]
        return frame
    adapter, transport = scoped_dispatcher([invalid, completed_turn()])
    with pytest.raises(DispatcherError):
        adapter.answer(case())
    assert transport.closed


def test_ambiguous_multiple_commit_snapshots_are_rejected():
    adapter, _ = scoped_dispatcher([committed_answer(), committed_answer(text="Other"), completed_turn()])
    with pytest.raises(DispatcherError, match="ambiguous"):
        adapter.answer(case())


def test_complete_structured_answer_keeps_nested_content_and_excludes_provenance_subtrees():
    adapter, _ = scoped_dispatcher([
        committed_answer(canvas=[{"type": "card", "title": "Assessment", "content": [
            {"type": "text", "content": "Full finding."},
            {"type": "container", "children": [{"type": "text", "content": "Safety detail."}]},
            {"type": "text", "_source_correlation_id": "tool-call", "content": "Tool preview."},
            {"type": "card", "_source_agent": "tool", "content": [{"type": "text", "content": "Tool descendant."}]},
            {"type": "text", "intermediate": True, "content": "Intermediate."},
            {"type": "text", "preview": True, "content": "Preview."},
            {"type": "text", "phase": "intermediate", "content": "Intermediate phase."},
            {"type": "text", "hidden": True, "content": "Hidden."},
            {"type": "button", "label": "Action"},
        ]}]), completed_turn(),
    ])
    assert adapter.answer(case()).text == "Final answer.\nAssessment\nFull finding.\nSafety detail."


def test_promoted_document_identity_must_belong_to_the_current_chat():
    import hashlib
    identity = "doc_" + hashlib.sha1(b"chat-1|Document").hexdigest()[:12]
    document = {"type": "card", "component_id": identity, "title": "Document",
                "content": [{"type": "text", "content": "Full promoted answer."}]}
    adapter, _ = scoped_dispatcher([committed_answer(canvas=[document]), completed_turn()])
    assert "Full promoted answer." in adapter.answer(case()).text
    for changed in ({"title": "Other document"}, {"type": "text"}, {"title": None}):
        adapter, _ = scoped_dispatcher([committed_answer(canvas=[dict(document, **changed)]), completed_turn()])
        with pytest.raises(DispatcherError):
            adapter.answer(case())


@pytest.mark.parametrize("component", [
    1, {"type": "unknown", "text": "Unsupported"},
    {"type": "text", "content": {}}, {"type": "text", "title": []},
])
def test_unsupported_or_invalid_final_components_fail_closed(component):
    adapter, _ = scoped_dispatcher([committed_answer(canvas=[component]), completed_turn()])
    with pytest.raises(DispatcherError):
        adapter.answer(case())


def test_aggregate_final_answer_limit_includes_canvas_text():
    adapter, _ = scoped_dispatcher([
        committed_answer(text="x" * 32768, canvas=[{"type": "text", "content": "y" * 32768}]),
        completed_turn(),
    ])
    with pytest.raises(DispatcherError, match="size bound"):
        adapter.answer(case())


def test_decodable_excessive_nesting_and_node_count_are_rejected():
    for raw in ("[" * 70 + "0" + "]" * 70, json.dumps([0] * 20001)):
        adapter, transport = scoped_dispatcher([raw])
        with pytest.raises(DispatcherError, match="bound"):
            adapter.answer(case())
        assert transport.closed


def test_nested_http_json_is_a_protocol_failure():
    adapter, _ = dispatcher([], http=lambda *_args: Response(200, b"[" * 4001 + b"0" + b"]" * 4001))
    with pytest.raises(DispatcherError, match="nesting"):
        adapter.create_chat()


@pytest.mark.parametrize("kind", ["auth_error", "auth_required", "submission_rejected"])
def test_authentication_or_current_submission_denial_closes_transport(kind):
    def denied(request):
        return {"type": kind, "submission_id": request["submission_id"]}
    adapter, transport = scoped_dispatcher([denied])
    with pytest.raises(DispatcherDenied):
        adapter.answer(case())
    assert transport.closed


def test_audit_events_match_the_new_owned_chat_even_without_ui_frame_correlation():
    adapter, _ = scoped_dispatcher([
        {"type": "audit_append", "event": {"event_id": "foreign", "conversation_id": "foreign-chat"}},
        {"type": "audit_append", "event": {"event_id": "current", "conversation_id": "chat-1"}},
        committed_answer(), completed_turn(),
    ])
    assert adapter.answer(case()).audit_event_ids == ("current",)


def test_correlated_tool_denial_preview_is_recorded_but_not_graded_as_answer():
    adapter, _ = scoped_dispatcher([
        scoped_frame(type="ui_render", target="chat", components=[
            {"type": "alert", "message": "Tool permission denied during preview."}]),
        committed_answer(), completed_turn(),
    ])
    answer = adapter.answer(case())
    assert answer.text == "Final answer."
    assert answer.tool_denials == ("permission denied",)


def test_invalid_utf8_dispatcher_frame_is_a_protocol_failure():
    adapter, transport = scoped_dispatcher([b"\xff"])
    with pytest.raises(DispatcherError, match="UTF-8"):
        adapter.answer(case())
    assert transport.closed


def test_deadline_and_response_byte_limits_close_the_transport():
    now = [0]
    adapter, transport = scoped_dispatcher([], timeout_seconds=1, clock=lambda: now[0])
    original_send = transport.send

    def send(text):
        original_send(text)
        if len(transport.sent) == 2:
            now[0] = 2

    transport.send = send
    with pytest.raises(DispatcherTimeout):
        adapter.answer(case())
    assert transport.closed
    adapter, transport = scoped_dispatcher([json.dumps({"type": "ignored", "data": "x" * 900000})] * 5)
    with pytest.raises(DispatcherError, match="response exceeds"):
        adapter.answer(case())
    assert transport.closed


def test_final_assistant_component_part_retains_its_nested_answer_text():
    snapshot = committed_answer()
    def structured(request):
        frame = snapshot(request)
        frame["transcript"][-1]["parts"] = [{"type": "components", "components": [
            {"type": "card", "title": "Final assessment", "content": [
                {"type": "text", "content": "Complete structured conclusion."}]}]}]
        return frame
    adapter, _ = scoped_dispatcher([structured, completed_turn()])
    assert adapter.answer(case()).text == "Final assessment\nComplete structured conclusion."


def test_collected_card_text_matches_visible_renderer_fields_and_child_branch():
    from webrender.renderer import render_one

    document = {"type": "card", "title": "Visible title.", "label": "Unused label.",
        "text": "Unused card text.", "message": "Unused card message.",
        "children": [{"type": "text", "content": "Visible child.", "text": "Unused text alias.",
            "children": [{"type": "text", "content": "Unused text descendant."}]}],
        "content": [{"type": "text", "content": "Shadowed content."}],
        "components": [{"type": "text", "content": "Unused components."}],
        "items": [{"type": "text", "content": "Unused items."}]}
    html = render_one(document)
    assert "Visible title." in html and "Visible child." in html
    assert "Unused" not in html and "Shadowed" not in html
    adapter, _ = scoped_dispatcher([committed_answer(canvas=[document]), completed_turn()])
    assert adapter.answer(case()).text == "Final answer.\nVisible title.\nVisible child."


@pytest.mark.parametrize("kind", ["card", "container"])
def test_empty_children_fall_back_to_visible_content_without_container_labels(kind):
    component = {"type": kind, "title": "Title.", "label": "Unused label.",
        "children": [], "content": [{"type": "text", "content": "Visible fallback."}]}
    adapter, _ = scoped_dispatcher([committed_answer(canvas=[component]), completed_turn()])
    expected = "Final answer.\nTitle.\nVisible fallback." if kind == "card" else "Final answer.\nVisible fallback."
    assert adapter.answer(case()).text == expected


def test_alert_collects_only_visible_title_and_message():
    adapter, _ = scoped_dispatcher([committed_answer(canvas=[{
        "type": "alert", "title": "Visible alert.", "message": "Visible message.",
        "content": "Unused content.", "label": "Unused label.",
        "children": [{"type": "text", "content": "Unused alert descendant."}],
    }]), completed_turn()])
    assert adapter.answer(case()).text == "Final answer.\nVisible alert.\nVisible message."


@pytest.mark.parametrize("kind", ["markdown", "row", "column", "notification"])
def test_unrendered_answer_component_vocabulary_fails_closed(kind):
    adapter, transport = scoped_dispatcher([
        committed_answer(canvas=[{"type": kind, "content": "Invisible answer."}]), completed_turn()])
    with pytest.raises(DispatcherError, match="unsupported"):
        adapter.answer(case())
    assert transport.closed


@pytest.mark.parametrize("identities", [
    {"id": "ordinary-author-id", "component_id": "doc_foreign_invalid"},
    {"id": "doc_0957faa0c8a7", "component_id": "doc_foreign_invalid"},
])
def test_every_present_promoted_identity_must_match_the_current_chat(identities):
    adapter, transport = scoped_dispatcher([committed_answer(canvas=[{
        "type": "card", **identities, "title": "Document",
        "content": [{"type": "text", "content": "Synthetic promoted body."}],
    }]), completed_turn()])
    with pytest.raises(DispatcherError, match="identity"):
        adapter.answer(case())
    assert transport.closed


def test_promoted_document_keeps_server_id_and_semantic_component_id():
    import hashlib

    identity = "doc_" + hashlib.sha1(b"chat-1|Document").hexdigest()[:12]
    adapter, _ = scoped_dispatcher([committed_answer(canvas=[{
        "type": "card", "id": identity, "component_id": "au_" + identity, "title": "Document",
        "content": [{"type": "text", "content": "Full promoted answer."}],
    }]), completed_turn()])
    assert adapter.answer(case()).text == "Final answer.\nDocument\nFull promoted answer."


@pytest.mark.parametrize("kind", [[], {}])
def test_malformed_component_type_is_a_protocol_failure(kind):
    adapter, transport = scoped_dispatcher([
        committed_answer(canvas=[{"type": kind, "content": "Invalid type."}]), completed_turn()])
    with pytest.raises(DispatcherError, match="unsupported"):
        adapter.answer(case())
    assert transport.closed


@pytest.mark.parametrize("children", ["Unstructured body.", {"type": "text", "content": "Unlisted child."}])
def test_unstructured_selected_children_fail_closed(children):
    adapter, transport = scoped_dispatcher([
        committed_answer(canvas=[{"type": "card", "children": children}]), completed_turn()])
    with pytest.raises(DispatcherError, match="children"):
        adapter.answer(case())
    assert transport.closed
