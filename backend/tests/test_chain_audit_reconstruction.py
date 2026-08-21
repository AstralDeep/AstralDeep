"""T019 (056-delegated-agent-chaining): two-hop chain reconstruction from the
tamper-evident audit log ALONE (FR-026, SC-003 — closes 048's deferred T018).

Drives a real two-hop chain (human → agent-a → agent-b → agent-c) through the
mediated hop seam with a REAL Recorder over the live ``audit_events`` table,
reconstructs the full authority path purely from stored rows, and proves
``verify_chain`` detects a tampered record.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from audit.recorder import Recorder, get_recorder, set_recorder  # noqa: E402
from audit.repository import AuditRepository  # noqa: E402
from orchestrator import delegation as dg  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from shared.protocol import AgentHopRequest, MCPResponse  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


@pytest.fixture(scope="module")
def db():
    with isolated_plane_runtime("chain_audit") as runtime:
        yield runtime


@pytest.fixture()
def recorder(db, tmp_path):
    prev = get_recorder()
    rec = Recorder(
        AuditRepository(
            plane_runtime=db,
            plane_repositories=db.repositories,
        ),
        retry_queue=tmp_path / "audit-retry.jsonl",
    )
    set_recorder(rec)
    yield rec
    set_recorder(prev)


@pytest.fixture(autouse=True)
def chaining_on(monkeypatch):
    monkeypatch.setitem(flags._flags, "recursive_delegation", True)


@pytest.fixture
def orch():
    from orchestrator.concurrency_cap import ConcurrencyCap
    from orchestrator.hooks import HookManager
    from orchestrator.orchestrator import Orchestrator

    # Keep the real delegation methods and audit repository, but do not
    # compose an unrelated application-scoped Plane graph for the method
    # harness itself.
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
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    for aid in ("agent-a", "agent-b", "agent-c"):
        o.local_agents[aid] = MagicMock()
    skills = [SimpleNamespace(id="tool_b"), SimpleNamespace(id="tool_c")]
    o.agent_cards["agent-b"] = SimpleNamespace(skills=skills)
    o.agent_cards["agent-c"] = SimpleNamespace(skills=skills)
    o._execute_with_retry = AsyncMock(return_value=MCPResponse(result="ok"))
    return o


async def _hop(orch, *, parent_req, initiator, callee, tool, hop_id):
    ws = SimpleNamespace(_hop_futures={})
    fut = asyncio.get_running_loop().create_future()
    ws._hop_futures[hop_id] = fut
    await orch._handle_agent_hop_request(ws, AgentHopRequest(
        request_id=hop_id, parent_request_id=parent_req,
        initiator_agent_id=initiator, callee_agent_id=callee,
        tool_name=tool, arguments={}))
    return await asyncio.wait_for(fut, timeout=5)


@pytest.mark.asyncio
async def test_two_hop_reconstruction_and_tamper_evidence(orch, recorder, db):
    user = f"u-chain-{uuid.uuid4().hex[:8]}"
    chat = f"c-{uuid.uuid4().hex[:8]}"
    now = int(time.time())

    # Root: the flat (depth-0) token the orchestrator minted for agent-a.
    root = {"sub": user, "act": {"sub": "agent:agent-a"},
            "scope": "tools:read tool:tool_b tool:tool_c",
            "iss": "mock-astral-delegation", "aud": "agent-svc",
            "iat": now, "exp": now + 300, "delegation": True}
    ui_ws = MagicMock()
    ui_ws.machine_claims = None
    orch.ui_sessions[ui_ws] = {"sub": user}
    orch._register_dispatch_context(
        "req-a", "agent-a",
        {"user_id": user, "session_id": chat,
         "_delegation_token": dg.encode_delegation_payload(root)}, ui_ws)

    # Hop 1: agent-a → agent-b.
    resp1 = await _hop(orch, parent_req="req-a", initiator="agent-a",
                       callee="agent-b", tool="tool_b", hop_id="hop-1")
    assert resp1.result == "ok"
    child1_token = orch._execute_with_retry.await_args.args[3]["_delegation_token"]

    # agent-b's dispatch is now in flight; the orchestrator records it.
    orch._register_dispatch_context(
        "req-b", "agent-b",
        {"user_id": user, "session_id": chat,
         "_delegation_token": child1_token}, ui_ws)

    # Hop 2: agent-b → agent-c (child minted off child1).
    resp2 = await _hop(orch, parent_req="req-b", initiator="agent-b",
                       callee="agent-c", tool="tool_c", hop_id="hop-2")
    assert resp2.result == "ok"

    # Give the Recorder's off-thread inserts a moment to land. The DB reads go
    # through asyncio.to_thread so the 052 event-loop-blocking guard (enforced
    # in CI via LOOP_GUARD_ENFORCE=1) does not flag them.
    def _read_rows():
        return db.fetch_all(
            "SELECT * FROM audit_events WHERE actor_user_id = ? "
            "ORDER BY recorded_at ASC, event_id ASC", (user,))

    for _ in range(50):
        rows = await asyncio.to_thread(_read_rows)
        if len(rows) >= 8:
            break
        await asyncio.sleep(0.1)
    rows = [dict(r) for r in rows]

    # ---- Reconstruction from audit_events ALONE (SC-003) ----
    hop_rows = [r for r in rows if r["event_class"] == "delegation"]
    enforce = [r for r in hop_rows if r["action_type"] == "delegation.hop.enforce"]
    assert len(enforce) == 2, f"expected 2 enforce rows, got {len(hop_rows)}"

    def _meta(r):
        m = r["inputs_meta"]
        if isinstance(m, Mapping):
            return json.loads(json.dumps(dict(m)))
        return json.loads(m)

    chains = sorted((_meta(r)["actor_chain"] for r in enforce), key=len)
    assert chains[0] == ["agent:agent-b", "agent:agent-a"]
    assert chains[1] == ["agent:agent-c", "agent:agent-b", "agent:agent-a"]
    # Full path recovered: human → a → b → c.
    path = [enforce[0]["actor_user_id"]] + list(reversed(chains[1]))
    assert path == [user, "agent:agent-a", "agent:agent-b", "agent:agent-c"]
    # Depths recorded per hop.
    assert sorted(_meta(r)["delegation_depth"] for r in enforce) == [1, 2]
    # Each hop's mint/enforce pair shares a correlation id with its tool pair.
    for r in enforce:
        corr = r["correlation_id"]
        pair = [x for x in rows if x["correlation_id"] == corr]
        kinds = sorted(x["action_type"] for x in pair)
        assert any(k.startswith("delegation.hop.mint") for k in kinds)
        assert any(k.startswith("tool.") and k.endswith(".start") for k in kinds)
        assert any(k.startswith("tool.") and k.endswith(".end") for k in kinds)

    # ---- Tamper evidence (verify_chain) ----
    # All DB work runs off the event loop (LOOP_GUARD_ENFORCE=1 in CI).
    repo = AuditRepository(
        plane_runtime=db,
        plane_repositories=db.repositories,
    )
    assert await asyncio.to_thread(repo.verify_chain, user) is None  # intact
    victim = enforce[-1]["event_id"]

    def _tamper():
        # audit_events is trigger-protected append-only (a DB-level UPDATE is
        # refused outright — itself part of the tamper-evidence posture). To
        # prove the HASH CHAIN also detects tampering, simulate an attacker with
        # direct storage access: a raw superuser session with triggers disabled.
        with db.transaction() as transaction:
            transaction.execute("SET LOCAL session_replication_role = replica")
            transaction.execute(
                "UPDATE audit_events SET description = 'tampered' "
                "WHERE event_id = %s",
                (victim,),
            )

    await asyncio.to_thread(_tamper)
    assert await asyncio.to_thread(repo.verify_chain, user) == victim
