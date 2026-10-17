"""Checks REST terminal proposals require a visible unscoped browser review context.
Conversation continuity remains authoritative and context changes cannot leave an executable handoff.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from orchestrator import remote_confirmation as rc
from tests.test_terminal_handoff_delivery import handoff as handoff
from tests.test_terminal_approvals import fixture as fixture
from tests.test_new_chat_scope_088 import runtime as runtime
from tests.helpers.terminal_connection import registered_connection


def _scope(fixture, kind):
    if kind == "chat":
        fixture.orch._ws_active_chat = {id(fixture.socket): "11111111-1111-4111-8111-111111111111"}
    elif kind in {"completed", "active"}:
        fixture.orch._conversation_scopes = {id(fixture.socket): {"snapshot_completed": kind == "completed"}}
    elif kind == "native":
        fixture.orch.rote = SimpleNamespace(get_profile=lambda socket: SimpleNamespace(device_type="android"))
    else:
        fixture.orch.rote = None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chat", "completed", "active", "native", "unknown_device"])
async def test_ineligible_context_refuses_before_dispatch_or_durable_proposal(handoff, kind):
    fixture = handoff.fixture
    _scope(fixture, kind)
    dispatch = fixture.orch.execute_authorized_tool
    fixture.orch.execute_authorized_tool = AsyncMock(side_effect=dispatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    assert response.status_code == 409
    assert response.json() == {"error": "terminal_review_context_required"}
    assert fixture.db.rows == {}
    fixture.orch.execute_authorized_tool.assert_not_awaited()
    fixture.orch.send_ui_render.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_loss_before_normal_proposal_creation_leaves_no_durable_row(handoff):
    fixture = handoff.fixture
    original = fixture.orch.execute_authorized_tool

    async def changed(**kwargs):
        _scope(fixture, "chat")
        return await original(**kwargs)

    fixture.orch.execute_authorized_tool = changed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    assert response.status_code == 409
    assert response.json() == {"error": "terminal_review_context_required"}
    assert fixture.db.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chat", "completed"])
async def test_scope_change_during_card_delivery_retires_handoff_after_positive_transport_ack(handoff, kind):
    fixture = handoff.fixture

    async def moved(*args, **kwargs):
        _scope(fixture, kind)
        return True

    fixture.orch.send_ui_render.side_effect = moved
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    assert response.status_code == 409
    assert response.json() == {"error": "terminal_review_context_required"}
    assert handoff.state["request"].status == "failed"
    fixture.orch.send_ui_render = AsyncMock(return_value=True)
    await rc.handle_decision(fixture.orch, fixture.socket, "owner", handoff.state["card"]["content"][-2]["payload"])
    fixture.orch.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_review_socket_receives_card_while_scoped_sibling_is_never_used(handoff):
    fixture = handoff.fixture
    scoped = object()
    fixture.orch.ui_sessions[scoped] = dict(fixture.claims)
    fixture.orch._conversation_scopes = {id(scoped): {"snapshot_completed": True}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    assert response.status_code == 202 and response.json()["status"] == "pending"
    fixture.orch.send_ui_render.assert_awaited_once()
    assert fixture.orch.send_ui_render.await_args.args[0] is fixture.socket


@pytest.mark.asyncio
async def test_changed_socket_cannot_count_delivery_when_an_unsent_sibling_keeps_request_current(handoff):
    fixture = handoff.fixture
    sibling = object()
    fixture.orch.ui_sessions[sibling] = dict(fixture.claims)

    async def moved(socket, *args, **kwargs):
        if socket is fixture.socket:
            _scope(fixture, "chat")
            return True
        return False

    fixture.orch.send_ui_render.side_effect = moved
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    assert response.status_code == 409
    assert handoff.state["request"].status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["_conversation_scopes", "_ws_active_chat"])
@pytest.mark.parametrize("state", ["missing", "malformed"])
async def test_missing_or_malformed_server_context_maps_never_certify_freshness(handoff, field, state):
    fixture = handoff.fixture
    if state == "missing":
        delattr(fixture.orch, field)
    else:
        setattr(fixture.orch, field, [])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    assert response.status_code == 409
    assert response.json() == {"error": "terminal_review_context_required"}
    assert fixture.db.rows == {}
    fixture.orch.send_ui_render.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["_conversation_scopes", "_ws_active_chat"])
async def test_replaced_empty_context_map_cannot_acknowledge_the_captured_review_context(handoff, field):
    fixture = handoff.fixture

    async def changed(*args, **kwargs):
        setattr(fixture.orch, field, {})
        return True

    fixture.orch.send_ui_render.side_effect = changed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    assert response.status_code == 409
    assert response.json() == {"error": "terminal_review_context_required"}
    assert handoff.state["request"].status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["settings", "machines", "safety", []])
async def test_open_or_malformed_chrome_context_refuses_before_proposal(handoff, surface):
    fixture = handoff.fixture
    fixture.orch._open_chrome_surface = {id(fixture.socket): surface}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    assert response.status_code == 409
    assert response.json() == {"error": "terminal_review_context_required"}
    assert fixture.db.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["open", "initialized", "replaced", "malformed"])
async def test_chrome_context_change_during_delivery_invalidates_the_captured_review(handoff, change):
    fixture = handoff.fixture
    if change == "replaced":
        fixture.orch._open_chrome_surface = {}

    async def changed(*args, **kwargs):
        fixture.orch._open_chrome_surface = {id(fixture.socket): "machines"} if change == "open" else (
            [] if change == "malformed" else {})
        return True

    fixture.orch.send_ui_render.side_effect = changed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    assert response.status_code == 409
    assert response.json() == {"error": "terminal_review_context_required"}
    assert handoff.state["request"].status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "declined"])
async def test_completed_handoff_is_readable_after_entering_chat_but_current_permission_is_still_required(handoff, outcome):
    from shared.protocol import MCPResponse

    fixture = handoff.fixture
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        created = await client.post("/api/terminals", json={})
        assert created.status_code == 202
        request = handoff.state["request"]
        if outcome == "succeeded":
            request.complete(MCPResponse(result={"session": {"session_id": "completed-session"}}))
        else:
            await rc.handle_decision(fixture.orch, fixture.socket, "owner", handoff.state["card"]["content"][-1]["payload"])
        _scope(fixture, "chat")
        response = await client.get("/api/terminals/approvals/" + request.proposal_id)
        assert response.status_code == 200
        assert response.json()["status"] == outcome
        fixture.orch.tool_permissions.is_tool_allowed = lambda *args: False
        denied = await client.get("/api/terminals/approvals/" + request.proposal_id)
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_actual_new_chat_clears_maps_but_never_recovers_terminal_review_eligibility(handoff, runtime):
    import json
    import time

    from orchestrator.orchestrator import Orchestrator
    from shared.protocol import MCPResponse

    orch, socket = runtime.orchestrator, runtime.socket
    orch._terminal_review_scoped = set()
    registered_connection(orch, socket)
    orch.ui_sessions[socket].update(exp=time.time() + 600)
    await Orchestrator.handle_ui_message(orch, socket, json.dumps({"type": "ui_event", "action": "new_chat", "payload": {}}))
    assert id(socket) not in orch._conversation_scopes and id(socket) not in orch._ws_active_chat
    assert any(frame.get("type") == "chat_created" and frame["payload"]["chat_id"] == runtime.new_chat for frame in socket.frames)
    orch.rote.get_profile = lambda websocket: SimpleNamespace(device_type="browser")
    orch.execute_authorized_tool = AsyncMock(return_value=MCPResponse(result={"session": {"session_id": "must-not-open"}}))
    handoff.app.state.orchestrator = orch
    handoff.fixture.claims = orch.ui_sessions[socket]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    orch.execute_authorized_tool.assert_not_awaited()
    assert response.status_code == 409
    assert response.json() == {"error": "terminal_review_context_required"}
    assert id(socket) in orch._terminal_review_scoped


@pytest.mark.asyncio
async def test_new_chat_marks_review_ineligible_before_its_first_blocking_effect(runtime):
    import json

    orch, socket = runtime.orchestrator, runtime.socket
    orch._terminal_review_scoped = set()

    def create(**kwargs):
        assert id(socket) in orch._terminal_review_scoped
        return runtime.new_chat

    orch.history.create_chat.side_effect = create
    await orch.handle_ui_message(socket, json.dumps({"type": "ui_event", "action": "new_chat", "payload": {}}))
    orch.history.create_chat.assert_called_once()
    assert id(socket) in orch._terminal_review_scoped


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "malformed", "marked", "replaced"])
async def test_sticky_review_marker_requires_current_host_owned_set(handoff, kind):
    fixture = handoff.fixture
    if kind == "missing":
        del fixture.orch._terminal_review_scoped
    elif kind == "malformed":
        fixture.orch._terminal_review_scoped = {}
    elif kind == "marked":
        fixture.orch._terminal_review_scoped.add(id(fixture.socket))
    else:
        async def changed(*args, **kwargs):
            fixture.orch._terminal_review_scoped = set()
            return True
        fixture.orch.send_ui_render.side_effect = changed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    assert response.status_code == 409
    assert response.json() == {"error": "terminal_review_context_required"}
    if kind != "replaced":
        assert fixture.db.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["handle_ui_connection", "handle_ui_connection_fastapi"])
async def test_normal_disconnect_removes_only_its_sticky_review_marker(monkeypatch, entrypoint):
    from orchestrator.orchestrator import Orchestrator
    from shared.feature_flags import flags
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setitem(flags._flags, "hook_system", False)

    class Socket:
        async def accept(self):
            return None

        async def receive_text(self):
            raise WebSocketDisconnect()

        async def recv(self):
            raise StopAsyncIteration()

    socket, other = Socket(), object()
    markers = {id(socket), id(other)}
    orch = SimpleNamespace(
        ui_clients=[], ui_sessions={}, _registered_events={}, _terminal_review_scoped=markers,
        _ws_active_chat={}, _conversation_scopes={}, _ws_timeline_mode={}, _ws_welcome={},
        stream_manager=None, _cleanup_streams=lambda *args: None, _clear_voice_control_binding=lambda *args: None,
        _drop_chat_locks=lambda *args: None, rote=SimpleNamespace(cleanup=lambda *args: None),
        _disconnect_personal_agent_host=AsyncMock(), _teardown_owner_tunnels=AsyncMock(),
        _teardown_computer_host=AsyncMock(),
    )

    async def serve(transport, receive):
        await receive()

    orch._serve_ui_frames = serve
    await getattr(Orchestrator, entrypoint)(orch, socket)
    assert orch._terminal_review_scoped is markers and markers == {id(other)}
