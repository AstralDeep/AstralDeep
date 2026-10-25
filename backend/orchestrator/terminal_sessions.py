"""Retains bounded owner-scoped terminal output and cleanup for existing sessions.
New execution uses only explicitly configured isolated SSH runtime profiles.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import fcntl
import logging
import math
import os
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from orchestrator.remote_machines import audit_machine_event
from orchestrator.terminal_security import (
    TerminalAuthority, TerminalError, require_terminal_enabled, require_terminal_execution,
)

logger = logging.getLogger("TerminalSessions")

INPUT_CHUNK_MAX = int(os.getenv("TERMINAL_INPUT_MAX_BYTES", str(16 * 1024)))
OUTPUT_RING_BYTES = int(os.getenv("TERMINAL_RING_BYTES", str(128 * 1024)))
OUTPUT_CHUNK_MAX = int(os.getenv("TERMINAL_OUTPUT_CHUNK_MAX", str(64 * 1024)))
SESSIONS_PER_OWNER = int(os.getenv("TERMINAL_SESSIONS_PER_OWNER", "4"))
IDLE_TIMEOUT_S = float(os.getenv("TERMINAL_IDLE_TIMEOUT_S", str(30 * 60)))
MAX_LIFETIME_S = float(os.getenv("TERMINAL_MAX_LIFETIME_S", str(8 * 3600)))
SWEEP_INTERVAL_S = float(os.getenv("TERMINAL_SWEEP_INTERVAL_S", "30"))
READ_WAIT_MAX_S = 25.0
RETAINED_SESSION_LIMIT = 256
COLS_RANGE = (2, 1000)
ROWS_RANGE = (2, 300)
DEFAULT_COLS, DEFAULT_ROWS = 80, 24
TARGET_MAX_BYTES = 128
DIRECTORY_MAX_BYTES = 4096

_READ_CHUNK = 4096
_POLL_SLICE_S = 0.2



class TerminalSession:
    def __init__(self, session_id: str, owner_id: str, master_fd: int,
                 process: subprocess.Popen, cwd: str, *,
                 now: Callable[[], float] = time.monotonic, remote=None, binding=None, authority=None,
                 target: Optional[str] = None, directory: Optional[str] = None) -> None:
        self.session_id = session_id
        self.owner_id = owner_id
        self.master_fd = master_fd
        self.process = process
        self.cwd = cwd
        self.remote = remote
        self.binding = binding
        self.authority = authority
        self.target = target
        self.directory = directory
        self.exit_code: Optional[int] = None
        self.effect_lock = threading.RLock()
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
        if self.remote is not None:
            while not self.closed:
                try:
                    data = self.remote.receive(_READ_CHUNK)
                except Exception:
                    self.close("transport_lost")
                    return
                if data is None:
                    continue
                if not data:
                    self.capture_exit()
                    self.close("process_exited")
                    return
                self.append_output(data)
            return
        while not self.closed:
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
            except (OSError, ValueError):
                break
            if not ready:
                if self.exited():
                    self.capture_exit()
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
                self.capture_exit()
                self.close("process_exited")
                break
            self.append_output(data)

    def touch(self) -> None:
        self.last_activity_at = self._now()

    def capture_exit(self) -> None:
        if self.exit_code is not None:
            return
        try:
            value = self.remote.exit_status() if self.remote is not None else self.process.poll()
        except Exception:
            value = None
        if type(value) is int:
            self.exit_code = value

    def close(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        if self.remote is not None:
            with self.effect_lock:
                self.capture_exit()
                try:
                    self.remote.close()
                except Exception:
                    logger.warning("terminal_session.close_failed session=%s", self.session_id)
            return
        _signal_group(self, signal.SIGKILL)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("terminal_session.reap_timeout session=%s pid=%s",
                           self.session_id, self.process.pid)
        except OSError:
            pass
        self.capture_exit()
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        shutil.rmtree(self.cwd, ignore_errors=True)


class TerminalSessionManager:
    def __init__(self, *, clock: Optional[Callable[[], float]] = None, orchestrator=None) -> None:
        self._sessions: Dict[str, TerminalSession] = {}
        self._orchestrator = orchestrator
        self._pending: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._clock = clock or time.monotonic
        self._sweeper: Optional[threading.Thread] = None
        self._sweeper_stop = threading.Event()

    def require_execution(self, owner_id: str):
        from orchestrator.terminal_ssh import resolve

        require_terminal_enabled()
        if self._orchestrator is None:
            raise TerminalError("terminal_isolation_unavailable", 503)
        return resolve(self._orchestrator, owner_id)

    def runtime_binding(self, owner_id: str, *, session_id: str | None = None) -> str:
        binding, _ = self.require_execution(owner_id)
        if session_id is not None:
            session = self._get_live(owner_id, session_id)
            if session.remote is None or session.binding != binding:
                session.close("runtime_changed")
                raise TerminalError("terminal_target_changed", 409)
        return self._binding_digest(binding)

    def runtime_preview(self, owner_id: str, *, runtime_binding: str, session_id: str | None = None) -> str:
        binding = self._approved_binding(owner_id, runtime_binding=runtime_binding, session_id=session_id)
        address = json.dumps(binding.address, ensure_ascii=True)[1:-1]
        return (f"{address}:{binding.port} as {binding.username}; machine {binding.machine_id}; "
                f"host key {binding.host_key_fingerprint}")

    def runtime_machine_id(self, owner_id: str, *, runtime_binding: str, session_id: str | None = None) -> str:
        return self._approved_binding(owner_id, runtime_binding=runtime_binding, session_id=session_id).machine_id

    def _approved_binding(self, owner_id, *, runtime_binding, session_id):
        binding, _ = self.require_execution(owner_id)
        if runtime_binding != self._binding_digest(binding):
            raise TerminalError("terminal_target_changed", 409)
        if session_id is not None:
            session = self._get_live(owner_id, session_id)
            if session.remote is None or session.binding != binding:
                raise TerminalError("terminal_target_changed", 409)
        return binding

    @staticmethod
    def _binding_digest(binding):
        public = {name: getattr(binding, name) for name in (
            "profile_digest", "owner_id", "machine_id", "address", "port", "username", "host_key_fingerprint",
        )}
        return hashlib.sha256(json.dumps(public, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def capability(self, owner_id: str) -> Dict[str, Any]:
        from orchestrator.terminal_security import terminal_capability

        return terminal_capability(self, owner_id)

    def _check_authority(self, authority, owner_id, tool_name):
        if (not isinstance(authority, TerminalAuthority) or authority.owner_id != owner_id
                or not authority.matches(self._orchestrator)):
            raise TerminalError("terminal_authority_required", 403)
        authority.assert_current(tool_name)

    def _check_remote(self, session, authority, tool_name):
        if session.remote is None:
            return
        try:
            self._check_authority(authority, session.owner_id, tool_name)
            if session.closed:
                if tool_name == "terminal_read":
                    return
                raise TerminalError("terminal_session_closed", 409)
            if self._sweeper_stop.is_set():
                raise TerminalError("terminal_unavailable", 503)
            self._check_session_binding(session)
        except Exception:
            session.close("authority_lost")
            raise

    def _check_session_binding(self, session):
        session.authority.assert_session_current("start_terminal")
        binding, _ = self.require_execution(session.owner_id)
        if binding != session.binding:
            raise TerminalError("terminal_target_changed", 409)

    def list_sessions(self, owner_id: str) -> List[Dict[str, Any]]:
        require_terminal_enabled()
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
            "target": session.target,
            "directory": session.directory,
            "exit_code": session.exit_code,
        }

    def create(self, owner_id: str, *, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS,
               command: Optional[str] = None, target: Optional[str] = None,
               directory: Optional[str] = None, authority=None, runtime_binding=None) -> Dict[str, Any]:
        require_terminal_execution(self, owner_id)
        self._check_authority(authority, owner_id, "start_terminal")
        if runtime_binding != self.runtime_binding(owner_id):
            raise TerminalError("terminal_target_changed", 409)
        cols = _bounded_dimension(cols, COLS_RANGE, "terminal_cols_invalid")
        rows = _bounded_dimension(rows, ROWS_RANGE, "terminal_rows_invalid")
        if command is not None and (not isinstance(command, str) or not command.strip()
                                    or len(command) > 512 or "\x00" in command):
            raise TerminalError("terminal_command_invalid", 422)
        requested_target = None if target is None else _bounded_target(target)
        directory = None if directory is None else _bounded_directory(directory)
        with self._lock:
            pending_total = sum(self._pending.values())
            closed = sorted((session for session in self._sessions.values() if session.closed),
                            key=lambda session: session.created_at)
            while closed and len(self._sessions) + pending_total >= RETAINED_SESSION_LIMIT:
                self._sessions.pop(closed.pop(0).session_id, None)
            count = sum(session.owner_id == owner_id and not session.closed for session in self._sessions.values())
            if len(self._sessions) + pending_total >= RETAINED_SESSION_LIMIT:
                raise TerminalError("terminal_session_limit_reached", 429)
            if self._sweeper_stop.is_set():
                raise TerminalError("terminal_unavailable", 503)
            if count + self._pending.get(owner_id, 0) >= SESSIONS_PER_OWNER:
                raise TerminalError("terminal_session_limit_reached", 429)
            self._pending[owner_id] = self._pending.get(owner_id, 0) + 1
        remote = session = None
        successful = False
        try:
            from orchestrator.terminal_ssh import SSHPTY

            binding, ssh_target = self.require_execution(owner_id)
            if runtime_binding != self._binding_digest(binding):
                raise TerminalError("terminal_target_changed", 409)
            if requested_target is not None and requested_target != binding.machine_id:
                raise TerminalError("terminal_target_changed", 409)

            def check():
                self._check_authority(authority, owner_id, "start_terminal")
                current, _ = self.require_execution(owner_id)
                if current != binding or self._sweeper_stop.is_set():
                    raise TerminalError("terminal_target_changed", 409)

            remote = SSHPTY.open(ssh_target, cols, rows, command, check, directory=directory)
            check()
            session = TerminalSession(uuid.uuid4().hex, owner_id, -1, None, "", now=self._clock,
                                      remote=remote, binding=binding, authority=replace(
                                          authority, _execution_fence=None, _admission=None,
                                          _runtime=None, _request_check=None),
                                      target=binding.machine_id, directory=directory)
            session.cols, session.rows = cols, rows
            with self._lock:
                check()
                self._sessions[session.session_id] = session
            session.start_pump()
            self._ensure_sweeper()
            check()
            audit_machine_event(owner_id, "terminal_session_start", "isolated terminal session started",
                                machine_id=binding.machine_id)
            result = self.describe(session)
            successful = True
            return result
        except TerminalError:
            if remote is not None:
                remote.close()
            raise
        except Exception:
            if remote is not None:
                remote.close()
            raise TerminalError("terminal_spawn_failed", 503) from None
        finally:
            with self._lock:
                if session is not None and not successful:
                    self._sessions.pop(session.session_id, None)
                pending = self._pending.get(owner_id, 1) - 1
                if pending:
                    self._pending[owner_id] = pending
                else:
                    self._pending.pop(owner_id, None)

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

    def write(self, owner_id: str, session_id: str, data_b64: str, *, authority=None, runtime_binding=None) -> Dict[str, Any]:
        try:
            require_terminal_execution(self, owner_id)
        except TerminalError:
            if self._orchestrator is not None:
                self.close_all_for_owner(owner_id, "runtime_unavailable")
            raise
        if not isinstance(data_b64, str) or len(data_b64) > (INPUT_CHUNK_MAX + 2) // 3 * 4:
            raise TerminalError("terminal_input_invalid", 422)
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (ValueError, binascii.Error):
            raise TerminalError("terminal_input_invalid", 422) from None
        if not 1 <= len(data) <= INPUT_CHUNK_MAX:
            raise TerminalError("terminal_input_invalid", 422)
        session = self._get_live(owner_id, session_id)
        if session.remote is None:
            raise TerminalError("terminal_isolation_unavailable", 503)
        if runtime_binding != self.runtime_binding(owner_id, session_id=session_id):
            raise TerminalError("terminal_target_changed", 409)
        with session.effect_lock:
            try:
                session.remote.write(data, lambda: self._check_remote(session, authority, "terminal_write"))
            except TerminalError:
                raise
            except Exception:
                session.close("write_failed")
                raise TerminalError("terminal_write_failed", 503) from None
            session.touch()
        return self.describe(session)

    def resize(self, owner_id: str, session_id: str, *, cols: int, rows: int, authority=None) -> Dict[str, Any]:
        require_terminal_enabled()
        session = self._get_live(owner_id, session_id)
        cols = _bounded_dimension(cols, COLS_RANGE, "terminal_cols_invalid")
        rows = _bounded_dimension(rows, ROWS_RANGE, "terminal_rows_invalid")
        if session.remote is not None:
            with session.effect_lock:
                self._check_remote(session, authority, "terminal_resize")
                try:
                    session.remote.resize(cols, rows)
                except Exception:
                    session.close("transport_lost")
                    raise TerminalError("terminal_transport_lost", 503) from None
        else:
            _apply_winsize(session.master_fd, (cols, rows))
            _signal_group(session, signal.SIGWINCH)
        session.cols, session.rows = cols, rows
        session.touch()
        return self.describe(session)

    def interrupt(self, owner_id: str, session_id: str, *, authority=None) -> Dict[str, Any]:
        session = self._get_live(owner_id, session_id)
        if session.remote is not None:
            with session.effect_lock:
                try:
                    session.remote.write(b"\x03", lambda: self._check_remote(session, authority, "terminal_interrupt"))
                except TerminalError:
                    raise
                except Exception:
                    session.close("transport_lost")
                    raise TerminalError("terminal_transport_lost", 503) from None
            session.touch()
            return self.describe(session)
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
             wait_seconds: float = 0.0, authority=None) -> Dict[str, Any]:
        require_terminal_enabled()
        session = self._get_owned(owner_id, session_id)
        self._check_remote(session, authority, "terminal_read")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise TerminalError("terminal_offset_invalid", 422)
        wait_seconds = _bounded_wait(wait_seconds)
        deadline = self._clock() + wait_seconds
        while True:
            chunk, next_offset, truncated, eof = session.read(offset)
            if chunk or eof or wait_seconds <= 0 or self._clock() >= deadline:
                self._check_remote(session, authority, "terminal_read")
                return {
                    "session_id": session_id,
                    "offset": offset,
                    "next_offset": next_offset,
                    "data_b64": base64.b64encode(chunk).decode("ascii"),
                    "bytes": len(chunk),
                    "truncated": truncated,
                    "eof": eof,
                    "exit_code": session.exit_code,
                    "close_reason": session.close_reason,
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
        outcome = {"session_id": session_id, "closed": True, "reason": session.close_reason}
        if session.exit_code is not None:
            outcome["exit_code"] = session.exit_code
        return outcome

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
        for session in live:
            if session.remote is not None:
                try:
                    self._check_session_binding(session)
                except Exception:
                    session.close("authority_lost")
        expired = [s for s in live if not s.closed
                   if now - s.last_activity_at >= IDLE_TIMEOUT_S
                   or now - s.created_at >= MAX_LIFETIME_S]
        for session in expired:
            session.close("expired")
            audit_machine_event(session.owner_id, "terminal_session_expire",
                               "interactive terminal session expired", machine_id=session.session_id)
        return len(expired)

    def close_for_connection(self, websocket) -> int:
        with self._lock:
            sessions = [session for session in self._sessions.values()
                        if session.authority is not None and session.authority._websocket is websocket
                        and not session.closed]
        for session in sessions:
            session.close("disconnected")
        return len(sessions)

    def shutdown(self) -> None:
        self._sweeper_stop.set()
        for session in list(self._sessions.values()):
            session.close("shutdown")

    def _wait_readable(self, session: TerminalSession, timeout: float) -> None:
        if timeout <= 0 or session.closed:
            return
        if session.remote is not None:
            time.sleep(min(timeout, _POLL_SLICE_S))
            return
        try:
            select.select([session.master_fd], [], [], timeout)
        except (OSError, ValueError):
            time.sleep(min(timeout, _POLL_SLICE_S))

    def _ensure_sweeper(self) -> None:
        with self._lock:
            if self._sweeper_stop.is_set():
                raise TerminalError("terminal_unavailable", 503)
            if self._sweeper is not None and self._sweeper.is_alive():
                return
            self._sweeper = threading.Thread(target=self._sweep_loop, name="terminal-sweeper", daemon=True)
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


def _bounded_target(value: Any) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value.encode("utf-8")) > TARGET_MAX_BYTES
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise TerminalError("terminal_target_invalid", 422)
    return value


def _bounded_directory(value: Any) -> str:
    if (not isinstance(value, str) or not value.startswith("/")
            or len(value.encode("utf-8")) > DIRECTORY_MAX_BYTES
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise TerminalError("terminal_directory_invalid", 422)
    return value


def _bounded_wait(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise TerminalError("terminal_wait_invalid", 422)
    return min(float(value), READ_WAIT_MAX_S)


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
    "TARGET_MAX_BYTES", "DIRECTORY_MAX_BYTES",
]
