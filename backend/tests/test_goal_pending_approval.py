"""Tests HITL approval barriers and bounded goal evidence across approval continuation."""

from types import SimpleNamespace

import pytest

from orchestrator.goal_progress import CompletionDecision, GoalProgress
from orchestrator.goal_requirements import build_goal_contract, validate_goal_proofs
from orchestrator import hitl_confirmation
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import _dispatch, task_runtime  # noqa: F401


def _approval_result():
    return SimpleNamespace(
        result={"_data": {"status": "confirmation_required", "message": "Review this action."}},
        error=None,
        ui_components=[{"type": "card", "title": "Review action"}],
    )


def test_confirmation_required_is_pending_not_success_or_proof():
    progress = GoalProgress("Create the requested project.")
    progress.record("call-1", "make_directory", {"path": "/project"}, _approval_result(),
                    agent_id="remote-compute-1")

    outcome = progress.evidence()["call_outcomes"][0]
    assert outcome["outcome"] == "pending_approval"
    assert outcome["completed"] is False
    assert progress.requires_approval is True
    assert progress.assess(CompletionDecision("complete")).status != "complete"

    assessment = validate_goal_proofs(
        build_goal_contract("action"), {"outcome": ["call-1"]},
        progress.private_observations(), None,
    )
    assert assessment.accepted is False


def test_confirmation_required_detector_uses_actual_result_envelope():
    assert GoalProgress.is_approval_pending(_approval_result()) is True
    assert GoalProgress.is_approval_pending(SimpleNamespace(
        result={"_data": {"status": "approved"}}, error=None,
    )) is False


@pytest.mark.parametrize("tool_name,result,expected", [
    ("write_file", SimpleNamespace(result={"_data": {"path": "/project/main.py", "bytes": 100}}, error=None), True),
    ("run_command", SimpleNamespace(result={"_data": {
        "exit_code": 0, "timed_out": False, "background": False, "cwd": "/project",
    }}, error=None), True),
    ("run_command", SimpleNamespace(result={"_data": {
        "job_id": "job-a", "background": True,
    }}, error=None), False),
    ("read_output", SimpleNamespace(result={"_data": {
        "job_id": "job-a", "state": "running",
    }}, error=None), False),
    ("write_file", _approval_result(), False),
    ("write_file", SimpleNamespace(result={"_data": {"code": "effect_uncertain"}}, error=None), False),
    ("write_file", None, False),
])
def test_completed_success_predicate_uses_recording_semantics(tool_name, result, expected):
    assert GoalProgress.is_completed_success(tool_name, result) is expected


def test_approved_effect_resolves_only_its_token_and_records_actual_result():
    progress = GoalProgress("Create the requested project.")
    progress.record("call-3", "make_directory", {"path": "/project/a"}, _approval_result(),
                    agent_id="remote-compute-1", approval_token="approval-a")
    progress.record("call-4", "make_directory", {"path": "/project/b"}, _approval_result(),
                    agent_id="remote-compute-1", approval_token="approval-b")
    actual = SimpleNamespace(result={"_data": {"path": "/project/a"}}, error=None)

    assert progress.resolve_approval(
        "approval-a", actual, tool_name="make_directory", arguments={"path": "/project/a"},
        agent_id="remote-compute-1",
    ) is True
    assert progress.requires_approval is True
    outcomes = progress.evidence()["call_outcomes"]
    assert [item["outcome"] for item in outcomes] == [
        "pending_approval", "pending_approval", "success",
    ]
    successes = progress.evidence()["completed_successes"]
    assert len(successes) == 1
    assert successes[0]["tool"] == "make_directory"


def test_approval_resolution_rejects_wrong_action_or_second_pending_card():
    progress = GoalProgress("Create the requested project.")
    progress.record("call-5", "make_directory", {"path": "/project/a"}, _approval_result(),
                    agent_id="remote-compute-1", approval_token="approval-a")

    assert progress.resolve_approval(
        "approval-a", _approval_result(), tool_name="make_directory",
        arguments={"path": "/project/a"}, agent_id="remote-compute-1",
    ) is False
    assert progress.resolve_approval(
        "approval-a", SimpleNamespace(result={"_data": {"path": "/project/a"}}, error=None),
        tool_name="write_file", arguments={"path": "/project/a/main.py"},
        agent_id="remote-compute-1",
    ) is False
    assert progress.requires_approval is True


def test_fast_approval_is_retained_until_the_pending_dispatch_is_recorded():
    progress = GoalProgress("Create the requested project.")
    actual = SimpleNamespace(result={"_data": {"path": "/project/a"}}, error=None)

    assert progress.resolve_approval(
        "approval-race", actual, tool_name="make_directory", arguments={"path": "/project/a"},
        agent_id="remote-compute-1",
    ) is True
    assert progress.requires_approval is False
    progress.record("call-race", "make_directory", {"path": "/project/a"}, _approval_result(),
                    agent_id="remote-compute-1", approval_token="approval-race")

    assert progress.requires_approval is False
    assert [item["outcome"] for item in progress.evidence()["call_outcomes"]] == [
        "pending_approval", "success",
    ]


def _goal_context_fixture():
    websocket = object()
    orch = SimpleNamespace(
        ui_sessions={websocket: {"sub": "owner"}},
        _ws_active_chat={id(websocket): "chat"},
    )
    progress = GoalProgress("Create a project and verify it.")
    hitl_confirmation.begin_task(orch, websocket, "owner", "chat")
    return orch, websocket, progress


