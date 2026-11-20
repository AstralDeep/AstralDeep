"""The monitoring parent of one confined call: it selects the backend, prepares the Seatbelt profile or the bubblewrap command and starts the tool through the run.py trampoline or the inner launcher, then enforces the time and process limits until the tool exits.
run() captures the output up to a per-stream cap for in-process callers, and main() is the run.py launch command that passes its own stdio through.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import secrets
import selectors
import signal
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from . import canonjson, linux, seatbelt
from .constants import (
    BWRAP_PATHS,
    CORE_LIMIT_BYTES,
    LAUNCH_MARKER,
    LINUX_BASE,
    MESSAGES,
    OUTPUT_LIMIT_BYTES,
    PROXY_PORT,
    REFUSED_EXIT_STATUS,
    SANDBOX_EXEC,
    NAMESPACE_DENIED,
    TRAMPOLINE_READY,
)
from .errors import ContextError, ExecRefused, PolicyError, Refusal, refuse
from .facts import probe
from .fold import contains
from .policy import Policy
from .proxy import Proxy
from .select import Backend, select
from .watchdog import LinuxWatch, Watch, user_process_count

POLL_SECONDS = 0.01
DRAIN_SECONDS = 2.0
READ_CHUNK = 65536
STATUS_LIMIT_BYTES = 256
NPROC_SLACK = 32
PROFILE_SUBDIR = "exec-profiles"
TEARDOWN_WAIT_SECONDS = 5.0
SANDBOX_EXEC_FAILURES = frozenset({64, 65, 71})
RUN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")
INNER_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inner.py")
PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
USAGE = "usage: run.py launch --policy FILE --data-dir DIR --home DIR --env-file FILE -- argv..."
_LOG = logging.getLogger("astral_exec.launch")


class _DigestLog(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        return f"digest={self.extra['digest']} {msg}", kwargs


def _log(digest: str) -> _DigestLog:
    return _DigestLog(_LOG, {"digest": digest})


@dataclass(frozen=True)
class Outcome:
    code: str | None
    exit_code: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    refusal: Refusal | None = None
    truncated: tuple[str, ...] = ()

    @property
    def message(self) -> str | None:
        if self.refusal is not None:
            return self.refusal.message
        return MESSAGES[self.code] if self.code else None


@dataclass(frozen=True)
class _Call:
    document: dict[str, Any]
    argv: list[str]
    env: dict[str, str]
    marker: str
    profile: str


@dataclass(frozen=True)
class _LinuxCall:
    document: dict[str, Any]
    plan: dict[str, Any]
    argv: list[str]
    env: dict[str, str]
    marker: str
    bwrap: str


def _seatbelt_failed(proc: subprocess.Popen, stderr: bytes) -> bool:
    return proc.returncode in SANDBOX_EXEC_FAILURES and stderr.startswith(
        b"sandbox-exec:"
    )


_LINUX_SIGNAL_CODES = {128 + signal.SIGXFSZ: "exec.file_limit"}
_LINUX_STATUS_REFUSALS = {
    b"fail " + NAMESPACE_DENIED.encode(): refuse(
        "exec.backend_unavailable", NAMESPACE_DENIED
    )
}


class _Stopped(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _checked_argv(argv: Any) -> list[str]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
        raise ValueError("argv must be a non-empty list of strings")
    if not all(isinstance(arg, str) and "\0" not in arg for arg in argv):
        raise ValueError("argv must hold strings without NUL")
    if argv[0].startswith("-"):
        raise ValueError("the tool path must not look like an option")
    return list(argv)


def _checked_env(env: Any) -> dict[str, str]:
    if not isinstance(env, Mapping):
        raise ValueError("env must be a mapping of strings")
    for key, value in env.items():
        if not (isinstance(key, str) and isinstance(value, str)):
            raise ValueError("env keys and values must be strings")
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("env holds a malformed entry")
        if key.startswith("DYLD_"):
            raise ValueError("macOS strips DYLD_ variables before the tool starts")
    if LAUNCH_MARKER in env:
        raise ValueError(f"{LAUNCH_MARKER} is reserved for the launcher")
    return dict(env)


def _checked_stdin(stdin: Any) -> bytes | None:
    if stdin is not None and not isinstance(stdin, (bytes, bytearray, memoryview)):
        raise ValueError("stdin must be bytes or None")
    return None if stdin is None else bytes(stdin)


def _proxied_env(env: Any, document: dict[str, Any], port: int | None) -> dict[str, str]:
    child_env = _checked_env(env)
    if document["hosts"] and port is not None:
        url = f"http://127.0.0.1:{port}"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            child_env[name] = url
            child_env[name.lower()] = url
        child_env["NO_PROXY"] = child_env["no_proxy"] = ""
    return child_env


def _seatbelt_call(
    policy: Policy, argv: Any, env: Any, proxy_port: int | None
) -> _Call:
    document = policy.document
    if document["hosts"] and proxy_port is None:
        raise ContextError("a policy with hosts needs an egress proxy")
    marker = secrets.token_hex(16)
    child_env = _proxied_env(env, document, proxy_port)
    child_env[LAUNCH_MARKER] = marker
    profile = seatbelt.render(policy, {"proxy_port": proxy_port})
    return _Call(document, _checked_argv(argv), child_env, marker, profile)


def _linux_call(
    policy: Policy, argv: Any, env: Any, chosen: Backend, egress_socket: str | None
) -> _LinuxCall:
    document = policy.document
    built = linux.plan(policy, egress_socket)
    interpreter = sys.executable
    if not os.path.isabs(interpreter):
        raise ContextError("the sandbox interpreter must be an absolute path")
    roots = (*LINUX_BASE, *document["reads"])
    if not any(contains(root, interpreter, "linux") for root in roots):
        raise ContextError("the sandbox interpreter must sit under a read root")
    if chosen.bwrap not in BWRAP_PATHS:
        raise ContextError("the Linux backend needs a bwrap path from the fixed list")
    return _LinuxCall(
        document,
        built,
        _checked_argv(argv),
        _proxied_env(env, document, PROXY_PORT),
        secrets.token_hex(16),
        chosen.bwrap,
    )


def _profile_folder(data_dir: str) -> str:
    folder = os.path.join(data_dir, PROFILE_SUBDIR)
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(folder)
    if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
        raise ContextError("the profile folder is not a real directory")
    if info.st_uid != os.geteuid():
        raise ContextError("the profile folder is not owned by this user")
    if info.st_mode & 0o077:
        raise ContextError("the profile folder is group or world accessible")
    return folder


def _write_profile(text: str, data_dir: str) -> str:
    folder = _profile_folder(data_dir)
    fd, path = tempfile.mkstemp(prefix="profile-", suffix=".sb", dir=folder)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        _remove(path)
        raise
    try:
        with handle:
            handle.write(text)
    except BaseException:
        _remove(path)
        raise
    return path


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _command(
    call: _Call, profile: str, status_fd: int, env_fd: int, nproc: int | None
) -> list[str]:
    if not os.path.isabs(sys.executable):
        raise ContextError("the trampoline needs an absolute interpreter path")
    limits = call.document["limits"]
    cpu = math.ceil(call.document["time"]["wall_ms"] / 1000) + 1
    flags = [
        "--core",
        str(CORE_LIMIT_BYTES),
        "--fsize",
        str(limits["file_bytes"]),
        "--nofile",
        str(limits["open_files"]),
        "--cpu",
        str(cpu),
        "--status-fd",
        str(status_fd),
        "--env-fd",
        str(env_fd),
    ]
    if nproc is not None:
        flags += ["--nproc", str(nproc)]
    return [
        sys.executable,
        "-I",
        "-B",
        "-S",
        RUN_PY,
        "rlimit",
        *flags,
        "--",
        SANDBOX_EXEC,
        "-f",
        profile,
        "--",
        *call.argv,
    ]


def _spawn(
    call: _Call, profile: str, capture: bool, data: bytes | None
) -> tuple[subprocess.Popen, int, int]:
    stdin: Any = None
    if capture:
        stdin = subprocess.DEVNULL if data is None else subprocess.PIPE
    output = subprocess.PIPE if capture else None
    processes = call.document["limits"]["processes"]
    nproc = user_process_count() + processes + NPROC_SLACK if processes > 1 else None
    status_read, status_write = os.pipe()
    env_read, env_write = os.pipe()
    try:
        proc = subprocess.Popen(
            _command(call, profile, status_write, env_read, nproc),
            env={},
            cwd=call.document["workspace"],
            stdin=stdin,
            stdout=output,
            stderr=output,
            start_new_session=True,
            close_fds=True,
            pass_fds=(status_write, env_read),
        )
    except BaseException:
        os.close(status_read)
        os.close(env_write)
        raise
    finally:
        os.close(status_write)
        os.close(env_read)
    return proc, status_read, env_write


class _Streams:
    def __init__(
        self,
        proc: subprocess.Popen,
        status_fd: int,
        env_fd: int,
        env: bytes,
        data: bytes | None,
        extra_sends: Sequence[tuple[int, bytes]] = (),
    ) -> None:
        self._selector = selectors.DefaultSelector()
        self._closers: dict[int, Callable[[], None]] = {}
        self._pending: dict[int, memoryview] = {}
        self._kept = {
            "stdout": bytearray(),
            "stderr": bytearray(),
            "status": bytearray(),
        }
        self.truncated: set[str] = set()
        self._watch(status_fd, "status", lambda: os.close(status_fd))
        self._send(env_fd, env, lambda: os.close(env_fd))
        for extra_fd, payload in extra_sends:
            self._send(extra_fd, payload, lambda fd=extra_fd: os.close(fd))
        for name in ("stdout", "stderr"):
            stream = getattr(proc, name)
            if stream is not None:
                self._watch(stream.fileno(), name, stream.close)
        if proc.stdin is not None:
            if data:
                self._send(proc.stdin.fileno(), data, proc.stdin.close)
            else:
                proc.stdin.close()

    def _watch(self, fd: int, name: str, closer: Callable[[], None]) -> None:
        self._selector.register(fd, selectors.EVENT_READ, name)
        self._closers[fd] = closer

    def _send(self, fd: int, data: bytes, closer: Callable[[], None]) -> None:
        os.set_blocking(fd, False)
        self._selector.register(fd, selectors.EVENT_WRITE, None)
        self._closers[fd] = closer
        self._pending[fd] = memoryview(data)

    def _finish(self, fd: int) -> None:
        self._selector.unregister(fd)
        self._pending.pop(fd, None)
        self._closers.pop(fd)()

    def output(self, name: str) -> bytes:
        return bytes(self._kept[name])

    def reading(self) -> bool:
        return any(key.data is not None for key in self._selector.get_map().values())

    def pump(self, timeout: float) -> bool:
        if not self._selector.get_map():
            return False
        for key, _events in self._selector.select(timeout):
            if key.data is None:
                self._write(key.fd)
            else:
                self._read(key.fd, key.data)
        return True

    def _write(self, fd: int) -> None:
        pending = self._pending[fd]
        try:
            written = os.write(fd, pending[:READ_CHUNK])
        except BlockingIOError:
            return
        except BrokenPipeError:
            written = len(pending)
        self._pending[fd] = pending[written:]
        if not self._pending[fd]:
            self._finish(fd)

    def _read(self, fd: int, name: str) -> None:
        chunk = os.read(fd, READ_CHUNK)
        if not chunk:
            self._finish(fd)
            return
        kept = self._kept[name]
        limit = STATUS_LIMIT_BYTES if name == "status" else OUTPUT_LIMIT_BYTES
        room = max(limit - len(kept), 0)
        if len(chunk) > room and name != "status":
            self.truncated.add(name)
        kept += chunk[:room]

    def drain(self, seconds: float, log: _DigestLog) -> None:
        end = time.monotonic() + seconds
        while self.reading():
            left = end - time.monotonic()
            if left <= 0:
                log.warning("exec output stayed open after teardown")
                return
            self.pump(min(left, POLL_SECONDS))

    def close(self) -> None:
        for fd in list(self._closers):
            self._finish(fd)
        self._selector.close()


def _monitor(
    proc: subprocess.Popen,
    streams: _Streams,
    watch: Any,
    limits: Mapping[str, int],
    deadline: float,
) -> str | None:
    next_sample = 0.0
    while True:
        if not streams.pump(POLL_SECONDS):
            try:
                proc.wait(POLL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is not None:
            return None
        now = time.monotonic()
        if now >= deadline:
            return "exec.time_limit"
        if now < next_sample:
            continue
        next_sample = now + POLL_SECONDS
        processes, memory = watch.sample()
        if memory > limits["memory_bytes"]:
            return "exec.memory_limit"
        if processes > limits["processes"]:
            return "exec.process_limit"


def _end(proc: subprocess.Popen, watch: Any, log: _DigestLog) -> bool:
    survivors: set[int] | None = None
    if watch is not None:
        try:
            survivors = watch.end()
        except Exception as error:
            log.warning("exec teardown failed: %s", type(error).__name__)
    if survivors is None:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        return False
    if survivors:
        log.warning("exec processes survived teardown count=%d", len(survivors))
    return survivors == set()


@contextmanager
def _deferred_stop() -> Iterator[None]:
    # A stop signal taken by any thread trips the main thread's handler despite the mask, so the handler is swapped out until the sweep ends.
    seen: list[int] = []
    swapped: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in STOP_SIGNALS:
            if signal.getsignal(signum) is _stop:
                swapped[signum] = signal.signal(
                    signum, lambda number, _frame: seen.append(number)
                )
    try:
        yield
    finally:
        for signum, handler in swapped.items():
            signal.signal(signum, handler)
    if seen:
        _stop(seen[0], None)


_SIGNAL_CODES = {
    -signal.SIGXFSZ: "exec.file_limit",
    -signal.SIGXCPU: "exec.time_limit",
}


def _supervise(
    proc: subprocess.Popen,
    status_fd: int,
    env_fd: int,
    env: Mapping[str, str],
    limits: Mapping[str, int],
    wall_ms: int,
    marker: str,
    data: bytes | None,
    watch_factory: Callable[[int, bytes | None], Any] = Watch,
    log: _DigestLog | None = None,
    *,
    extra_sends: Sequence[tuple[int, bytes]] = (),
    backend_failed: Callable[[subprocess.Popen, bytes], bool] | None = _seatbelt_failed,
    signal_codes: Mapping[int, str] = _SIGNAL_CODES,
    status_refusals: Mapping[bytes, Refusal] | None = None,
) -> Outcome:
    log = log if log is not None else _log("none")
    deadline = time.monotonic() + wall_ms / 1000
    entry = f"{LAUNCH_MARKER}={marker}".encode() if limits["processes"] > 1 else None
    streams = None
    watch = None
    code: str | None = None
    ended = False
    reaped = True
    try:
        try:
            encoded = json.dumps(dict(env)).encode()
            streams = _Streams(proc, status_fd, env_fd, encoded, data, extra_sends)
            watch = watch_factory(proc.pid, entry)
            if getattr(watch, "started", True):
                code = _monitor(proc, streams, watch, limits, deadline)
            elif proc.poll() is None:
                raise OSError("the confined child is not visible to the watchdog")
        except Exception as error:
            log.warning("exec monitor failed: %s", type(error).__name__)
            code = "exec.launch_failed"
        finally:
            swept = False

            def sweep() -> None:
                nonlocal ended, reaped, swept
                blocked = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
                try:
                    ended = _end(proc, watch, log)
                    if streams is not None:
                        streams.drain(DRAIN_SECONDS, log)
                    try:
                        proc.wait(timeout=TEARDOWN_WAIT_SECONDS)
                    except subprocess.TimeoutExpired:
                        reaped = False
                        log.warning(
                            "exec child did not exit after teardown pid=%d", proc.pid
                        )
                    swept = True
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, blocked)

            try:
                with _deferred_stop():
                    sweep()
            except _Stopped:
                # A stop that lands before the handler is swapped has already set the signals to ignore, so this sweep runs to the end.
                if not swept:
                    sweep()
                raise
    finally:
        if streams is not None:
            streams.close()
        else:
            for fd in (status_fd, env_fd, *(fd for fd, _payload in extra_sends)):
                try:
                    os.close(fd)
                except OSError:
                    pass
    if streams is None:
        return Outcome("exec.launch_failed", None)
    stdout, stderr = streams.output("stdout"), streams.output("stderr")
    truncated = tuple(sorted(streams.truncated))
    refusal = (status_refusals or {}).get(streams.output("status"))
    if refusal is not None:
        log.info("exec refused code=%s reason=%s", refusal.code, refusal.reason)
        return Outcome(
            refusal.code, None, stdout, stderr, refusal=refusal, truncated=truncated
        )
    if streams.output("status") != TRAMPOLINE_READY:
        log.warning("exec trampoline refused: %r", streams.output("status")[:64])
        return Outcome("exec.launch_failed", None, stdout, stderr, truncated=truncated)
    if backend_failed is not None and backend_failed(proc, stderr):
        log.warning("exec backend failed exit=%s", proc.returncode)
        return Outcome("exec.launch_failed", None, stdout, stderr, truncated=truncated)
    if not ended or not reaped:
        code = "exec.launch_failed"
    code = code or signal_codes.get(proc.returncode)
    return Outcome(code, proc.returncode, stdout, stderr, truncated=truncated)


def _close(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _linux_command(
    call: _LinuxCall,
    script_fd: int,
    pkg_fd: int,
    plan_fd: int,
    env_fd: int,
    status_fd: int,
) -> list[str]:
    return [
        call.bwrap,
        *call.plan["bwrap"],
        "--",
        sys.executable,
        "-I",
        "-S",
        "-B",
        f"/proc/self/fd/{script_fd}",
        "--package-fd",
        str(pkg_fd),
        "--plan-fd",
        str(plan_fd),
        "--env-fd",
        str(env_fd),
        "--status-fd",
        str(status_fd),
        "--",
        *call.argv,
    ]


def _linux_spawn(
    call: _LinuxCall, capture: bool, data: bytes | None
) -> tuple[subprocess.Popen, int, int, int]:
    stdin: Any = None
    if capture:
        stdin = subprocess.DEVNULL if data is None else subprocess.PIPE
    output = subprocess.PIPE if capture else None
    child_fds: list[int] = []
    parent_fds: list[int] = []
    try:
        script_fd = os.open(INNER_PY, os.O_RDONLY | os.O_CLOEXEC)
        child_fds.append(script_fd)
        pkg_fd = os.open(PACKAGE_PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        child_fds.append(pkg_fd)
        plan_read, plan_write = os.pipe()
        child_fds.append(plan_read)
        parent_fds.append(plan_write)
        env_read, env_write = os.pipe()
        child_fds.append(env_read)
        parent_fds.append(env_write)
        status_read, status_write = os.pipe()
        child_fds.append(status_write)
        parent_fds.append(status_read)
        command = _linux_command(
            call, script_fd, pkg_fd, plan_read, env_read, status_write
        )
        proc = subprocess.Popen(
            command,
            env={},
            cwd=call.document["workspace"],
            stdin=stdin,
            stdout=output,
            stderr=output,
            start_new_session=True,
            close_fds=True,
            pass_fds=tuple(child_fds),
        )
    except BaseException:
        for fd in parent_fds:
            _close(fd)
        raise
    finally:
        for fd in child_fds:
            _close(fd)
    return proc, status_read, env_write, plan_write


def _launch_seatbelt(
    policy: Policy, call: _Call, capture: bool, data: bytes | None, log: _DigestLog
) -> Outcome:
    profile = None
    try:
        profile = _write_profile(call.profile, policy.loaded_for[1])
        proc, status_fd, env_fd = _spawn(call, profile, capture, data)
        log.info("exec launch backend=seatbelt pid=%d", proc.pid)
        outcome = _supervise(
            proc,
            status_fd,
            env_fd,
            call.env,
            call.document["limits"],
            call.document["time"]["wall_ms"],
            call.marker,
            data,
            log=log,
        )
        log.info("exec outcome code=%s exit=%s", outcome.code, outcome.exit_code)
        return outcome
    except Exception as error:
        log.warning("exec launch failed at spawn: %s", type(error).__name__)
        return Outcome("exec.launch_failed", None)
    finally:
        if profile is not None:
            _remove(profile)


def _launch_linux(
    call: _LinuxCall, capture: bool, data: bytes | None, log: _DigestLog
) -> Outcome:
    plan_bytes = json.dumps(call.plan).encode()
    try:
        proc, status_fd, env_fd, plan_fd = _linux_spawn(call, capture, data)
    except Exception as error:
        log.warning("exec launch failed at spawn: %s", type(error).__name__)
        return Outcome("exec.launch_failed", None)
    log.info("exec launch backend=linux pid=%d", proc.pid)
    hosted = bool(call.document["hosts"])
    try:
        outcome = _supervise(
            proc,
            status_fd,
            env_fd,
            call.env,
            call.document["limits"],
            call.document["time"]["wall_ms"],
            call.marker,
            data,
            watch_factory=lambda root, marker: LinuxWatch(root, hosted=hosted),
            log=log,
            extra_sends=((plan_fd, plan_bytes),),
            backend_failed=None,
            signal_codes=_LINUX_SIGNAL_CODES,
            status_refusals=_LINUX_STATUS_REFUSALS,
        )
    except Exception as error:
        log.warning("exec launch failed at supervision: %s", type(error).__name__)
        return Outcome("exec.launch_failed", None)
    log.info("exec outcome code=%s exit=%s", outcome.code, outcome.exit_code)
    return outcome


def _launch(
    policy: Any,
    argv: Any,
    env: Any,
    stdin: Any,
    facts: Mapping[str, Any] | None,
    capture: bool,
    egress_socket: str | None = None,
    proxy_options: Mapping[str, Any] | None = None,
) -> Outcome:
    digest = policy.digest() if isinstance(policy, Policy) else "none"
    log = _log(digest)
    proxy = None
    try:
        try:
            if not isinstance(policy, Policy) or policy.loaded_for is None:
                raise ContextError("run needs a policy loaded with Policy.load")
            chosen = select(
                policy, probe() if facts is None else facts, policy.loaded_for[1]
            )
            if isinstance(chosen, Refusal):
                log.info("exec refused code=%s", chosen.code)
                return Outcome(chosen.code, None, refusal=chosen)
            data = _checked_stdin(stdin)
            hosts = policy.document["hosts"]
            if chosen == Backend("seatbelt"):
                if hosts:
                    proxy = Proxy(hosts, "tcp", **(proxy_options or {}))
                    proxy.start()
                call = _seatbelt_call(
                    policy, argv, env, proxy.address if proxy is not None else None
                )
            elif chosen.name == "linux":
                if hosts and egress_socket is None:
                    proxy = Proxy(hosts, "unix", **(proxy_options or {}))
                    proxy.start()
                    egress_socket = str(proxy.address)
                linux_call = _linux_call(policy, argv, env, chosen, egress_socket)
            else:
                raise ContextError(
                    f"this launcher cannot start the {chosen.name} backend"
                )
        except Exception as error:
            log.warning("exec launch failed before start: %s", type(error).__name__)
            return Outcome("exec.launch_failed", None)
        if chosen.name == "linux":
            return _launch_linux(linux_call, capture, data, log)
        return _launch_seatbelt(policy, call, capture, data, log)
    finally:
        if proxy is not None:
            proxy.close()


def _run(
    policy: Policy,
    argv: Sequence[str],
    env: Mapping[str, str],
    stdin: bytes | None = None,
    *,
    facts: Mapping[str, Any] | None = None,
    capture: bool = True,
    egress_socket: str | None = None,
    proxy_options: Mapping[str, Any] | None = None,
) -> Outcome:
    return _launch(
        policy, argv, env, stdin, facts, capture, egress_socket, proxy_options
    )


def run(
    policy: Policy,
    argv: Sequence[str],
    env: Mapping[str, str],
    stdin: bytes | None = None,
) -> Outcome:
    return _launch(policy, argv, env, stdin, None, True)


def _stop(signum: int, _frame: Any) -> None:
    for each in STOP_SIGNALS:
        signal.signal(each, signal.SIG_IGN)
    raise _Stopped(signum)


def _refused(code: str, message: str | None) -> int:
    print(json.dumps({"code": code, "message": message}), file=sys.stderr, flush=True)
    return REFUSED_EXIT_STATUS


def _read_env(path: str) -> dict[str, str]:
    with open(path, "rb") as handle:
        return _checked_env(canonjson.strict_loads(handle.read()))


def _status(outcome: Outcome) -> int:
    if outcome.code is not None or outcome.exit_code is None:
        return _refused(outcome.code or "exec.launch_failed", outcome.message)
    return outcome.exit_code if outcome.exit_code >= 0 else 128 - outcome.exit_code


def main(args: Sequence[str]) -> int:
    args = list(args)
    if "--" not in args or args.index("--") == len(args) - 1:
        print(USAGE, file=sys.stderr)
        return 2
    split = args.index("--")
    parser = argparse.ArgumentParser(prog="run.py launch", usage=USAGE)
    for name in ("--policy", "--data-dir", "--home", "--env-file"):
        parser.add_argument(name, required=True)
    options = parser.parse_args(args[:split])
    previous = {signum: signal.signal(signum, _stop) for signum in STOP_SIGNALS}
    try:
        digest = "none"
        try:
            facts = probe()
            with open(options.policy, "rb") as handle:
                data = handle.read()
            policy = Policy.load(
                data, facts["platform"], options.data_dir, options.home
            )
            digest = policy.digest()
            env = _read_env(options.env_file)
        except ExecRefused as error:
            return _refused(error.code, error.refusal.message)
        except PolicyError as error:
            return _refused(error.code, str(error))
        except Exception as error:
            _log(digest).warning("exec launch failed before start: %s", error)
            return _refused("exec.launch_failed", MESSAGES["exec.launch_failed"])
        return _status(_launch(policy, args[split + 1 :], env, None, facts, False))
    except _Stopped as stopped:
        return 128 + stopped.signum
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
