"""Feature 058 (T031) — user-agent lifecycle through the REAL orchestrator.

Unlike ``test_byo_authoring_flow`` (which pins the phase machine), these drive the
chrome surface's handlers against a live ``Orchestrator``: owner-only listing,
derived running/offline status, revise-requires-a-fresh-Analyze, delete-stops-the-
host, and cross-user invisibility. Only LLM-dependent code generation is stubbed;
draft creation, the tunnel, registration, owner binding, routing, and soft-delete
paths are the real ones.

Sync (DB-touching) helpers ride ``_t`` (asyncio.to_thread) — feature 052's
event-loop-blocking detector is CI-enforced with an empty allowlist.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.orchestrator import Orchestrator  # noqa: E402
from orchestrator import agent_authoring as aa  # noqa: E402
from orchestrator import user_agents as ua  # noqa: E402
from orchestrator.agent_generator import (  # noqa: E402
    BYO_BUNDLE_FILENAMES,
    BYO_RUNTIME_CONTRACT_VERSION,
    BYO_RUNTIME_LOCK_SHA256,
)
from orchestrator.agent_lifecycle import BYO_ORIGIN  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from shared.protocol import AgentCard, AgentSkill, RegisterAgent  # noqa: E402
from orchestrator.projection_surfaces import authoring  # noqa: E402

BUNDLE = {name: f"# {name}\n" for name in BYO_BUNDLE_FILENAMES}
BUNDLE["mcp_tools.py"] = "TOOL_REGISTRY = {}\n"


async def _t(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


class FakeUI:
    def __init__(self):
        self.sent = []

    async def send_text(self, t):
        self.sent.append(t)

    async def send(self, t):
        self.sent.append(t)

    async def close(self, *a, **k):
        return None


@dataclass(frozen=True)
class LifecycleIds:
    owner: str
    foreign: str
    agent_id: str
    host_session_id: str


@pytest.fixture
def lifecycle_ids():
    token = uuid.uuid4().hex[:12]
    return LifecycleIds(
        owner=f"byolife-owner-{token}",
        foreign=f"byolife-foreign-{token}",
        agent_id=f"ua-mailer-{token}",
        host_session_id=f"hs-{token}",
    )


async def _cleanup(orch, ids):
    """Tear down only this fixture's typed, owner-scoped Plane records."""
    registry = orch.user_agent_registry
    draft_store = orch.lifecycle_manager.draft_store
    row = await _t(ua.get_user_agent, registry, ids.agent_id)
    if row is not None and row.get("deleted_at") is None:
        await _t(ua.soft_delete, registry, ids.agent_id)
    drafts = await _t(
        draft_store.list_byo_sessions,
        ids.owner,
        origin=BYO_ORIGIN,
        limit=2000,
    )
    for draft in drafts:
        await _t(draft_store.delete_draft_agent, draft["id"])


@pytest.fixture()
async def orch(monkeypatch, lifecycle_ids):
    monkeypatch.setitem(flags._flags, "byo_agents", True)
    o = await asyncio.to_thread(Orchestrator)
    # Code generation is the only LLM-dependent lifecycle call in this suite.
    # Draft creation remains real and persists through PlaneDraftStore.
    o.lifecycle_manager.generate_code = AsyncMock(
        side_effect=lambda *a, **k: {
            "status": "generated",
            "files": dict(BUNDLE),
            "runtime_manifest": {},
            "bundle_sha256": "a" * 64,
            "revision_id": str(uuid.uuid4()),
            "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
            "required_runtime_lock_sha256": BYO_RUNTIME_LOCK_SHA256,
            "artifact_relative_path": "generated/test-bundle",
            "agent_name": "Mailer",
            "state_revision": 1,
        })
    o.deliver_agent_bundle = AsyncMock(return_value=1)
    o._call_llm_json = AsyncMock(return_value=None)
    try:
        yield o
    finally:
        try:
            await _cleanup(o, lifecycle_ids)
        finally:
            await asyncio.wait_for(o._close_started_services(), timeout=15.0)


def _reg_frame(agent_id, name="Mailer"):
    card = AgentCard(name=name, description="mails", agent_id=agent_id,
                     skills=[AgentSkill(name="send_mail", description="s", id="send_mail",
                                        scope="tools:read", input_schema={})])
    return RegisterAgent(agent_card=card).to_json()


async def _connect_host(orch, ids, *, owner=None, agent_id=None):
    """Bring a user agent live exactly as the desktop host does: tunnel a
    register_agent frame over the owner's authenticated UI socket."""
    owner = owner or ids.owner
    agent_id = agent_id or ids.agent_id
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": owner}
    orch.ui_clients.append(ws)
    await orch._handle_agent_tunnel(ws, SimpleNamespace(
        action="agent_tunnel",
        payload={"agent_id": agent_id, "frame": _reg_frame(agent_id),
                 "host_session_id": ids.host_session_id}))
    return ws


