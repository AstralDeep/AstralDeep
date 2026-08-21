"""Feature 058 T011 (SC-002) — zero user-agent processes on the orchestrator host.

A BYO agent's code belongs to the user and runs on the user's desktop. Two paths
could put it on the central host, so both are pinned here:

1. the boot relaunch, which re-Popen'd every ``draft_agents`` row in status
   ``live`` with no origin filter, and
2. ``start_draft_agent`` itself, which is the only thing that Popens generated
   code — it now refuses a ``byo_client`` draft outright, so a future call site
   cannot reintroduce (1).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator import agent_authoring as aa  # noqa: E402
from orchestrator.agent_lifecycle import AgentLifecycleManager, BYO_ORIGIN  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402
from orchestrator.user_agents import UserAgentRegistry  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


async def _t(fn, *a, **k):
    """Run a synchronous (DB-touching) helper off the event loop (052)."""
    return await asyncio.to_thread(fn, *a, **k)


@pytest.fixture()
def plane_runtime():
    with isolated_plane_runtime("byo_offserver") as runtime:
        yield runtime


async def _live_draft(lifecycle, origin):
    draft = await lifecycle.create_draft(
        user_id="u-offsrv",
        agent_name="Offserver Probe",
        description="d" * 20,
        origin=origin,
    )
    lifecycle.draft_store.update_draft_agent(draft["id"], status="live")
    return draft["id"]


def test_boot_relaunch_uses_typed_plane_draft_inventory():
    start_source = inspect.getsource(Orchestrator.start)
    server_source = inspect.getsource(Orchestrator._run_started_server)

    assert "await self._run_started_server()" in start_source
    assert "self.lifecycle_manager.draft_store.list_relaunchable_drafts" in (
        server_source
    )
    assert "history.db.afetch_all" not in server_source


async def test_start_draft_agent_refuses_a_byo_draft(
    plane_runtime, monkeypatch, tmp_path
):
    import subprocess

    def _no_popen(*a, **kw):
        raise AssertionError("Popen'd a user agent on the orchestrator host (SC-002)")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)
    lm = AgentLifecycleManager(
        orchestrator=None,
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    lm._agents_dir = str(tmp_path)
    draft_id = await _live_draft(lm, BYO_ORIGIN)
    try:
        with pytest.raises(ValueError, match="BYO"):
            await lm.start_draft_agent(draft_id)
    finally:
        await _t(lm.draft_store.delete_draft_agent, draft_id)


async def test_approve_agent_refuses_a_byo_draft(plane_runtime, tmp_path):
    # approve_agent both exec's the tools in-process AND Popens them. A BYO draft
    # id belongs to the user, so this entry point is reachable — refuse it.
    lm = AgentLifecycleManager(
        orchestrator=None,
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    lm._agents_dir = str(tmp_path)
    draft_id = await _live_draft(lm, BYO_ORIGIN)
    try:
        with pytest.raises(ValueError, match="BYO"):
            await lm.approve_agent(draft_id)
    finally:
        await _t(lm.draft_store.delete_draft_agent, draft_id)


async def test_refine_validates_a_byo_draft_out_of_process(
    plane_runtime, monkeypatch, tmp_path
):
    # The refine entry point validates too — and validation EXECUTES the tools.
    lm = AgentLifecycleManager(
        orchestrator=None,
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    lm._agents_dir = str(tmp_path)
    draft_id = await _live_draft(lm, BYO_ORIGIN)
    slug = (await _t(lm.draft_store.get_draft_agent, draft_id))["agent_slug"]
    agent_dir = os.path.join(lm._agents_dir, slug)
    os.makedirs(agent_dir, exist_ok=True)
    with open(os.path.join(agent_dir, "mcp_tools.py"), "w", encoding="utf-8") as fh:
        fh.write("TOOL_REGISTRY = {}\n")

    def _boom(*a, **kw):
        raise AssertionError("in-process exec of BYO code on refine (G1 violation)")

    monkeypatch.setattr(lm.validator, "validate", _boom)
    lm.generator.refine_tools_file = AsyncMock(return_value=(
        "from astralprims import Text\n\n"
        "def t(**kwargs):\n"
        "    return {'_ui_components': [Text(content='x').to_dict()], '_data': {}}\n\n"
        "TOOL_REGISTRY = {'t': {'function': t, 'description': 'd',\n"
        "  'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}\n"))
    try:
        state = await lm.refine_agent(draft_id, "add a tool")
        assert state["status"] == "generated"
        assert json.loads(state["validation_report"])["passed"]
    finally:
        shutil.rmtree(agent_dir, ignore_errors=True)
        await _t(lm.draft_store.delete_draft_agent, draft_id)


async def test_authoring_path_never_starts_a_process(monkeypatch, plane_runtime):
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", MagicMock(
        side_effect=AssertionError("BYO authoring spawned a process")))

    o = SimpleNamespace()
    o.user_agent_registry = UserAgentRegistry(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    lifecycle = AgentLifecycleManager(
        orchestrator=o,
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    o.lifecycle_manager = lifecycle
    created_origins = []
    created_draft_ids = []
    created_target_ids = []
    create_draft = lifecycle.create_draft

    async def _create_draft(**kwargs):
        created_origins.append(kwargs.get("origin"))
        draft = await create_draft(**kwargs)
        created_draft_ids.append(draft["id"])
        created_target_ids.append(draft["target_agent_id"])
        return draft

    lifecycle.create_draft = AsyncMock(side_effect=_create_draft)
    o.deliver_agent_bundle = AsyncMock(return_value=1)

    async def _generate(draft_id, **kw):
        # The origin filter only protects us if the row is stamped BEFORE the
        # draft can be picked up — i.e. before generation, not after delivery.
        assert BYO_ORIGIN in created_origins, \
            "draft was generated before it was stamped byo_client"
        assert kw.get("target") == "byo"
        # The immutable Plane-allocated target, not a name-derived identity,
        # is carried from draft persistence into generation.
        assert kw.get("agent_id") == created_target_ids[-1]
        return {"status": "generated",
                "files": {"agent_main.py": "x", "mcp_tools.py": "y",
                          "manifest.json": "{}"}}

    lifecycle.generate_code = AsyncMock(side_effect=_generate)
    lifecycle.start_draft_agent = AsyncMock()
    lifecycle.approve_agent = AsyncMock()

    try:
        res = await aa.author_and_deliver(
            o, user_id="u-offsrv", agent_name="Offserver",
            description="greets the owner by their name",
            declared_tools=["greet"], declared_scopes=["tools:read"],
            plan={"tools_used": ["greet"], "tool_scopes": {"greet": "tools:read"}})
        assert res["status"] == "delivered"          # bundle went to the host…
        o.deliver_agent_bundle.assert_awaited_once()
        lifecycle.start_draft_agent.assert_not_awaited()   # …and nowhere else
        lifecycle.approve_agent.assert_not_awaited()
    finally:
        for draft_id in created_draft_ids:
            await _t(lifecycle.draft_store.delete_draft_agent, draft_id)
