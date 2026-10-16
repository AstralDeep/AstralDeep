"""Owner-scoped interactive terminal sessions: each session is one sandboxed PTY child
process inside the backend runtime, created and operated only by its authenticated owner
through orchestrator/api.py's terminal router. Output is retained in a bounded ring so a
CLI or panel can reconnect by byte offset; sessions expire through an idle/max-lifetime
sweeper and every state transition is audited.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import logging
import os
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from orchestrator.remote_machines import audit_machine_event
from orchestrator.sandbox import build_limits, make_preexec, sandbox_env

logger = logging.getLogger("TerminalSessions")

INPUT_CHUNK_MAX = int(os.getenv("TERMINAL_INPUT_MAX_BYTES", str(16 * 1024)))
OUTPUT_RING_BYTES = int(os.getenv("TERMINAL_RING_BYTES", str(128 * 1024)))
OUTPUT_CHUNK_MAX = int(os.getenv("TERMINAL_OUTPUT_CHUNK_MAX", str(64 * 1024)))
SESSIONS_PER_OWNER = int(os.getenv("TERMINAL_SESSIONS_PER_OWNER", "4"))
IDLE_TIMEOUT_S = float(os.getenv("TERMINAL_IDLE_TIMEOUT_S", str(30 * 60)))
MAX_LIFETIME_S = float(os.getenv("TERMINAL_MAX_LIFETIME_S", str(8 * 3600)))
SWEEP_INTERVAL_S = float(os.getenv("TERMINAL_SWEEP_INTERVAL_S", "30"))
READ_WAIT_MAX_S = 25.0
COLS_RANGE = (2, 1000)
ROWS_RANGE = (2, 300)
DEFAULT_COLS, DEFAULT_ROWS = 80, 24

_READ_CHUNK = 4096
_POLL_SLICE_S = 0.2


class TerminalError(Exception):
    def __init__(self, code: str, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class TerminalSession:
    def __init__(self, session_id: str, owner_id: str, master_fd: int,
                 process: subprocess.Popen, cwd: str, *,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.session_id = session_id
        self.owner_id = owner_id
        self.master_fd = master_fd
        self.process = process
        self.cwd = cwd
        self.cols, self.rows = DEFAULT_COLS, DEFAULT_ROWS
        self.created_at = now()
        self.last_activity_at = self.created_at
        self.closed = False
        self.close_reason: Optional[str] = None
        self._ring = bytearray()
        self._ring_offset = 0
        self._ring_lock = threading.Lock()
        self._now = now
        self._pump: Optional[threading.Thread] = None

    @property
    def ring_start(self) -> int:
        with self._ring_lock:
            return self._ring_offset

    @property
    def ring_end(self) -> int:
        with self._ring_lock:
            return self._ring_offset + len(self._ring)

    def exited(self) -> bool:
        return self.process.poll() is not None

    def append_output(self, data: bytes) -> None:
        if not data:
            return
        with self._ring_lock:
            self._ring.extend(data)
            overflow = len(self._ring) - OUTPUT_RING_BYTES
            if overflow > 0:
                del self._ring[:overflow]
                self._ring_offset += overflow
        self.last_activity_at = self._now()

    def read(self, offset: int) -> Tuple[bytes, int, bool, bool]:
        with self._ring_lock:
            ring_start = self._ring_offset
            ring = bytes(self._ring)
        truncated = offset < ring_start
        if offset < ring_start:
            offset = ring_start
        ring_end = ring_start + len(ring)
        if offset > ring_end:
            offset = ring_end
        start = offset - ring_start
        chunk = ring[start:start + OUTPUT_CHUNK_MAX]
        return chunk, offset + len(chunk), truncated, self.closed

    def start_pump(self) -> None:
        if self._pump is not None:
            return
        self._pump = threading.Thread(target=self._pump_loop,
                                      name=f"terminal-pump-{self.session_id}", daemon=True)
        self._pump.start()

    def _pump_loop(self) -> None:
        while not self.closed:
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
            except (OSError, ValueError):
                break
            if not ready:
                if self.exited():
                    self.close("process_exited")
                    break
                continue
            try:
                data = os.read(self.master_fd, _READ_CHUNK)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                self.close("process_exited")
                break
            if not data:
                self.close("process_exited")
                break
            self.append_output(data)

    def touch(self) -> None:
        self.last_activity_at = self._now()

    def close(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        _signal_group(self, signal.SIGKILL)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("terminal_session.reap_timeout session=%s pid=%s",
                           self.session_id, self.process.pid)
        except OSError:
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        shutil.rmtree(self.cwd, ignore_errors=True)


class TerminalSessionManager:
    def __init__(self, *, clock: Optional[Callable[[], float]] = None,
                 spawn: Optional[Callable[[List[str], Dict[str, str], str, Tuple[int, int]], Tuple[int, Any, str]]] = None) -> None:
        self._sessions: Dict[str, TerminalSession] = {}
        self._creating: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._clock = clock or time.monotonic
        self._spawn = spawn or _spawn_shell
        self._sweeper: Optional[threading.Thread] = None
        self._sweeper_stop = threading.Event()

    def list_sessions(self, owner_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            live = [s for s in self._sessions.values() if s.owner_id == owner_id and not s.closed]
            return [self.describe(s) for s in sorted(live, key=lambda s: s.created_at)]

    def describe(self, session: TerminalSession) -> Dict[str, Any]:
        return {
            "session_id": session.session_id,
            "cols": session.cols,
            "rows": session.rows,
            "created_at_monotonic": round(session.created_at, 3),
            "idle_seconds": round(max(0.0, self._clock() - session.last_activity_at), 3),
            "ring_start": session.ring_start,
            "ring_end": session.ring_end,
        }

    def create(self, owner_id: str, *, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS,
               command: Optional[str] = None) -> Dict[str, Any]:
        cols = _bounded_dimension(cols, COLS_RANGE, "terminal_cols_invalid")
        rows = _bounded_dimension(rows, ROWS_RANGE, "terminal_rows_invalid")
        with self._lock:
            live = [s for s in self._sessions.values() if s.owner_id == owner_id and not s.closed]
            if len(live) + self._creating.get(owner_id, 0) >= SESSIONS_PER_OWNER:
                raise TerminalError("terminal_session_limit_reached", 409)
            self._creating[owner_id] = self._creating.get(owner_id, 0) + 1
        session = None
        cwd = None
        try:
            cwd = _child_cwd()
            try:
                master_fd, process, cwd = self._spawn(_shell_argv(command), _child_env(cwd), cwd, (cols, rows))
            except OSError as exc:
                logger.warning("terminal_session.spawn_failed owner=%s error=%s", owner_id, exc)
                raise TerminalError("terminal_spawn_failed", 503) from None
            session = TerminalSession(str(uuid.uuid4()), owner_id, master_fd,
                                      process, cwd, now=self._clock)
            session.cols, session.rows = cols, rows
        finally:
            with self._lock:
                remaining = self._creating[owner_id] - 1
                if remaining:
                    self._creating[owner_id] = remaining
                else:
                    self._creating.pop(owner_id)
                if session is not None:
                    self._sessions[session.session_id] = session
            if session is None and cwd is not None:
                shutil.rmtree(cwd, ignore_errors=True)
        session.start_pump()
        self._ensure_sweeper()
        audit_machine_event(owner_id, "terminal_session_create",
                           "interactive terminal session opened", machine_id=session.session_id)
        return self.describe(session)

    def _get_owned(self, owner_id: str, session_id: str) -> TerminalSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or session.owner_id != owner_id:
            raise TerminalError("terminal_session_not_found", 404)
        return session

    def _get_live(self, owner_id: str, session_id: str) -> TerminalSession:
        session = self._get_owned(owner_id, session_id)
        if session.closed:
            raise TerminalError("terminal_session_closed", 409)
        return session

    def write(self, owner_id: str, session_id: str, data_b64: str) -> Dict[str, Any]:
        session = self._get_live(owner_id, session_id)
        try:
            data = base64.b64decode(data_b64 or "", validate=True)
        except (ValueError, TypeError):
            raise TerminalError("terminal_input_invalid", 422) from None
        if not data or len(data) > INPUT_CHUNK_MAX:
            raise TerminalError("terminal_input_invalid", 422)
        try:
            os.write(session.master_fd, data)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                session.close("process_exited")
                raise TerminalError("terminal_session_closed", 409) from None
            logger.warning("terminal_session.write_failed session=%s error=%s", session_id, exc)
            raise TerminalError("terminal_write_failed", 500) from None
        session.touch()
        return self.describe(session)

    def resize(self, owner_id: str, session_id: str, *, cols: int, rows: int) -> Dict[str, Any]:
        session = self._get_live(owner_id, session_id)
        cols = _bounded_dimension(cols, COLS_RANGE, "terminal_cols_invalid")
        rows = _bounded_dimension(rows, ROWS_RANGE, "terminal_rows_invalid")
        _apply_winsize(session.master_fd, (cols, rows))
        _signal_group(session, signal.SIGWINCH)
        session.cols, session.rows = cols, rows
        session.touch()
        return self.describe(session)

    def interrupt(self, owner_id: str, session_id: str) -> Dict[str, Any]:
        session = self._get_live(owner_id, session_id)
        try:
            foreground = os.tcgetpgrp(session.master_fd)
        except OSError:
            foreground = None
        if foreground and foreground > 1:
            _signal_pgid(foreground, signal.SIGINT)
        else:
            _signal_group(session, signal.SIGINT)
        session.touch()
        audit_machine_event(owner_id, "terminal_session_interrupt",
                           "interactive terminal session interrupt", machine_id=session_id)
        return self.describe(session)

    def read(self, owner_id: str, session_id: str, *, offset: int,
             wait_seconds: float = 0.0) -> Dict[str, Any]:
        session = self._get_owned(owner_id, session_id)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise TerminalError("terminal_offset_invalid", 422)
        wait_seconds = _bounded_wait(wait_seconds)
        deadline = self._clock() + wait_seconds
        while True:
            chunk, next_offset, truncated, eof = session.read(offset)
            if chunk or eof or wait_seconds <= 0 or self._clock() >= deadline:
                return {
                    "session_id": session_id,
                    "offset": offset,
                    "next_offset": next_offset,
                    "data_b64": base64.b64encode(chunk).decode("ascii"),
                    "bytes": len(chunk),
                    "truncated": truncated,
                    "eof": eof,
                    "session": None if eof else self.describe(session),
                }
            self._wait_readable(session, min(_POLL_SLICE_S, deadline - self._clock()))

    def close(self, owner_id: str, session_id: str) -> Dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or session.owner_id != owner_id:
            raise TerminalError("terminal_session_not_found", 404)
        if not session.closed:
            session.close("owner_close")
            audit_machine_event(owner_id, "terminal_session_close",
                               "interactive terminal session closed", machine_id=session_id)
        return {"session_id": session_id, "closed": True, "reason": session.close_reason}

    def close_all_for_owner(self, owner_id: str, reason: str) -> int:
        with self._lock:
            targets = [s for s in self._sessions.values()
                       if s.owner_id == owner_id and not s.closed]
        for session in targets:
            session.close(reason)
        return len(targets)

    def sweep(self) -> int:
        now = self._clock()
        with self._lock:
            live = [s for s in self._sessions.values() if not s.closed]
        expired = [s for s in live
                   if now - s.last_activity_at >= IDLE_TIMEOUT_S
                   or now - s.created_at >= MAX_LIFETIME_S]
        for session in expired:
            session.close("expired")
            audit_machine_event(session.owner_id, "terminal_session_expire",
                               "interactive terminal session expired", machine_id=session.session_id)
        return len(expired)

    def shutdown(self) -> None:
        self._sweeper_stop.set()
        for session in list(self._sessions.values()):
            session.close("shutdown")

    def _wait_readable(self, session: TerminalSession, timeout: float) -> None:
        if timeout <= 0 or session.closed:
            return
        try:
            select.select([session.master_fd], [], [], timeout)
        except (OSError, ValueError):
            time.sleep(min(timeout, _POLL_SLICE_S))

    def _ensure_sweeper(self) -> None:
        if self._sweeper is not None and self._sweeper.is_alive():
            return
        self._sweeper_stop.clear()
        self._sweeper = threading.Thread(target=self._sweep_loop, name="terminal-sweeper",
                                         daemon=True)
        self._sweeper.start()

    def _sweep_loop(self) -> None:
        while not self._sweeper_stop.wait(SWEEP_INTERVAL_S):
            try:
                self.sweep()
            except Exception:  # noqa: BLE001
                logger.warning("terminal_session.sweep_failed", exc_info=True)


def _signal_group(session: TerminalSession, sig: signal.Signals) -> None:
    try:
        _signal_pgid(os.getpgid(session.process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _signal_pgid(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _bounded_dimension(value: Any, bounds: Tuple[int, int], code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not bounds[0] <= value <= bounds[1]:
        raise TerminalError(code, 422)
    return value


def _bounded_wait(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise TerminalError("terminal_wait_invalid", 422)
    return min(float(value), READ_WAIT_MAX_S)


def _child_cwd() -> str:
    return tempfile.mkdtemp(prefix="astral-terminal-")


def _child_env(cwd: str) -> Dict[str, str]:
    env = sandbox_env({"PATH": os.getenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")}, cwd)
    env["TERM"] = "xterm-256color"
    env["LANG"] = "C.UTF-8"
    env["HOME"] = cwd
    return env


def _shell_argv(command: Optional[str]) -> List[str]:
    shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    if command:
        return [shell, "-c", command]
    return [shell, "-i"]


def _spawn_shell(argv: List[str], env: Dict[str, str], cwd: str,
                 size: Tuple[int, int]) -> Tuple[int, Any, str]:
    master_fd, slave_fd = os.openpty()
    _apply_winsize(master_fd, size)
    preexec = make_preexec(build_limits())
    try:
        process = subprocess.Popen(
            argv, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            start_new_session=True, close_fds=True, env=env, cwd=cwd,
            preexec_fn=preexec)
    except OSError:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    os.close(slave_fd)
    _set_nonblocking(master_fd)
    return master_fd, process, cwd


def _apply_winsize(fd: int, size: Tuple[int, int]) -> None:
    try:
        winsize = struct.pack("HHHH", size[1], size[0], 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


__all__ = [
    "TerminalError", "TerminalSession", "TerminalSessionManager",
    "INPUT_CHUNK_MAX", "OUTPUT_RING_BYTES", "OUTPUT_CHUNK_MAX",
    "SESSIONS_PER_OWNER", "IDLE_TIMEOUT_S", "MAX_LIFETIME_S", "READ_WAIT_MAX_S",
    "COLS_RANGE", "ROWS_RANGE", "DEFAULT_COLS", "DEFAULT_ROWS",
]
