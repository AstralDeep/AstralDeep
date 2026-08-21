"""T014-T017 (056-delegated-agent-chaining): the mediated hop seam.

An agent-initiated hop resolves its context and PARENT authority from the
orchestrator's own dispatch record, mints a strictly-narrower child
delegation (scopes ∩, exp ≤ parent, depth+1, actor chain terminating at the
human), and re-enters the FULL single-path gate stack — with the meta-tool
bypass structurally unavailable, empty intersections refused fail-closed,
per-hop verification, and every refusal per-call (the session is never torn
down). Credentials are injected per-(user, callee), never forwarded.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator import delegation as dg  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from shared.protocol import AgentHopRequest, MCPResponse  # noqa: E402


@pytest.fixture(autouse=True)
def chaining_on(monkeypatch):
    monkeypatch.setitem(flags._flags, "recursive_delegation", True)


@pytest.fixture
def orch():
    from orchestrator.concurrency_cap import ConcurrencyCap
    from orchestrator.hooks import HookManager
    from orchestrator.orchestrator import Orchestrator

    # Exercise the real delegation and dispatch methods without composing the
    # application-scoped Plane graph; every touched collaborator is explicit.
    o = Orchestrator.__new__(Orchestrator)
    o.agents = {}
    o.a2a_clients = {}
    o.local_agents = {}
    o.agent_cards = {}
    o.ui_sessions = {}
    o.security_flags = {}
    o._dispatch_context = {}
    o._chain_budgets = {}
    o._pending_cap_entries = {}
    o._hop_cap_entries = {}
    o._job_context = {}
    o._active_request = {}
    o._taint_trackers = {}
    o.concurrency_cap = ConcurrencyCap(max_per_user_agent=3)
    o.hooks = HookManager()
    o.stream_manager = None
    o._llm_store = SimpleNamespace(
        get=AsyncMock(return_value=None),
        get_system=AsyncMock(return_value=None),
    )
    o.send_ui_render = AsyncMock()
    o.tool_permissions = MagicMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o.tool_permissions.get_enabled_scope_names = MagicMock(return_value=["tools:read"])
    o.tool_permissions.get_tool_scope = MagicMock(return_value="tools:read")
    o._map_file_paths = lambda cid, a, **k: a
    o.credential_manager = MagicMock()
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(
        side_effect=lambda u, a: f"cred-for-{a}")
    o.local_agents["initiator-1"] = MagicMock()
    o.local_agents["callee-1"] = MagicMock()
    o.agent_cards["callee-1"] = SimpleNamespace(
        skills=[SimpleNamespace(id="peer_tool")])

    o._dispatched = {}

    async def _capture(ws, agent_id, tool_name, args, max_retries=None, **_kwargs):
        o._dispatched.update(agent_id=agent_id, tool_name=tool_name,
                             args=dict(args))
        return MCPResponse(result="peer-ok")

    o._execute_with_retry = _capture
    return o


def _parent(scope="tools:read tool:peer_tool", depth=0, user="u1",
            initiator="initiator-1", exp_in=300, act=None):
    now = int(time.time())
    p = {"sub": user, "act": act or {"sub": f"agent:{initiator}"},
         "scope": scope, "iss": "mock-astral-delegation", "aud": "agent-svc",
         "iat": now, "exp": now + exp_in, "delegation": True}
    if depth:
        p["delegation_depth"] = depth
        p["max_delegation_depth"] = 3
    return p


def _register_parent(orch, parent, *, req_id="req-parent", agent="initiator-1",
                     user="u1", chat="c1"):
    ui_ws = MagicMock()
    ui_ws.machine_claims = None
    orch._register_dispatch_context(
        req_id, agent,
        {"user_id": user, "session_id": chat,
         "_delegation_token": dg.encode_delegation_payload(parent)},
        ui_ws)
    return ui_ws


class _InitiatorWS:
    def __init__(self):
        self._hop_futures = {}


async def _run_hop(orch, *, callee="callee-1", tool="peer_tool",
                   parent_req="req-parent", initiator="initiator-1",
                   arguments=None):
    ws = _InitiatorWS()
    fut = asyncio.get_running_loop().create_future()
    ws._hop_futures["hop-1"] = fut
    await orch._handle_agent_hop_request(ws, AgentHopRequest(
        request_id="hop-1", parent_request_id=parent_req,
        initiator_agent_id=initiator, callee_agent_id=callee,
        tool_name=tool, arguments=arguments or {"q": "hi"}))
    return await asyncio.wait_for(fut, timeout=2)


def _err(resp):
    return (resp.error or {}).get("message", "") if resp else ""


# --------------------------------------------------------------------------- #
# The happy path: child authority invariants (T015, FR-002)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_hop_executes_under_child_authority(orch):
    parent = _parent()
    _register_parent(orch, parent)
    resp = await _run_hop(orch)
    assert resp.result == "peer-ok"
    assert orch._dispatched["agent_id"] == "callee-1"

    child_token = orch._dispatched["args"]["_delegation_token"]
    assert child_token != dg.encode_delegation_payload(parent)  # never the parent's
    child = dg.decode_token_payload(child_token)
    # scopes ⊆ parent
    assert set(child["scope"].split()) <= set(parent["scope"].split())
    assert child["scope"]  # non-empty grant
    # exp ≤ parent, aud/iss inherited
    assert child["exp"] <= parent["exp"]
    assert child["aud"] == parent["aud"]
    assert child["iss"] == parent["iss"]
    # depth = parent + 1
    assert child["delegation_depth"] == 1
    # actor chain names the callee then the initiator, terminating at the human
    assert dg.actor_chain(child) == ["agent:callee-1", "agent:initiator-1"]
    assert child["sub"] == "u1"


@pytest.mark.asyncio
async def test_initiator_credentials_never_forwarded(orch):
    """FR-008: the callee gets its own per-(user, callee) credentials."""
    _register_parent(orch, _parent())
    await _run_hop(orch)
    assert orch._dispatched["args"]["_credentials"] == "cred-for-callee-1"
    assert "cred-for-initiator-1" not in str(orch._dispatched["args"])


# --------------------------------------------------------------------------- #
# Mediation refusals (T014) — all per-call, honest, non-terminating
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_flag_off_hop_is_inert(orch, monkeypatch):
    monkeypatch.setitem(flags._flags, "recursive_delegation", False)
    _register_parent(orch, _parent())
    resp = await _run_hop(orch)
    assert "chaining is disabled" in _err(resp)
    assert not orch._dispatched


@pytest.mark.asyncio
async def test_unknown_parent_dispatch_refused(orch):
    resp = await _run_hop(orch, parent_req="req-never-registered")
    assert "no active parent dispatch" in _err(resp)
    assert not orch._dispatched


@pytest.mark.asyncio
async def test_initiator_spoof_refused(orch):
    """The frame's initiator must match OUR record of the dispatch."""
    _register_parent(orch, _parent(), agent="initiator-1")
    resp = await _run_hop(orch, initiator="evil-agent-9")
    assert "no active parent dispatch" in _err(resp)
    assert not orch._dispatched


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", ["__orchestrator__", "__scheduler__",
                                      "__memory__", "__desktop_codegen__"])
