"""Tests for persistent_agents/store.py: the host guard keeps authority, assignment and
admission in one transaction; denial rolls back without a callback or leaking private
error text; guard callbacks cannot return an awaitable.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astralplane.repositories import RepositoryConflictError
from persistent_agents.models import AssignmentError
from persistent_agents.store import AssignmentStore


def guarded_store(repository):
    transaction = object()

    async def run(callback):
        return callback(transaction)

    adapter = SimpleNamespace(run_in_transaction=AsyncMock(side_effect=run))
    runtime = SimpleNamespace(repositories=SimpleNamespace(assignments=repository))
    return AssignmentStore(plane_runtime=runtime, async_runtime=adapter), transaction


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", [None, "unsupported"])
async def test_old_plane_contract_refuses_before_opening_transaction(guard):
    store, _ = guarded_store(SimpleNamespace(assert_current_assignment_execution=guard))
    callback = Mock()
    with pytest.raises(AssignmentError, match="assignment_repository_contract_unavailable") as error:
        await store.current_execution_transaction(fence=object(), binding=object(), callback=callback)
    assert error.value.status_code == 503
    callback.assert_not_called()
    store.async_runtime.run_in_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_then_callback_share_transaction_and_exact_action_restriction():
    events = []
    current, fence, binding, action_id = object(), object(), object(), object()
    repository = SimpleNamespace(assert_current_assignment_execution=Mock(
        side_effect=lambda *args, **kwargs: (events.append("guard"), current)[1]))
    store, transaction = guarded_store(repository)

    def write(tx, repo, record):
        assert (tx, repo, record) == (transaction, repository, current)
        events.append("write")
        return "committed"

    assert await store.current_execution_transaction(
        fence=fence, binding=binding, action_id=action_id, callback=write) == "committed"
    assert events == ["guard", "write"]
    repository.assert_current_assignment_execution.assert_called_once_with(
        transaction, fence=fence, binding=binding, action_id=action_id)


@pytest.mark.asyncio
async def test_guard_denial_rolls_back_without_callback_or_private_error_text():
    repository = SimpleNamespace(assert_current_assignment_execution=Mock(side_effect=
        RepositoryConflictError("PRIVATE authority detail", code="assignment_authorization_unavailable")))
    store, _ = guarded_store(repository)
    callback = Mock()
    with pytest.raises(AssignmentError) as error:
        await store.current_execution_transaction(fence=object(), binding=object(), callback=callback)
    assert error.value.code == "assignment_authorization_unavailable"
    assert error.value.status_code == 409
    assert "PRIVATE" not in str(error.value)
    callback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("coroutine", [True, False])
async def test_incompatible_async_guard_cannot_authorize_mutation(coroutine):
    awaited = []

    async def guard():
        awaited.append(True)

    class OtherAwaitable:
        def __await__(self):
            awaited.append(True)
            yield

    result = guard() if coroutine else OtherAwaitable()
    store, _ = guarded_store(SimpleNamespace(assert_current_assignment_execution=Mock(return_value=result)))
    callback = Mock()
    with pytest.raises(AssignmentError, match="assignment_repository_contract_unavailable"):
        await store.current_execution_transaction(fence=object(), binding=object(), callback=callback)
    callback.assert_not_called()
    assert not awaited
    if coroutine:
        assert result.cr_frame is None


@pytest.mark.asyncio
@pytest.mark.parametrize("coroutine", [True, False])
async def test_transaction_callback_cannot_return_awaitable(coroutine):
    store, _ = guarded_store(SimpleNamespace(assert_current_assignment_execution=Mock()))
    awaited = []

    async def remote():
        awaited.append(True)

    class OtherAwaitable:
        def __await__(self):
            awaited.append(True)
            yield

    result = remote() if coroutine else OtherAwaitable()
    with pytest.raises(AssignmentError, match="assignment_transaction_callback_invalid"):
        await store.current_execution_transaction(
            fence=object(), binding=object(), callback=lambda *args: result)
    assert not awaited
    if coroutine:
        assert result.cr_frame is None
