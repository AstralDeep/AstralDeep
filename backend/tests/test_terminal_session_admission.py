"""Checks that unsupported terminal admission cannot allocate host resources or accept an executor override.
Concurrent requests retain a truthful unavailable result rather than simulating working shell sessions.
"""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from orchestrator import terminal_sessions as ts
from shared.feature_flags import flags


@pytest.mark.parametrize("enabled,reason,status", [
    (False, "terminal_disabled", 404),
    (True, "terminal_isolation_unavailable", 503),
])
def test_concurrent_admission_is_refused_before_any_host_resource(monkeypatch, enabled, reason, status):
    monkeypatch.setitem(flags._flags, "terminal_shell", enabled)
    effect = Mock(side_effect=AssertionError("host resource must not be allocated"))
    monkeypatch.setattr(ts, "_child_cwd", effect, raising=False)
    monkeypatch.setattr(ts.os, "openpty", effect)
    monkeypatch.setattr(ts.subprocess, "Popen", effect)
    manager = ts.TerminalSessionManager()

    def attempt(owner):
        with pytest.raises(ts.TerminalError) as refused:
            manager.create(owner, command="printf synthetic")
        return refused.value.code, refused.value.status_code

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(attempt, ["owner-a", "owner-a", "owner-b", "owner-b"])) == [(reason, status)] * 4
    effect.assert_not_called()
    assert manager._sessions == {}
    assert manager._sweeper is None


def test_public_spawn_override_cannot_enable_unsupported_execution():
    with pytest.raises(TypeError, match="spawn"):
        ts.TerminalSessionManager(spawn=lambda *_: None)


@pytest.mark.parametrize("enabled,reason", [(False, "terminal_disabled"), (True, "terminal_isolation_unavailable")])
def test_input_is_refused_without_looking_up_or_writing_to_an_existing_session(monkeypatch, enabled, reason):
    monkeypatch.setitem(flags._flags, "terminal_shell", enabled)
    manager = ts.TerminalSessionManager()
    manager._get_live = Mock(side_effect=AssertionError("unsupported input must not access a PTY"))
    with pytest.raises(ts.TerminalError, match=reason):
        manager.write("owner", "retained-session", "eAo=")
    manager._get_live.assert_not_called()
