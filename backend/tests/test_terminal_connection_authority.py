"""Exercises terminal execution authority across SSH admission and connection invalidation. Reads retain independent owner checks."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest

from orchestrator import terminal_ssh
from orchestrator.terminal_security import TerminalError
from tests.helpers.terminal_connection import registered_connection
from tests.test_terminal_ssh_runtime import isolated as isolated


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