async def test_hop_cannot_reach_meta_tool_handlers(orch, reserved):
    """FR-003/FR-018: reserved pseudo-agent ids are structurally unavailable."""
    _register_parent(orch, _parent())
    resp = await _run_hop(orch, callee=reserved)
    assert "not a dispatchable agent" in _err(resp)
    assert not orch._dispatched


@pytest.mark.asyncio
async def test_no_parent_authority_refused(orch):
    """A parent dispatch that ran unscoped (dev fail-open) cannot spawn hops —
    hops exercise real minting in every posture (D17.2)."""
    ui_ws = MagicMock()
    ui_ws.machine_claims = None
    orch._register_dispatch_context(
        "req-parent", "initiator-1",
        {"user_id": "u1", "session_id": "c1"}, ui_ws)  # no token
    resp = await _run_hop(orch)
    assert "no delegated authority" in _err(resp)
    assert not orch._dispatched


@pytest.mark.asyncio
async def test_disabled_callee_refused_and_session_survives(orch):
    """US1-AS2: explicit opt-out always wins; honest error, no teardown."""
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=False)
    _register_parent(orch, _parent())
    resp = await _run_hop(orch)
    assert "restricted for this agent" in _err(resp)
    assert not orch._dispatched
    # A second hop still flows through mediation — nothing was torn down.
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    resp2 = await _run_hop(orch)
    assert resp2.result == "peer-ok"


