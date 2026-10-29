"""Exercises in-progress checkpoints through the ordinary goal chat loop."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.feature_flags import flags
from tests import test_turn_seams_loop as seams
from tests.test_turn_seams_loop import _last_assistant_text, _msg, _register, _tc, _usage, _ws

orch = seams.orch


@pytest.fixture
def checkpoint_orch(orch, monkeypatch):
    from orchestrator import goal_verification
    from orchestrator.goal_requirements import build_goal_contract

    monkeypatch.setattr(goal_verification, "classify_goal", AsyncMock(return_value=build_goal_contract("informational")))
    monkeypatch.setitem(flags._flags, "goal_harness", True)
    monkeypatch.setitem(flags._flags, "typesafe_routing", False)
    monkeypatch.setitem(flags._flags, "designer_all_devices", False)
    orch._design_turn_post_done = AsyncMock(return_value=None)
    return orch


async def _chat(o):
    chat_id = "goal-checkpoint-" + uuid.uuid4().hex
    await asyncio.to_thread(o.history.create_chat, chat_id, user_id="seam-user")
    return chat_id, _ws(o)


def _verdict(status, remaining=(), reason=""):
    return _msg(content=json.dumps({"status": status, "remaining": list(remaining), "reason": reason})), _usage()


@pytest.mark.asyncio
async def test_eight_successful_tool_calls_get_checkpoint_advice_before_final_verifier(
    checkpoint_orch, user_skills_disabled,
):
    o = checkpoint_orch
    _register(o)
    chat_id, ws = await _chat(o)
    planner_calls = 0
    checkpoint_calls = []
    final_reviews = []
    saw_advice = []
    advice = "Read back the files and run the requested checks."

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner_calls
        if kwargs.get("feature") == "goal_verification":
            user_payload = json.loads(messages[-1]["content"])
            if user_payload.get("review_phase") == "in_progress":
                checkpoint_calls.append((messages, tools_desc, kwargs))
                return _verdict("continue", [advice], "The eight-call checkpoint still has unverified work.")
            final_reviews.append(messages)
            return _verdict("complete")
        planner_calls += 1
        objective_anchor = next(item["content"] for item in messages if isinstance(item, dict)
                                and item["role"] == "system" and item["content"].startswith("Treat this request"))
        assert '"objective": "Create and verify the project"' in objective_anchor
        if planner_calls <= 8:
            assert not any("Progress review advice within current permissions" in item.get("content", "")
                           for item in messages if isinstance(item, dict) and item["role"] == "system")
            return _msg(tool_calls=[_tc(cid=f"checkpoint-call-{planner_calls}")]), _usage()
        saw_advice.append(any("Progress review advice within current permissions" in item.get("content", "")
                              and advice in item["content"]
                              for item in messages if isinstance(item, dict) and item["role"] == "system"))
        return _msg(content="The project was checked successfully."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"ok": True, "items": ["present"]}}, error=None,
        ui_components=[], correlation_id=None,
    ))

    await o.handle_chat_message(ws, "Create and verify the project", chat_id, user_id="seam-user")

    assert planner_calls == 9
    assert len(checkpoint_calls) == 1
    checkpoint_messages, checkpoint_tools, checkpoint_kwargs = checkpoint_calls[0]
    assert checkpoint_tools is None
    assert checkpoint_kwargs["allow_stream"] is False
    assert json.loads(checkpoint_messages[-1]["content"])["review_phase"] == "in_progress"
    assert len(final_reviews) == 1
    assert saw_advice == [True]
    assert "checked successfully" in await _last_assistant_text(o, chat_id, "seam-user")
    assert o.execute_single_tool.await_count == 8
    assert o.token_usage[chat_id]["total_tokens"] == 22


@pytest.mark.asyncio
@pytest.mark.parametrize(("review_text", "expected_reason"), [
    ('{"status":"blocked","remaining":[],"reason":"The project folder is unavailable."}',
     "The project folder is unavailable."),
    ('{"status":"complete","remaining":[],"reason":"Everything is finished."}',
     "The progress review was invalid; pause and inspect before continuing."),
])
async def test_blocked_or_invalid_checkpoint_stops_ninth_planner_and_publishes_incomplete(
    checkpoint_orch, monkeypatch, user_skills_disabled, review_text, expected_reason,
):
    from orchestrator import goal_verification

    o = checkpoint_orch
    monkeypatch.setenv("GOAL_MAX_TURNS", "12")
    _register(o)
    chat_id, ws = await _chat(o)
    stage = SimpleNamespace(publication_role="atomic", completion_state="completed")
    o._append_conversation_message = AsyncMock(return_value=123)
    o._publish_conversation_snapshot = AsyncMock(return_value={})
    planner_calls = 0
    review_calls = 0
    decisions = []
    parse_review = goal_verification.parse_progress_review

    def capture_decision(text):
        decision = parse_review(text)
        decisions.append(decision)
        return decision

    monkeypatch.setattr(goal_verification, "parse_progress_review", capture_decision)

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner_calls, review_calls
        if kwargs.get("feature") == "goal_verification":
            review_calls += 1
            if json.loads(messages[-1]["content"]).get("review_phase") == "in_progress":
                return _msg(content=review_text), _usage()
            pytest.fail("the final completion verifier must not run after a blocked checkpoint")
        planner_calls += 1
        return _msg(tool_calls=[_tc(cid=f"blocked-call-{planner_calls}")]), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"ok": True}}, error=None, ui_components=[], correlation_id=None,
    ))

    await o._handle_chat_message_impl(
        ws, "Create and verify the project", chat_id, user_id="seam-user",
        conversation_stage=stage, conversation_request_generation=str(uuid.uuid4()),
    )

    assert planner_calls == 8
    assert review_calls == 1
    assert len(decisions) == 1
    assert decisions[0].status == "blocked"
    assert decisions[0].reason == expected_reason
    assert o.execute_single_tool.await_count == 8
    assert stage.completion_state == "failed"
    o._publish_conversation_snapshot.assert_awaited_once()
    incomplete = [call.kwargs.get("content") for call in o._append_conversation_message.await_args_list
                  if "Request incomplete" in json.dumps(call.kwargs.get("content"))]
    assert len(incomplete) == 1
    assert incomplete[0][0]["message"] == expected_reason


@pytest.mark.asyncio
async def test_cancelled_checkpoint_accumulates_usage_without_advice_or_next_effect(
    checkpoint_orch, monkeypatch, user_skills_disabled,
):
    o = checkpoint_orch
    monkeypatch.setenv("GOAL_MAX_TURNS", "12")
    _register(o)
    chat_id, ws = await _chat(o)
    stage = SimpleNamespace(publication_role="atomic", completion_state="completed")
    o._append_conversation_message = AsyncMock(return_value=123)
    o._publish_conversation_snapshot = AsyncMock(return_value={})
    planner_calls = 0
    review_calls = 0

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner_calls, review_calls
        if kwargs.get("feature") == "goal_verification":
            review_calls += 1
            assert json.loads(messages[-1]["content"]).get("review_phase") == "in_progress"
            o.cancelled_sessions[id(websocket)] = True
            return _verdict("continue", ["Do not run after cancellation."])
        planner_calls += 1
        return _msg(tool_calls=[_tc(cid=f"cancel-call-{planner_calls}")]), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"ok": True}}, error=None, ui_components=[], correlation_id=None,
    ))

    await o._handle_chat_message_impl(
        ws, "Create and verify the project", chat_id, user_id="seam-user",
        conversation_stage=stage, conversation_request_generation=str(uuid.uuid4()),
    )

    assert planner_calls == 8
    assert review_calls == 1
    assert o.execute_single_tool.await_count == 8
    assert stage.completion_state == "cancelled"
    assert o.token_usage[chat_id]["total_tokens"] == 18
    o._publish_conversation_snapshot.assert_awaited_once()
    assert not any("Do not run after cancellation" in str(call.kwargs.get("content"))
                   for call in o._append_conversation_message.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(("checkpoint_kind", "expected_reason"), [
    ("provider_error", "The in-progress review could not be completed safely."),
    ("truncated", "The in-progress review was incomplete; pause before continuing."),
])
async def test_checkpoint_provider_errors_and_truncation_publish_explicit_incomplete(
    checkpoint_orch, monkeypatch, user_skills_disabled, checkpoint_kind, expected_reason,
):
    o = checkpoint_orch
    monkeypatch.setenv("GOAL_MAX_TURNS", "12")
    _register(o)
    chat_id, ws = await _chat(o)
    stage = SimpleNamespace(publication_role="atomic", completion_state="completed")
    o._append_conversation_message = AsyncMock(return_value=123)
    o._publish_conversation_snapshot = AsyncMock(return_value={})
    planner_calls = 0
    review_calls = 0

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner_calls, review_calls
        if kwargs.get("feature") == "goal_verification":
            review_calls += 1
            assert json.loads(messages[-1]["content"]).get("review_phase") == "in_progress"
            if checkpoint_kind == "provider_error":
                raise RuntimeError("provider unavailable")
            reply = _msg(content='{"status":"continue","remaining":["Incomplete advice."],"reason":"x"}')
            reply._astral_finish_reason = "length"
            return reply, _usage()
        planner_calls += 1
        return _msg(tool_calls=[_tc(cid=f"checkpoint-error-{planner_calls}")]), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"ok": True}}, error=None, ui_components=[], correlation_id=None,
    ))

    await o._handle_chat_message_impl(
        ws, "Create and verify the project", chat_id, user_id="seam-user",
        conversation_stage=stage, conversation_request_generation=str(uuid.uuid4()),
    )

    assert planner_calls == 8
    assert review_calls == 1
    assert o.execute_single_tool.await_count == 8
    assert stage.completion_state == "failed"
    o._publish_conversation_snapshot.assert_awaited_once()
    assert any(expected_reason in json.dumps(call.kwargs.get("content"))
               for call in o._append_conversation_message.await_args_list)


@pytest.mark.asyncio
async def test_async_checkpoint_cancellation_propagates_without_planner_continuation(
    checkpoint_orch, monkeypatch, user_skills_disabled,
):
    o = checkpoint_orch
    monkeypatch.setenv("GOAL_MAX_TURNS", "12")
    _register(o)
    chat_id, ws = await _chat(o)
    stage = SimpleNamespace(publication_role="atomic", completion_state="completed")
    o._append_conversation_message = AsyncMock(return_value=123)
    o._publish_conversation_snapshot = AsyncMock(return_value={})
    planner_calls = 0
    review_calls = 0

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner_calls, review_calls
        if kwargs.get("feature") == "goal_verification":
            review_calls += 1
            assert json.loads(messages[-1]["content"]).get("review_phase") == "in_progress"
            raise asyncio.CancelledError()
        planner_calls += 1
        return _msg(tool_calls=[_tc(cid=f"cancelled-provider-{planner_calls}")]), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"ok": True}}, error=None, ui_components=[], correlation_id=None,
    ))

    with pytest.raises(asyncio.CancelledError):
        await o._handle_chat_message_impl(
            ws, "Create and verify the project", chat_id, user_id="seam-user",
            conversation_stage=stage, conversation_request_generation=str(uuid.uuid4()),
        )

    assert planner_calls == 8
    assert review_calls == 1
    assert o.execute_single_tool.await_count == 8
    o._publish_conversation_snapshot.assert_not_awaited()
    assert o.token_usage[chat_id]["total_tokens"] == 16


@pytest.mark.asyncio
async def test_task_currentness_cancellation_after_checkpoint_fence_prevents_advice(
    checkpoint_orch, monkeypatch, user_skills_disabled,
):
    from orchestrator.task_state import TaskState

    o = checkpoint_orch
    monkeypatch.setenv("GOAL_MAX_TURNS", "12")
    monkeypatch.setitem(flags._flags, "task_state_machine", True)
    _register(o)
    chat_id, ws = await _chat(o)
    stage = SimpleNamespace(publication_role="atomic", completion_state="completed")
    task = SimpleNamespace(
        task_id="checkpoint-fence", tool_calls_made=[], turn_count=0,
        max_turns=12, _canonical_state=lambda: TaskState.RUNNING,
    )
    o.task_manager.admit_task = AsyncMock(return_value=task)
    o.task_manager.transition_task = AsyncMock()
    o.task_manager.refresh_task = AsyncMock()
    checks = 0

    async def assert_current(_task):
        nonlocal checks
        checks += 1
        if checks == 10:
            o.cancelled_sessions[id(ws)] = True
        return task

    o.task_manager.assert_current_execution = AsyncMock(side_effect=assert_current)
    o._append_conversation_message = AsyncMock(return_value=123)
    o._publish_conversation_snapshot = AsyncMock(return_value={})
    planner_calls = 0
    review_calls = 0

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner_calls, review_calls
        if kwargs.get("feature") == "goal_verification":
            review_calls += 1
            assert json.loads(messages[-1]["content"]).get("review_phase") == "in_progress"
            return _verdict("continue", ["Advice must not be injected after cancellation."])
        planner_calls += 1
        return _msg(tool_calls=[_tc(cid=f"fence-call-{planner_calls}")]), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"ok": True}}, error=None, ui_components=[], correlation_id=None,
    ))

    await o._handle_chat_message_impl(
        ws, "Create and verify the project", chat_id, user_id="seam-user",
        conversation_stage=stage, conversation_request_generation=str(uuid.uuid4()),
        operation_context={"operation": "chat_message", "owner": "seam-user"},
    )

    assert checks == 10
    assert planner_calls == 8
    assert review_calls == 1
    assert o.execute_single_tool.await_count == 8
    assert stage.completion_state == "cancelled"
    assert not any("Advice must not be injected" in json.dumps(call.kwargs.get("content"))
                   for call in o._append_conversation_message.await_args_list)
    assert o.task_manager.transition_task.await_args.args[1] is TaskState.CANCELLED


@pytest.mark.asyncio
async def test_goal_harness_off_does_not_call_progress_review(checkpoint_orch, monkeypatch, user_skills_disabled):
    o = checkpoint_orch
    monkeypatch.setitem(flags._flags, "goal_harness", False)
    _register(o)
    chat_id, ws = await _chat(o)
    calls = 0

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs.get("feature") != "goal_verification"
        return _msg(content="A concise answer."), _usage()

    o._call_llm = provider
    await o.handle_chat_message(ws, "Explain one thing", chat_id, user_id="seam-user")

    assert calls == 1
    assert "concise answer" in await _last_assistant_text(o, chat_id, "seam-user")
