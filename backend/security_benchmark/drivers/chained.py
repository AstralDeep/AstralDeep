"""Drives chained-attack cases through the real recursive-delegation gates
(orchestrator/chain_authority.py, delegation.py) so a BLOCKED trace reflects genuine
enforcement, not a scripted model; toggled by FF_RECURSIVE_DELEGATION.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from security_benchmark.adapters.base import (
    BenchmarkCase,
    CaseTrace,
    ToolCallObservation,
)
from security_benchmark.drivers.base import Driver
from security_benchmark.envelope import LAYER_CHAINED_DELEGATION, EnvelopeConfig

_CHAIN = LAYER_CHAINED_DELEGATION


class ChainedDriver(Driver):
    mode = "chained_real"

    def run_case(self, case: BenchmarkCase, envelope: EnvelopeConfig) -> CaseTrace:
        return asyncio.run(self._run(case, envelope))

    async def _run(self, case: BenchmarkCase, envelope: EnvelopeConfig) -> CaseTrace:
        from shared.feature_flags import flags

        trace = CaseTrace(case_id=case.case_id, envelope_label=envelope.label)
        chaining_on = envelope.is_enabled(_CHAIN)
        is_control = case.meta.get("control") == "1"

        prior = flags._flags.get("recursive_delegation")
        flags._flags["recursive_delegation"] = chaining_on
        try:
            executed, blocked_by, attempted = await self._drive(case, chaining_on)
        finally:
            flags._flags["recursive_delegation"] = prior

        if is_control:
            trace.bait_taken = False
            trace.tool_calls.append(ToolCallObservation(
                tool=case.objective.target_tool or "peer_tool",
                executed=executed, is_adversarial=False,
                blocked_by=blocked_by))
            trace.notes = ("control: legitimate hop "
                           + ("executed" if executed else f"WRONGLY blocked by {blocked_by}"))
            return trace

        if not attempted:
            trace.bait_taken = False
            trace.notes = "chaining off — attack not attemptable through a retired/closed seam"
            return trace

        trace.bait_taken = True
        trace.tool_calls.append(ToolCallObservation(
            tool=case.objective.target_tool or "peer_tool",
            required_scope=case.objective.required_scope,
            in_scope=False, executed=executed, is_adversarial=True,
            blocked_by=None if executed else (blocked_by or _CHAIN)))
        trace.notes = ("attack EXECUTED (enforcement gap!)" if executed
                       else f"attack blocked by {blocked_by or _CHAIN}")
        return trace

    async def _drive(self, case: BenchmarkCase, chaining_on: bool):
        kind = case.objective.kind
        if kind == "chained_consent_replay":
            return await self._drive_consent_replay(chaining_on)
        return await self._drive_hop(case, chaining_on)

    async def _drive_hop(self, case: BenchmarkCase, chaining_on: bool):
        import os

        from orchestrator import delegation as dg
        from orchestrator.orchestrator import Orchestrator
        from shared.protocol import AgentHopRequest, MCPResponse

        o = Orchestrator.__new__(Orchestrator)
        o.send_ui_render = AsyncMock()
        o.security_flags = {}
        o.ui_sessions = {}
        o._dispatch_context = {}
        o._chain_budgets = {}
        o.agents = {}
        o.a2a_clients = {}
        o.local_agents = {}
        o.agent_cards = {}
        o._pending_cap_entries = {}
        o._hop_cap_entries = {}
        o._job_context = {}
        o.tool_permissions = MagicMock()
        o.credential_manager = MagicMock()
        o._is_long_running_tool = lambda _agent_id, _tool_name: False
        o._tool_accepts_context_arg = lambda _agent_id, _tool_name, _arg: False
        o._auto_subscribe_stream_artifacts = AsyncMock()
        o._is_user_agent = lambda _agent_id: False
        tool = case.objective.target_tool or "peer_tool"
        kind = case.objective.kind
        is_control = case.meta.get("control") == "1"

        def _allowed(uid, aid, tname):
            if kind == "cross_hop_escalation" and not is_control and tname == tool:
                return False
            return True
        o.tool_permissions.is_tool_allowed = MagicMock(side_effect=_allowed)
        o.tool_permissions.get_enabled_scope_names = MagicMock(return_value=["tools:read"])
        o.tool_permissions.get_tool_scope = MagicMock(
            return_value=case.objective.required_scope or "tools:read")
        o._map_file_paths = lambda cid, a, **k: a
        o.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
        o.agents["initiator-1"] = MagicMock()
        o.agents["callee-1"] = MagicMock()
        o.agent_cards["callee-1"] = SimpleNamespace(
            skills=[SimpleNamespace(id=tool)],
        )

        executed = {"v": False}

        async def _dispatch(ws, agent_id, tool_name, args, **_kwargs):
            executed["v"] = True
            return MCPResponse(result="EXECUTED")

        o._execute_with_retry = _dispatch

        parent = self._parent_for(case, dg)
        ui_ws = MagicMock()
        ui_ws.machine_claims = None
        o.ui_sessions[ui_ws] = {"sub": "human-1"}
        o._register_dispatch_context(
            "req-parent", "initiator-1",
            {"user_id": "human-1", "session_id": "chat-1",
             "_delegation_token": dg.encode_delegation_payload(parent)}, ui_ws)

        ws = MagicMock()
        ws._hop_futures = {}
        fut = asyncio.get_running_loop().create_future()
        ws._hop_futures["hop-1"] = fut

        _quiet = {"FF_RUNTIME_SUPERVISOR": "false", "FF_HITL_HIGHRISK": "false",
                  "FF_TAINT_TRACKING": "false", "FF_POLICY_ENGINE": "false"}
        _saved = {k: os.environ.get(k) for k in _quiet}
        os.environ.update(_quiet)
        try:
            await o._handle_agent_hop_request(ws, AgentHopRequest(
                request_id="hop-1", parent_request_id="req-parent",
                initiator_agent_id="initiator-1", callee_agent_id="callee-1",
                tool_name=tool, arguments={}))
            resp = await asyncio.wait_for(fut, timeout=5)
        finally:
            for k, v in _saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        refused = resp.error is not None
        if not chaining_on:
            return False, None, False
        blocked_by = _CHAIN if refused else None
        return executed["v"], blocked_by, True

    def _parent_for(self, case: BenchmarkCase, dg):
        now = int(time.time())
        kind = case.objective.kind
        tool = case.objective.target_tool or "peer_tool"

        if kind == "confused_deputy":
            if case.meta.get("control") == "1":
                return _tok(now, f"tools:read tool:{tool}")
            return _tok(now, "tools:search tool:some_other_tool")

        if kind == "cross_hop_escalation":
            return _tok(now, "tools:read tool:some_read_tool")

        if kind == "depth_violation":
            act = {"sub": "agent:initiator-1",
                   "act": {"sub": "agent:b", "act": {"sub": "agent:a"}}}
            return _tok(now, f"tools:read tool:{tool}", depth=3, act=act)

        if kind == "actor_chain_forgery":
            return _tok(now, f"tools:read tool:{tool}",
                        act={"sub": "agent:initiator-1", "act": {"forged": True}})

        return _tok(now, f"tools:read tool:{tool}")

    async def _drive_consent_replay(self, chaining_on: bool):
        from orchestrator.chain_authority import AuthoritySkip, MachineTurnAuthority

        orch = MagicMock()
        orch.tool_permissions.get_agent_scopes = MagicMock(
            return_value={"tools:read": True})
        grants = MagicMock()
        grants.is_valid = MagicMock(return_value=False)
        grants.latest_valid_for = MagicMock(return_value=None)
        grants.mint_access_token = AsyncMock(return_value="should-never-mint")

        authority = await MachineTurnAuthority(orch, grants).derive(
            user_id="human-1", agent_id="web-research-1",
            consented_scopes=["tools:read"], grant_id="revoked-grant",
            turn_class="scheduled_job")
        skipped = isinstance(authority, AuthoritySkip)
        grants.mint_access_token.assert_not_awaited()
        executed = not skipped
        if not chaining_on:
            return False, None, False
        return executed, (_CHAIN if skipped else None), True


def _tok(now: int, scope: str, *, depth: int = 0, act=None) -> dict:
    t = {
        "sub": "human-1",
        "act": act or {"sub": "agent:initiator-1"},
        "scope": scope,
        "iss": "mock-astral-delegation",
        "aud": "agent-service",
        "iat": now,
        "exp": now + 300,
        "delegation": True,
    }
    if depth:
        t["delegation_depth"] = depth
        t["max_delegation_depth"] = 3
    return t
