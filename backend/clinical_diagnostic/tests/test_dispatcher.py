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


def dispatcher(frames, http=None, **over):
    transport = FakeTransport(frames)
    fields = dict(
        token="secret-token",
        ws_uri="ws://127.0.0.1:8001/ws",
        http_uri="http://127.0.0.1:8001",
        transport_factory=lambda: transport,
        http=http or (lambda *_a, **_k: Response(201, {"chat_id": "chat-1"})),
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


def test_answer_registers_chats_and_collects_visible_components():
    frames = [
        {"type": "audit_append", "event": {"event_id": "audit-1"}},
        {"type": "ui_render", "target": "chat", "components": [
            {"type": "text", "text": "Required fact."},
            {"type": "card", "title": "Safety action.", "components": [
                {"type": "text", "content": "Cannot determine remotely."}
            ]},
        ]},
        {"type": "operation_status", "terminal": True, "state": "completed"},
    ]
    adapter, transport = dispatcher(frames)
    answer = adapter.answer(case())
    assert "Required fact." in answer.text
    assert "Safety action." in answer.text
    assert "Cannot determine remotely." in answer.text
    assert answer.chat_id == "chat-1"
    assert answer.frame_types == ("audit_append", "ui_render", "operation_status")
    assert answer.audit_event_ids == ("audit-1",)
    assert transport.sent[0]["type"] == "register_ui"
    assert transport.sent[0]["token"] == "secret-token"
    assert transport.sent[1]["action"] == "chat_message"
    assert transport.sent[1]["payload"]["chat_id"] == "chat-1"
    assert transport.closed


def test_answer_falls_back_to_rendered_html_and_assistant_messages():
    frames = [
        {"type": "ui_render", "target": "history", "html": "<p>History fact.</p>"},
        {"type": "assistant_message", "role": "assistant", "content": "Assistant fact."},
        {"type": "turn_complete"},
    ]
    adapter, _ = dispatcher(frames)
    answer = adapter.answer(case())
    assert "History fact." in answer.text
    assert "Assistant fact." in answer.text


def test_answer_accepts_chat_status_terminal_frames():
    frames = [
        {"type": "ui_render", "target": "chat", "components": [
            {"type": "text", "text": "Fact."}
        ]},
        {"type": "chat_status", "status": "done"},
    ]
    adapter, _ = dispatcher(frames)
    assert "Fact." in adapter.answer(case()).text
    adapter, _ = dispatcher([
        {"type": "ui_render", "target": "chat", "components": [
            {"type": "text", "text": "Fact."}
        ]},
        {"type": "chat_status", "status": "failed"},
    ])
    with pytest.raises(DispatcherDenied):
        adapter.answer(case())


def test_answer_refuses_missing_terminal_and_missing_text():
    adapter, _ = dispatcher([{"type": "ui_render", "target": "chat",
                              "components": [{"type": "text", "text": "Fact."}]}],
                            max_frames=1)
    with pytest.raises(DispatcherError, match="terminal"):
        adapter.answer(case())
    adapter, _ = dispatcher([{"type": "operation_status", "terminal": True}])
    with pytest.raises(DispatcherError) as caught:
        adapter.answer(case())
    assert caught.value.code is FailureCode.MALFORMED_ANSWER


def test_answer_refuses_denials_failed_terminals_and_bad_frames():
    adapter, _ = dispatcher([{"type": "error", "message": "authentication required"}])
    with pytest.raises(DispatcherDenied):
        adapter.answer(case())
    adapter, _ = dispatcher([{"type": "operation_status", "terminal": True,
                              "state": "failed"}])
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


def test_answer_marks_tool_denial_alerts_without_false_positives():
    frames = [
        {"type": "ui_render", "target": "chat", "components": [
            {"type": "alert", "message": "Tool 'x' is restricted for this agent."}
        ]},
        {"type": "operation_status", "terminal": True},
    ]
    adapter, _ = dispatcher(frames)
    answer = adapter.answer(case())
    assert answer.tool_denials
    frames = [
        {"type": "ui_render", "target": "chat", "components": [
            {"type": "text", "text": "The note is not authorized for sharing."}
        ]},
        {"type": "operation_status", "terminal": True},
    ]
    adapter, _ = dispatcher(frames)
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

    class Connection:
        def __init__(self):
            self.frames = [
                '{"type": "ui_render", "target": "chat", '
                '"components": [{"type": "text", "text": "Fact."}]}',
                '{"type": "operation_status", "terminal": true}',
            ]

        def send(self, _text):
            return None

        def recv(self, timeout=None):
            return self.frames.pop(0)

        def close(self):
            return None

    monkeypatch.setattr(sync_client, "connect", lambda *_a, **_k: Connection())
    adapter, _ = dispatcher([])
    adapter._transport_factory = adapter._default_transport
    adapter._http = lambda *_a, **_k: Response(201, {"chat_id": "chat-1"})
    answer = adapter.answer(case())
    assert answer.chat_id == "chat-1"

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


def test_answer_accepts_byte_frames_and_string_components():
    frames = [
        json.dumps({"type": "ui_render", "target": "chat", "components": "Text fact."}).encode(),
        {"type": "operation_status", "terminal": True},
    ]
    adapter, _ = dispatcher(frames)
    assert adapter.answer(case()).text == "Text fact."


def test_answer_refuses_non_text_frames():
    class RawTransport(FakeTransport):
        def recv(self, timeout):
            return 42

    adapter, _ = dispatcher([], transport_factory=lambda: RawTransport([]))
    with pytest.raises(DispatcherError, match="non-text"):
        adapter.answer(case())


def test_answer_ignores_non_assistant_message_roles():
    adapter, _ = dispatcher([
        {"type": "assistant_message", "role": "user", "content": "ignored"},
        {"type": "operation_status", "terminal": True},
    ])
    with pytest.raises(DispatcherError, match="without visible answer"):
        adapter.answer(case())


def test_denial_text_falls_back_when_no_message_is_present():
    adapter, _ = dispatcher([{"type": "error"}])
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
