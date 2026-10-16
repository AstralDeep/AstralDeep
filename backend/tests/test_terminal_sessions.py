"""Verifies retained terminal output, owner isolation and cleanup without executing a shell.
The manager's execution refusals are covered separately by the terminal security boundary suites.
"""

import base64
import errno
import fcntl
import os
import signal
import struct
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestrator import terminal_sessions as ts
from shared.feature_flags import flags


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value


class RetainedProcess:
    pid = -1
    done = False

    def poll(self):
        return 0 if self.done else None

    def wait(self, timeout):
        self.done = True
        return 0


@pytest.fixture
def retained(monkeypatch, tmp_path):
    monkeypatch.setitem(flags._flags, "terminal_shell", True)
    audits = []
    monkeypatch.setattr(ts, "audit_machine_event", lambda *args, **kwargs: audits.append((args, kwargs)))
    clock = Clock()
    manager = ts.TerminalSessionManager(clock=clock)
    descriptors = []

    def add(owner="owner", ident="session", process=None):
        read_fd, write_fd = os.pipe()
        descriptors.extend([(fd, os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in (read_fd, write_fd)])
        cwd = tmp_path / ident
        cwd.mkdir()
        session = ts.TerminalSession(ident, owner, read_fd, process or RetainedProcess(), str(cwd), now=clock)
        manager._sessions[ident] = session
        return session, write_fd

    yield SimpleNamespace(manager=manager, add=add, clock=clock, audits=audits)
    manager.shutdown()
    for fd, device, inode in descriptors:
        try:
            current = os.fstat(fd)
            if (current.st_dev, current.st_ino) == (device, inode):
                os.close(fd)
        except OSError:
            pass


def test_owner_listing_and_monotonic_activity(retained):
    first, _ = retained.add(ident="first")
    retained.clock.value += 2
    retained.add(ident="second")
    retained.add(owner="other", ident="foreign")
    assert [x["session_id"] for x in retained.manager.list_sessions("owner")] == ["first", "second"]
    first.touch()
    assert retained.manager.describe(first)["idle_seconds"] == 0
    first.append_output(b"")
    assert first.ring_end == 0


@pytest.mark.parametrize("method", ["read", "resize", "interrupt", "close"])
@pytest.mark.parametrize("owner,ident", [("other", "session"), ("owner", "missing")])
def test_unknown_and_foreign_sessions_are_indistinguishable(retained, method, owner, ident):
    retained.add()
    arguments = {"read": {"offset": 0}, "resize": {"cols": 80, "rows": 24}}
    with pytest.raises(ts.TerminalError) as refused:
        getattr(retained.manager, method)(owner, ident, **arguments.get(method, {}))
    assert (refused.value.code, refused.value.status_code) == ("terminal_session_not_found", 404)


def test_ring_bounds_binary_chunks_offsets_and_closed_output(retained, monkeypatch):
    monkeypatch.setattr(ts, "OUTPUT_RING_BYTES", 8)
    monkeypatch.setattr(ts, "OUTPUT_CHUNK_MAX", 4)
    session, _ = retained.add()
    session.append_output(b"0123456789")
    result = retained.manager.read("owner", "session", offset=0)
    assert base64.b64decode(result["data_b64"]) == b"2345"
    assert result["truncated"] and result["next_offset"] == 6
    assert (session.ring_start, session.ring_end) == (2, 10)
    assert session.read(100) == (b"", 10, False, False)
    retained.manager.close("owner", "session")
    closed = retained.manager.read("owner", "session", offset=6)
    assert base64.b64decode(closed["data_b64"]) == b"6789"
    assert closed["eof"] and closed["session"] is None


@pytest.mark.parametrize("offset", [True, -1, "0"])
def test_invalid_read_offsets_are_refused(retained, offset):
    retained.add()
    with pytest.raises(ts.TerminalError, match="terminal_offset_invalid"):
        retained.manager.read("owner", "session", offset=offset)


@pytest.mark.parametrize("wait", [True, -1, "1", float("nan"), float("inf")])
def test_invalid_waits_are_refused_without_polling(retained, wait):
    retained.add()
    with pytest.raises(ts.TerminalError, match="terminal_wait_invalid"):
        retained.manager.read("owner", "session", offset=0, wait_seconds=wait)


def test_bounded_wait_resolves_output_and_deadline_using_controlled_clock(retained, monkeypatch):
    session, _ = retained.add()
    calls = []

    def readable(value, timeout):
        calls.append(timeout)
        value.append_output(b"ready")

    monkeypatch.setattr(retained.manager, "_wait_readable", readable)
    assert retained.manager.read("owner", "session", offset=0, wait_seconds=100)["bytes"] == 5
    assert calls == [ts._POLL_SLICE_S]
    session._ring.clear()
    monkeypatch.setattr(retained.manager, "_wait_readable", lambda *_: setattr(retained.clock, "value", 1100))
    assert retained.manager.read("owner", "session", offset=0, wait_seconds=1)["bytes"] == 0


@pytest.mark.parametrize("field,value", [("cols", True), ("cols", 1), ("cols", 1001), ("rows", "2"), ("rows", 301)])
def test_resize_rejects_invalid_dimensions(retained, field, value):
    retained.add()
    dimensions = {"cols": 80, "rows": 24, field: value}
    with pytest.raises(ts.TerminalError, match="terminal_" + field + "_invalid"):
        retained.manager.resize("owner", "session", **dimensions)


def test_resize_and_interrupt_target_only_the_retained_owned_session(retained, monkeypatch):
    session, _ = retained.add()
    size = Mock()
    group = Mock()
    foreground = Mock()
    monkeypatch.setattr(ts, "_apply_winsize", size)
    monkeypatch.setattr(ts, "_signal_group", group)
    monkeypatch.setattr(ts, "_signal_pgid", foreground)
    monkeypatch.setattr(os, "tcgetpgrp", lambda _: 12345)
    assert retained.manager.resize("owner", "session", cols=120, rows=40)["cols"] == 120
    size.assert_called_once_with(session.master_fd, (120, 40))
    group.assert_called_once_with(session, signal.SIGWINCH)
    retained.manager.interrupt("owner", "session")
    foreground.assert_called_once_with(12345, signal.SIGINT)
    monkeypatch.setattr(os, "tcgetpgrp", Mock(side_effect=OSError()))
    retained.manager.interrupt("owner", "session")
    assert group.call_args.args == (session, signal.SIGINT)
    retained.manager.close("owner", "session")
    with pytest.raises(ts.TerminalError, match="terminal_session_closed"):
        retained.manager.resize("owner", "session", cols=80, rows=24)


def test_owner_cleanup_still_works_after_feature_disable(retained, monkeypatch):
    session, _ = retained.add()
    other, _ = retained.add(owner="other", ident="other")
    monkeypatch.setitem(flags._flags, "terminal_shell", False)
    assert retained.manager.close_all_for_owner("owner", "security_shutdown") == 1
    assert session.closed and not other.closed
    assert retained.manager.close("owner", "session")["reason"] == "security_shutdown"
    assert retained.manager.close("other", "other")["closed"]


def test_sweeper_expires_idle_and_lifetime_and_shutdown_closes_remainder(retained, monkeypatch):
    monkeypatch.setattr(ts, "IDLE_TIMEOUT_S", 10)
    monkeypatch.setattr(ts, "MAX_LIFETIME_S", 20)
    idle, _ = retained.add(ident="idle")
    old, _ = retained.add(ident="old")
    retained.clock.value = 1021
    old.last_activity_at = 1020
    assert retained.manager.sweep() == 2
    assert idle.closed and old.closed
    assert [a[0][1] for a in retained.audits] == ["terminal_session_expire"] * 2
    live, _ = retained.add(ident="remaining")
    retained.manager.shutdown()
    assert live.closed and retained.manager._sweeper_stop.is_set()


@pytest.mark.parametrize("failure", [OSError(), subprocess.TimeoutExpired("synthetic", 5)])
def test_close_handles_reaper_and_descriptor_errors(retained, failure):
    process = RetainedProcess()
    process.wait = Mock(side_effect=failure)
    session, _ = retained.add(process=process)
    os.close(session.master_fd)
    session.close("shutdown")
    session.close("ignored_second_reason")
    assert session.closed and session.close_reason == "shutdown"
    process.wait.assert_called_once_with(timeout=5)


def test_pipe_pump_retains_real_bytes_and_closes_on_eof(retained):
    session, writer = retained.add()
    os.write(writer, b"retained output")
    os.close(writer)
    session.start_pump()
    session.start_pump()
    session._pump.join(timeout=5)
    assert not session._pump.is_alive()
    assert session.closed
    assert session.read(0)[0] == b"retained output"


@pytest.mark.parametrize("failure", [OSError(errno.EAGAIN, "retry"), OSError(errno.EWOULDBLOCK, "retry")])
def test_pump_retries_nonblocking_read_then_stops_on_eof(retained, monkeypatch, failure):
    session, _ = retained.add()
    monkeypatch.setattr(ts.select, "select", lambda *_: ([session.master_fd], [], []))
    monkeypatch.setattr(ts.os, "read", Mock(side_effect=[failure, b""]))
    session._pump_loop()
    assert session.closed


@pytest.mark.parametrize("result", [[], None])
def test_pump_detects_process_exit_or_invalid_descriptor(retained, monkeypatch, result):
    process = RetainedProcess()
    process.done = True
    session, _ = retained.add(process=process)
    if result is None:
        monkeypatch.setattr(ts.select, "select", Mock(side_effect=ValueError()))
    else:
        monkeypatch.setattr(ts.select, "select", lambda *_: ([], [], []))
    session._pump_loop()
    if result == []:
        assert session.closed


def test_poll_and_sweep_failure_paths_are_bounded(retained, monkeypatch):
    session, _ = retained.add()
    sleep = Mock()
    monkeypatch.setattr(ts.time, "sleep", sleep)
    monkeypatch.setattr(ts.select, "select", Mock(side_effect=OSError()))
    retained.manager._wait_readable(session, 0)
    retained.manager._wait_readable(session, 0.01)
    sleep.assert_called_once_with(0.01)
    retained.manager._sweeper_stop = SimpleNamespace(wait=Mock(side_effect=[False, False, True]), set=Mock())
    retained.manager.sweep = Mock(side_effect=[RuntimeError("synthetic"), 0])
    retained.manager._sweep_loop()
    assert retained.manager.sweep.call_count == 2


def test_signal_helpers_and_ioctl_failures_do_not_escape(retained, monkeypatch):
    session, _ = retained.add()
    monkeypatch.setattr(ts.os, "getpgid", Mock(side_effect=ProcessLookupError()))
    ts._signal_group(session, signal.SIGINT)
    monkeypatch.setattr(ts.os, "getpgid", lambda _: 12345)
    for failure in [ProcessLookupError(), PermissionError(), OSError()]:
        monkeypatch.setattr(ts.os, "killpg", Mock(side_effect=failure))
        ts._signal_group(session, signal.SIGINT)
    seen = []
    monkeypatch.setattr(ts.fcntl, "ioctl", lambda fd, request, data: seen.append(struct.unpack("HHHH", data)))
    ts._apply_winsize(session.master_fd, (120, 40))
    assert seen == [(40, 120, 0, 0)]
    monkeypatch.setattr(ts.fcntl, "ioctl", Mock(side_effect=OSError()))
    ts._apply_winsize(session.master_fd, (120, 40))
    ts._set_nonblocking(session.master_fd)
    assert fcntl.fcntl(session.master_fd, fcntl.F_GETFL) & os.O_NONBLOCK
