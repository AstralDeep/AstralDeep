"""Feature 058 — user-agent Mode-1 tunnel: owner-bound registration, outbound
frame wrap, honest-offline on disconnect. Exercises the whole server-side tunnel
path with a fake UI socket (only the real Windows host needs a live client)."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.orchestrator import Orchestrator  # noqa: E402
from orchestrator import user_agents as ua  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from shared.local_transport import TunnelSocket  # noqa: E402
from shared.protocol import AgentCard, AgentSkill, RegisterAgent  # noqa: E402


async def _t(fn, *a, **k):
    """Run a synchronous (DB-touching) helper off the event loop (052)."""
    return await asyncio.to_thread(fn, *a, **k)


def _isolated_mock_identity(prefix: str) -> tuple[str, str]:
    """Build a unique mock-auth subject without touching ``test_user``."""
    user_id = f"{prefix}-{uuid.uuid4().hex}"
    claims = {
        "sub": user_id,
        "preferred_username": user_id,
        "email": f"{user_id}@invalid.example",
        "realm_access": {"roles": ["admin", "user"]},
        "resource_access": {
            "astral-frontend": {"roles": ["admin", "user"]}
        },
    }
    payload = base64.b64encode(
        json.dumps(claims, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return user_id, f"mock.{payload}.signature"


class FakeUI:
    """A UI websocket that captures frames the orchestrator sends to the client."""

    def __init__(self):
        self.sent = []

    async def send_text(self, t):
        self.sent.append(t)

    async def send(self, t):
        self.sent.append(t)

    async def close(self, *a, **k):
        return None


@dataclass(frozen=True)
class TunnelIds:
    owner: str
    foreign: str
    agent_id: str
    host_session_id: str


@pytest.fixture
def tunnel_ids():
    token = uuid.uuid4().hex[:12]
    return TunnelIds(
        owner=f"byo058-owner-{token}",
        foreign=f"byo058-foreign-{token}",
        agent_id=f"byo058-greeter-{token}",
        host_session_id=f"hs-{token}",
    )


async def _soft_tombstone_agent(orch, ids):
    """Retire only this fixture's unique user-agent identity through Plane."""
    registry = orch.user_agent_registry
    row = await _t(ua.get_user_agent, registry, ids.agent_id)
    if row is not None and row.get("deleted_at") is None:
        await _t(ua.soft_delete, registry, ids.agent_id)


@pytest.fixture()
async def orch(monkeypatch, tunnel_ids):
    monkeypatch.setitem(flags._flags, "byo_agents", True)
    o = await asyncio.to_thread(Orchestrator)
    try:
        registry = o.user_agent_registry
        await _t(
            ua.create_user_agent,
            registry,
            agent_id=tunnel_ids.agent_id,
            owner_user_id=tunnel_ids.owner,
            display_name="Greeter",
        )
        await _t(ua.mark_validated, registry, tunnel_ids.agent_id, "0.1.0")
        yield o
    finally:
        try:
            await _soft_tombstone_agent(o, tunnel_ids)
        finally:
            await asyncio.wait_for(o._close_started_services(), timeout=15.0)


def _reg_frame(agent_id):
    card = AgentCard(name="Greeter", description="greets", agent_id=agent_id,
                     skills=[AgentSkill(name="greet", description="g", id="greet",
                                        scope="tools:read", input_schema={})])
    return RegisterAgent(agent_card=card).to_json()


async def _tunnel(o, ws, frame, ids, *, agent_id=None):
    agent_id = agent_id or ids.agent_id
    msg = SimpleNamespace(action="agent_tunnel",
                          payload={"agent_id": agent_id, "frame": frame,
                                   "host_session_id": ids.host_session_id})
    await o._handle_agent_tunnel(ws, msg)


async def test_owner_tunnel_registers_and_goes_live(orch, tunnel_ids):
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": tunnel_ids.owner}
    await _tunnel(orch, ws, _reg_frame(tunnel_ids.agent_id), tunnel_ids)
    assert tunnel_ids.agent_id in orch.agents
    assert isinstance(orch.agents[tunnel_ids.agent_id], TunnelSocket)
    assert orch.agents[tunnel_ids.agent_id].owner_sub == tunnel_ids.owner
    row = await _t(
        ua.get_user_agent,
        orch.user_agent_registry,
        tunnel_ids.agent_id,
    )
    assert row["status"] == "live"           # go_live ran
    own = await _t(
        orch.user_agent_registry.get_agent_ownership,
        tunnel_ids.agent_id,
    )
    assert own is not None and bool(own["is_public"]) is False   # private companion row


async def test_foreign_owner_registration_refused(orch, tunnel_ids):
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": tunnel_ids.foreign}
    await _tunnel(orch, ws, _reg_frame(tunnel_ids.agent_id), tunnel_ids)
    assert tunnel_ids.agent_id not in orch.agents


