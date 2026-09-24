"""Tests for the BYO user-agent lifecycle through a live Orchestrator: owner-only
listing, derived running/offline tunnel status, revise-requires-fresh-Analyze, and
delete stopping the host and soft-deleting.
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
from fastapi import Request

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
from orchestrator.human_request_authority import (  # noqa: E402
    authenticate_current_human_request, bind_human_caller,
)
from verification.drivers.fixture_identity import FixtureIdentity  # noqa: E402

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
        owner=f"__verif__byolife-{token}_owner",
        foreign=f"__verif__byolife-{token}_foreign",
        agent_id=f"ua-mailer-{token}",
        host_session_id=f"hs-{token}",
    )


async def _cleanup(orch, ids):
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
async def orch(monkeypatch, lifecycle_ids, tmp_path):
    monkeypatch.setitem(flags._flags, "byo_agents", True)
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("MOCK_AUTH", "false")
    o = await asyncio.to_thread(Orchestrator)
    identity = FixtureIdentity(lifecycle_ids.owner.rsplit("_", 1)[0])
    o._lifecycle_fixture_identity = identity
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    o.knowledge_index = SimpleNamespace(knowledge_dir=str(knowledge))
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
            try:
                await asyncio.wait_for(o._close_started_services(), timeout=15.0)
            finally:
                identity.close()


async def _render(orch, owner, roles, params):
    _claims, token = orch._lifecycle_fixture_identity.claims_and_token({
        "sub": owner, "realm_access": {"roles": roles},
    })
    request = Request({
        "type": "http", "method": "GET", "scheme": "https",
        "path": "/api/authoring", "query_string": b"",
        "headers": [(b"authorization", ("Bearer " + token).encode())],
        "app": SimpleNamespace(state=SimpleNamespace(orchestrator=orch)),
    })
    caller = await authenticate_current_human_request(
        request, boundary=orch.human_request_boundary)
    with bind_human_caller(caller):
        result = await authoring.render(orch, owner, roles, params)
        await caller.verify_delivery()
        return result


def _reg_frame(agent_id, name="Mailer"):
    card = AgentCard(name=name, description="mails", agent_id=agent_id,
                     skills=[AgentSkill(name="send_mail", description="s", id="send_mail",
                                        scope="tools:read", input_schema={})])
    return RegisterAgent(agent_card=card).to_json()


async def _connect_host(orch, ids, *, owner=None, agent_id=None):
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


async def test_list_derives_running_from_a_live_tunnel(orch, lifecycle_ids):
    await _t(_seed_agent, orch, lifecycle_ids)
    html = await _render(orch, lifecycle_ids.owner, ["user"], {})
    assert "Mailer" in html and "offline" in html and "running" not in html

    await _connect_host(orch, lifecycle_ids)
    assert aa.agent_status(orch, lifecycle_ids.owner, lifecycle_ids.agent_id) == "running"
    html = await _render(orch, lifecycle_ids.owner, ["user"], {})
    assert "running" in html
    assert "desktop host" in html


async def test_list_is_owner_only(orch, lifecycle_ids):
    await _t(_seed_agent, orch, lifecycle_ids)
    await _connect_host(orch, lifecycle_ids)
    foreign_html = await _render(orch, lifecycle_ids.foreign, ["user"], {})
    assert "Mailer" not in foreign_html and lifecycle_ids.agent_id not in foreign_html
    assert await _t(
        ua.list_user_agents,
        orch.user_agent_registry,
        lifecycle_ids.foreign,
    ) == []
    assert "Mailer" in await _render(
        orch,
        lifecycle_ids.owner,
        ["user"],
        {},
    )


async def test_running_status_is_not_leaked_across_owners(orch, lifecycle_ids):
    await _t(_seed_agent, orch, lifecycle_ids)
    await _connect_host(orch, lifecycle_ids)
    assert aa.agent_status(
        orch,
        lifecycle_ids.foreign,
        lifecycle_ids.agent_id,
    ) == "offline"
    assert aa.host_online(orch, lifecycle_ids.foreign) is False
    assert aa.host_online(orch, lifecycle_ids.owner) is True


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
    assert any(json.loads(f).get("type") == "agent_stop" for f in ws.sent)

    row = await _t(
        ua.get_user_agent,
        orch.user_agent_registry,
        lifecycle_ids.agent_id,
    )
    assert row["status"] == "disabled" and row["deleted_at"] is not None
    assert lifecycle_ids.agent_id not in await _render(
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
    assert aa.phase_of(session) == "specify"
    assert session["revises_agent_id"] == lifecycle_ids.agent_id
    assert lifecycle_ids.agent_id in orch.agents

    result = await aa.generate_from_session(orch, lifecycle_ids.owner, draft_id)
    assert result["status"] == "gate_blocked"
    orch.lifecycle_manager.generate_code.assert_not_awaited()

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
    assert row["status"] == "live" and row["revalidation_required"] is False


async def test_revalidation_required_blocks_registration_and_is_surfaced(
    orch,
    lifecycle_ids,
):
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
    assert not ok and "Analyze" in reason
    await _connect_host(orch, lifecycle_ids)
    assert lifecycle_ids.agent_id not in orch.agents

    html = await _render(orch, lifecycle_ids.owner, ["user"], {})
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
