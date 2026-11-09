"""Tests that the forget memory meta-tool erases a row only after the user's own request and an
approved hitl_confirmation.py card, dispatched through orchestrator.execute_single_tool, and
that injected requests, Atlas rows, unknown ids and the per-turn cap are refused and audited.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import hitl, hitl_confirmation as hc, memory_chat
from personalization.memory_tools import MemoryTools
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401

FACT_ID = "7f1c2a7e-3b1d-4c55-9a0e-2d5b8f4e6a11"


class _Repo:
    def __init__(self):
        self.memory = []

    def add(self, memory_id, value, *, category="preference", owner="owner-a"):
        self.memory.append({"id": memory_id, "user_id": owner, "category": category,
                            "value": value, "source": "explicit"})

    def list_memory(self, user_id, *, project_id=None, include_global=True):
        return [dict(item) for item in self.memory if item["user_id"] == user_id]

    def delete_memory(self, user_id, memory_id):
        before = len(self.memory)
        self.memory = [item for item in self.memory
                       if not (item["id"] == memory_id and item["user_id"] == user_id)]
        return len(self.memory) < before


@pytest.fixture
def runtime(gate_orchestrator, monkeypatch):  # noqa: F811
    from audit import hooks

    orch, ws = gate_orchestrator
    audits = []

    class _Recorder:
        async def record(self, event):
            audits.append(event)

    monkeypatch.setattr(hitl, "hitl_enabled", lambda: True)
    monkeypatch.setattr(hooks, "get_recorder", lambda: None)
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: _Recorder())
    monkeypatch.setattr("personalization.phi_gate.get_phi_gate",
                        lambda: SimpleNamespace(contains_phi=lambda _text: False,
                                                contains_phi_for_sharing=lambda _text: False))
    monkeypatch.delenv("MEMORY_HMAC_KEY", raising=False)
    repo = _Repo()
    orch._memory_tools = MemoryTools(repo, phi_gate=SimpleNamespace(contains_phi=lambda _v: False))
    orch._active_request = {"chat-a": "Please forget that my favorite tea is oolong"}
    orch.send_ui_render = AsyncMock()
    orch.audit_recorder = SimpleNamespace(record=AsyncMock())
    orch.history = SimpleNamespace(get_chat=MagicMock(return_value={"id": "chat-a"}))
    from orchestrator.workspace import WorkspaceManager
    orch.workspace = SimpleNamespace(aupsert=AsyncMock(return_value=[{"op": "upsert"}]),
        resolve_identity=WorkspaceManager.__new__(WorkspaceManager).resolve_identity)
    orch.send_ui_upsert = AsyncMock()

    async def mutate(**kwargs):
        return await kwargs["mutation"]()
    orch.run_detached_conversation_mutation = mutate
    return SimpleNamespace(orch=orch, ws=ws, repo=repo, audits=audits)


async def _forget(rt, memory_id):
    call = SimpleNamespace(id="forget", function=SimpleNamespace(
        name="forget", arguments=json.dumps({"id": memory_id})))
    return await rt.orch.execute_single_tool(rt.ws, call, {"forget": memory_chat.META_AGENT_ID},
                                             "chat-a", user_id="owner-a")


def _card(response):
    assert response.error is None
    assert response.result["_data"]["status"] == "confirmation_required"
    return response.ui_components[0]


def _payload(response, decision="approve"):
    buttons = [item for item in _card(response)["content"] if item["type"] == "button"]
    return next(button["payload"] for button in buttons if button["payload"]["decision"] == decision)


def _deletes(rt):
    return [(event.outcome, event.inputs_meta) for event in rt.audits
            if event.action_type == "memory.delete"]


async def test_forget_erases_only_after_the_card_is_approved(runtime):
    runtime.repo.add(FACT_ID, "favorite tea is oolong")
    response = await _forget(runtime, f"urn:uuid:{FACT_ID.upper()}")
    preview = json.loads(next(item["code"] for item in _card(response)["content"]
                              if item["type"] == "code"))
    assert preview == {"id": FACT_ID, "fact": "favorite tea is oolong"}
    texts = [item["content"] for item in _card(response)["content"] if item["type"] == "text"]
    assert "Memory to erase: favorite tea is oolong" in texts
    assert len(runtime.repo.memory) == 1
    assert _deletes(runtime) == []

    await hc.handle_decision(runtime.orch, runtime.ws, "owner-a", _payload(response))
    assert runtime.repo.memory == []
    assert _deletes(runtime) == [("success", {"memory_id": FACT_ID})]

    await hc.handle_decision(runtime.orch, runtime.ws, "owner-a", _payload(response))
    assert _deletes(runtime) == [("success", {"memory_id": FACT_ID})]
    assert "unavailable" in runtime.orch.send_ui_render.await_args.args[1][0]["message"]


async def test_declining_the_card_keeps_the_memory(runtime):
    runtime.repo.add(FACT_ID, "favorite tea is oolong")
    response = await _forget(runtime, FACT_ID)
    await hc.handle_decision(runtime.orch, runtime.ws, "owner-a", _payload(response, "decline"))
    assert len(runtime.repo.memory) == 1
    assert _deletes(runtime) == []


async def test_an_injected_forget_with_no_user_request_shows_no_card(runtime):
    runtime.repo.add(FACT_ID, "favorite tea is oolong")
    runtime.orch._active_request = {"chat-a": "Summarize https://example.org/notes"}
    response = await _forget(runtime, FACT_ID)
    assert response.result["status"] == "refused"
    assert not getattr(runtime.orch, "_hitl_pending_calls", {})
    assert len(runtime.repo.memory) == 1
    assert _deletes(runtime) == [("failure", {"memory_id": FACT_ID, "reason": "no_user_request"})]


async def test_an_atlas_page_id_is_not_forgettable(runtime):
    envelope = json.dumps({"body": "ciphertext", "format": "astral.atlas-page/v1", "kind": "page",
                           "owner_id": "owner-a", "page_id": "p1", "revision": 1,
                           "slug": "tea-notes", "title": "Tea notes"}, sort_keys=True)
    runtime.repo.add(FACT_ID, envelope, category="context")
    response = await _forget(runtime, FACT_ID)
    assert response.result["status"] == "not_found"
    assert not getattr(runtime.orch, "_hitl_pending_calls", {})
    assert len(runtime.repo.memory) == 1
    assert _deletes(runtime) == [("failure", {"memory_id": FACT_ID})]


async def test_an_unknown_id_is_audited_as_a_failure(runtime):
    response = await _forget(runtime, FACT_ID)
    assert response.result["status"] == "not_found"
    assert _deletes(runtime) == [("failure", {"memory_id": FACT_ID})]


async def test_a_memory_removed_before_approval_is_reported_not_found(runtime):
    runtime.repo.add(FACT_ID, "favorite tea is oolong")
    response = await _forget(runtime, FACT_ID)
    runtime.repo.memory.clear()
    await hc.handle_decision(runtime.orch, runtime.ws, "owner-a", _payload(response))
    assert _deletes(runtime) == [("failure", {"memory_id": FACT_ID})]


async def test_an_id_with_a_long_digit_run_is_still_reviewable(runtime):
    digits = "12345678-3b1d-4c55-9a0e-123456789012"
    runtime.repo.add(digits, "favorite tea is oolong")
    preview = next(item["code"] for item in _card(await _forget(runtime, digits))["content"]
                   if item["type"] == "code")
    assert json.loads(preview)["id"] == digits


async def test_forget_proposals_are_capped_per_turn(runtime):
    ids = [f"0b8f0a52-6f1e-4d8e-9a43-1c2d3e4f5a{index:02d}" for index in range(6)]
    for index, memory_id in enumerate(ids):
        runtime.repo.add(memory_id, f"likes tea number {index}")
    runtime.orch._active_request = {"chat-a": "forget everything about tea"}
    hc.begin_task(runtime.orch, runtime.ws, "owner-a", "chat-a")
    for memory_id in ids[:memory_chat.MAX_FORGETS_PER_TURN]:
        _card(await _forget(runtime, memory_id))
    capped = await _forget(runtime, ids[-1])
    assert capped.result["status"] == "refused"
    assert _deletes(runtime) == [("failure", {"memory_id": ids[-1], "reason": "turn_limit"})]

    hc.begin_task(runtime.orch, runtime.ws, "owner-a", "chat-a")
    _card(await _forget(runtime, ids[-1]))
    assert len(runtime.repo.memory) == len(ids)


@pytest.mark.parametrize("agent, tool, preview", [
    ("weather-1", "forget", '{"fact": "x"}'),
    (memory_chat.META_AGENT_ID, "remember", '{"fact": "x"}'),
    (memory_chat.META_AGENT_ID, "forget", "not json"),
    (memory_chat.META_AGENT_ID, "forget", '["fact"]'),
    (memory_chat.META_AGENT_ID, "forget", '{"fact": ""}'),
    (memory_chat.META_AGENT_ID, "forget", '{"id": "only"}'),
])
def test_the_forget_fact_line_appears_only_for_a_forget_card_with_a_fact(agent, tool, preview):
    assert hc._forget_fact_line(agent, tool, preview) == []