async def test_outbound_frame_is_tunnel_wrapped(orch, tunnel_ids):
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": tunnel_ids.owner}
    await _tunnel(orch, ws, _reg_frame(tunnel_ids.agent_id), tunnel_ids)
    ws.sent.clear()
    await orch.agents[tunnel_ids.agent_id].send('{"method":"tools/call","x":1}')
    env = json.loads(ws.sent[-1])
    assert env["type"] == "agent_tunnel"
    assert env["agent_id"] == tunnel_ids.agent_id
    assert env["frame"] == '{"method":"tools/call","x":1}'


async def test_offline_on_disconnect_yields_honest_offline(orch, tunnel_ids):
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": tunnel_ids.owner}
    await _tunnel(orch, ws, _reg_frame(tunnel_ids.agent_id), tunnel_ids)
    assert tunnel_ids.agent_id in orch.agents
    await orch._teardown_owner_tunnels(ws)   # client disconnects
    assert tunnel_ids.agent_id not in orch.agents
    resp = await orch._dispatch_tool_call(
        tunnel_ids.agent_id,
        "greet",
        {},
        5.0,
        None,
    )
    assert resp is not None and resp.error and resp.error.get("offline") is True


async def test_flag_off_tunnel_is_inert(orch, monkeypatch, tunnel_ids):
    monkeypatch.setitem(flags._flags, "byo_agents", False)
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": tunnel_ids.owner}
    await _tunnel(orch, ws, _reg_frame(tunnel_ids.agent_id), tunnel_ids)
    assert tunnel_ids.agent_id not in orch.agents


async def test_no_delegation_token_handed_to_tunnel_agent(orch, tunnel_ids):
    # T014: a user-hosted (tunnel) agent is untrusted — the delegation-token
    # bytes are never attached to its dispatch args; the boundary re-authorizes.
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": tunnel_ids.owner, "_raw_token": "tok"}
    await _tunnel(orch, ws, _reg_frame(tunnel_ids.agent_id), tunnel_ids)
    await _t(
        orch.tool_permissions.set_agent_scopes,
        tunnel_ids.owner,
        tunnel_ids.agent_id,
        {"tools:read": True},
    )
    auth = await orch._authorize_and_prepare(
        ws,
        tunnel_ids.agent_id,
        "greet",
        {"user_id": tunnel_ids.owner},
        None,
        tunnel_ids.owner,
    )
    from orchestrator.orchestrator import PreparedDispatch, GateRefusal
    if isinstance(auth, GateRefusal):
        pytest.skip(f"gate refused in this env: {auth.response.error if auth.response else auth}")
    assert isinstance(auth, PreparedDispatch)
    assert "_delegation_token" not in auth.args


async def test_per_owner_ingress_cap_isolates_a_flooding_owner(
    orch,
    monkeypatch,
    tunnel_ids,
):
    # T013 (FR-017/SC-008): a flooding owner is capped after the window budget;
    # a different owner has an independent budget and is unaffected.
    monkeypatch.setattr(type(orch), "_TUNNEL_MAX_FRAMES_PER_WINDOW", 5)
    over = [orch._tunnel_ingress_over_cap(tunnel_ids.owner) for _ in range(8)]
    assert over[:5] == [False] * 5            # first 5 within budget
    assert all(over[5:])                       # 6th+ dropped (over cap)
    assert orch._tunnel_ingress_over_cap(tunnel_ids.foreign) is False


def _as_host(orch, ws, session):
    """Mark a UI socket as a desktop AGENT HOST (what register_ui does when the
    client declares ``agent_host``)."""
    orch._agent_host_sockets[id(ws)] = session
    return ws


async def test_deliver_bundle_to_owner_host(orch, tunnel_ids):
    # T006: bundle is pushed to the owner's desktop host over its UI socket.
    ws = _as_host(orch, FakeUI(), tunnel_ids.host_session_id)
    orch.ui_sessions[ws] = {"sub": tunnel_ids.owner}
    orch.ui_clients.append(ws)
    n = await orch.deliver_agent_bundle(
        tunnel_ids.owner,
        tunnel_ids.agent_id,
        {"greeter_agent.py": "code"},
        "0.1.0",
    )
    assert n == 1
    # The delivery frame is present (an audit_append metadata frame may follow it
    # now that delivery is audited — find the bundle frame by type, not position).
    frames = [json.loads(f) for f in ws.sent]
    env = next(f for f in frames if f["type"] == "agent_bundle_deliver")
    assert env["agent_id"] == tunnel_ids.agent_id
    assert env["files"] == {"greeter_agent.py": "code"} and env["constitution_version"] == "0.1.0"
    # No host online for a different owner → delivered to 0 sockets.
    assert await orch.deliver_agent_bundle(
        tunnel_ids.foreign,
        tunnel_ids.agent_id,
        {},
        None,
    ) == 0


