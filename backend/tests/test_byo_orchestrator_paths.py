"""Feature 058 — orchestrator tunnel / delivery / registration EDGE branches.

``test_byo_tunnel.py`` pins the happy owner-bound tunnel; this covers the
fail-safe corners: a malformed frame, the per-owner ingress cap dropping a frame
inside the tunnel handler, a reconnect superseding a stale socket, honest-offline
NOTIFYING the owner's other sockets, and the send/close/go_live exception handlers
that must never abort a delivery, delete, or refusal.

Sync DB helpers ride ``_t`` (asyncio.to_thread) — 052's loop-blocking detector is
CI-enforced with an empty allowlist.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.orchestrator import Orchestrator  # noqa: E402
from orchestrator import user_agents as ua  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from shared.local_transport import TunnelSocket  # noqa: E402
from shared.protocol import AgentCard, AgentSkill, RegisterAgent  # noqa: E402

async def _t(fn, *a, **k):
    return await asyncio.to_thread(fn, *a, **k)


class FakeUI:
    def __init__(self):
        self.sent = []

    async def send_text(self, t):
        self.sent.append(t)

    async def send(self, t):
        self.sent.append(t)

    async def close(self, *a, **k):
        return None


class RaisingUI(FakeUI):
    """A socket that blows up on every outbound send — the orchestrator must
    swallow it and carry on with the other sockets."""

    async def send_text(self, t):
        raise RuntimeError("socket send exploded")

    async def send(self, t):
        raise RuntimeError("socket send exploded")


@dataclass(frozen=True)
class EdgeIds:
    owner: str
    foreign: str
    agent_id: str
    host_session_id: str
    reconnect_session_id: str


@pytest.fixture()
def edge_ids():
    token = uuid.uuid4().hex[:12]
    return EdgeIds(
        owner=f"byoedge-owner-{token}",
        foreign=f"byoedge-foreign-{token}",
        agent_id=f"byoedge-greeter-{token}",
        host_session_id=f"byoedge-host-{token}",
        reconnect_session_id=f"byoedge-reconnect-{token}",
    )


@pytest.fixture()
async def orch(monkeypatch, edge_ids):
    monkeypatch.setitem(flags._flags, "byo_agents", True)
    o = await asyncio.to_thread(Orchestrator)
    try:
        await _t(
            ua.create_user_agent,
            o.user_agent_registry,
            agent_id=edge_ids.agent_id,
            owner_user_id=edge_ids.owner,
            display_name="Greeter",
        )
        await _t(
            ua.mark_validated,
            o.user_agent_registry,
            edge_ids.agent_id,
            "0.1.0",
        )
        yield o
    finally:
        try:
            row = await _t(
                ua.get_user_agent,
                o.user_agent_registry,
                edge_ids.agent_id,
            )
            if row is not None and row.get("deleted_at") is None:
                await _t(ua.soft_delete, o.user_agent_registry, edge_ids.agent_id)
        finally:
            await asyncio.wait_for(o._close_started_services(), timeout=15.0)


def _reg_frame(agent_id):
    card = AgentCard(name="Greeter", description="greets", agent_id=agent_id,
                     skills=[AgentSkill(name="greet", description="g", id="greet",
                                        scope="tools:read", input_schema={})])
    return RegisterAgent(agent_card=card).to_json()


async def _tunnel(
    o,
    ws,
    edge_ids,
    frame=None,
    agent_id=None,
    host_session_id=None,
):
    agent_id = agent_id or edge_ids.agent_id
    host_session_id = host_session_id or edge_ids.host_session_id
    payload = {"agent_id": agent_id, "frame": frame if frame is not None else _reg_frame(agent_id),
               "host_session_id": host_session_id}
    await o._handle_agent_tunnel(ws, SimpleNamespace(action="agent_tunnel", payload=payload))


# ── malformed frame + ingress cap inside the tunnel handler ──────────────────

async def test_tunnel_ignores_a_frame_missing_the_agent_id(orch, edge_ids):
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": edge_ids.owner}
    await orch._handle_agent_tunnel(ws, SimpleNamespace(
        action="agent_tunnel",
        payload={"frame": _reg_frame(edge_ids.agent_id)},
    ))   # no agent_id
    assert edge_ids.agent_id not in orch.agents  # malformed frame registered nothing


async def test_tunnel_drops_a_frame_that_trips_the_ingress_cap(
    orch,
    edge_ids,
    monkeypatch,
):
    monkeypatch.setattr(type(orch), "_TUNNEL_MAX_FRAMES_PER_WINDOW", 1)
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": edge_ids.owner}
    assert orch._tunnel_ingress_over_cap(edge_ids.owner) is False
    await _tunnel(orch, ws, edge_ids)  # this frame is over the cap
    assert edge_ids.agent_id not in orch.agents  # dropped before registration


# ── reconnect supersedes the stale socket ────────────────────────────────────

async def test_tunnel_reconnect_supersedes_the_stale_socket(orch, edge_ids):
    ws1 = FakeUI()
    orch.ui_sessions[ws1] = {"sub": edge_ids.owner}
    await _tunnel(orch, ws1, edge_ids)
    sock = orch._tunnel_sockets[(edge_ids.owner, edge_ids.agent_id)]
    assert isinstance(sock, TunnelSocket) and sock.ui_websocket is ws1

    ws2 = FakeUI()                              # the host reconnects on a new socket
    orch.ui_sessions[ws2] = {"sub": edge_ids.owner}
    await _tunnel(
        orch,
        ws2,
        edge_ids,
        host_session_id=edge_ids.reconnect_session_id,
    )
    assert orch._tunnel_sockets[(edge_ids.owner, edge_ids.agent_id)] is sock
    assert sock.ui_websocket is ws2
    assert sock.host_session_id == edge_ids.reconnect_session_id
    # outbound now rides the new socket, not the stale one
    ws1.sent.clear()
    ws2.sent.clear()
    await sock.send('{"x":1}')
    assert any(json.loads(f).get("type") == "agent_tunnel" for f in ws2.sent)
    assert ws1.sent == []


# ── honest-offline notifies the owner's OTHER sockets ────────────────────────

async def test_teardown_notifies_the_owners_other_sockets_and_skips_foreigners(
    orch,
    edge_ids,
):
    host = FakeUI()
    orch.ui_sessions[host] = {"sub": edge_ids.owner}
    orch.ui_clients.append(host)
    await _tunnel(orch, host, edge_ids)
    assert edge_ids.agent_id in orch.agents

    other = FakeUI()                            # the owner's second socket (e.g. a tab)
    orch.ui_sessions[other] = {"sub": edge_ids.owner}
    orch.ui_clients.append(other)
    stranger = FakeUI()                         # a different user — must NOT be told
    orch.ui_sessions[stranger] = {"sub": edge_ids.foreign}
    orch.ui_clients.append(stranger)

    await orch._teardown_owner_tunnels(host)
    assert edge_ids.agent_id not in orch.agents  # went offline
    offline = [json.loads(f) for f in other.sent if json.loads(f).get("type") == "agent_offline"]
    assert offline and offline[0]["agent_id"] == edge_ids.agent_id
    assert stranger.sent == []                  # the stranger heard nothing


# ── deliver / delete / register exception handlers are swallowed ─────────────

async def test_delivery_swallows_a_raising_low_level_send(
    orch,
    edge_ids,
    monkeypatch,
):
    host = FakeUI()
    orch._agent_host_sockets[id(host)] = edge_ids.host_session_id
    orch.ui_sessions[host] = {"sub": edge_ids.owner}
    orch.ui_clients.append(host)

    async def _raise(ui, frame):
        raise RuntimeError("low-level send exploded")

    monkeypatch.setattr(orch, "_safe_send", _raise)
    # The push to the one host raises, but delivery must not crash — with no host
    # actually reached it reports 0, which the caller surfaces as honest 'no_host'.
    delivered = await orch.deliver_agent_bundle(
        edge_ids.owner,
        edge_ids.agent_id,
        {"mcp_tools.py": "c"},
        "0.1.0",
    )
    assert delivered == 0


async def test_delete_skips_foreign_sockets_and_swallows_a_send_error(
    orch,
    edge_ids,
):
    host = FakeUI()
    orch.ui_sessions[host] = {"sub": edge_ids.owner}
    await _tunnel(orch, host, edge_ids)
    orch.ui_clients.append(host)
    stranger = FakeUI()
    orch.ui_sessions[stranger] = {"sub": edge_ids.foreign}
    orch.ui_clients.append(stranger)
    boom = RaisingUI()                          # an owner socket whose agent_stop send fails
    orch.ui_sessions[boom] = {"sub": edge_ids.owner}
    orch.ui_clients.append(boom)

    assert await orch.delete_user_agent(
        edge_ids.owner,
        edge_ids.agent_id,
    ) is True
    assert edge_ids.agent_id not in orch.agents
    assert stranger.sent == []                                # foreign socket skipped
    row = await _t(
        ua.get_user_agent,
        orch.user_agent_registry,
        edge_ids.agent_id,
    )
    assert row["status"] == "disabled" and row["deleted_at"] is not None


async def test_refused_tunnel_registration_closes_and_swallows_a_close_error(
    orch,
    edge_ids,
):
    """A foreign-owner tunnel registration is refused, audited, and the socket is
    closed — a close() that itself raises must not turn the refusal into a crash."""
    closed = {"tried": False}

    class ClosingWS(FakeUI):
        is_user_agent_tunnel = True

        async def close(self, *a, **k):
            closed["tried"] = True
            raise RuntimeError("close exploded")

    ws = ClosingWS()
    ws.owner_sub = edge_ids.foreign
    ws.host_session_id = edge_ids.host_session_id
    await orch.register_agent(
        ws,
        RegisterAgent.from_json(_reg_frame(edge_ids.agent_id)),
    )
    assert closed["tried"] is True              # it attempted to close the socket
    assert edge_ids.agent_id not in orch.agents  # and did not register the agent


async def test_go_live_failure_during_registration_is_swallowed(
    orch,
    edge_ids,
    monkeypatch,
):
    """If go_live raises as the owner's host registers inward, the exception is
    logged, not propagated — the socket is already routed."""
    from orchestrator import user_agents as _ua_mod

    def _boom(*a, **k):
        raise RuntimeError("go_live exploded")

    monkeypatch.setattr(_ua_mod, "go_live", _boom)

    class TunnelWS(FakeUI):
        is_user_agent_tunnel = True

    ws = TunnelWS()
    ws.owner_sub = edge_ids.owner
    ws.host_session_id = edge_ids.host_session_id
    await orch.register_agent(
        ws,
        RegisterAgent.from_json(_reg_frame(edge_ids.agent_id)),
    )
    # Registration proceeded (routing set) even though go_live failed.
    assert orch.agents.get(edge_ids.agent_id) is ws
