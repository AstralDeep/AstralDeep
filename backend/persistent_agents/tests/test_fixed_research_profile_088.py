"""Tests for persistent_agents/execution.py and research_episode.py: unsupported
identities are refused before reader-config capture, requested tool/scope must match
the fixed reader profile, and locked checks run before any policy callback.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.execution import ActionExecutor
from persistent_agents.models import AssignmentError
from persistent_agents.research_episode import run_research_episode, source_request
from persistent_agents.research_input import ResearchInput, fixed_reader_source

AGENT, TOOL = "web-research-1", "fetch_page"
REQUEST = {"kind": "tool", "agent_id": AGENT, "tool_name": TOOL,
           "arguments": {"url": "https://example.test/page"}}


def record():
    return SimpleNamespace(
        owner_id="owner", assignment_id="assignment", instruction_revision=1, control_epoch=1,
        execution_profile="one_shot",
        operation={"version": 2, "kind": "research", "source_retention": "operation"},
        definition=SimpleNamespace(allowed_tools=(AGENT + ":" + TOOL,), source={
            "profile": "public_page", "agent_id": AGENT, "tool_name": TOOL,
            "arguments": dict(REQUEST["arguments"]), "linked_document_urls": [],
        }),
    )


def executor(current):
    value = ActionExecutor.__new__(ActionExecutor)
    value.record = current
    value.operation_sessions = object()
    value.interactive = False
    value.remote_marker = value.approved_action_id = None
    value.orch = SimpleNamespace(tool_permissions=SimpleNamespace(get_tool_scope=lambda *a: "tools:read"))
    value._research_guidance = SimpleNamespace(assert_local=lambda: None)
    return value


def change_record(value, change):
    if change == "additional_tool":
        value.definition.allowed_tools += ("other-1:read",)
    elif change == "only_other_tool":
        value.definition.allowed_tools = ("other-1:read",)
    elif change == "duplicate_tool":
        value.definition.allowed_tools *= 2
    elif change == "empty_tools":
        value.definition.allowed_tools = ()
    elif change == "invalid_tools":
        value.definition.allowed_tools = None
    elif change == "wrong_kind":
        value.operation["kind"] = "chat"
    elif change == "persistent":
        value.execution_profile = "persistent"
    elif change == "registered_source":
        value.definition.source["profile"] = "registered_reader"
    elif change == "wrong_source_agent":
        value.definition.source["agent_id"] = "other-1"
    elif change == "wrong_source_tool":
        value.definition.source["tool_name"] = "search"
    elif change == "missing_source":
        value.definition.source = None
    else:
        value.definition = None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "additional_tool", "only_other_tool", "duplicate_tool", "empty_tools", "invalid_tools",
    "persistent", "registered_source", "wrong_source_agent", "wrong_source_tool",
    "missing_source", "missing_definition",
])
async def test_shared_profile_refuses_before_reader_config_capture_or_episode_action(change):
    value = record()
    change_record(value, change)
    worker = executor(value)
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        worker._operation_reader(REQUEST)
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        source_request(value)
    config = SimpleNamespace(capture_user=AsyncMock(side_effect=AssertionError("config touched")))
    with pytest.raises(DispatchDenied, match="assignment_research_binding_changed"):
        await ResearchInput.capture(value, None, config_store=config)
    config.capture_user.assert_not_called()
    worker.action = AsyncMock(side_effect=AssertionError("action touched"))
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        await run_research_episode(worker)
    worker.action.assert_not_called()


@pytest.mark.asyncio
async def test_other_operation_kind_cannot_enter_research_but_keeps_fixed_reader_contract():
    value = record()
    change_record(value, "wrong_kind")
    worker = executor(value)
    worker._operation_reader(REQUEST)
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        source_request(value)
    config = SimpleNamespace(capture_user=AsyncMock(side_effect=AssertionError("config touched")))
    with pytest.raises(DispatchDenied, match="assignment_research_binding_changed"):
        await ResearchInput.capture(value, None, config_store=config)
    config.capture_user.assert_not_called()


def test_supported_source_is_detached_and_the_episode_uses_its_exact_request():
    value = record()
    detached = fixed_reader_source(value)
    detached["arguments"]["url"] = "https://example.test/changed"
    assert source_request(value) == REQUEST
    executor(value)._operation_reader(REQUEST)


@pytest.mark.parametrize("field,value", [("agent_id", "other-1"), ("tool_name", "other"),
                                        ("agent_id", "WEB-RESEARCH-1")])
def test_different_requested_tool_cannot_borrow_fixed_reader_policy(field, value):
    request = {**REQUEST, field: value}
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        executor(record())._operation_reader(request)


@pytest.mark.parametrize("scope", ["tools:search", "tools:write", None])
def test_other_scope_cannot_enter_the_fixed_reader_profile(scope):
    worker = executor(record())
    worker.orch.tool_permissions.get_tool_scope = lambda *a: scope
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        worker._operation_reader(REQUEST)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["agent", "tool", "owner", "assignment", "revision", "control",
                                    "boundary", "sensitivity", "interactive", "transient", "current_tools"])
async def test_locked_action_and_current_record_are_checked_before_policy_or_callback(change):
    current = record()
    worker = executor(record())
    request = {**REQUEST}
    intent = SimpleNamespace(request=request, boundary="read_only", sensitivity="ordinary",
                             interactive_only=False, transient_input=None)
    action = SimpleNamespace(owner_id=current.owner_id, assignment_id=current.assignment_id,
                             instruction_revision=1, control_epoch=1, intent=intent)
    if change in {"agent", "tool"}:
        request["agent_id" if change == "agent" else "tool_name"] = "other"
    elif change in {"owner", "assignment"}:
        setattr(action, change + "_id", "other")
    elif change in {"revision", "control"}:
        setattr(action, "instruction_revision" if change == "revision" else "control_epoch", 2)
    elif change in {"boundary", "sensitivity"}:
        setattr(intent, change, "unreplayable" if change == "boundary" else "sensitive")
    elif change == "interactive":
        intent.interactive_only = True
    elif change == "transient":
        intent.transient_input = object()
    else:
        current.definition.allowed_tools += ("other-1:read",)
    repository = SimpleNamespace(get_action=Mock(return_value=action))
    tx = object()
    async def transaction(*, callback, **kwargs):
        return callback(tx, repository, current)
    worker.store = SimpleNamespace(operation_lifecycle_transaction=transaction)
    worker.claim, worker.binding = SimpleNamespace(fence=object()), object()
    worker._assert_fixed_reader_policy = Mock(side_effect=AssertionError("policy touched"))
    callback = Mock(side_effect=AssertionError("callback touched"))
    with pytest.raises(AssignmentError) as caught:
        await worker._reader_policy_transaction(object(), "action", callback)
    assert caught.value.code == "assignment_operation_profile_unavailable"
    assert caught.value.status_code == 403
    repository.get_action.assert_called_once_with(
        tx, owner_id=current.owner_id, assignment_id=current.assignment_id, action_id="action",
    )
    worker._assert_fixed_reader_policy.assert_not_called()
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_reader_policy_transaction_without_captured_guidance_refuses_before_any_transaction():
    worker = executor(record())
    worker._research_guidance = None
    worker.store = SimpleNamespace(operation_lifecycle_transaction=AsyncMock(
        side_effect=AssertionError("transaction opened")))
    worker.claim, worker.binding = SimpleNamespace(fence=object()), object()
    callback = Mock(side_effect=AssertionError("callback touched"))
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        await worker._reader_policy_transaction(object(), "action", callback)
    worker.store.operation_lifecycle_transaction.assert_not_awaited()
    callback.assert_not_called()