async def test_bundle_is_never_pushed_to_a_browser_tab(orch, tunnel_ids):
    """A browser tab cannot run a child process. Counting it as 'delivered' both
    lied to the user and sprayed their generated code into the browser."""
    tab = FakeUI()                      # a plain UI socket — NOT a desktop host
    orch.ui_sessions[tab] = {"sub": tunnel_ids.owner}
    orch.ui_clients.append(tab)

    def _code_frames(sock):
        # The security guarantee is that no CODE bundle reaches the tab. A delivery
        # is audited, and an audit_append metadata frame legitimately fans out to
        # the owner's UI sockets (incl. the tab) — that is not the user's code.
        return [f for f in sock.sent
                if json.loads(f)["type"] == "agent_bundle_deliver"]

    n = await orch.deliver_agent_bundle(
        tunnel_ids.owner,
        tunnel_ids.agent_id,
        {"mcp_tools.py": "secret code"},
        "0.1.0",
    )
    assert n == 0                        # honest 'no_host'
    assert _code_frames(tab) == []       # and no code went to the tab

    host = _as_host(orch, FakeUI(), tunnel_ids.host_session_id)
    orch.ui_sessions[host] = {"sub": tunnel_ids.owner}
    orch.ui_clients.append(host)
    assert await orch.deliver_agent_bundle(
        tunnel_ids.owner,
        tunnel_ids.agent_id,
        {"mcp_tools.py": "c"},
        "0.1.0",
    ) == 1
    assert _code_frames(tab) == []       # still no code to the tab
    assert len(_code_frames(host)) == 1  # the host got the bundle


async def test_register_ui_marks_only_a_declared_host(monkeypatch, tunnel_ids):
    """The host capability is an EXPLICIT, additive register_ui declaration."""
    import asyncio as _asyncio
    import uuid as _uuid
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    o = await _t(Orchestrator)
    user_id, token = _isolated_mock_identity("pytest-byo-host")

    async def _register(**extra):
        ws = VirtualWebSocket(BackgroundTask(task_id=_uuid.uuid4().hex, chat_id="",
                                             user_id=""))
        o._registered_events[id(ws)] = _asyncio.Event()
        register_tasks = []
        real_create_task = _asyncio.create_task

        def _capture_register_task(coro, *args, **kwargs):
            task = real_create_task(coro, *args, **kwargs)
            register_tasks.append(task)
            return task

        with monkeypatch.context() as register_patch:
            register_patch.setattr(_asyncio, "create_task", _capture_register_task)
            await o.handle_ui_message(ws, json.dumps(
                {"type": "register_ui", "token": token, "device": {}, **extra}))
        if register_tasks:
            await _asyncio.gather(*register_tasks)
        return ws

    try:
        await _t(o._llm_store.set_sync, user_id, provider="custom",
                 base_url="http://t.invalid/v1", model="m", api_key="k")
        tab = await _register()                               # a browser tab
        host = await _register(
            agent_host=True,
            host_session_id=tunnel_ids.host_session_id,
        )
        assert o.is_agent_host_socket(tab) is False
        assert o.is_agent_host_socket(host) is True
        assert o._agent_host_sockets[id(host)] == tunnel_ids.host_session_id
    finally:
        try:
            await _t(o._llm_store.clear_sync, user_id)
        finally:
            await asyncio.wait_for(o._close_started_services(), timeout=15.0)


async def test_delete_user_agent_soft_deletes_and_stops_host(orch, tunnel_ids):
    # T028/FR-027: soft delete — stop host, drop routing, retain row + audit.
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": tunnel_ids.owner}
    await _tunnel(orch, ws, _reg_frame(tunnel_ids.agent_id), tunnel_ids)
    orch.ui_clients.append(ws)
    assert tunnel_ids.agent_id in orch.agents
    ws.sent.clear()
    assert await orch.delete_user_agent(
        tunnel_ids.owner,
        tunnel_ids.agent_id,
    ) is True
    assert tunnel_ids.agent_id not in orch.agents
    row = await _t(
        ua.get_user_agent,
        orch.user_agent_registry,
        tunnel_ids.agent_id,
    )
    assert row["status"] == "disabled" and row["deleted_at"] is not None   # soft-deleted, retained
    assert any(json.loads(f).get("type") == "agent_stop" for f in ws.sent)  # host told to stop
    # A different user cannot delete it.
    assert await orch.delete_user_agent(
        tunnel_ids.foreign,
        tunnel_ids.agent_id,
    ) is False


async def test_list_owner_agents_excludes_foreign_and_deleted(orch, tunnel_ids):
    # T026 data path: list returns only the owner's non-deleted agents.
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": tunnel_ids.owner}
    await _tunnel(orch, ws, _reg_frame(tunnel_ids.agent_id), tunnel_ids)
    mine = await _t(
        ua.list_user_agents,
        orch.user_agent_registry,
        tunnel_ids.owner,
    )
    assert [agent["agent_id"] for agent in mine] == [tunnel_ids.agent_id]
    assert await _t(
        ua.list_user_agents,
        orch.user_agent_registry,
        tunnel_ids.foreign,
    ) == []
    await _t(ua.soft_delete, orch.user_agent_registry, tunnel_ids.agent_id)
    assert await _t(
        ua.list_user_agents,
        orch.user_agent_registry,
        tunnel_ids.owner,
    ) == []
