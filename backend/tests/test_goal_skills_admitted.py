"""Run goal turns through the authenticated user-skill guidance wrapper."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

pytest_plugins = ("tests.test_human_socket_ingress_088",)


@pytest.mark.asyncio
@pytest.mark.parametrize("result_kind", ["success", "incomplete", "budget_ui"])
async def test_admitted_goal_turn_with_authenticated_user_skill_catalog(
    metadata, runtime, fixture, monkeypatch, tmp_path, result_kind, orchestrator_factory,
):
    from orchestrator import goal_verification
    from orchestrator.goal_requirements import build_goal_contract
    from orchestrator.user_skill_catalog import UserSkillFacade
    from shared.feature_flags import flags
    from rote.rote import ROTE
    from tests import test_connection_publication_owner as connection_helpers
    from tests import test_human_socket_ingress_088 as ingress_helpers
    from tests import test_turn_seams_loop as seams

    state, _, _ = metadata
    orch = state.orch
    initialized = orchestrator_factory()
    for name, value in vars(initialized).items():
        if not hasattr(orch, name):
            setattr(orch, name, value)
    connection_helpers.attach_plane(orch, runtime)
    orch.rote = ROTE()
    orch.rote.register_device(state.socket, {"device_type": "macos"})
    await ingress_helpers.registered(state)
    chat_id = await asyncio.to_thread(orch.history.create_chat, user_id=fixture[1])
    orch._llm_store.set_sync(
        fixture[1], provider="custom", base_url="http://test.invalid/v1",
        model="test-model", api_key="test-key",
    )
    facade = UserSkillFacade(orch, str(tmp_path))
    orch._user_skill_store = facade
    orch.agent_cards = {}
    orch.agents = {}
    seams._register(orch, "run_command")
    orch.summarize_chat_title = AsyncMock()
    orch._design_turn_post_done = AsyncMock(return_value=None)
    monkeypatch.setitem(flags._flags, "goal_harness", True)
    monkeypatch.setitem(flags._flags, "user_skills", True)
    monkeypatch.setitem(flags._flags, "skill_packs", True)
    monkeypatch.setitem(flags._flags, "slash_commands", True)
    from orchestrator.task_state import TaskManager

    orch.task_manager = TaskManager(orch.work_admission)
    monkeypatch.setitem(flags._flags, "task_state_machine", True)
    monkeypatch.setitem(flags._flags, "typesafe_routing", False)
    monkeypatch.setitem(flags._flags, "designer_all_devices", False)
    max_turns = 2 if result_kind == "budget_ui" else 4
    monkeypatch.setenv("GOAL_MAX_TURNS", str(max_turns))
    monkeypatch.setattr(
        goal_verification,
        "classify_goal",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=build_goal_contract("informational")),
    )
    catalog_reads = []
    original_list = facade.list

    async def checked_list(*args, **kwargs):
        catalog_reads.append(kwargs.get("caller"))
        return await original_list(*args, **kwargs)

    monkeypatch.setattr(facade, "list", checked_list)

    planner_calls = 0

    async def provider(_websocket, _messages, tools_desc=None, **kwargs):
        nonlocal planner_calls
        feature = kwargs.get("feature")
        if feature == "tool_dispatch":
            if result_kind == "incomplete":
                return None, None
            if result_kind == "budget_ui":
                planner_calls += 1
                call = seams._tc("run_command", f"goal-{planner_calls}")
                call.function.arguments = json.dumps({"command": f"echo {planner_calls}"})
                return seams._msg(tool_calls=[call]), seams._usage()
            return seams._msg(content="The requested explanation is complete."), seams._usage()
        if feature == "goal_verification":
            return seams._msg(content=json.dumps({
                "status": "complete", "remaining": [], "reason": "Answered directly.",
                "evidence": {},
            })), seams._usage()
        return seams._msg(content="The requested explanation is complete."), seams._usage()

    orch._call_llm = provider
    if result_kind == "budget_ui":
        from astralprims import Card, Text

        async def execute_tool(*_args, **_kwargs):
            return SimpleNamespace(
                result={"_data": {"exit_code": 0, "stdout": "ok"}},
                error=None,
                ui_components=[Card(title="Command result", content=[Text(content="Finished")]).to_dict()],
                correlation_id=None,
            )

        orch.execute_single_tool = AsyncMock(side_effect=execute_tool)
    request_generation = str(uuid4())
    submission_id = str(uuid4())
    frame = {
        "type": "ui_event", "action": "chat_message", "session_id": chat_id,
        "submission_id": submission_id, "request_generation": request_generation,
        "connection_generation": state.connection_generation,
        "payload": {
            "chat_id": chat_id, "submission_id": submission_id,
            "request_generation": request_generation,
            "connection_generation": state.connection_generation,
            "message": "Explain what a Python virtual environment does.",
        },
    }
    state.socket.feed(json.dumps(frame))
    terminal = await ingress_helpers.terminal(state)

    transcript = await asyncio.to_thread(orch.history.get_chat, chat_id, user_id=fixture[1])
    assert transcript["messages"][0]["role"] == "user"
    assert transcript["messages"][-1]["role"] == "assistant"
    assert len(catalog_reads) == 1 and catalog_reads[0] is not None
    assert orch.rote.get_profile(state.socket).device_type.value == "macos"
    from shared.protocol import ConversationSnapshot

    snapshots = [frame for frame in state.socket.payloads() if frame.get("type") == "conversation_snapshot"]
    assert len(snapshots) == 1
    emitted = ConversationSnapshot.from_dict(snapshots[0])
    snapshot_roles = [row["role"] for row in emitted.transcript]
    assert snapshot_roles == ["user", "assistant"]
    if result_kind == "success":
        assert terminal["state"] == "completed"
        assert "requested explanation" in json.dumps(transcript["messages"][-1]["content"])
    else:
        assert terminal["state"] == "failed"
        assert "Request incomplete" in json.dumps(transcript["messages"][-1]["content"])
        assert "Request incomplete" in json.dumps(emitted.transcript[-1])
    if result_kind == "budget_ui":
        assert planner_calls == max_turns
        assert orch.execute_single_tool.await_count == max_turns
        assert "Command result" in json.dumps(emitted.canvas["components"])
