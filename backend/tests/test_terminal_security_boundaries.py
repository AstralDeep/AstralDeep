"""Checks the terminal manager refuses unsupported execution and keeps system-scoped tools security flagged."""


import pytest

from agents.terminal_shell.mcp_tools import TOOL_REGISTRY
from orchestrator import terminal_sessions
from orchestrator.tool_security import ThreatCategory, ToolSecurityAnalyzer
from shared.feature_flags import flags


OWNER = "terminal-owner"
SESSION = "retained-session"
@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setitem(flags._flags, "terminal_shell", True)


def test_default_create_refuses_before_workspace_pty_or_process(enabled, monkeypatch):
    effects = []

    def forbidden(*args, **kwargs):
        effects.append(True)
        raise AssertionError("unsupported host execution was attempted")

    monkeypatch.setattr(terminal_sessions, "_child_cwd", forbidden, raising=False)
    monkeypatch.setattr(terminal_sessions.os, "openpty", forbidden)
    manager = terminal_sessions.TerminalSessionManager()
    with pytest.raises(terminal_sessions.TerminalError) as refused:
        manager.create(OWNER, command="printf synthetic")
    assert (refused.value.code, refused.value.status_code) == ("terminal_isolation_unavailable", 503)
    assert not effects


def test_default_write_refuses_before_accessing_any_session(enabled, monkeypatch):
    manager = terminal_sessions.TerminalSessionManager()
    effects = []

    def forbidden(*args):
        effects.append(True)
        raise AssertionError("unsupported host terminal input was attempted")

    monkeypatch.setattr(manager, "_get_live", forbidden)
    with pytest.raises(terminal_sessions.TerminalError) as refused:
        manager.write(OWNER, SESSION, "eAo=")
    assert (refused.value.code, refused.value.status_code) == ("terminal_isolation_unavailable", 503)
    assert not effects


@pytest.mark.parametrize("name", ["start_terminal", "terminal_write"])
def test_arbitrary_execution_is_system_scoped_and_hard_security_flagged(name):
    entry = TOOL_REGISTRY[name]
    assert entry["scope"] == "tools:system"
    assert entry["destructive"] == "always"
    flag = ToolSecurityAnalyzer().analyze_tool(name, entry["description"], entry["input_schema"])
    assert flag is not None
    assert flag.category is ThreatCategory.CODE_EXECUTION
    assert flag.blocked is True
