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
            json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "result": peer_result},
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
            200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "result": _v1_union_task({"v": 1})}
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
                "id": json.loads(request.content)["id"],
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
            json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "result": {"surprise": {"deep": 1}}},
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
            200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "result": {}}
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
            json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "error": {"code": -32000, "message": "peer refused"}},
        )

    _patch_transport(monkeypatch, _RecordingTransport(error_handler))
    orch = _orchestrator(_card("1.0"))
    refused = await orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)
    assert refused.error == {"message": "peer refused", "retryable": False}

    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"]})

    _patch_transport(monkeypatch, _RecordingTransport(empty_handler))
    silent = await orch._execute_via_a2a("peer-1", "echo", {}, timeout=5.0)
    assert silent.error == {"message": "A2A peer returned a non-conformant JSON-RPC response", "retryable": False}


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
            200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "result": _v1_union_task({"ok": True})}
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


@pytest.mark.parametrize(
    "version",
    ["0.4", "0.9", "1.0rc1", "1.0.dev1", "1.0+canary", "1!1.0", "v1.0", "1.0.0.4"],
)
def test_unadvertised_protocol_variants_are_never_normalized_to_supported_versions(version):
    with pytest.raises(A2ANegotiationError):
        select_request_version(_card(version))


def test_v1_request_uses_canonical_json_field_names():
    from a2a.types import Message, Role
    from shared.a2a_codec import build_send_message

    payload = build_send_message(
        "1.0",
        Message(
            message_id="message-1",
            context_id="context-1",
            task_id="task-1",
            role=Role.ROLE_USER,
            parts=[make_text_part("hello")],
        ),
        "request-1",
    )

    assert payload["params"]["message"]["messageId"] == "message-1"
    assert payload["params"]["message"]["contextId"] == "context-1"
    assert payload["params"]["message"]["taskId"] == "task-1"
    assert "message_id" not in payload["params"]["message"]


