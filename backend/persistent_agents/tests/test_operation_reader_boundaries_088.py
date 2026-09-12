"""Closed adapter contracts, alongside actual PostgreSQL dispatch journeys."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.models import AssignmentError
from persistent_agents.tests.test_dispatch import context
from persistent_agents.tests.test_dual_fence_088 import guarded_store


@pytest.mark.parametrize("extra", [
    {"_unknown": "private"}, {"url": "https://changed.org"},
    {"user_id": "other"}, {"session_id": "other"},
    {"_credentials": {"key": "cipher"}}, {"_credentials_encrypted": True},
    {"_credentials": {}, "_credentials_encrypted": True},
    {"_credentials": {"key": "cipher"}, "_credentials_encrypted": 1},
    {"_credentials": {"_private": "cipher"}, "_credentials_encrypted": True},
    {"_credentials": {"key": {}}, "_credentials_encrypted": True},
])
def test_closed_final_arguments_refuse_unbound_metadata(extra):
    ctx = context(strict_final_arguments=True, conversation_id="chat")
    with pytest.raises(DispatchDenied, match="assignment_action_binding_changed"):
        ctx.validate_final_tool_arguments({**ctx.arguments, **extra})


@pytest.mark.parametrize("arguments", [None, [], "private"])
def test_missing_final_argument_object_cannot_enter_strict_context(arguments):
    with pytest.raises(DispatchDenied):
        context(strict_final_arguments=True).validate_final_tool_arguments(arguments)


def test_exact_host_context_and_encrypted_transport_metadata_are_allowed():
    ctx = context(strict_final_arguments=True, conversation_id="chat")
    ctx.validate_final_tool_arguments({**ctx.arguments, "session_id": "chat", "user_id": "owner",
        "_credentials": {"key": "cipher"}, "_credentials_encrypted": True,
        "_session_llm_credentials": {"OPENAI_API_KEY": "synthetic"},
        "_delegation_token": "synthetic", "_cap_job_id": "synthetic"})


@pytest.mark.asyncio
async def test_strict_context_cannot_be_invoked_without_final_binding():
    ctx = context(strict_final_arguments=True)
    send = AsyncMock()
    with pytest.raises(DispatchDenied):
        await ctx.invoke_tool(send)
    ctx.authorize.assert_not_awaited()
    ctx.start.assert_not_awaited()
    send.assert_not_awaited()


def operation_store(repository):
    store, transaction = guarded_store(repository)
    store.plane_runtime.repositories.history = SimpleNamespace(sessions=SimpleNamespace(
        bound_request_execution_waits=Mock()))
    return store, transaction


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["put_action", "_load", "unknown", "start_action_for_execution"])
async def test_missing_or_unguarded_repository_method_fails_before_transaction(method):
    store, _ = operation_store(SimpleNamespace())
    with pytest.raises(AssignmentError, match="assignment_repository_contract_unavailable"):
        await store.call_for_operation(method)
    store.async_runtime.run_in_transaction.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("coroutine", [True, False])
async def test_named_repository_operation_cannot_return_or_await_remote_work(coroutine):
    calls = []
    async def work():
        calls.append("awaited")
    class OtherAwaitable:
        def __await__(self):
            calls.append("awaited")
            yield
    pending = work() if coroutine else OtherAwaitable()
    store, _ = operation_store(SimpleNamespace(put_action_for_execution=Mock(return_value=pending)))
    with pytest.raises(AssignmentError, match="assignment_repository_contract_unavailable"):
        await store.call_for_operation("put_action_for_execution")
    assert not calls
    if coroutine:
        assert pending.cr_frame is None


@pytest.mark.asyncio
async def test_cached_read_rechecks_after_actual_content_and_never_returns_on_guard_loss():
    guard = Mock(side_effect=[object(), AssignmentError("assignment_claim_stale", 409)])
    repository = SimpleNamespace(assert_current_assignment_execution=guard,
                                 get_action=Mock(return_value="retained private content"))
    store, transaction = operation_store(repository)
    fence = SimpleNamespace(owner_id="owner", assignment_id="assignment")
    binding, authority = object(), object()
    with pytest.raises(AssignmentError, match="assignment_claim_stale"):
        await store.read_current_action(fence=fence, binding=binding, action_id="action", authority=authority)
    assert guard.call_count == 2 and guard.call_args_list[0] == guard.call_args_list[1]
    repository.get_action.assert_called_once_with(transaction, owner_id="owner",
        assignment_id="assignment", action_id="action")


@pytest.mark.asyncio
async def test_cached_read_refuses_missing_guard_before_touching_content():
    repository = SimpleNamespace(get_action=Mock())
    store, _ = operation_store(repository)
    with pytest.raises(AssignmentError):
        await store.read_current_action(fence=object(), binding=object(), action_id="action", authority=object())
    repository.get_action.assert_not_called()
    store.async_runtime.run_in_transaction.assert_not_awaited()
