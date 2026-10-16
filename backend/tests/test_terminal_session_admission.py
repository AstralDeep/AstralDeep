"""Verifies atomic per-owner terminal admission and failed-spawn cleanup.
Controlled spawn barriers exercise TerminalSessionManager without launching a shell.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import tempfile

import pytest

from orchestrator import terminal_sessions as ts


@pytest.fixture
def manager_factory(monkeypatch, tmp_path):
    managers = []
    monkeypatch.setattr(ts.TerminalSession, "start_pump", lambda self: None)
    monkeypatch.setattr(ts.TerminalSessionManager, "_ensure_sweeper", lambda self: None)
    monkeypatch.setattr(ts, "_signal_group", lambda *args: None)
    monkeypatch.setattr(ts, "audit_machine_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(ts, "_child_cwd", lambda: tempfile.mkdtemp(dir=tmp_path))

    def build(spawn):
        manager = ts.TerminalSessionManager(spawn=spawn)
        managers.append(manager)
        return manager

    yield build
    for manager in managers:
        manager.shutdown()


def _spawned(cwd):
    process = SimpleNamespace(pid=-1, poll=lambda: None, wait=lambda timeout: 0)
    return -1, process, cwd


def test_pending_owner_creation_counts_against_the_cap_without_blocking_other_owners(
    manager_factory, monkeypatch,
):
    monkeypatch.setattr(ts, "SESSIONS_PER_OWNER", 1)
    entered, release = Event(), Event()
    attempts = []

    def spawn(argv, env, cwd, dimensions):
        attempts.append(cwd)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(timeout=10)
        return _spawned(cwd)

    manager = manager_factory(spawn)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(manager.create, "owner-a")
        try:
            assert entered.wait(timeout=10)
            other = manager.create("owner-b")
            assert other["session_id"] in {item["session_id"] for item in manager.list_sessions("owner-b")}
            with pytest.raises(ts.TerminalError) as refused:
                manager.create("owner-a")
            assert (refused.value.code, refused.value.status_code) == ("terminal_session_limit_reached", 409)
            assert len(attempts) == 2
        finally:
            release.set()
        assert first.result(timeout=10)["session_id"]
    assert len(manager.list_sessions("owner-a")) == 1


@pytest.mark.parametrize("failure", [OSError("synthetic spawn failure"), RuntimeError("synthetic spawn failure")])
def test_failed_spawn_releases_the_owner_slot_and_removes_its_directory(manager_factory, monkeypatch, failure):
    monkeypatch.setattr(ts, "SESSIONS_PER_OWNER", 1)
    attempts = []

    def spawn(argv, env, cwd, dimensions):
        attempts.append(cwd)
        if len(attempts) == 1:
            raise failure
        return _spawned(cwd)

    manager = manager_factory(spawn)
    expected = ts.TerminalError if isinstance(failure, OSError) else RuntimeError
    with pytest.raises(expected):
        manager.create("owner-a")
    assert not Path(attempts[0]).exists()
    assert manager.create("owner-a")["session_id"]
    with pytest.raises(ts.TerminalError, match="terminal_session_limit_reached"):
        manager.create("owner-a")


def test_failed_create_retains_another_pending_owner_reservation(manager_factory, monkeypatch):
    monkeypatch.setattr(ts, "SESSIONS_PER_OWNER", 2)
    entered, release = Event(), Event()
    attempts = []

    def spawn(argv, env, cwd, dimensions):
        attempts.append(cwd)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(timeout=10)
        elif len(attempts) == 2:
            raise OSError("synthetic spawn failure")
        return _spawned(cwd)

    manager = manager_factory(spawn)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(manager.create, "owner-a")
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(ts.TerminalError, match="terminal_spawn_failed"):
                manager.create("owner-a")
            assert manager.create("owner-a")["session_id"]
            with pytest.raises(ts.TerminalError, match="terminal_session_limit_reached"):
                manager.create("owner-a")
            assert len(attempts) == 3
        finally:
            release.set()
        assert first.result(timeout=10)["session_id"]
    assert len(manager.list_sessions("owner-a")) == 2


def test_workspace_creation_failure_does_not_reserve_owner_capacity(manager_factory, monkeypatch):
    monkeypatch.setattr(ts, "SESSIONS_PER_OWNER", 1)
    create_directory = ts._child_cwd
    attempts = []

    def directory():
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("synthetic workspace failure")
        return create_directory()

    monkeypatch.setattr(ts, "_child_cwd", directory)
    manager = manager_factory(lambda argv, env, cwd, dimensions: _spawned(cwd))
    with pytest.raises(OSError, match="synthetic workspace failure"):
        manager.create("owner-a")
    assert manager.create("owner-a")["session_id"]