def test_goal_context_is_only_exposed_inside_server_approval_continuation():
    orch, websocket, progress = _goal_context_fixture()
    binding = hitl_confirmation.bind_goal_context(
        orch, websocket, "owner", "chat", progress, {"project_id": "project"},
        build_goal_contract("local_project"),
    )

    assert binding is not None
    assert hitl_confirmation.current_goal_context(orch, websocket, "owner", "chat") is None
    with hitl_confirmation._goal_continuation_scope(binding):
        assert hitl_confirmation.current_goal_context(orch, websocket, "owner", "chat") is binding
        assert hitl_confirmation.current_goal_context(orch, object(), "owner", "chat") is None
    assert hitl_confirmation.current_goal_context(orch, websocket, "owner", "chat") is None


def test_goal_continuation_binding_expires_with_task_or_cancellation():
    orch, websocket, progress = _goal_context_fixture()
    binding = hitl_confirmation.bind_goal_context(
        orch, websocket, "owner", "chat", progress, {"project_id": "project"},
        build_goal_contract("local_project"),
    )
    assert binding is not None
    with hitl_confirmation._goal_continuation_scope(binding):
        hitl_confirmation.begin_task(orch, websocket, "owner", "chat")
        assert hitl_confirmation.current_goal_context(orch, websocket, "owner", "chat") is None

    binding = hitl_confirmation.bind_goal_context(
        orch, websocket, "owner", "chat", progress, {"project_id": "project"},
        build_goal_contract("local_project"),
    )
    assert binding is not None
    with hitl_confirmation._goal_continuation_scope(binding):
        hitl_confirmation.cancel_pending(orch, websocket, "owner", "chat")
        assert hitl_confirmation.current_goal_context(orch, websocket, "owner", "chat") is None


@pytest.mark.parametrize("change", ["owner", "chat", "cancellation"])
def test_goal_continuation_binding_rejects_changed_session_authority(change):
    orch, websocket, progress = _goal_context_fixture()
    binding = hitl_confirmation.bind_goal_context(
        orch, websocket, "owner", "chat", progress, {"project_id": "project"},
        build_goal_contract("local_project"),
    )
    assert binding is not None
    if change == "owner":
        orch.ui_sessions[websocket]["sub"] = "other-owner"
    elif change == "chat":
        orch._ws_active_chat[id(websocket)] = "other-chat"
    else:
        hitl_confirmation.cancel_pending(orch, websocket, "owner", "chat")
    with hitl_confirmation._goal_continuation_scope(binding):
        assert hitl_confirmation.current_goal_context(orch, websocket, "owner", "chat") is None


def test_goal_call_scope_is_opaque_and_restores_nested_dispatch_identity():
    assert hitl_confirmation.current_goal_call_token() is None
    with hitl_confirmation.goal_call_scope("call-a"):
        assert hitl_confirmation.current_goal_call_token() == "call-a"
        with hitl_confirmation.goal_call_scope("call-b"):
            assert hitl_confirmation.current_goal_call_token() == "call-b"
        assert hitl_confirmation.current_goal_call_token() == "call-a"
    assert hitl_confirmation.current_goal_call_token() is None


async def test_real_review_card_retains_same_goal_context_and_call_token(task_runtime):  # noqa: F811
    orch, websocket, _effects, _turns, _proposals, _outcome = task_runtime
    hitl_confirmation.begin_task(orch, websocket, "owner-a", "chat-a")
    progress = GoalProgress("Send the message after review.")
    binding = hitl_confirmation.bind_goal_context(
        orch, websocket, "owner-a", "chat-a", progress, {"project_id": "project-a"},
        build_goal_contract("action"),
    )

    with hitl_confirmation.goal_call_scope("dispatch-token"):
        response = await _dispatch((orch, websocket), args={"to": "a@example.org", "body": "safe"})

    call = next(iter(orch._hitl_pending_calls.values()))
    assert response.result["_data"]["status"] == "confirmation_required"
    assert call.goal_context is binding
    assert call.goal_call_token == "dispatch-token"


async def test_goal_context_survives_await_inside_approval_continuation():
    import asyncio

    orch, websocket, progress = _goal_context_fixture()
    binding = hitl_confirmation.bind_goal_context(
        orch, websocket, "owner", "chat", progress, {"project_id": "project"},
        build_goal_contract("local_project"),
    )
    observed = []

    async def send(_websocket, _payload):
        return True

    async def serialized(_websocket, _message, chat, _display, *, user_id, **_kwargs):
        await asyncio.sleep(0)
        observed.append(hitl_confirmation.current_goal_context(orch, websocket, user_id, chat))

    orch._safe_send = send
    orch._serialized_chat = serialized
    continuation = hitl_confirmation.continue_after_approval(
        orch, websocket, "owner", "chat", "continue", "Continuing", None,
        guard=lambda: True, goal_context=binding, name="goal-continuation-test",
    )
    await continuation

    assert observed == [binding]


async def test_stale_goal_context_does_not_resume_after_approval():
    orch, websocket, progress = _goal_context_fixture()
    binding = hitl_confirmation.bind_goal_context(
        orch, websocket, "owner", "chat", progress, {"project_id": "project"},
        build_goal_contract("local_project"),
    )
    calls = []

    async def send(_websocket, _payload):
        return True

    async def serialized(*args, **kwargs):
        calls.append((args, kwargs))

    orch._safe_send = send
    orch._serialized_chat = serialized
    hitl_confirmation.cancel_pending(orch, websocket, "owner", "chat")
    continuation = hitl_confirmation.continue_after_approval(
        orch, websocket, "owner", "chat", "continue", "Continuing", None,
        guard=lambda: True, goal_context=binding, name="stale-goal-continuation-test",
    )
    await continuation

    assert calls == []