def _seed_agent(orch, ids, *, owner=None, agent_id=None, name="Mailer"):
    owner = owner or ids.owner
    agent_id = agent_id or ids.agent_id
    registry = orch.user_agent_registry
    ua.create_user_agent(registry, agent_id=agent_id, owner_user_id=owner, display_name=name,
                         declared_tools=["send_mail"], declared_scopes=["tools:read"])
    ua.mark_validated(registry, agent_id, "0.1.0")


# ── owner-only list + derived running/offline (T026) ─────────────────────────

async def test_list_derives_running_from_a_live_tunnel(orch, lifecycle_ids):
    await _t(_seed_agent, orch, lifecycle_ids)
    html = await authoring.render(orch, lifecycle_ids.owner, ["user"], {})
    assert "Mailer" in html and "offline" in html and "running" not in html

    await _connect_host(orch, lifecycle_ids)
    assert aa.agent_status(orch, lifecycle_ids.owner, lifecycle_ids.agent_id) == "running"
    html = await authoring.render(orch, lifecycle_ids.owner, ["user"], {})
    assert "running" in html
    # FR-024: the surface always says where these things actually run.
    assert "desktop host" in html


async def test_list_is_owner_only(orch, lifecycle_ids):
    await _t(_seed_agent, orch, lifecycle_ids)
    await _connect_host(orch, lifecycle_ids)
    foreign_html = await authoring.render(orch, lifecycle_ids.foreign, ["user"], {})
    assert "Mailer" not in foreign_html and lifecycle_ids.agent_id not in foreign_html
    assert await _t(
        ua.list_user_agents,
        orch.user_agent_registry,
        lifecycle_ids.foreign,
    ) == []
    # …and the OWNER's own view is unaffected by the other user existing.
    assert "Mailer" in await authoring.render(
        orch,
        lifecycle_ids.owner,
        ["user"],
        {},
    )


async def test_running_status_is_not_leaked_across_owners(orch, lifecycle_ids):
    """Liveness is keyed by (owner, agent_id): another user asking about the same
    id must not see it as running."""
    await _t(_seed_agent, orch, lifecycle_ids)
    await _connect_host(orch, lifecycle_ids)
    assert aa.agent_status(
        orch,
        lifecycle_ids.foreign,
        lifecycle_ids.agent_id,
    ) == "offline"
    assert aa.host_online(orch, lifecycle_ids.foreign) is False
    assert aa.host_online(orch, lifecycle_ids.owner) is True


# ── cross-user invisibility of authoring sessions (FR-016) ───────────────────

async def test_foreign_user_cannot_see_or_drive_a_session(orch, lifecycle_ids):
    session = await aa.start_session(orch, user_id=lifecycle_ids.owner, agent_name="Mailer",
                                     description="sends my own mail every morning")
    assert await _t(aa.get_session, orch, lifecycle_ids.owner, session["id"]) is not None
    assert await _t(
        aa.get_session,
        orch,
        lifecycle_ids.foreign,
        session["id"],
    ) is None
    assert await _t(aa.list_sessions, orch, lifecycle_ids.foreign) == []

    # …and every write path refuses for the non-owner.
    ok, _phase, _msg = await _t(
        aa.advance,
        orch,
        lifecycle_ids.foreign,
        session["id"],
        {},
    )
    assert not ok
    result = await aa.generate_from_session(orch, lifecycle_ids.foreign, session["id"])
    assert result["status"] == "unavailable"
    orch.lifecycle_manager.generate_code.assert_not_awaited()

    _s, _p, notice = await authoring._h_generate(
        orch,
        None,
        lifecycle_ids.foreign,
        ["user"],
        {"draft_id": session["id"]},
    )
    assert "not available" in notice
    orch.lifecycle_manager.generate_code.assert_not_awaited()


# ── delete (T028) ────────────────────────────────────────────────────────────

async def test_delete_stops_the_host_and_soft_deletes(orch, lifecycle_ids):
    await _t(_seed_agent, orch, lifecycle_ids)
    ws = await _connect_host(orch, lifecycle_ids)
    assert lifecycle_ids.agent_id in orch.agents
    ws.sent.clear()

    _s, _p, notice = await authoring._h_delete(
        orch,
        None,
        lifecycle_ids.owner,
        ["user"],
        {"agent_id": lifecycle_ids.agent_id},
    )
    assert "Deleted" in notice
    assert lifecycle_ids.agent_id not in orch.agents
    assert (lifecycle_ids.owner, lifecycle_ids.agent_id) not in orch._tunnel_sockets
    assert any(json.loads(f).get("type") == "agent_stop" for f in ws.sent)  # host told

    row = await _t(
        ua.get_user_agent,
        orch.user_agent_registry,
        lifecycle_ids.agent_id,
    )
    assert row["status"] == "disabled" and row["deleted_at"] is not None   # retained
    assert lifecycle_ids.agent_id not in await authoring.render(
        orch,
        lifecycle_ids.owner,
        ["user"],
        {},
    )


