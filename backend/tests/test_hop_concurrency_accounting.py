"""T009 (056-delegated-agent-chaining): dual-slot hop concurrency accounting.

A long-running chained hop charges BOTH the executing agent's and the
initiating agent's (user, agent) slots (FR-019), so fan-out cannot multiply a
user's effective concurrency past the per-agent cap on either side.
Reject-not-queue semantics are preserved, and every release site frees both
slots.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.orchestrator import GateRefusal, PreparedDispatch  # noqa: E402


@pytest.fixture
def orch(monkeypatch, orchestrator_factory):
    o = orchestrator_factory()
    o.audit_recorder = MagicMock()
    o.audit_recorder.record = AsyncMock()
    o.send_ui_render = AsyncMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o._map_file_paths = lambda cid, a, **k: a
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    o.local_agents["callee"] = MagicMock()
    o.local_agents["initiator"] = MagicMock()
    monkeypatch.setattr(o, "_is_long_running_tool", lambda a, t: True)
    return o


async def _auth_hop(orch, *, initiator="initiator", callee="callee"):
    return await orch._authorize_and_prepare(
        MagicMock(), callee, "long_tool", {}, "c1", "u1",
        initiating_agent_id=initiator)


@pytest.mark.asyncio
async def test_hop_charges_both_slots(orch):
    out = await _auth_hop(orch)
    assert isinstance(out, PreparedDispatch)
    cap_id = out.cap_job_id
    assert orch.concurrency_cap.inflight_count("u1", "callee") == 1
    assert orch.concurrency_cap.inflight_count("u1", "initiator") == 1
    assert orch._pending_cap_entries[cap_id] == ("u1", "callee")
    assert orch._hop_cap_entries[cap_id] == ("u1", "initiator")


@pytest.mark.asyncio
async def test_direct_call_charges_only_executing_slot(orch):
    out = await orch._authorize_and_prepare(
        MagicMock(), "callee", "long_tool", {}, "c1", "u1")
    assert isinstance(out, PreparedDispatch)
    assert orch.concurrency_cap.inflight_count("u1", "callee") == 1
    assert not orch._hop_cap_entries


@pytest.mark.asyncio
async def test_initiator_fanout_bounded_by_own_cap(orch):
    """One initiator fanning out to N callees is bounded by ITS slot cap."""
    cap = orch.concurrency_cap.max_per_user_agent
    for i in range(cap):
        orch.local_agents[f"callee{i}"] = MagicMock()
        out = await _auth_hop(orch, callee=f"callee{i}")
        assert isinstance(out, PreparedDispatch), f"hop {i} should be admitted"
    orch.local_agents["callee-extra"] = MagicMock()
    out = await _auth_hop(orch, callee="callee-extra")
    assert isinstance(out, GateRefusal)
    assert "initiator" in (out.response.error or {}).get("message", "")
    # The refused hop must not leak the callee slot it briefly held.
    assert orch.concurrency_cap.inflight_count("u1", "callee-extra") == 0


@pytest.mark.asyncio
async def test_executing_slot_cap_still_applies(orch):
    cap = orch.concurrency_cap.max_per_user_agent
    for i in range(cap):
        assert await orch.concurrency_cap.acquire("u1", "callee", f"j{i}")
    out = await _auth_hop(orch)
    assert isinstance(out, GateRefusal)
    assert "callee" in (out.response.error or {}).get("message", "")
    # Rejected before touching the initiator's slot.
    assert orch.concurrency_cap.inflight_count("u1", "initiator") == 0


@pytest.mark.asyncio
async def test_release_frees_both_slots(orch):
    out = await _auth_hop(orch)
    cap_id = out.cap_job_id
    entry = orch._pending_cap_entries.pop(cap_id)
    await orch.concurrency_cap.release(entry[0], entry[1], cap_id)
    await orch._release_hop_cap_slot(cap_id)
    assert orch.concurrency_cap.inflight_count("u1", "callee") == 0
    assert orch.concurrency_cap.inflight_count("u1", "initiator") == 0
    assert cap_id not in orch._hop_cap_entries


@pytest.mark.asyncio
async def test_self_hop_charges_single_slot_once(orch):
    """A→A hops must not double-charge (and thus deadlock) one slot."""
    out = await _auth_hop(orch, initiator="callee", callee="callee")
    assert isinstance(out, PreparedDispatch)
    assert orch.concurrency_cap.inflight_count("u1", "callee") == 1
    assert not orch._hop_cap_entries


# ── disconnect sweep: whose slot does an agent's death actually free? ─────────
#
# _pending_cap_entries is keyed by the EXECUTING agent; _hop_cap_entries by the
# INITIATING one. Sweeping both dicts in a single teardown loop meant an
# initiator's disconnect tore down a job still running on a live callee.

@pytest.mark.asyncio
async def test_sweep_on_initiator_death_spares_the_live_callee(orch):
    """The initiator drops mid-hop. Its own dual-charged slot must be released,
    but the callee is still executing: its slot, its _pending_cap_entries row and
    its _job_context must all survive, or the terminal ToolProgress finds no
    context and the result never reaches the chat."""
    out = await _auth_hop(orch)
    cap_id = out.cap_job_id
    orch._job_context[cap_id] = {"chat_id": "c1", "user_id": "u1"}

    swept = await orch._sweep_cap_slots_for_agent("initiator")

    assert swept == 1
    # Initiator's dual charge released.
    assert orch.concurrency_cap.inflight_count("u1", "initiator") == 0
    assert cap_id not in orch._hop_cap_entries
    # Callee untouched — still running.
    assert orch.concurrency_cap.inflight_count("u1", "callee") == 1
    assert orch._pending_cap_entries[cap_id] == ("u1", "callee")
    assert cap_id in orch._job_context


@pytest.mark.asyncio
async def test_sweep_on_executor_death_releases_both_and_drops_context(orch):
    """The executing agent dies, so its JobPoller dies with it and nothing will
    ever emit the terminal progress — full teardown on both sides."""
    out = await _auth_hop(orch)
    cap_id = out.cap_job_id
    orch._job_context[cap_id] = {"chat_id": "c1", "user_id": "u1"}

    swept = await orch._sweep_cap_slots_for_agent("callee")

    assert swept == 1
    assert orch.concurrency_cap.inflight_count("u1", "callee") == 0
    assert orch.concurrency_cap.inflight_count("u1", "initiator") == 0
    assert cap_id not in orch._pending_cap_entries
    assert cap_id not in orch._hop_cap_entries
    assert cap_id not in orch._job_context


@pytest.mark.asyncio
async def test_sweep_counts_each_job_once_for_a_self_hop(orch):
    """A→A: the cid appears only in _pending_cap_entries, so it must not be
    counted or released twice."""
    await _auth_hop(orch, initiator="callee", callee="callee")

    swept = await orch._sweep_cap_slots_for_agent("callee")

    assert swept == 1
    assert orch.concurrency_cap.inflight_count("u1", "callee") == 0
    assert not orch._pending_cap_entries


@pytest.mark.asyncio
async def test_sweep_of_unrelated_agent_touches_nothing(orch):
    out = await _auth_hop(orch)
    cap_id = out.cap_job_id
    assert await orch._sweep_cap_slots_for_agent("someone-else") == 0
    assert orch._pending_cap_entries[cap_id] == ("u1", "callee")
    assert orch._hop_cap_entries[cap_id] == ("u1", "initiator")