@pytest.mark.asyncio
async def test_security_flag_block_refuses_hop(orch):
    """FR-029: hard security-flag blocks are never clearable by chaining."""
    orch.security_flags["callee-1"] = {
        "peer_tool": {"blocked": True, "reason": "threat"}}
    _register_parent(orch, _parent())
    resp = await _run_hop(orch)
    assert "system-blocked" in _err(resp)
    assert not orch._dispatched


# --------------------------------------------------------------------------- #
# Mint-time refusals (T015/T016, FR-002/FR-005)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_over_depth_refused_fail_closed(orch):
    """US1-AS3: a chain already at max depth cannot mint a further hop."""
    deep = _parent(depth=3, act={"sub": "agent:initiator-1",
                                 "act": {"sub": "agent:b",
                                         "act": {"sub": "agent:a"}}})
    _register_parent(orch, deep)
    resp = await _run_hop(orch)
    assert "depth" in _err(resp).lower() or "budget" in _err(resp).lower()
    assert not orch._dispatched


@pytest.mark.asyncio
async def test_empty_intersection_refused(orch):
    """FR-005/D3: an empty scope intersection refuses — never a silent
    do-nothing token."""
    _register_parent(orch, _parent(scope="tools:write tool:unrelated_tool"))
    resp = await _run_hop(orch)
    assert "Chained hop refused" in _err(resp)
    assert not orch._dispatched


@pytest.mark.asyncio
async def test_out_of_scope_tool_refused_by_enforcement(orch):
    """T017: authorize_chained_tool_call refuses a tool outside the child's
    attenuated scopes, per-call, without teardown."""
    orch.agent_cards["callee-1"] = SimpleNamespace(
        skills=[SimpleNamespace(id="peer_tool"), SimpleNamespace(id="other_tool")])
    orch.tool_permissions.get_enabled_scope_names = MagicMock(
        return_value=["tools:search"])
    # Parent grants tools:search + tool:other_tool — peer_tool itself is not
    # coverable, and the child's tool-level scopes exclude it.
    _register_parent(orch, _parent(scope="tools:search tool:other_tool"))
    resp = await _run_hop(orch)
    assert "outside delegated scope" in _err(resp)
    assert not orch._dispatched


@pytest.mark.asyncio
async def test_tampered_actor_chain_refused(orch):
    """T017: a malformed/severed act chain fails verification per-call."""
    bad = _parent(act={"sub": "agent:initiator-1", "act": {"broken": True}})
    _register_parent(orch, bad)
    resp = await _run_hop(orch)
    assert "actor chain" in _err(resp)
    assert not orch._dispatched


# --------------------------------------------------------------------------- #
# Chain budget (FR-021, charged per hop at mediation)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_hop_budget_exhaustion_refuses(orch):
    from orchestrator.chain_authority import ChainBudget
    orch._chain_budgets["c1"] = ChainBudget(turn_id="t", chat_id="c1",
                                            max_hops=1, wall_clock_s=999)
    _register_parent(orch, _parent())
    first = await _run_hop(orch)
    assert first.result == "peer-ok"
    second = await _run_hop(orch)
    assert "budget exhausted" in _err(second)


# --------------------------------------------------------------------------- #
# Dispatch-parity hop leg (T010 extension): gates refuse hops identically
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_hop_gate_refusal_matches_single_path(orch):
    orch.security_flags["callee-1"] = {
        "peer_tool": {"blocked": True, "reason": "threat"}}
    _register_parent(orch, _parent())
    hop_resp = await _run_hop(orch)

    import json as _json
    tc = SimpleNamespace(function=SimpleNamespace(
        name="peer_tool", arguments=_json.dumps({"q": "hi"})))
    direct = await orch.execute_single_tool(
        MagicMock(), tc, {"peer_tool": "callee-1"}, "c1", user_id="u1")
    assert _err(hop_resp) == (direct.error or {}).get("message")
