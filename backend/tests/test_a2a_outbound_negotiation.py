"""Tests for the outbound A2A version negotiation (shared/a2a_codec.py,
Orchestrator._execute_via_a2a): the advertised card version, selected request
version, JSON-RPC method and response decoder must agree, a strict v1 SDK
server's task and message branches must project into MCP responses, and
unsupported advertised versions must fail visibly without a silent downgrade.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from typing import Any, Dict, List

import httpx
import pytest
from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a2a.utils.constants import VERSION_HEADER  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402
from shared.a2a_bridge import (  # noqa: E402
    a2a_card_to_custom,
    custom_card_to_a2a,
    make_data_part,
    make_text_part,
)
from shared.a2a_codec import (  # noqa: E402
    A2ANegotiationError,
    decode_send_message_result,
    select_request_version,
)

PEER_BASE = "http://localhost:9411"
PEER_URL = f"{PEER_BASE}/a2a"


def _card(version: str | None, *, url: str = PEER_URL) -> AgentCard:
    interface = AgentInterface(protocol_binding="JSONRPC", url=url)
    if version is not None:
        interface.protocol_version = version
    return AgentCard(
        name="Peer",
        description="test peer",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        skills=[AgentSkill(id="echo", name="echo", description="echo", tags=["t"])],
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        supported_interfaces=[interface],
    )


def _orchestrator(card: AgentCard | None) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.a2a_clients = {"peer-1": PEER_BASE}
    orch.agent_urls = {}
    orch.a2a_agent_cards = {"peer-1": card} if card is not None else {}
    return orch


class _RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, responder):
        self.requests: List[httpx.Request] = []
        self._responder = responder

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self._responder(request)
        if inspect.isawaitable(result):
            result = await result
        return result


_ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_transport(monkeypatch, transport: httpx.AsyncBaseTransport) -> None:
    class _PatchedAsyncClient(_ORIGINAL_ASYNC_CLIENT):
        def __init__(self, **kwargs: Any) -> None:
            kwargs.pop("timeout", None)
            kwargs.pop("headers", None)
            kwargs.pop("transport", None)
            super().__init__(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _PatchedAsyncClient)


class _ScriptedPeer(AgentExecutor):
    def __init__(self, branch: str):
        self.branch = branch

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        from a2a.server.tasks.task_updater import TaskUpdater

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if self.branch == "message":
            await event_queue.enqueue_event(
                updater.new_agent_message([make_text_part("peer says hi")])
            )
            return
        from shared.a2a_bridge import ensure_task_created

        await ensure_task_created(context, event_queue)
        await updater.start_work()
        await updater.add_artifact([make_data_part({"done": True})], name="result")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


def _strict_v1_app(branch: str):
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import (
        create_agent_card_routes,
        create_jsonrpc_routes,
    )
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from fastapi import FastAPI

    card = _card("1.0")
    handler = DefaultRequestHandler(
        agent_executor=_ScriptedPeer(branch),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI()
    for route in create_jsonrpc_routes(handler, rpc_url="/a2a", enable_v0_3_compat=False):
        app.router.routes.append(route)
    for route in create_agent_card_routes(card, card_url="/a2a/.well-known/agent-card.json"):
        app.router.routes.append(route)
    return app


def _v1_union_task(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "task": {
            "id": "task-1",
            "contextId": "ctx-1",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [
                {
                    "artifactId": "a-1",
                    "name": "result",
                    "parts": [{"data": payload}],
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_strict_v1_sdk_server_task_branch_is_projected(monkeypatch):
    app = _strict_v1_app("task")
    real_asgi = httpx.ASGITransport(app=app)

    async def responder(request: httpx.Request) -> httpx.Response:
        return await real_asgi.handle_async_request(request)

    recording = _RecordingTransport(responder)
    _patch_transport(monkeypatch, recording)
    orch = _orchestrator(_card("1.0"))

    response = await orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)

    assert response.error is None
    assert response.result == {"done": True}
    assert response.correlation_id
    sent = recording.requests[0]
    body = json.loads(sent.content)
    assert sent.url.path == "/a2a"
    assert body["method"] == "SendMessage"
    assert sent.headers[VERSION_HEADER] == "1.0"
    assert body["params"]["message"]["parts"][0]["data"]["method"] == "tools/call"


@pytest.mark.asyncio
async def test_strict_v1_sdk_server_message_branch_is_projected(monkeypatch):
    app = _strict_v1_app("message")
    real_asgi = httpx.ASGITransport(app=app)

    async def responder(request: httpx.Request) -> httpx.Response:
        return await real_asgi.handle_async_request(request)

    recording = _RecordingTransport(responder)
    _patch_transport(monkeypatch, recording)
    orch = _orchestrator(_card("1.0"))

    response = await orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)

    assert response.error is None
    assert response.result == "peer says hi"


@pytest.mark.asyncio
async def test_served_strict_v1_card_advertises_protocol_version_1_0(monkeypatch):
    app = _strict_v1_app("task")
    transport = httpx.ASGITransport(app=app)
    _patch_transport(monkeypatch, transport)
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get(f"{PEER_BASE}/a2a/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"


@pytest.mark.parametrize(
    "advertised,method,peer_result,expected",
    [
        ("1.0", "SendMessage", _v1_union_task({"ok": 1}), {"ok": 1}),
        (None, "SendMessage", _v1_union_task({"ok": 1}), {"ok": 1}),
        (
            "0.3",
            "message/send",
            {
                "kind": "task",
                "id": "task-2",
                "contextId": "ctx-2",
                "status": {"state": "completed"},
                "artifacts": [
                    {
                        "artifactId": "a-2",
                        "name": "result",
                        "parts": [{"kind": "data", "data": {"legacy": True}}],
                    }
                ],
            },
            {"legacy": True},
        ),
    ],
)
@pytest.mark.asyncio
async def test_advertised_version_method_and_decoder_agree(
    monkeypatch, advertised, method, peer_result, expected
):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = json.loads(request.content)["method"]
        seen["version"] = request.headers[VERSION_HEADER]
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": "1", "result": peer_result},
        )

    transport = _RecordingTransport(handler)
    _patch_transport(monkeypatch, transport)
    orch = _orchestrator(_card(advertised))

    response = await orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)

    assert response.error is None
    assert response.result == expected
    assert seen["method"] == method
    card_version = advertised or "1.0"
    assert seen["version"] == card_version
    assert seen["version"] == select_request_version(_card(advertised))


@pytest.mark.asyncio
async def test_peer_advertising_both_versions_selects_v1(monkeypatch):
    card = _card("0.3")
    newer = AgentInterface(
        protocol_binding="JSONRPC", url=PEER_URL, protocol_version="1.0"
    )
    card.supported_interfaces.insert(0, newer)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = json.loads(request.content)["method"]
        seen["version"] = request.headers[VERSION_HEADER]
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": "1", "result": _v1_union_task({"v": 1})}
        )

    _patch_transport(monkeypatch, _RecordingTransport(handler))
    orch = _orchestrator(card)

    response = await orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)

    assert response.result == {"v": 1}
    assert seen["method"] == "SendMessage"
    assert seen["version"] == "1.0"


@pytest.mark.asyncio
async def test_v0_3_only_peer_is_supported_only_when_deliberately_selected(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["method"] = body["method"]
        seen["version"] = request.headers[VERSION_HEADER]
        seen["params"] = body["params"]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "result": {
                    "kind": "message",
                    "messageId": "m-1",
                    "role": "agent",
                    "parts": [{"kind": "text", "text": "legacy hello"}],
                },
            },
        )

    _patch_transport(monkeypatch, _RecordingTransport(handler))
    legacy_only_card = _card("0.3")
    legacy_orch = _orchestrator(legacy_only_card)

    response = await legacy_orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)
    assert response.result == "legacy hello"
    assert seen["method"] == "message/send"
    assert seen["version"] == "0.3"
    assert seen["params"]["message"]["kind"] == "message"
    assert seen["params"]["message"]["role"] == "user"


@pytest.mark.parametrize("advertised", ["2.0", "0.2", "not-a-version"])
@pytest.mark.asyncio
async def test_unsupported_versions_fail_visibly_without_send(monkeypatch, advertised):
    transport = _RecordingTransport(
        lambda request: httpx.Response(500, json={"boom": True})
    )
    _patch_transport(monkeypatch, transport)
    orch = _orchestrator(_card(advertised))

    response = await orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)

    assert transport.requests == []
    assert response.result is None
    assert response.error is not None
    assert response.error["retryable"] is False
    assert advertised in response.error["message"]


@pytest.mark.asyncio
async def test_non_jsonrpc_only_card_fails_visibly(monkeypatch):
    transport = _RecordingTransport(
        lambda request: httpx.Response(500, json={"boom": True})
    )
    _patch_transport(monkeypatch, transport)
    card = AgentCard(
        name="Peer",
        description="grpc only",
        version="1.0.0",
        capabilities=AgentCapabilities(),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="GRPC", url=PEER_URL, protocol_version="1.0"
            )
        ],
    )
    orch = _orchestrator(card)

    response = await orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)

    assert transport.requests == []
    assert response.error is not None
    assert response.error["retryable"] is False
    assert "JSONRPC" in response.error["message"]


@pytest.mark.asyncio
async def test_malformed_v1_result_is_a_visible_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": "1", "result": {"surprise": {"deep": 1}}},
        )

    _patch_transport(monkeypatch, _RecordingTransport(handler))
    orch = _orchestrator(_card("1.0"))

    response = await orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)

    assert response.result is None
    assert response.error is not None
    assert response.error["retryable"] is False
    assert "non-conformant" in response.error["message"]


@pytest.mark.asyncio
async def test_empty_v1_union_is_a_visible_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": "1", "result": {}}
        )

    _patch_transport(monkeypatch, _RecordingTransport(handler))
    orch = _orchestrator(_card("1.0"))

    response = await orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)

    assert response.result is None
    assert response.error is not None
    assert response.error["retryable"] is False
    assert "neither a task nor a message" in response.error["message"]


@pytest.mark.asyncio
async def test_peer_error_and_missing_result_surface_as_responses(monkeypatch):
    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": "1", "error": {"code": -32000, "message": "peer refused"}},
        )

    _patch_transport(monkeypatch, _RecordingTransport(error_handler))
    orch = _orchestrator(_card("1.0"))
    refused = await orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)
    assert refused.error == {"message": "peer refused", "retryable": False}

    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1"})

    _patch_transport(monkeypatch, _RecordingTransport(empty_handler))
    silent = await orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)
    assert silent.error == {"message": "No response from A2A agent", "retryable": True}


@pytest.mark.asyncio
async def test_delegation_agent_key_and_caller_capabilities_survive_negotiation(
    monkeypatch,
):
    monkeypatch.setenv("AGENT_API_KEY", "negotiation-key-0123456789")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["data"] = json.loads(request.content)["params"]["message"]["parts"][0][
            "data"
        ]
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": "1", "result": _v1_union_task({"ok": True})}
        )

    _patch_transport(monkeypatch, _RecordingTransport(handler))
    orch = _orchestrator(_card("1.0"))

    response = await orch._execute_via_a2a(
        "peer-1",
        "echo",
        {"_delegation_token": "delegated-token"},
        timeout=5.0,
        caller_capabilities={"le_ts": {"lease": "cap-1"}},
    )

    assert response.result == {"ok": True}
    assert seen["headers"]["x-astral-agent-key"] == "negotiation-key-0123456789"
    assert seen["headers"]["authorization"] == "Bearer delegated-token"
    assert seen["headers"][VERSION_HEADER.lower()] == "1.0"
    assert seen["data"]["caller_capabilities"] == {"le_ts": {"lease": "cap-1"}}


def test_select_request_version_preferences():
    assert select_request_version(None) == "1.0"
    assert select_request_version(_card("1.0")) == "1.0"
    assert select_request_version(_card("1.0.2")) == "1.0"
    assert select_request_version(_card("0.3")) == "0.3"
    assert select_request_version(_card("0.3.0")) == "0.3"
    assert select_request_version(_card(None)) == "1.0"

    both = _card("0.3")
    both.supported_interfaces.insert(
        0,
        AgentInterface(protocol_binding="JSONRPC", url=PEER_URL, protocol_version="1.0"),
    )
    assert select_request_version(both) == "1.0"

    unsupported = _card("2.0")
    with pytest.raises(A2ANegotiationError) as exc:
        select_request_version(unsupported)
    assert exc.value.advertised_versions == ("2.0",)


def test_decode_rejects_wrong_version_result():
    with pytest.raises(A2ANegotiationError):
        decode_send_message_result("9.9", {"task": {}})


def test_encode_rejects_wrong_version_request():
    from a2a.types import Message as A2AMessage, Role, Part

    from shared.a2a_codec import build_send_message

    with pytest.raises(A2ANegotiationError):
        build_send_message(
            "9.9",
            A2AMessage(
                message_id="m", role=Role.ROLE_USER, parts=[Part(text="x")]
            ),
            "req-1",
        )


def test_v0_3_decoder_fallbacks_and_unknown_shapes():
    message_shape = {
        "messageId": "m-9",
        "role": "agent",
        "parts": [{"kind": "text", "text": "no kind field"}],
    }
    decoded = decode_send_message_result("0.3", message_shape)
    assert decoded.WhichOneof("payload") == "message"
    assert decoded.message.parts[0].text == "no kind field"

    task_shape = {
        "id": "t-9",
        "contextId": "c-9",
        "status": {"state": "completed"},
    }
    decoded = decode_send_message_result("0.3", task_shape)
    assert decoded.WhichOneof("payload") == "task"
    assert decoded.task.id == "t-9"

    assert decode_send_message_result("0.3", "not-a-dict").WhichOneof("payload") is None
    assert decode_send_message_result("0.3", {"kind": "other"}).WhichOneof("payload") is None

    with pytest.raises(A2ANegotiationError):
        decode_send_message_result("0.3", {"kind": "task", "id": 7, "parts": "bad"})


def test_emitted_cards_declare_protocol_version_1_0():
    from orchestrator.a2a_orchestrator_executor import build_orchestrator_a2a_card
    from types import SimpleNamespace

    custom = a2a_card_to_custom(
        custom_card_to_a2a(
            SimpleNamespace(
                name="Bundled", description="d", version="2.0.0", skills=[]
            ),
            "http://localhost:9500",
        ),
        "bundled-1",
    )
    bundled = custom_card_to_a2a(custom, "http://localhost:9500")
    assert bundled.supported_interfaces[0].protocol_version == "1.0"

    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_cards = {}
    orch.agents = {}
    orch.local_agents = {}
    orch.security_flags = {}
    orch._is_draft_agent = lambda agent_id: False
    orch.tool_permissions = SimpleNamespace(list_disabled_agents=lambda user_id: ())
    emitted = build_orchestrator_a2a_card(orch)
    assert emitted.supported_interfaces[0].protocol_version == "1.0"
