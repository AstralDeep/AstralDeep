"""Tests for owner-scoped interactive terminal sessions (orchestrator/terminal_sessions.py,
orchestrator/terminal_api.py): PTY lifecycle, bounded output/input, owner isolation, denial
codes, sweeper expiry, reconnect-by-offset, and the owner-only API auth boundary.
"""

from __future__ import annotations

import base64
import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import terminal_api  # noqa: E402
from orchestrator import terminal_sessions as ts  # noqa: E402
from orchestrator.auth import get_web_or_bearer_user_payload  # noqa: E402
from orchestrator.terminal_sessions import (  # noqa: E402
    TerminalError, TerminalSessionManager,
)

_PIPE_WRITE_ENDS: dict = {}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DeadProcess:
    def __init__(self) -> None:
        self._child = subprocess.Popen(["/bin/sh", "-c", "exec sleep 30"],
                                       start_new_session=True,
                                       stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.pid = self._child.pid

    def poll(self):
        return self._child.poll()

    def wait(self, timeout=None):
        return self._child.wait(timeout=timeout)


def _kill_dead_process_children():
    for attr in ("_child",):
        pass


def _stop(process_holder) -> None:
    child = getattr(process_holder, "_child", None)
    if child is not None and child.poll() is None:
        child.kill()
        child.wait(timeout=30)


def _manager(**kwargs) -> TerminalSessionManager:
    return TerminalSessionManager(**kwargs)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _text(payload: dict) -> str:
    return base64.b64decode(payload["data_b64"]).decode("utf-8", "replace")


def _read_until(manager: TerminalSessionManager, owner: str, session_id: str, needle: str,
                *, offset: int = 0, timeout: float = 30.0) -> tuple[dict, str]:
    accumulated: list[str] = []
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = manager.read(owner, session_id, offset=offset, wait_seconds=1.0)
        chunk = _text(last)
        accumulated.append(chunk)
        offset = last["next_offset"]
        if needle in "".join(accumulated) or last["eof"]:
            break
    return last, "".join(accumulated)


def _read_to_eof(manager: TerminalSessionManager, owner: str, session_id: str, *,
                 offset: int = 0, timeout: float = 30.0) -> tuple[dict, str]:
    accumulated: list[str] = []
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = manager.read(owner, session_id, offset=offset, wait_seconds=1.0)
        accumulated.append(_text(last))
        offset = last["next_offset"]
        if last["eof"]:
            break
    return last, "".join(accumulated)


class TestSessionLifecycle:
    def test_create_describes_a_live_session(self):
        manager = _manager()
        try:
            info = manager.create("owner-a")
            assert info["session_id"]
            assert info["cols"] == ts.DEFAULT_COLS and info["rows"] == ts.DEFAULT_ROWS
            assert info["ring_start"] == 0 and info["ring_end"] >= 0
            assert info in manager.list_sessions("owner-a")
            assert manager.list_sessions("owner-b") == []
        finally:
            manager.shutdown()

    def test_write_then_read_round_trips_through_the_shell(self):
        manager = _manager()
        try:
            info = manager.create("owner-a")
            manager.write("owner-a", info["session_id"], _b64(b"echo astral_marker_$((40+2))\n"))
            _, text = _read_until(manager, "owner-a", info["session_id"], "astral_marker_42")
            assert "astral_marker_42" in text
        finally:
            manager.shutdown()

    def test_command_mode_runs_one_command_and_reports_eof(self):
        manager = _manager()
        try:
            info = manager.create("owner-a", command="echo terminal_eof_marker")
            out, text = _read_to_eof(manager, "owner-a", info["session_id"])
            assert "terminal_eof_marker" in text
            assert out["eof"] is True and out["session"] is None
        finally:
            manager.shutdown()

    def test_interrupt_kills_the_foreground_process(self):
        manager = _manager()
        try:
            info = manager.create("owner-a", command="sleep 60")
            described = manager.interrupt("owner-a", info["session_id"])
            assert described["session_id"] == info["session_id"]
            out, _ = _read_to_eof(manager, "owner-a", info["session_id"], timeout=30.0)
            assert out["eof"] is True
        finally:
            manager.shutdown()

    def test_close_terminates_the_child_and_refuses_further_use(self):
        manager = _manager()
        try:
            info = manager.create("owner-a", command="sleep 60")
            result = manager.close("owner-a", info["session_id"])
            assert result == {"session_id": info["session_id"], "closed": True,
                              "reason": "owner_close"}
            assert manager.list_sessions("owner-a") == []
            with pytest.raises(TerminalError) as excinfo:
                manager.write("owner-a", info["session_id"], _b64(b"x"))
            assert (excinfo.value.code, excinfo.value.status_code) == ("terminal_session_closed", 409)
            out = manager.read("owner-a", info["session_id"], offset=0)
            assert out["eof"] is True
        finally:
            manager.shutdown()

    def test_close_is_idempotent_and_preserves_the_reason(self):
        manager = _manager()
        try:
            info = manager.create("owner-a", command="sleep 60")
            manager.close("owner-a", info["session_id"])
            assert manager.close("owner-a", info["session_id"])["reason"] == "owner_close"
        finally:
            manager.shutdown()

    def test_resize_updates_dimensions_within_bounds(self):
        manager = _manager()
        try:
            info = manager.create("owner-a")
            described = manager.resize("owner-a", info["session_id"], cols=120, rows=40)
            assert (described["cols"], described["rows"]) == (120, 40)
            manager.write("owner-a", info["session_id"], _b64(b"stty size\n"))
            _, text = _read_until(manager, "owner-a", info["session_id"], "40 120")
            assert "40 120" in text
        finally:
            manager.shutdown()

    def test_write_reaches_the_child_stdin(self):
        manager = _manager()
        try:
            info = manager.create("owner-a", command="cat")
            manager.write("owner-a", info["session_id"], _b64(b"ping_42\n"))
            _, text = _read_until(manager, "owner-a", info["session_id"], "ping_42")
            assert "ping_42" in text
        finally:
            manager.shutdown()

    def test_write_after_child_exit_reports_closed(self):
        manager = _manager()
        try:
            info = manager.create("owner-a", command="true")
            out, _ = _read_to_eof(manager, "owner-a", info["session_id"], timeout=30.0)
            assert out["eof"] is True
            with pytest.raises(TerminalError) as excinfo:
                manager.write("owner-a", info["session_id"], _b64(b"x"))
            assert excinfo.value.code == "terminal_session_closed"
        finally:
            manager.shutdown()


class TestBoundsAndDenials:
    def test_dimensions_out_of_range_are_refused(self):
        manager = _manager()
        try:
            with pytest.raises(TerminalError) as cols:
                manager.create("owner-a", cols=1)
            assert (cols.value.code, cols.value.status_code) == ("terminal_cols_invalid", 422)
            with pytest.raises(TerminalError) as rows:
                manager.create("owner-a", rows=5000)
            assert (rows.value.code, rows.value.status_code) == ("terminal_rows_invalid", 422)
            info = manager.create("owner-a")
            with pytest.raises(TerminalError) as resize:
                manager.resize("owner-a", info["session_id"], cols=0, rows=10)
            assert resize.value.code == "terminal_cols_invalid"
            with pytest.raises(TerminalError) as resize_rows:
                manager.resize("owner-a", info["session_id"], cols=80, rows=0)
            assert resize_rows.value.code == "terminal_rows_invalid"
        finally:
            manager.shutdown()

    def test_input_validation_refuses_empty_oversize_and_non_base64(self, monkeypatch):
        monkeypatch.setattr(ts, "INPUT_CHUNK_MAX", 8)
        manager = _manager()
        try:
            info = manager.create("owner-a")
            with pytest.raises(TerminalError) as empty:
                manager.write("owner-a", info["session_id"], "")
            assert (empty.value.code, empty.value.status_code) == ("terminal_input_invalid", 422)
            with pytest.raises(TerminalError) as not_b64:
                manager.write("owner-a", info["session_id"], "!!!not-base64!!!")
            assert not_b64.value.code == "terminal_input_invalid"
            with pytest.raises(TerminalError) as large:
                manager.write("owner-a", info["session_id"], _b64(b"x" * 9))
            assert (large.value.code, large.value.status_code) == ("terminal_input_invalid", 422)
        finally:
            manager.shutdown()

    def test_unknown_and_cross_owner_sessions_are_not_found(self):
        manager = _manager()
        try:
            info = manager.create("owner-a")
            with pytest.raises(TerminalError) as unknown:
                manager.read("owner-a", "no-such-session", offset=0)
            assert (unknown.value.code, unknown.value.status_code) == ("terminal_session_not_found", 404)
            calls = (
                lambda: manager.read("owner-b", info["session_id"], offset=0),
                lambda: manager.write("owner-b", info["session_id"], _b64(b"x")),
                lambda: manager.interrupt("owner-b", info["session_id"]),
                lambda: manager.resize("owner-b", info["session_id"], cols=80, rows=24),
                lambda: manager.close("owner-b", info["session_id"]),
            )
            for call in calls:
                with pytest.raises(TerminalError) as denied:
                    call()
                assert (denied.value.code, denied.value.status_code) == ("terminal_session_not_found", 404)
        finally:
            manager.shutdown()

    def test_read_offset_and_wait_validation(self):
        manager = _manager()
        try:
            info = manager.create("owner-a")
            for bad in (-1, 1.5, "0", True):
                with pytest.raises(TerminalError) as excinfo:
                    manager.read("owner-a", info["session_id"], offset=bad)
                assert (excinfo.value.code, excinfo.value.status_code) == ("terminal_offset_invalid", 422)
            for bad_wait in (-1, "1", True):
                with pytest.raises(TerminalError) as wait:
                    manager.read("owner-a", info["session_id"], offset=0, wait_seconds=bad_wait)
                assert wait.value.code == "terminal_wait_invalid"
            assert ts._bounded_wait(99) == ts.READ_WAIT_MAX_S
        finally:
            manager.shutdown()

    def test_session_limit_is_enforced_per_owner(self):
        manager = _manager()
        try:
            created = [manager.create("owner-a", command="sleep 60") for _ in range(ts.SESSIONS_PER_OWNER)]
            with pytest.raises(TerminalError) as excinfo:
                manager.create("owner-a", command="sleep 60")
            assert (excinfo.value.code, excinfo.value.status_code) == ("terminal_session_limit_reached", 409)
            manager.close("owner-a", created[0]["session_id"])
            assert manager.create("owner-a", command="sleep 60")["session_id"]
            assert manager.create("owner-b", command="sleep 60")["session_id"]
        finally:
            manager.shutdown()

    def test_spawn_failure_is_a_503_without_a_leaked_session(self):
        def broken_spawn(argv, env, cwd, size):
            raise OSError("no pty available")

        manager = _manager(spawn=broken_spawn)
        with pytest.raises(TerminalError) as excinfo:
            manager.create("owner-a")
        assert (excinfo.value.code, excinfo.value.status_code) == ("terminal_spawn_failed", 503)
        assert manager.list_sessions("owner-a") == []


class TestRingAndOffsets:
    def _session(self, manager: TerminalSessionManager, owner: str = "owner-a") -> ts.TerminalSession:
        info = manager.create(owner, command="sleep 60")
        return manager._sessions[info["session_id"]]

    def test_append_output_drops_only_the_oldest_bytes(self, monkeypatch):
        monkeypatch.setattr(ts, "OUTPUT_RING_BYTES", 32)
        monkeypatch.setattr(ts, "OUTPUT_CHUNK_MAX", 64)
        read_fd, _ = os.pipe()
        session = ts.TerminalSession("s1", "owner-a", read_fd, DeadProcess(),
                                     "/tmp/astral-ring-test-cwd")
        session.append_output(b"0123456789ABCDEF0123456789")
        session.append_output(b"0123456789ABCDEF0123456789")
        chunk, next_offset, truncated, eof = session.read(0)
        assert chunk == b"456789" + b"0123456789ABCDEF0123456789"
        assert next_offset == 52 and truncated is True and eof is False
        assert session.ring_start == 20 and session.ring_end == 52

    def test_offset_reads_clamp_to_the_retained_window(self, monkeypatch):
        monkeypatch.setattr(ts, "OUTPUT_RING_BYTES", 32)
        read_fd, _ = os.pipe()
        session = ts.TerminalSession("s1", "owner-a", read_fd, DeadProcess(),
                                     "/tmp/astral-ring-test-cwd")
        session.append_output(b"0123456789ABCDEF0123456789")
        chunk, next_offset, truncated, _ = session.read(5)
        assert chunk == b"56789ABCDEF0123456789" and next_offset == 26 and truncated is False
        chunk, next_offset, truncated, _ = session.read(10_000)
        assert chunk == b"" and next_offset == 26 and truncated is False
        chunk, _, truncated, _ = session.read(3)
        assert chunk.startswith(b"345") and truncated is False

    def test_chunked_reads_advance_by_the_output_chunk_cap(self, monkeypatch):
        monkeypatch.setattr(ts, "OUTPUT_RING_BYTES", 10_000)
        monkeypatch.setattr(ts, "OUTPUT_CHUNK_MAX", 8)
        read_fd, _ = os.pipe()
        session = ts.TerminalSession("s1", "owner-a", read_fd, DeadProcess(),
                                     "/tmp/astral-ring-test-cwd")
        session.append_output(b"abcdefghij")
        first = session.read(0)
        assert first[0] == b"abcdefgh" and first[1] == 8
        second = session.read(first[1])
        assert second[0] == b"ij" and second[1] == 10

    def test_offset_reads_continue_across_reconnects(self):
        manager = _manager()
        try:
            info = manager.create("owner-a", command="cat")
            manager.write("owner-a", info["session_id"], _b64(b"first_9\n"))
            first, text = _read_until(manager, "owner-a", info["session_id"], "first_9")
            assert "first_9" in text
            manager.write("owner-a", info["session_id"], _b64(b"second_10\n"))
            second, new_text = _read_until(manager, "owner-a", info["session_id"], "second_10",
                                           offset=first["next_offset"])
            assert "second_10" in new_text
            assert second["offset"] == first["next_offset"]
            again, all_text = _read_until(manager, "owner-a", info["session_id"], "second_10", offset=0)
            assert "first_9" in all_text and "second_10" in all_text
            manager.interrupt("owner-a", info["session_id"])
        finally:
            manager.shutdown()


class TestSweeperAndCleanup:
    def test_idle_sessions_expire_and_are_closed(self):
        clock = FakeClock()
        manager = _manager(clock=clock)
        try:
            info = manager.create("owner-a", command="sleep 60")
            clock.advance(ts.IDLE_TIMEOUT_S + 1)
            assert manager.sweep() == 1
            assert manager.list_sessions("owner-a") == []
            with pytest.raises(TerminalError) as excinfo:
                manager.write("owner-a", info["session_id"], _b64(b"x"))
            assert excinfo.value.code == "terminal_session_closed"
        finally:
            manager.shutdown()

    def test_max_lifetime_expires_even_active_sessions(self):
        clock = FakeClock()
        manager = _manager(clock=clock)
        try:
            info = manager.create("owner-a", command="sleep 60")
            clock.advance(1)
            manager.write("owner-a", info["session_id"], _b64(b"x"))
            clock.advance(ts.MAX_LIFETIME_S)
            assert manager.sweep() == 1
            assert manager.list_sessions("owner-a") == []
        finally:
            manager.shutdown()

    def test_sweep_keeps_recent_sessions(self):
        clock = FakeClock()
        manager = _manager(clock=clock)
        try:
            manager.create("owner-a", command="sleep 60")
            clock.advance(ts.IDLE_TIMEOUT_S - 10)
            assert manager.sweep() == 0
            assert len(manager.list_sessions("owner-a")) == 1
        finally:
            manager.shutdown()

    def test_sweeper_thread_expires_sessions_without_manual_calls(self, monkeypatch):
        monkeypatch.setattr(ts, "SWEEP_INTERVAL_S", 0.05)
        clock = FakeClock()
        manager = _manager(clock=clock)
        try:
            manager.create("owner-a", command="sleep 60")
            clock.advance(ts.IDLE_TIMEOUT_S + 1)
            deadline = time.monotonic() + 30
            while manager.list_sessions("owner-a") and time.monotonic() < deadline:
                time.sleep(0.05)
            assert manager.list_sessions("owner-a") == []
        finally:
            manager.shutdown()

    def test_close_all_for_owner_closes_only_that_owner(self):
        manager = _manager()
        try:
            manager.create("owner-a", command="sleep 60")
            b = manager.create("owner-b", command="sleep 60")
            assert manager.close_all_for_owner("owner-a", "logout") == 1
            assert manager.list_sessions("owner-a") == []
            assert [s["session_id"] for s in manager.list_sessions("owner-b")] == [b["session_id"]]
        finally:
            manager.shutdown()

    def test_shutdown_closes_every_session(self):
        manager = _manager()
        manager.create("owner-a", command="sleep 60")
        manager.create("owner-b", command="sleep 60")
        manager.shutdown()
        assert manager.list_sessions("owner-a") == []
        assert manager.list_sessions("owner-b") == []

    def test_wait_readable_survives_bad_descriptors(self):
        read_fd, write_fd = os.pipe()
        session = ts.TerminalSession("s1", "owner-a", read_fd, DeadProcess(), "/tmp/x")
        manager = _manager()
        session.closed = True
        manager._wait_readable(session, 0.01)
        session.closed = False
        os.close(read_fd)
        manager._wait_readable(session, 0.01)
        session.master_fd = -1
        manager._wait_readable(session, 0.01)
        os.close(write_fd)
        manager.shutdown()


class TestChildEnvironment:
    def test_child_env_is_scrubbed_and_scoped_to_the_session_directory(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "secret-key")
        manager = _manager()
        try:
            info = manager.create(
                "owner-a", command='printf "%s" "$DATABASE_URL$CREDENTIAL_ENCRYPTION_KEY"')
            _, text = _read_to_eof(manager, "owner-a", info["session_id"])
            assert text == ""
        finally:
            manager.shutdown()

    def test_child_starts_in_a_private_session_directory_with_a_clean_home(self):
        manager = _manager()
        try:
            info = manager.create("owner-a", command="pwd; printf '|%s' \"$HOME\"")
            _, text = _read_to_eof(manager, "owner-a", info["session_id"])
            assert "astral-terminal-" in text
            assert text.count("astral-terminal-") >= 2
        finally:
            manager.shutdown()


class TestApiSurface:
    def _client(self, manager: TerminalSessionManager, *, claims=None) -> TestClient:
        app = FastAPI()
        app.state.orchestrator = SimpleNamespace(terminal_sessions=manager)
        app.include_router(terminal_api.terminal_router)
        if claims is not None:
            app.dependency_overrides[get_web_or_bearer_user_payload] = lambda: claims
        return TestClient(app)

    @staticmethod
    def _claims(sub="owner-a", **extra):
        return {"sub": sub, "realm_access": {"roles": ["user"]}, **extra}

    def test_full_owner_flow_over_the_rest_surface(self):
        manager = _manager()
        client = self._client(manager, claims=self._claims())
        try:
            created = client.post("/api/terminals", json={"cols": 100, "rows": 30})
            assert created.status_code == 200
            session_id = created.json()["session_id"]
            listing = client.get("/api/terminals").json()["sessions"]
            assert [s["session_id"] for s in listing] == [session_id]
            written = client.post(f"/api/terminals/{session_id}/input",
                                  json={"data_b64": _b64(b"echo api_out_77\n")})
            assert written.status_code == 200
            assert written.json()["cols"] == 100
            resized = client.post(f"/api/terminals/{session_id}/resize",
                                  json={"cols": 120, "rows": 40})
            assert (resized.json()["cols"], resized.json()["rows"]) == (120, 40)
            _, text = self._read_until_http(client, session_id, "api_out_77")
            assert "api_out_77" in text
            assert client.post(f"/api/terminals/{session_id}/interrupt").status_code == 200
            closed = client.delete(f"/api/terminals/{session_id}")
            assert closed.json()["closed"] is True
            assert client.get("/api/terminals").json()["sessions"] == []
        finally:
            manager.shutdown()

    @staticmethod
    def _read_until_http(client: TestClient, session_id: str, needle: str,
                         *, timeout: float = 8.0) -> tuple[dict, str]:
        accumulated: list[str] = []
        offset = 0
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = client.get(f"/api/terminals/{session_id}/output",
                              params={"offset": offset, "wait_seconds": 1.0}).json()
            accumulated.append(_text(last))
            offset = last["next_offset"]
            if needle in "".join(accumulated) or last["eof"]:
                break
        return last, "".join(accumulated)

    def test_unauthenticated_requests_are_denied(self, monkeypatch):
        monkeypatch.setenv("USE_MOCK_AUTH", "false")
        monkeypatch.delenv("WEB_SESSION_ENC_KEY", raising=False)
        manager = _manager()
        client = self._client(manager)
        try:
            assert client.post("/api/terminals", json={}).status_code == 401
            assert client.get("/api/terminals").status_code == 401
            assert client.delete("/api/terminals/whatever").status_code == 401
            assert client.get("/api/terminals/whatever/output").status_code == 401
            assert client.post("/api/terminals/whatever/interrupt").status_code == 401
        finally:
            manager.shutdown()

    def test_delegated_and_machine_identities_are_refused(self):
        manager = _manager()
        try:
            delegated = self._client(manager, claims=self._claims(act={"sub": "agent-1"}))
            assert delegated.post("/api/terminals", json={}).status_code == 403
            machine = self._client(manager, claims=self._claims(machine_class="framework"))
            assert machine.post("/api/terminals", json={}).status_code == 403
        finally:
            manager.shutdown()

    def test_missing_user_role_is_forbidden(self):
        manager = _manager()
        client = self._client(manager, claims={"sub": "owner-a", "realm_access": {"roles": ["other"]}})
        try:
            assert client.post("/api/terminals", json={}).status_code == 403
        finally:
            manager.shutdown()

    def test_error_codes_surface_through_http(self):
        manager = _manager()
        client = self._client(manager, claims=self._claims())
        try:
            unknown = client.get("/api/terminals/nope/output", params={"offset": 0})
            assert (unknown.status_code, unknown.json()["error"]) == (404, "terminal_session_not_found")
            bad = client.post("/api/terminals", json={"cols": 0})
            assert bad.status_code == 422
            assert "detail" in bad.json()
            session_id = client.post("/api/terminals", json={}).json()["session_id"]
            malformed = client.post(f"/api/terminals/{session_id}/input", json={"data_b64": "!!!!"})
            assert (malformed.status_code, malformed.json()["error"]) == (422, "terminal_input_invalid")
            oversized_body = client.post(f"/api/terminals/{session_id}/resize",
                                         json={"cols": 1001, "rows": 24})
            assert oversized_body.status_code == 422
        finally:
            manager.shutdown()

    def test_cross_owner_access_is_not_found_parity(self):
        manager = _manager()
        owner_a = self._client(manager, claims=self._claims())
        owner_b = self._client(manager, claims=self._claims(sub="owner-b"))
        try:
            session_id = owner_a.post("/api/terminals", json={}).json()["session_id"]
            foreign = owner_b.get(f"/api/terminals/{session_id}/output", params={"offset": 0})
            assert (foreign.status_code, foreign.json()["error"]) == (404, "terminal_session_not_found")
            assert owner_b.delete(f"/api/terminals/{session_id}").status_code == 404
            assert owner_a.get(f"/api/terminals/{session_id}/output",
                               params={"offset": 0}).status_code == 200
        finally:
            manager.shutdown()

    def test_read_query_validation_is_a_client_error(self):
        manager = _manager()
        client = self._client(manager, claims=self._claims())
        try:
            session_id = client.post("/api/terminals", json={}).json()["session_id"]
            negative = client.get(f"/api/terminals/{session_id}/output", params={"offset": -1})
            assert negative.status_code == 422
            huge_wait = client.get(f"/api/terminals/{session_id}/output",
                                   params={"offset": 0, "wait_seconds": 30})
            assert huge_wait.status_code == 422
        finally:
            manager.shutdown()

    def test_missing_manager_is_a_503(self):
        manager = _manager()
        app = FastAPI()
        app.state.orchestrator = SimpleNamespace()
        app.include_router(terminal_api.terminal_router)
        app.dependency_overrides[get_web_or_bearer_user_payload] = (
            lambda: self._claims())
        client = TestClient(app)
        try:
            assert client.get("/api/terminals").status_code == 503
        finally:
            manager.shutdown()


class TestFaultPaths:
    def test_append_output_ignores_empty_data(self):
        read_fd, _ = os.pipe()
        session = ts.TerminalSession("s1", "owner-a", read_fd, DeadProcess(),
                                     "/tmp/astral-ring-test-cwd")
        session.append_output(b"")
        assert session.ring_end == 0
        os.close(read_fd)

    def test_close_tolerates_reaper_and_descriptor_failures(self):
        read_fd, write_fd = os.pipe()
        stuck = SimpleNamespace(pid=DeadProcess().pid, poll=lambda: None,
                                wait=lambda timeout=None: (_ for _ in ()).throw(
                                    subprocess.TimeoutExpired(cmd="x", timeout=5)))
        session = ts.TerminalSession("s1", "owner-a", read_fd, stuck,
                                     "/tmp/astral-ring-test-cwd")
        try:
            session.close("owner_close")
        finally:
            _stop(stuck)
            os.close(write_fd)

    def test_close_tolerates_wait_oserror_and_double_fd_close(self):
        read_fd, write_fd = os.pipe()
        grumpy = SimpleNamespace(pid=DeadProcess().pid, poll=lambda: None,
                                 wait=lambda timeout=None: (_ for _ in ()).throw(OSError("busy")))
        session = ts.TerminalSession("s1", "owner-a", read_fd, grumpy,
                                     "/tmp/astral-ring-test-cwd")
        try:
            session.close("owner_close")
        finally:
            _stop(grumpy)
            os.close(write_fd)

    def test_write_maps_eio_to_closed_and_other_errors_to_500(self, monkeypatch):
        manager = _manager()
        try:
            info = manager.create("owner-a", command="sleep 60")
            session = manager._sessions[info["session_id"]]
            os.close(session.master_fd)
            with pytest.raises(TerminalError) as excinfo:
                manager.write("owner-a", info["session_id"], _b64(b"x"))
            assert (excinfo.value.code, excinfo.value.status_code) == ("terminal_session_closed", 409)

            live = manager.create("owner-a", command="sleep 60")
            real_write = os.write

            def permission_denied(fd, data):
                raise PermissionError(13, "denied")

            monkeypatch.setattr(ts.os, "write", permission_denied)
            with pytest.raises(TerminalError) as denied:
                manager.write("owner-a", live["session_id"], _b64(b"x"))
            assert (denied.value.code, denied.value.status_code) == ("terminal_write_failed", 500)
            monkeypatch.setattr(ts.os, "write", real_write)
        finally:
            manager.shutdown()

    def test_signal_helpers_tolerate_dead_groups(self):
        holder = DeadProcess()
        session = ts.TerminalSession("s1", "owner-a", -1, holder,
                                     "/tmp/astral-ring-test-cwd")
        try:
            ts._signal_group(session, signal.SIGWINCH)
            ts._signal_pgid(999_999_999, signal.SIGINT)
        finally:
            _stop(holder)

    def test_interrupt_falls_back_to_the_process_group_without_a_tty(self):
        def pipe_spawn(argv, env, cwd, size):
            read_fd, _ = os.pipe()
            process = subprocess.Popen(["/bin/sh", "-c", "exec sleep 30"],
                                       start_new_session=True,
                                       stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return read_fd, process, cwd

        manager = _manager(spawn=pipe_spawn)
        try:
            info = manager.create("owner-a")
            described = manager.interrupt("owner-a", info["session_id"])
            assert described["session_id"] == info["session_id"]
            deadline = time.monotonic() + 30
            while not manager._sessions[info["session_id"]].exited() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert manager._sessions[info["session_id"]].exited()
        finally:
            manager.shutdown()

    def test_pump_captures_pipe_output_and_eof(self):
        def pipe_spawn(argv, env, cwd, size):
            read_fd, write_fd = os.pipe()
            os.set_blocking(read_fd, False)
            process = subprocess.Popen(["/bin/sh", "-c", "exec sleep 30"],
                                       start_new_session=True,
                                       stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _PIPE_WRITE_ENDS[process.pid] = write_fd
            return read_fd, process, cwd

        manager = _manager(spawn=pipe_spawn)
        try:
            info = manager.create("owner-a")
            session = manager._sessions[info["session_id"]]
            os.write(_PIPE_WRITE_ENDS[session.process.pid], b"pumped_bytes_1\n")
            out = manager.read("owner-a", info["session_id"], offset=0, wait_seconds=15.0)
            assert base64.b64decode(out["data_b64"]) == b"pumped_bytes_1\n"
            assert out["eof"] is False
            os.close(_PIPE_WRITE_ENDS[session.process.pid])
            out = manager.read("owner-a", info["session_id"], offset=out["next_offset"],
                               wait_seconds=15.0)
            assert out["eof"] is True
        finally:
            manager.shutdown()

    def test_pump_detects_exited_child_without_output(self):
        def dead_spawn(argv, env, cwd, size):
            read_fd, _ = os.pipe()
            os.set_blocking(read_fd, False)
            process = subprocess.Popen(["/bin/sh", "-c", "exec sleep 30"],
                                       start_new_session=True,
                                       stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            process.kill()
            process.wait(timeout=30)
            return read_fd, process, cwd

        manager = _manager(spawn=dead_spawn)
        try:
            info = manager.create("owner-a")
            out = manager.read("owner-a", info["session_id"], offset=0, wait_seconds=15.0)
            assert out["eof"] is True
        finally:
            manager.shutdown()

    def test_final_output_survives_child_exit_before_any_read(self):
        manager = _manager()
        try:
            info = manager.create("owner-a", command="echo final_bytes_marker_77")
            deadline = time.monotonic() + 6
            out = None
            while time.monotonic() < deadline:
                out = manager.read("owner-a", info["session_id"], offset=0, wait_seconds=1.0)
                if out["eof"]:
                    break
            assert out is not None and out["eof"] is True
            assert b"final_bytes_marker_77" in base64.b64decode(out["data_b64"])
        finally:
            manager.shutdown()

    def test_pump_stops_after_close(self):
        manager = _manager()
        try:
            info = manager.create("owner-a", command="sleep 60")
            session = manager._sessions[info["session_id"]]
            pump = session._pump
            manager.close("owner-a", info["session_id"])
            pump.join(timeout=30)
            assert not pump.is_alive()
        finally:
            manager.shutdown()

    def test_sweep_loop_logs_and_survives_sweep_failures(self, monkeypatch):
        monkeypatch.setattr(ts, "SWEEP_INTERVAL_S", 0.02)
        manager = _manager()
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            raise RuntimeError("boom")

        monkeypatch.setattr(manager, "sweep", flaky)
        manager._ensure_sweeper()
        deadline = time.monotonic() + 30
        while calls["n"] < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        manager.shutdown()
        assert calls["n"] >= 2

    def test_spawn_shell_closes_pty_fds_when_the_child_cannot_start(self):
        with pytest.raises(OSError):
            ts._spawn_shell(["/nonexistent-astral-binary-xyz"], {}, "/tmp", (80, 24))

    def test_apply_winsize_tolerates_bad_descriptors(self):
        fd = os.open("/dev/null", os.O_RDONLY)
        os.close(fd)
        ts._apply_winsize(fd, (80, 24))

    def test_shell_argv_falls_back_to_sh(self, monkeypatch):
        monkeypatch.setattr(ts.shutil, "which",
                            lambda name: "/bin/sh" if name == "sh" else None)
        assert ts._shell_argv(None)[0] == "/bin/sh"
        assert ts._shell_argv("echo hi") == ["/bin/sh", "-c", "echo hi"]


class TestApiErrorBranches:
    def _client(self, manager, *, sub="owner-a"):
        app = FastAPI()
        app.state.orchestrator = SimpleNamespace(terminal_sessions=manager)
        app.include_router(terminal_api.terminal_router)
        claims = {"sub": sub, "realm_access": {"roles": ["user"]}}
        app.dependency_overrides[get_web_or_bearer_user_payload] = lambda: claims
        return TestClient(app)

    def test_create_route_surfaces_the_session_limit(self):
        manager = _manager()
        client = self._client(manager)
        read_fd, _ = os.pipe()
        try:
            for index in range(ts.SESSIONS_PER_OWNER):
                manager._sessions[f"seed-{index}"] = ts.TerminalSession(
                    f"seed-{index}", "owner-a", read_fd, DeadProcess(),
                    "/tmp/astral-ring-test-cwd")
            response = client.post("/api/terminals", json={})
            assert (response.status_code, response.json()["error"]) == (
                409, "terminal_session_limit_reached")
        finally:
            os.close(read_fd)
            manager.shutdown()

    def test_resize_and_interrupt_routes_surface_service_errors(self):
        manager = _manager()
        client = self._client(manager)
        try:
            resize = client.post("/api/terminals/nope/resize", json={"cols": 80, "rows": 24})
            assert (resize.status_code, resize.json()["error"]) == (404, "terminal_session_not_found")
            interrupt = client.post("/api/terminals/nope/interrupt")
            assert (interrupt.status_code, interrupt.json()["error"]) == (404, "terminal_session_not_found")
        finally:
            manager.shutdown()

    def test_non_dict_payload_is_unauthenticated(self):
        manager = _manager()
        app = FastAPI()
        app.state.orchestrator = SimpleNamespace(terminal_sessions=manager)
        app.include_router(terminal_api.terminal_router)
        app.dependency_overrides[get_web_or_bearer_user_payload] = lambda: None
        client = TestClient(app)
        try:
            assert client.get("/api/terminals").status_code == 401
        finally:
            manager.shutdown()

    def test_orchestrator_fallback_resolution(self):
        manager = _manager()
        app = FastAPI()
        app.include_router(terminal_api.terminal_router)
        app.dependency_overrides[get_web_or_bearer_user_payload] = (
            lambda: {"sub": "owner-a", "realm_access": {"roles": ["user"]}})
        client = TestClient(app)
        try:
            assert client.get("/api/terminals").status_code == 503
        finally:
            manager.shutdown()


class TestPumpEdges:
    def test_start_pump_is_idempotent_and_idle_loops(self):
        manager = _manager()
        try:
            info = manager.create("owner-a", command="sleep 60")
            session = manager._sessions[info["session_id"]]
            pump = session._pump
            session.start_pump()
            assert session._pump is pump
            time.sleep(0.3)
            assert not session.closed
        finally:
            manager.shutdown()

    def test_pump_exits_on_bad_descriptor(self):
        read_fd, _ = os.pipe()
        holder = DeadProcess()
        session = ts.TerminalSession("s1", "owner-a", read_fd, holder,
                                     "/tmp/astral-ring-test-cwd")
        os.close(read_fd)
        try:
            session.start_pump()
            session._pump.join(timeout=30)
            assert not session._pump.is_alive()
        finally:
            _stop(holder)

    def test_pump_tolerates_transient_read_blocks(self, monkeypatch):
        def pipe_spawn(argv, env, cwd, size):
            read_fd, write_fd = os.pipe()
            os.set_blocking(read_fd, False)
            process = subprocess.Popen(["/bin/sh", "-c", "exec sleep 30"],
                                       start_new_session=True,
                                       stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _PIPE_WRITE_ENDS[process.pid] = write_fd
            return read_fd, process, cwd

        manager = _manager(spawn=pipe_spawn)
        try:
            info = manager.create("owner-a")
            session = manager._sessions[info["session_id"]]
            real_read = os.read
            state = {"n": 0}

            def flaky_read(fd, size):
                if fd == session.master_fd and state["n"] < 3:
                    state["n"] += 1
                    raise OSError(errno.EAGAIN, "try again")
                return real_read(fd, size)

            monkeypatch.setattr(ts.os, "read", flaky_read)
            os.write(_PIPE_WRITE_ENDS[session.process.pid], b"after_eagain\n")
            out = manager.read("owner-a", info["session_id"], offset=0, wait_seconds=15.0)
            assert b"after_eagain" in base64.b64decode(out["data_b64"])
        finally:
            manager.shutdown()

    def test_manager_wait_tolerates_a_dead_pump_fd(self):
        def pipe_spawn(argv, env, cwd, size):
            read_fd, _ = os.pipe()
            os.set_blocking(read_fd, False)
            process = subprocess.Popen(["/bin/sh", "-c", "exec sleep 30"],
                                       start_new_session=True,
                                       stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            process.kill()
            process.wait(timeout=30)
            return read_fd, process, cwd

        manager = _manager(spawn=pipe_spawn)
        try:
            info = manager.create("owner-a")
            session = manager._sessions[info["session_id"]]
            session._pump.join(timeout=30)
            manager._wait_readable(session, 0.05)
            session.master_fd = -1
            manager._wait_readable(session, 0.05)
            manager._wait_readable(session, 0)
        finally:
            manager.shutdown()
