"""Checks compaction stays bound to the active goal's authenticated connection."""

import json

import pytest
from unittest.mock import AsyncMock

from orchestrator.compaction import compact_messages
from shared.feature_flags import flags
from tests.test_goal_chat_loop_edges import _chat, _msg, _register, _usage

pytest_plugins = ("tests.test_turn_seams_loop",)


@pytest.fixture
def base_orch(orch):
    return orch


@pytest.fixture
def goal_orch(base_orch, monkeypatch):
    from orchestrator import goal_verification
    from orchestrator.goal_requirements import build_goal_contract

    monkeypatch.setattr(
        goal_verification,
        "classify_goal",
        AsyncMock(return_value=build_goal_contract("informational")),
    )
    monkeypatch.setitem(flags._flags, "goal_harness", True)
    monkeypatch.setitem(flags._flags, "typesafe_routing", False)
    monkeypatch.setitem(flags._flags, "designer_all_devices", False)
    base_orch._design_turn_post_done = AsyncMock(return_value=None)
    return base_orch


@pytest.mark.asyncio
async def test_goal_compaction_calls_provider_with_original_websocket(
    goal_orch, monkeypatch, user_skills_disabled
):
    import orchestrator.orchestrator as orchestrator_module

    orchestrator = goal_orch
    _register(orchestrator)
    monkeypatch.setitem(flags._flags, "message_compaction", True)
    chat_id, websocket = await _chat(orchestrator)
    observed = []

    async def compact(messages, model, llm_call, **kwargs):
        result, _usage = await llm_call(None, [{"role": "user", "content": "summary"}])
        assert result.content == "Compacted."
        return messages, False

    monkeypatch.setattr(orchestrator_module, "compact_messages", compact)

    async def provider(actual_websocket, messages, tools_desc=None, **kwargs):
        observed.append((actual_websocket, kwargs.get("feature")))
        if kwargs.get("feature") == "goal_verification":
            return _msg(content=json.dumps({"status": "complete", "remaining": [], "reason": ""})), _usage()
        if kwargs.get("feature") == "context_compaction":
            return _msg(content="Compacted."), _usage()
        return _msg(content="Done."), _usage()

    orchestrator._call_llm = provider
    await orchestrator.handle_chat_message(
        websocket, "Give me a verified summary", chat_id, user_id="seam-user"
    )

    assert (websocket, "context_compaction") in observed
    assert None not in [actual_websocket for actual_websocket, _feature in observed]


@pytest.mark.asyncio
async def test_failed_compaction_preserves_the_complete_history():
    messages = [{"role": "system", "content": "Keep this instruction."}]
    for index in range(12):
        messages.extend([
            {"role": "user", "content": f"Question {index} " + "alpha " * 400},
            {"role": "assistant", "content": f"Answer {index} " + "beta " * 400},
        ])
    messages.append({"role": "user", "content": "The current request."})

    async def rejected(_websocket, _prompt):
        raise PermissionError("harness_owner_invalid")

    compacted, did_compact = await compact_messages(messages, "gpt-4", rejected)

    assert compacted is messages
    assert did_compact is False
