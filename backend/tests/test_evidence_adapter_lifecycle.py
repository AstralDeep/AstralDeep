"""Checks packing against the real host-bound evidence adapter and current dispatch lifecycle.
Unavailable or replaced recall capability preserves authorized results without publishing unreachable previews.
"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from agents.evidence.evidence_agent import EvidenceAgent
from agents.evidence.mcp_server import MCPServer
from orchestrator.hooks import HookEvent, HookManager
from orchestrator.orchestrator import Orchestrator
from shared.feature_flags import flags
from shared.protocol import MCPResponse
from tests.test_evidence_service import (
    AGENT, TEXT, TOOL, bound as bound, evidence_turn, events, fixture as fixture,
    human as human, invoke, pack, runtime as runtime, service as service,
    signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio


def install_adapter(state, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "adapter-key.pem"))
    monkeypatch.setenv("FF_INPROCESS_AGENTS", "true")
    monkeypatch.setitem(flags._flags, "inprocess_agents", True)
    adapter = EvidenceAgent(state.host, port=8312)
    state.host.local_agents["evidence-1"] = adapter
    state.host.agent_cards["evidence-1"] = adapter.card
    return adapter


def change_adapter(state, adapter, change, monkeypatch):
    if change in {"inprocess_off", "packing_off"}:
        flag = "inprocess_agents" if change == "inprocess_off" else "observation_packing"
        monkeypatch.setenv("FF_" + flag.upper(), "false")
        monkeypatch.setitem(flags._flags, flag, False)
    elif change == "missing_local":
        state.host.local_agents.pop("evidence-1", None)
    elif change == "fake_instance":
        state.host.local_agents["evidence-1"] = SimpleNamespace(card=adapter.card, mcp_server=adapter.mcp_server)
    elif change == "fake_server":
        adapter.mcp_server = SimpleNamespace(_orchestrator=state.host)
    elif change == "server_recall_missing":
        adapter.mcp_server.tools = deepcopy(adapter.mcp_server.tools)
        adapter.mcp_server.tools.pop("recall_observation")
    elif change == "foreign_host":
        adapter.mcp_server._orchestrator = SimpleNamespace()
    elif change == "missing_service":
        state.host._evidence_context = None
    elif change == "replaced_service":
        state.host._evidence_context = SimpleNamespace(archive=state.archive)
    elif change == "missing_card":
        state.host.agent_cards.pop("evidence-1", None)
    elif change == "replaced_card":
        state.host.agent_cards["evidence-1"] = deepcopy(adapter.card)
    elif change == "missing_recall":
        adapter.card.skills = [skill for skill in adapter.card.skills if skill.id != "recall_observation"]
    elif change == "invalid_skills":
        adapter.card.skills = None
    elif change == "changed_scope":
        next(skill for skill in adapter.card.skills if skill.id == "recall_observation").scope = "tools:write"
    elif change == "wrong_identity":
        adapter.card.agent_id = "foreign-adapter"
    elif change == "invalid_metadata":
        adapter.card.metadata["invalid_value"] = object()
    elif change == "changed_card":
        adapter.card.metadata["changed_after_admission"] = True
    elif change == "replaced_server":
        adapter.mcp_server = MCPServer(state.host)
    elif change == "changed_server_tools":
        adapter.mcp_server.tools = deepcopy(adapter.mcp_server.tools)
        adapter.mcp_server.tools["recall_observation"]["description"] = "Changed after admission"
    else:
        replacement = EvidenceAgent(state.host, port=8312)
        state.host.local_agents["evidence-1"] = replacement
        state.host.agent_cards["evidence-1"] = replacement.card


@pytest.mark.parametrize("compaction", [False, True])
async def test_registered_host_adapter_keeps_exact_recall_independent_of_compaction(
    human, bound, fixture, tmp_path, monkeypatch, compaction,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        install_adapter(state, tmp_path, monkeypatch)
        monkeypatch.setitem(flags._flags, "safe_compaction", compaction)
        packed = await pack(state)
        assert packed.error is None and packed.result["view"] == "partial_preview"
        recalled = await invoke(state, arguments={"reference": packed.result["reference"]})
        assert recalled.error is None and recalled.result["text"] == TEXT
        assert state.archive.retained_bytes == len(TEXT.encode())
        totals = await state.evidence.usage.totals(state.owner, state.chat)
        assert totals["model_calls"] == 0 and totals["recall_bytes"] == len(TEXT.encode())


@pytest.mark.parametrize("change", [
    "missing_local", "fake_instance", "fake_server", "server_recall_missing", "foreign_host", "missing_service",
    "replaced_service", "missing_card",
    "replaced_card", "missing_recall", "invalid_skills", "changed_scope", "wrong_identity",
    "invalid_metadata", "inprocess_off",
])
async def test_unavailable_adapter_refuses_shortening_and_preserves_authorized_full_result(
    human, bound, fixture, tmp_path, monkeypatch, change,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        adapter = install_adapter(state, tmp_path, monkeypatch)
        change_adapter(state, adapter, change, monkeypatch)
        original = MCPResponse(request_id="source", result=TEXT)
        returned = await pack(state, original)
        assert returned is original and returned.result == TEXT and returned.error is None
        assert state.archive.retained_bytes == 0 and state.evidence._sources == {}
        records = await events(state)
        assert any(item.action_type == "evidence.capture_refused" for item in records)
        assert not any(item.action_type == "evidence.capture" for item in records)
        rendered = [component for args, _kwargs in state.notices for component in args[1]]
        assert state.notices and "shortening was refused" in json.dumps(rendered)
        assert "Beginning preserved evidence" not in "".join(item.model_dump_json() for item in records)


@pytest.mark.parametrize("packing,compaction", [(False, False), (True, False), (False, True), (True, True)])
async def test_inprocess_off_cannot_publish_preview_in_any_context_control_combination(
    human, bound, fixture, tmp_path, monkeypatch, packing, compaction,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        adapter = install_adapter(state, tmp_path, monkeypatch)
        monkeypatch.setitem(flags._flags, "observation_packing", packing)
        monkeypatch.setitem(flags._flags, "safe_compaction", compaction)
        change_adapter(state, adapter, "inprocess_off", monkeypatch)
        original = MCPResponse(request_id="source", result=TEXT)
        assert await pack(state, original) is original
        assert original.result == TEXT and state.archive.retained_bytes == 0
        assert not state.evidence._sources
        records = await events(state)
        assert any(item.action_type == "evidence.capture_refused" for item in records) is packing
        assert not any(item.action_type == "evidence.capture" for item in records)


@pytest.mark.parametrize("boundary", ["capture_audit", "privacy"])
@pytest.mark.parametrize("change", [
    "missing_local", "inprocess_off", "packing_off", "replaced_adapter", "replaced_server", "changed_card",
    "changed_server_tools", "missing_service", "replaced_service",
])
async def test_adapter_lost_during_awaited_capture_work_cleans_text_before_preview_commit(
    human, bound, fixture, tmp_path, monkeypatch, boundary, change,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        adapter = install_adapter(state, tmp_path, monkeypatch)
        changed = []
        original = MCPResponse(request_id="source", result=TEXT)
        before = deepcopy(original.result)
        if boundary == "capture_audit":
            record = state.evidence._audit

            async def after_audit(*args, **kwargs):
                receipt = await record(*args, **kwargs)
                if args[2] == "capture" and not changed:
                    assert state.archive.retained_bytes == len(TEXT.encode())
                    changed.append(True)
                    change_adapter(state, adapter, change, monkeypatch)
                return receipt

            monkeypatch.setattr(state.evidence, "_audit", after_audit)
        else:
            verify = state.evidence._verify_retained_text

            async def after_privacy(*args, **kwargs):
                await verify(*args, **kwargs)
                if not changed:
                    assert state.archive.retained_bytes == len(TEXT.encode())
                    changed.append(True)
                    change_adapter(state, adapter, change, monkeypatch)

            monkeypatch.setattr(state.evidence, "_verify_retained_text", after_privacy)
        returned = await pack(state, original)
        assert changed == [True] and returned is original and returned.result == before
        assert state.archive.retained_bytes == 0 and state.evidence._sources == {}
        records = await events(state)
        assert any(item.action_type == "evidence.capture_refused" for item in records)
        rendered = [component for args, _kwargs in state.notices for component in args[1]]
        assert not any(component.get("label") == "Partial preview" for component in rendered)


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("change", ["missing_local", "inprocess_off"])
async def test_real_source_dispatch_post_hook_adapter_loss_preserves_full_authorized_result(
    human, bound, fixture, tmp_path, monkeypatch, parallel, change,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        adapter = install_adapter(state, tmp_path, monkeypatch)
        original = MCPResponse(request_id="source", result=TEXT)
        monkeypatch.setitem(flags._flags, "hook_system", True)
        state.host.hooks = HookManager()
        state.host._evidence_context = state.evidence
        changed = []

        async def machine_guard(*_args):
            return None

        async def physical_source(*_args, **_kwargs):
            return original

        async def after_source(context):
            if context.event == HookEvent.POST_TOOL_USE and context.agent_id == AGENT:
                assert context.tool_result == TEXT
                changed.append(True)
                change_adapter(state, adapter, change, monkeypatch)

        state.host._guard_machine_meta_tool = machine_guard
        state.host._execute_with_retry = physical_source
        state.host._protected_dispatch_channel = lambda *_args, **_kwargs: "websocket"
        state.host.hooks.register(HookEvent.POST_TOOL_USE, after_source)
        if parallel:
            returned = await Orchestrator._execute_with_retry_audited(
                state.host, state.socket, AGENT, TOOL, {"query": "synthetic"}, state.chat, state.owner,
            )
        else:
            call = SimpleNamespace(function=SimpleNamespace(name=TOOL, arguments=json.dumps({"query": "synthetic"})))
            returned = await Orchestrator.execute_single_tool(
                state.host, state.socket, call, {TOOL: AGENT}, state.chat, state.owner,
            )
        assert changed == [True] and returned is original and returned.result == TEXT
        assert state.archive.retained_bytes == 0 and not state.evidence._sources
        assert not getattr(returned, "_evidence_transient", False)


async def test_adapter_refusal_never_restores_full_result_after_source_permission_loss(
    human, bound, fixture, tmp_path, monkeypatch,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        adapter = install_adapter(state, tmp_path, monkeypatch)
        change_adapter(state, adapter, "missing_local", monkeypatch)
        await asyncio.to_thread(state.host.tool_permissions.set_tool_overrides, state.owner, AGENT, {TOOL: False})
        original = MCPResponse(request_id="source", result=TEXT)
        returned = await pack(state, original)
        assert returned.error is not None and returned.result is None
        assert original.result == TEXT and state.archive.retained_bytes == 0