@pytest.mark.parametrize("version", ["1.0", "0.3"])
@pytest.mark.asyncio
async def test_selected_interface_url_and_version_stay_bound(monkeypatch, version):
    selected_url = f"{PEER_BASE}/rpc/{version}?agent=peer"

    def handler(request):
        body = json.loads(request.content)
        assert str(request.url) == selected_url
        assert request.headers[VERSION_HEADER] == version
        result = (
            _v1_union_task({"selected": version})
            if version == "1.0"
            else {
                "kind": "message",
                "messageId": "legacy-message",
                "role": "agent",
                "parts": [{"kind": "text", "text": "legacy"}],
            }
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    transport = _RecordingTransport(handler)
    _patch_transport(monkeypatch, transport)
    response = await _orchestrator(_card(version, url=selected_url))._execute_via_a2a(
        "peer-1", "echo", {}
    )

    assert response.error is None
    assert len(transport.requests) == 1


def test_interface_preference_order_is_respected():
    card = _card("0.3")
    card.supported_interfaces.append(
        AgentInterface(protocol_binding="JSONRPC", url=PEER_URL, protocol_version="1.0")
    )
    assert select_request_version(card) == "0.3"
    card.supported_interfaces[0].protocol_version = "0.9"
    assert select_request_version(card) == "1.0"


@pytest.mark.parametrize(
    "url",
    [
        "http://attacker.example/a2a",
        "https://localhost:9411/a2a",
        "http://localhost:9412/a2a",
        "http://user@localhost:9411/a2a",
        "http://localhost:9411/a2a#fragment",
        "http://localhost:bad/a2a",
        "/relative/a2a",
    ],
)
@pytest.mark.asyncio
async def test_untrusted_interface_url_is_rejected_before_credentials_or_send(monkeypatch, url):
    monkeypatch.setenv("AGENT_API_KEY", "test-key-for-isolation")
    transport = _RecordingTransport(lambda request: httpx.Response(500))
    _patch_transport(monkeypatch, transport)

    response = await _orchestrator(_card("1.0", url=url))._execute_via_a2a(
        "peer-1", "echo", {"_delegation_token": "test-delegation"}
    )

    assert transport.requests == []
    assert response.result is None
    assert response.error["retryable"] is False


@pytest.mark.parametrize(
    "result",
    [
        {"message": {}},
        {"message": {"messageId": "m", "role": "ROLE_AGENT", "parts": []}},
        {"message": {"role": "ROLE_AGENT", "parts": [{"text": "hi"}]}},
        {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
        {"message": {"messageId": "m", "role": "ROLE_AGENT", "parts": [{}]}},
        {"task": {"status": {"state": "TASK_STATE_COMPLETED"}}},
        {"task": {"id": "t"}},
        {"task": {"id": "t", "status": {}}},
    ],
)
def test_incomplete_v1_payload_cannot_project_success(result):
    with pytest.raises(A2ANegotiationError):
        decode_send_message_result("1.0", result)


@pytest.mark.parametrize(
    "invalid",
    ["wrong-id", "missing-id", "wrong-jsonrpc", "missing-jsonrpc", "result-and-error", "nonobject", "invalid-json"],
)
@pytest.mark.asyncio
async def test_malformed_rpc_reply_never_succeeds_or_requests_a_retry(monkeypatch, invalid):
    def handler(request):
        body = json.loads(request.content)
        reply = {"jsonrpc": "2.0", "id": body["id"], "result": _v1_union_task({"ok": True})}
        if invalid == "wrong-id":
            reply["id"] = "different-request"
        elif invalid == "missing-id":
            reply.pop("id")
        elif invalid == "wrong-jsonrpc":
            reply["jsonrpc"] = "1.0"
        elif invalid == "missing-jsonrpc":
            reply.pop("jsonrpc")
        elif invalid == "result-and-error":
            reply["error"] = {"code": -32000, "message": "refused"}
        elif invalid == "nonobject":
            reply = [reply]
        elif invalid == "invalid-json":
            return httpx.Response(200, content=b"not JSON")
        return httpx.Response(200, json=reply)

    transport = _RecordingTransport(handler)
    _patch_transport(monkeypatch, transport)
    response = await _orchestrator(_card("1.0"))._execute_via_a2a("peer-1", "echo", {})

    assert len(transport.requests) == 1
    assert response.result is None
    assert response.error["retryable"] is False


@pytest.mark.asyncio
async def test_v1_tenant_and_credentials_use_separate_wire_fields(monkeypatch):
    card = _card("1.0")
    card.supported_interfaces[0].tenant = "tenant-1"

    def handler(request):
        body = json.loads(request.content)
        assert body["params"]["tenant"] == "tenant-1"
        data = body["params"]["message"]["parts"][0]["data"]
        assert data["arguments"] == {"value": 7}
        assert data["caller_capabilities"] == {"le_ts": {"lease": "lease-1"}}
        assert request.headers["Authorization"] == "Bearer delegated-token"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": _v1_union_task({"ok": True})})

    _patch_transport(monkeypatch, _RecordingTransport(handler))
    response = await _orchestrator(card)._execute_via_a2a(
        "peer-1", "echo", {"value": 7, "_delegation_token": "delegated-token"},
        caller_capabilities={"le_ts": {"lease": "lease-1"}},
    )
    assert response.error is None


@pytest.mark.asyncio
async def test_legacy_tenant_is_rejected_without_silent_routing_loss(monkeypatch):
    card = _card("0.3")
    card.supported_interfaces[0].tenant = "tenant-1"
    transport = _RecordingTransport(lambda request: httpx.Response(500))
    _patch_transport(monkeypatch, transport)
    response = await _orchestrator(card)._execute_via_a2a("peer-1", "echo", {})
    assert transport.requests == []
    assert response.error["retryable"] is False
    assert "tenant" in response.error["message"]


def test_legacy_encoder_refuses_a_tenant():
    from a2a.types import Message, Role
    from shared.a2a_codec import build_send_message

    with pytest.raises(A2ANegotiationError, match="tenant"):
        build_send_message("0.3", Message(message_id="m", role=Role.ROLE_USER, parts=[make_text_part("hi")]), "r", tenant="t")


@pytest.mark.parametrize("registered,advertised", [
    ("https://peer.example", "https://PEER.example:443/rpc"),
    ("http://peer.example:80", "http://peer.example/rpc"),
])
def test_same_origin_normalizes_case_and_default_ports(registered, advertised):
    from shared.a2a_codec import resolve_request_interface

    selected = resolve_request_interface(_card("1.0", url=advertised), registered)
    assert selected.url == advertised
    assert selected.version == "1.0"


@pytest.mark.asyncio
async def test_egress_denial_occurs_before_authentication_headers_or_send(monkeypatch):
    monkeypatch.delenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("AGENT_KEY_TRUSTED_HOSTS", raising=False)
    monkeypatch.setattr("shared.external_http._resolve_host_addresses", lambda host: ["10.0.0.9"])

    def forbidden_headers(url):
        pytest.fail("credentials must not be prepared for a denied destination")

    monkeypatch.setattr("orchestrator.agent_peer_auth.agent_auth_headers", forbidden_headers)
    private_base = "http://private-peer.example"
    orch = _orchestrator(_card("1.0", url=f"{private_base}/rpc"))
    orch.a2a_clients["peer-1"] = private_base
    transport = _RecordingTransport(lambda request: httpx.Response(500))
    _patch_transport(monkeypatch, transport)

    response = await orch._execute_via_a2a("peer-1", "echo", {})
    assert transport.requests == []
    assert response.error["retryable"] is False
    assert "egress is blocked" in response.error["message"]


@pytest.mark.asyncio
async def test_response_size_limit_is_enforced_while_streaming(monkeypatch):
    monkeypatch.setattr("shared.external_http.DEFAULT_MAX_RESPONSE_BYTES", 8)
    transport = _RecordingTransport(lambda request: httpx.Response(200, content=b"x" * 9))
    _patch_transport(monkeypatch, transport)
    response = await _orchestrator(_card("1.0"))._execute_via_a2a("peer-1", "echo", {})
    assert len(transport.requests) == 1
    assert response.error["retryable"] is False
    assert "permitted size" in response.error["message"]


@pytest.mark.parametrize("error", ["refused", {}, {"code": True, "message": "refused"}, {"code": -32000, "message": None}])
@pytest.mark.asyncio
async def test_malformed_rpc_error_is_not_a_retryable_transport_failure(monkeypatch, error):
    def handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "error": error})

    _patch_transport(monkeypatch, _RecordingTransport(handler))
    response = await _orchestrator(_card("1.0"))._execute_via_a2a("peer-1", "echo", {})
    assert response.result is None
    assert response.error["retryable"] is False
    assert "JSON-RPC error" in response.error["message"]


