"""Exercises worker disconnect cleanup under ASGI and repeated asyncio cancellation.
The endpoint must finish exact-connection release and its authority hook before exit.
"""

import asyncio
from types import SimpleNamespace

import anyio
import pytest

from orchestrator.voice_worker_endpoint import _cleanup_connection


@pytest.mark.asyncio
async def test_asgi_cancel_scope_cannot_interrupt_release_or_hook():
    calls = []

    async def unregister(connection):
        await anyio.sleep(0)
        calls.append(connection)
        return ("released-session",)

    async def hook(receipt, released):
        await anyio.sleep(0)
        calls.append((receipt.connection_id, released))

    with anyio.CancelScope() as scope:
        scope.cancel()
        await _cleanup_connection(
            SimpleNamespace(unregister_worker=unregister),
            SimpleNamespace(connection_id="current-connection"),
            hook,
        )
    assert calls == ["current-connection", ("current-connection", ("released-session",))]


@pytest.mark.asyncio
async def test_repeated_task_cancellation_waits_for_one_cleanup_then_propagates():
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def unregister(connection):
        entered.set()
        await finish.wait()
        calls.append(connection)
        return ()

    async def hook(receipt, released):
        calls.append((receipt.connection_id, released))

    task = asyncio.create_task(_cleanup_connection(
        SimpleNamespace(unregister_worker=unregister),
        SimpleNamespace(connection_id="current-connection"),
        hook,
    ))
    await entered.wait()
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["current-connection", ("current-connection", ())]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("release failed"), asyncio.CancelledError()])
async def test_cleanup_failure_is_not_reported_as_success(error):
    async def unregister(_connection):
        raise error

    with pytest.raises(type(error)):
        await _cleanup_connection(
            SimpleNamespace(unregister_worker=unregister),
            SimpleNamespace(connection_id="current-connection"),
            None,
        )