async def test_delete_refused_for_a_non_owner(orch, lifecycle_ids):
    await _t(_seed_agent, orch, lifecycle_ids)
    await _connect_host(orch, lifecycle_ids)
    _s, _p, notice = await authoring._h_delete(
        orch,
        None,
        lifecycle_ids.foreign,
        ["user"],
        {"agent_id": lifecycle_ids.agent_id},
    )
    assert "not available" in notice
    row = await _t(
        ua.get_user_agent,
        orch.user_agent_registry,
        lifecycle_ids.agent_id,
    )
    assert row["deleted_at"] is None and lifecycle_ids.agent_id in orch.agents


# ── revise (T027 authoring half / FR-026) ────────────────────────────────────

async def test_revise_reenters_authoring_and_cannot_ship_without_a_new_analyze(
    orch,
    lifecycle_ids,
):
    await _t(_seed_agent, orch, lifecycle_ids)
    await _connect_host(orch, lifecycle_ids)

    _s, params, notice = await authoring._h_revise(
        orch,
        None,
        lifecycle_ids.owner,
        ["user"],
        {"agent_id": lifecycle_ids.agent_id},
    )
    assert "Analyze again" in notice
    draft_id = params["draft_id"]
    session = await _t(aa.get_session, orch, lifecycle_ids.owner, draft_id)
    assert aa.phase_of(session) == "specify"            # back to the start of the flow
    assert session["revises_agent_id"] == lifecycle_ids.agent_id
    # FR-026: the live version keeps running while the revision is authored.
    assert lifecycle_ids.agent_id in orch.agents

    # The revision cannot generate until IT passes Analyze.
    result = await aa.generate_from_session(orch, lifecycle_ids.owner, draft_id)
    assert result["status"] == "gate_blocked"
    orch.lifecycle_manager.generate_code.assert_not_awaited()

    # Walk it through the gates for real.
    ok, phase, msg = await _t(
        aa.advance, orch, lifecycle_ids.owner, draft_id,
        {"specification": "sends my own mail, now with attachments"})
    assert ok and phase == "clarify", msg
    await _t(
        orch.lifecycle_manager.draft_store.update_draft_agent,
        draft_id,
        clarify_answers=json.dumps(
            [{"question": "Which account?", "answer": "my work account"}],
        ),
    )
    assert (await _t(aa.advance, orch, lifecycle_ids.owner, draft_id, {}))[0]
    assert (await _t(aa.advance, orch, lifecycle_ids.owner, draft_id,
                     {"tools": "send_mail | tools:read | sends my own mail",
                      "scopes": "", "egress": ""}))[0]
    assert (await _t(
        aa.advance,
        orch,
        lifecycle_ids.owner,
        draft_id,
        {"tasks": "read\nsend"},
    ))[0]
    assert (await _t(
        aa.run_analyze,
        orch,
        lifecycle_ids.owner,
        draft_id,
    ))["status"] == "passed"

    result = await aa.generate_from_session(orch, lifecycle_ids.owner, draft_id)
    assert result["status"] == "delivered"
    assert result["agent_id"] == lifecycle_ids.agent_id
    row = await _t(
        ua.get_user_agent,
        orch.user_agent_registry,
        lifecycle_ids.agent_id,
    )
    # Delivery is stubbed, so the incumbent remains the authoritative live
    # revision; passing Analyze clears the revalidation fence without taking it
    # offline while the candidate activation is represented by the mock.
    assert row["status"] == "live" and row["revalidation_required"] is False


async def test_revalidation_required_blocks_registration_and_is_surfaced(
    orch,
    lifecycle_ids,
):
    """T029: a constitution bump flags the agent; the boundary refuses it until a
    fresh Analyze passes, and the surface says so."""
    await _t(_seed_agent, orch, lifecycle_ids)
    await _t(
        ua.mark_revalidation_required,
        orch.user_agent_registry,
        lifecycle_ids.agent_id,
        True,
    )

    ok, reason = await _t(
        ua.authorize_registration,
        orch.user_agent_registry,
        lifecycle_ids.owner,
        lifecycle_ids.agent_id,
    )
    assert not ok and "Analyze" in reason               # fail-closed at the boundary
    await _connect_host(orch, lifecycle_ids)
    assert lifecycle_ids.agent_id not in orch.agents

    html = await authoring.render(orch, lifecycle_ids.owner, ["user"], {})
    assert "rules changed" in html and "Analyze" in html


async def test_revise_refused_for_a_non_owner(orch, lifecycle_ids):
    await _t(_seed_agent, orch, lifecycle_ids)
    _s, _p, notice = await authoring._h_revise(
        orch,
        None,
        lifecycle_ids.foreign,
        ["user"],
        {"agent_id": lifecycle_ids.agent_id},
    )
    assert "not available" in notice
    assert await _t(aa.list_sessions, orch, lifecycle_ids.foreign) == []