@pytest.mark.asyncio
async def test_malformed_peer_reply_does_not_repeat_a_tool_effect(monkeypatch):
    transport = _RecordingTransport(lambda request: httpx.Response(200, content=b"not JSON"))
    _patch_transport(monkeypatch, transport)
    orch = _orchestrator(_card("1.0"))

    async def execute_and_wait(agent_id, tool_name, args, **kwargs):
        return await orch._execute_via_a2a(agent_id, tool_name, args)

    orch.execute_tool_and_wait = execute_and_wait
    response = await orch._execute_with_retry(None, "peer-1", "echo", {}, user_id="owner-1", channel="rest", audit_correlation_id="corr-1")
    assert len(transport.requests) == 1
    assert response.result is None
    assert response.error["retryable"] is False


@pytest.mark.parametrize("result", [None, [], "opaque", {"task": {}, "message": {}}])
def test_wrong_v1_union_shapes_are_not_accepted(result):
    with pytest.raises(A2ANegotiationError):
        decode_send_message_result("1.0", result)


@pytest.mark.parametrize("malformation", ["missing-artifact-id", "empty-artifact", "duplicate-artifact", "empty-part", "user-status-message", "empty-status-message"])
def test_invalid_task_output_or_status_message_cannot_project_success(malformation):
    result = _v1_union_task({"ok": True})
    task = result["task"]
    if malformation == "missing-artifact-id":
        task["artifacts"][0].pop("artifactId")
    elif malformation == "empty-artifact":
        task["artifacts"][0]["parts"] = []
    elif malformation == "duplicate-artifact":
        task["artifacts"].append(task["artifacts"][0].copy())
    elif malformation == "empty-part":
        task["artifacts"][0]["parts"] = [{}]
    elif malformation == "user-status-message":
        task["status"]["message"] = {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}
    elif malformation == "empty-status-message":
        task["status"]["message"] = {"messageId": "m", "role": "ROLE_AGENT", "parts": [{}]}
    with pytest.raises(A2ANegotiationError):
        decode_send_message_result("1.0", result)


@pytest.mark.parametrize("status,retryable", [(400, False), (401, False), (403, False), (307, False), (429, True), (503, True)])
@pytest.mark.asyncio
async def test_http_denials_and_redirects_do_not_redial_or_retry(monkeypatch, status, retryable):
    transport = _RecordingTransport(lambda request: httpx.Response(status, headers={"Location": "http://attacker.example/rpc"}))
    _patch_transport(monkeypatch, transport)
    response = await _orchestrator(_card("1.0"))._execute_via_a2a("peer-1", "echo", {})
    assert len(transport.requests) == 1
    assert response.error == {"message": f"A2A peer returned HTTP {status}", "retryable": retryable}


@pytest.mark.asyncio
async def test_timeout_remains_an_explicit_transport_failure(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    _patch_transport(monkeypatch, _RecordingTransport(handler))
    response = await _orchestrator(_card("1.0"))._execute_via_a2a("peer-1", "echo", {})
    assert response.error == {"message": "A2A tool call timed out", "retryable": True}


def test_completed_task_without_artifacts_remains_valid():
    result = _v1_union_task({})
    result["task"]["artifacts"] = []
    decoded = decode_send_message_result("1.0", result)
    assert decoded.task.id == "task-1"


def test_valid_agent_status_message_remains_projectable():
    result = _v1_union_task({})
    result["task"]["artifacts"] = []
    result["task"]["status"]["message"] = {"messageId": "status-1", "role": "ROLE_AGENT", "parts": [{"text": "done"}]}
    decoded = decode_send_message_result("1.0", result)
    from shared.a2a_bridge import a2a_response_to_mcp_response

    assert a2a_response_to_mcp_response(decoded.task, "request-1").result == "done"
