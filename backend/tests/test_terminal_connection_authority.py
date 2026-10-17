"""Exercises terminal execution authority across real UI disconnect and SSH admission boundaries.
Safe retained-session REST operations keep their independent owner authorization path.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import MethodType
from unittest.mock import Mock

import httpx
import pytest

from orchestrator import terminal_approvals, terminal_ssh
from orchestrator.orchestrator import Orchestrator
from orchestrator.terminal_security import TerminalError, bind_terminal_authority
from tests.helpers.terminal_connection import registered_connection
from tests.test_terminal_handoff_delivery import handoff as handoff
from tests.test_terminal_approvals import fixture as fixture
from tests.test_terminal_ssh_runtime import isolated as isolated


@pytest.mark.asyncio
async def test_real_eof_drain_invalidates_pending_and_bound_execution_before_cleanup(handoff):
    env, orch = handoff.fixture, handoff.fixture.orch
    orch._open_chrome_surface = {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    assert response.status_code == 202
    pending = handoff.state["request"]
    with terminal_approvals.effect_scope(pending):
        authority = bind_terminal_authority(orch, env.socket, "owner", "start_terminal")
    context = orch._connection_contexts[id(env.socket)]
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_capacity():
        entered.set()
        await release.wait()

    async def eof():
        raise StopAsyncIteration()

    orch._new_connection_context = lambda _: context
    orch._drain_connection_context = MethodType(Orchestrator._drain_connection_context, orch)
    orch._notify_interactive_capacity = blocked_capacity
    task = asyncio.create_task(Orchestrator._serve_ui_frames(orch, env.socket, eof))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert context.closing is True and context.registered is True
        with pytest.raises(TerminalError, match="terminal_review_context_required"):
            pending.assert_current()
        with pytest.raises(TerminalError):
            authority.assert_current("start_terminal")
        orch.execute_single_tool.assert_not_awaited()
    finally:
        release.set()
        with pytest.raises(StopAsyncIteration):
            await task


def _lose_context(orch, socket, loss):
    context = orch._connection_contexts[id(socket)]
    if loss == "closing":
        context.closing = True
    elif loss == "unregistered":
        context.registered = False
    elif loss == "foreign":
        context.websocket = object()
    elif loss == "retired":
        orch._connection_contexts.pop(id(socket))
    elif loss == "replacement":
        registered_connection(orch, socket)
    elif loss == "registry":
        orch._connection_contexts = dict(orch._connection_contexts)
    else:
        orch._connection_contexts = []


@pytest.mark.parametrize("loss", ["closing", "unregistered", "foreign", "retired", "replacement", "registry", "malformed"])
def test_context_loss_after_blocking_ssh_connect_closes_before_pty(isolated, monkeypatch, loss):
    env = isolated
    authority = env.authority("start_terminal")
    entered, release = Event(), Event()
    connect = terminal_ssh.ParamikoTransport._connect

    def blocked_connect(*args):
        result = connect(*args)
        result[0].open_session = Mock(wraps=result[0].open_session)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(terminal_ssh.ParamikoTransport, "_connect", blocked_connect)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(env.manager.create, "owner", authority=authority,
                                 runtime_binding=env.manager.runtime_binding("owner"))
        try:
            assert entered.wait(5)
            _lose_context(env.orch, env.socket, loss)
        finally:
            release.set()
        with pytest.raises(TerminalError, match="terminal_connection_unavailable"):
            future.result()
    assert env.state.channels[0].effects == []
    env.state.clients[0].open_session.assert_not_called()
    assert env.state.clients[0].closed
    assert env.manager._sessions == {} and env.manager._pending == {}


@pytest.mark.parametrize("loss", ["closing", "retired", "replacement", "registry"])
def test_retained_session_keeps_original_execution_connection_lifetime(isolated, loss):
    env = isolated
    ident = env.create()["session_id"]
    _lose_context(env.orch, env.socket, loss)
    authority = env.authority("terminal_read")
    with pytest.raises(TerminalError, match="terminal_connection_unavailable"):
        env.manager.read("owner", ident, offset=0, authority=authority)
    assert env.state.channels[0].closed


@pytest.mark.parametrize("tool", ["start_terminal", "terminal_write"])
def test_missing_current_context_never_binds_execution_authority(isolated, tool):
    isolated.orch._connection_contexts.clear()
    with pytest.raises(TerminalError, match="terminal_connection_unavailable"):
        isolated.authority(tool)
    assert isolated.state.connects == []


def test_safe_owner_read_authority_does_not_require_an_execution_connection(isolated):
    isolated.orch._connection_contexts.clear()
    for tool in ("list_terminals", "terminal_read", "terminal_resize", "terminal_interrupt", "close_terminal"):
        isolated.authority(tool).assert_current(tool)
