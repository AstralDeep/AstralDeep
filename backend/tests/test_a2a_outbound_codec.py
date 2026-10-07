"""Outbound A2A protocol negotiation tests: version selection, envelope building, and
response decoding in shared/a2a_codec.py, the v1 protocol_version advertised by card
builders, and orchestrator._execute_via_a2a against a strict v1 a2a-sdk server and a
v0.3 fixture."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a2a.server.agent_execution import AgentExecutor
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (
    AgentCard as A2AAgentCard,
    AgentCapabilities,
    AgentInterface,
    Message as A2AMessage,
    Part,
    Role,
)

from orchestrator.a2a_orchestrator_executor import build_orchestrator_a2a_card
from orchestrator.orchestrator import Orchestrator
from shared.a2a_bridge import (
    custom_card_to_a2a,
    ensure_task_created,
    make_data_part,
)
from shared.a2a_codec import (
    A2A_PROTOCOL_V03,
    A2A_PROTOCOL_V1,
    A2A_VERSION_HEADER,
    UnsupportedProtocolVersion,
    advertised_protocol_versions,
    build_send_headers,
    build_send_payload,
    decode_send_result,
    select_outbound_version,
)
from shared.protocol import (
    MCP_PROTOCOL_VERSION,
    AgentCard as CustomAgentCard,
    AgentSkill as CustomAgentSkill,
)

AGENT_ID = "strict-peer-1"
BASE_URL = "http://peer.test"
LETS_CAPABILITY = {"astraldeep.lets/v1": {"permit": "p1"}}


def _iface_card(versions):
    return SimpleNamespace(
        supported_interfaces=[
            SimpleNamespace(protocol_version=v, url=f"{BASE_URL}/a2a") for v in versions
        ]
    )


def _orch(card="absent"):
    o = Orchestrator.__new__(Orchestrator)
    o.agents = {}
    o.agent_urls = {}
    o.a2a_clients = {AGENT_ID: BASE_URL}
    o.a2a_agent_cards = {} if card == "absent" else {AGENT_ID: card}
    return o


class _Recorder:
    def __init__(self, app):
        self.app = app
        self.requests = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        self.requests.append(
            {
                "path": scope["path"],
                "headers": {
                    k.decode().lower(): v.decode() for k, v in scope["headers"]
                },
                "body": json.loads(body) if body else None,
            }
        )
        consumed = {"done": False}

        async def replay():
            if consumed["done"]:
                return {"type": "http.disconnect"}
            consumed["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


class _MessageExecutor(AgentExecutor):
    async def execute(self, context: object, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(
            A2AMessage(
                message_id="resp-msg-1",
                role=Role.ROLE_AGENT,
                parts=[Part(text="echo-reply")],
            )
        )

    async def cancel(self, context: object, event_queue: EventQueue) -> None:
        pass


class _TaskExecutor(AgentExecutor):
    async def execute(self, context: object, event_queue: EventQueue) -> None:
        await ensure_task_created(context, event_queue)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        await updater.add_artifact([make_data_part({"answer": 42})], name="result")
        await updater.complete()

    async def cancel(self, context: object, event_queue: EventQueue) -> None:
        pass


def _strict_v1_app(executor) -> _Recorder:
    card = A2AAgentCard(
        name="StrictPeer",
        description="strict v1 fixture",
        version="1.0.0",
        capabilities=AgentCapabilities(),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[],
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=f"{BASE_URL}/a2a",
                protocol_version=A2A_PROTOCOL_V1,
            )
        ],
    )
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI()
    for route in create_jsonrpc_routes(
        handler, rpc_url="/a2a", enable_v0_3_compat=False
    ):
        app.router.routes.append(route)
    return _Recorder(app)


def _v03_fixture_app() -> _Recorder:
    app = FastAPI()
    recorder = _Recorder(app)

    @app.post("/a2a")
    async def endpoint(request: Request):
        body = await request.json()
        recorder.requests.append(
            {
                "path": "/a2a",
                "headers": {
                    k: v
                    for k, v in request.headers.items()
                    if k.lower() != "content-type"
                },
                "body": body,
            }
        )
        if body.get("method") != "message/send":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "id": "task-1",
                    "contextId": "ctx-1",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [
                        {"name": "result", "parts": [{"data": {"legacy": True}}]}
                    ],
                },
            }
        )

    return recorder


def _patch_asgi(monkeypatch, recorder):
    real_client = httpx.AsyncClient

    def _factory(*a, **kw):
        kw["transport"] = httpx.ASGITransport(app=recorder)
        return real_client(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)


def _outbound_data_part(request_body: dict) -> dict:
    message = request_body["params"]["message"]
    for part in message["parts"]:
        if "data" in part:
            return part["data"]
    raise AssertionError("outbound message carried no data part")


async def test_strict_v1_server_message_branch_is_projected(monkeypatch):
    recorder = _strict_v1_app(_MessageExecutor())
    _patch_asgi(monkeypatch, recorder)
    response = await _orch()._execute_via_a2a(
        AGENT_ID,
        "get_weather",
        {"city": "oslo"},
        caller_capabilities=dict(LETS_CAPABILITY),
    )
    assert response.error is None
    assert response.result == "echo-reply"

    request = recorder.requests[0]
    assert request["path"] == "/a2a"
    assert request["headers"][A2A_VERSION_HEADER.lower()] == A2A_PROTOCOL_V1
    assert request["body"]["method"] == "SendMessage"
    assert request["body"]["jsonrpc"] == "2.0"
    data = _outbound_data_part(request["body"])
    assert data["method"] == "tools/call"
    assert data["name"] == "get_weather"
    assert data["protocol_version"] == MCP_PROTOCOL_VERSION
    assert data["caller_capabilities"] == LETS_CAPABILITY


async def test_strict_v1_server_task_branch_is_projected(monkeypatch):
    recorder = _strict_v1_app(_TaskExecutor())
    _patch_asgi(monkeypatch, recorder)
    response = await _orch()._execute_via_a2a(AGENT_ID, "get_weather", {"city": "oslo"})
    assert response.error is None
    assert response.result == {"answer": 42}


async def test_strict_v1_server_rejects_legacy_envelope_and_missing_header():
    recorder = _strict_v1_app(_MessageExecutor())
    message = {
        "message_id": "m-1",
        "role": "ROLE_USER",
        "parts": [{"data": {"method": "tools/call", "name": "t", "arguments": {}}}],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=recorder), base_url=BASE_URL
    ) as client:
        legacy = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/send",
                "params": {"message": message},
            },
        )
        assert legacy.json()["error"]["code"] == -32601

        unversioned = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "method": "SendMessage",
                "params": {"message": message},
            },
        )
        assert unversioned.json()["error"]["code"] == -32009

        versioned = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "3",
                "method": "SendMessage",
                "params": {"message": message},
            },
            headers={A2A_VERSION_HEADER: A2A_PROTOCOL_V1},
        )
        assert versioned.json()["result"]["message"]["parts"][0]["text"] == "echo-reply"


async def test_v03_fixture_is_served_only_through_the_compatibility_adapter(
    monkeypatch,
):
    recorder = _v03_fixture_app()
    _patch_asgi(monkeypatch, recorder)
    response = await _orch(_iface_card([A2A_PROTOCOL_V03]))._execute_via_a2a(
        AGENT_ID, "legacy_tool", {"q": 1}
    )
    assert response.error is None
    assert response.result == {"legacy": True}

    request = recorder.requests[0]
    assert request["body"]["method"] == "message/send"
    assert A2A_VERSION_HEADER.lower() not in request["headers"]


async def test_unsupported_advertised_version_fails_visibly(monkeypatch):
    called = {"http": False}
    real_client = httpx.AsyncClient

    def _tripwire(*a, **kw):
        called["http"] = True
        return real_client(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _tripwire)
    response = await _orch(_iface_card(["2.0"]))._execute_via_a2a(
        AGENT_ID, "any_tool", {}
    )
    assert called["http"] is False
    assert response.error is not None
    assert response.error["retryable"] is False
    assert "2.0" in response.error["message"]


class _FixedPayloadClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        return _FixedResponse(self._payload)


class _FixedResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _patch_fixed_payload(monkeypatch, payload):
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: _FixedPayloadClient(payload)
    )


async def test_uninterpretable_v1_result_fails_visibly(monkeypatch):
    _patch_fixed_payload(
        monkeypatch, {"jsonrpc": "2.0", "id": "r", "result": {"surprise": True}}
    )
    response = await _orch()._execute_via_a2a(AGENT_ID, "any_tool", {})
    assert response.error is not None
    assert response.error["retryable"] is False
    assert "Task or Message" in response.error["message"]


async def test_missing_result_is_reported_as_retryable(monkeypatch):
    _patch_fixed_payload(monkeypatch, {"jsonrpc": "2.0", "id": "r", "result": None})
    response = await _orch()._execute_via_a2a(AGENT_ID, "any_tool", {})
    assert response.error is not None
    assert response.error["retryable"] is True
    assert "No response" in response.error["message"]


async def test_version_mismatch_surfaces_the_peer_json_rpc_error(monkeypatch):
    recorder = _strict_v1_app(_MessageExecutor())
    _patch_asgi(monkeypatch, recorder)
    response = await _orch(_iface_card([A2A_PROTOCOL_V03]))._execute_via_a2a(
        AGENT_ID, "any_tool", {}
    )
    assert response.error is not None
    assert response.error["retryable"] is False
    assert "Method not found" in response.error["message"]


def test_card_emitters_advertise_protocol_version_1_0():
    card = CustomAgentCard(
        name="Bundled",
        description="d",
        agent_id="bundled-1",
        version="1.0.0",
        skills=[
            CustomAgentSkill(
                name="t", description="d", id="t", tags=[], scope="tools:read"
            )
        ],
        metadata={},
    )
    a2a_card = custom_card_to_a2a(card, f"{BASE_URL}/a2a")
    assert a2a_card.supported_interfaces[0].protocol_version == A2A_PROTOCOL_V1
    wire = MessageToDict(a2a_card.supported_interfaces[0])
    assert wire["protocolVersion"] == A2A_PROTOCOL_V1

    orchestrator = SimpleNamespace(agents={}, local_agents={}, agent_cards={})
    orchestrator_card = build_orchestrator_a2a_card(orchestrator)
    assert orchestrator_card.supported_interfaces[0].protocol_version == A2A_PROTOCOL_V1


def test_version_selection_follows_advertised_support():
    assert select_outbound_version(None) == A2A_PROTOCOL_V1
    assert select_outbound_version(_iface_card([])) == A2A_PROTOCOL_V1
    assert select_outbound_version(_iface_card([A2A_PROTOCOL_V1])) == A2A_PROTOCOL_V1
    assert select_outbound_version(_iface_card([A2A_PROTOCOL_V03])) == A2A_PROTOCOL_V03
    assert (
        select_outbound_version(_iface_card([A2A_PROTOCOL_V03, A2A_PROTOCOL_V1]))
        == A2A_PROTOCOL_V1
    )
    assert advertised_protocol_versions(_iface_card(["2.0", A2A_PROTOCOL_V1])) == [
        "2.0",
        A2A_PROTOCOL_V1,
    ]
    with pytest.raises(UnsupportedProtocolVersion):
        select_outbound_version(_iface_card(["2.0"]))


def test_request_version_method_and_decoder_agree():
    for version in (A2A_PROTOCOL_V1, A2A_PROTOCOL_V03):
        headers = build_send_headers(version)
        assert select_outbound_version(_iface_card([version])) == version
        payload = build_send_payload({"message_id": "m"}, "req-1", version)
        if version == A2A_PROTOCOL_V1:
            assert headers == {A2A_VERSION_HEADER: A2A_PROTOCOL_V1}
            assert payload["method"] == "SendMessage"
            result = {
                "message": {
                    "message_id": "m2",
                    "role": "ROLE_AGENT",
                    "parts": [{"text": "ok"}],
                }
            }
        else:
            assert headers == {}
            assert payload["method"] == "message/send"
            result = {
                "message_id": "m2",
                "role": "ROLE_AGENT",
                "parts": [{"text": "ok"}],
            }
        decoded = decode_send_result(result, version)
        assert decoded.parts[0].text == "ok"
        with pytest.raises(UnsupportedProtocolVersion):
            build_send_payload({"m": 1}, "r", "9.9")
        with pytest.raises(UnsupportedProtocolVersion):
            build_send_headers("9.9")
        with pytest.raises(UnsupportedProtocolVersion):
            decode_send_result({"task": {}}, "9.9")


def test_decoder_handles_union_bare_and_invalid_results():
    task = decode_send_result(
        {
            "task": {
                "id": "t1",
                "contextId": "c1",
                "status": {"state": "TASK_STATE_COMPLETED"},
            }
        },
        A2A_PROTOCOL_V1,
    )
    assert task.id == "t1"

    message = decode_send_result(
        {
            "message": {
                "message_id": "m1",
                "role": "ROLE_AGENT",
                "parts": [{"text": "hi"}],
            }
        },
        A2A_PROTOCOL_V1,
    )
    assert message.parts[0].text == "hi"

    bare_task = decode_send_result(
        {"id": "t2", "contextId": "c2", "status": {"state": "TASK_STATE_COMPLETED"}},
        A2A_PROTOCOL_V1,
    )
    assert bare_task.id == "t2"

    legacy_task = decode_send_result(
        {"id": "t3", "status": {"state": "TASK_STATE_COMPLETED"}},
        A2A_PROTOCOL_V03,
    )
    assert legacy_task.id == "t3"

    legacy_message = decode_send_result(
        {"message_id": "m3", "role": "ROLE_AGENT", "parts": [{"text": "yo"}]},
        A2A_PROTOCOL_V03,
    )
    assert legacy_message.parts[0].text == "yo"

    with pytest.raises(UnsupportedProtocolVersion):
        decode_send_result({"surprise": True}, A2A_PROTOCOL_V1)
    with pytest.raises(UnsupportedProtocolVersion):
        decode_send_result("opaque-string", A2A_PROTOCOL_V1)
