"""The monitoring parent of one confined call: it selects the backend, writes the Seatbelt profile, starts sandbox-exec through the run.py rlimit trampoline in a new session and enforces time, memory and processes until the tool exits.
run() captures the output up to a per-stream cap for in-process callers, and main() is the run.py launch command, which passes its own stdio through.
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
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import canonjson, seatbelt
from .constants import (
    CORE_LIMIT_BYTES,
    LAUNCH_MARKER,
    MESSAGES,
    OUTPUT_LIMIT_BYTES,
    REFUSED_EXIT_STATUS,
    SANDBOX_EXEC,
    TRAMPOLINE_READY,
)
from .errors import ContextError, ExecRefused, PolicyError, Refusal
from .facts import probe
from .policy import Policy
from .select import Backend, select
from .watchdog import Watch, user_process_count

POLL_SECONDS = 0.01
DRAIN_SECONDS = 2.0
READ_CHUNK = 65536
STATUS_LIMIT_BYTES = 256
NPROC_SLACK = 32
PROFILE_SUBDIR = "exec-profiles"
TEARDOWN_WAIT_SECONDS = 5.0
SANDBOX_EXEC_FAILURES = frozenset({64, 65, 71})
RUN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")
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


def _prepare(
    policy: Any, argv: Any, env: Any, facts: Mapping[str, Any] | None
) -> _Call | Refusal:
    if not isinstance(policy, Policy) or policy.loaded_for is None:
        raise ContextError("run needs a policy loaded with Policy.load")
    chosen = select(policy, probe() if facts is None else facts, policy.loaded_for[1])
    if isinstance(chosen, Refusal):
        return chosen
    if chosen != Backend("seatbelt"):
        raise ContextError(f"this launcher cannot start the {chosen.name} backend")
    document = policy.document
    if document["hosts"]:
        raise ContextError("a policy with hosts needs an egress proxy")
    marker = secrets.token_hex(16)
    child_env = _checked_env(env)
    child_env[LAUNCH_MARKER] = marker
    profile = seatbelt.render(policy, {"proxy_port": None})
    return _Call(document, _checked_argv(argv), child_env, marker, profile)


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
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
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
        return watch is None
    if survivors:
        log.warning("exec processes survived teardown count=%d", len(survivors))
    return survivors == set()


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
) -> Outcome:
    log = log if log is not None else _log("none")
    deadline = time.monotonic() + wall_ms / 1000
    entry = f"{LAUNCH_MARKER}={marker}".encode() if limits["processes"] > 1 else None
    streams = None
    watch = None
    code: str | None = None
    ended = False
    try:
        try:
            encoded = json.dumps(dict(env)).encode()
            streams = _Streams(proc, status_fd, env_fd, encoded, data)
            try:
                watch = watch_factory(proc.pid, entry)
            except OSError:
                if proc.poll() is None:
                    raise
            if watch is not None:
                code = _monitor(proc, streams, watch, limits, deadline)
        except Exception as error:
            log.warning("exec monitor failed: %s", type(error).__name__)
            code = "exec.launch_failed"
        finally:
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
            try:
                ended = _end(proc, watch, log)
                if streams is not None:
                    streams.drain(DRAIN_SECONDS, log)
                try:
                    proc.wait(timeout=TEARDOWN_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    log.warning(
                        "exec child did not exit after teardown pid=%d", proc.pid
                    )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
    finally:
        if streams is not None:
            streams.close()
        else:
            for fd in (status_fd, env_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
    if streams is None:
        return Outcome("exec.launch_failed", None)
    stdout, stderr = streams.output("stdout"), streams.output("stderr")
    truncated = tuple(sorted(streams.truncated))
    if streams.output("status") != TRAMPOLINE_READY:
        log.warning("exec trampoline refused: %r", streams.output("status")[:64])
        return Outcome("exec.launch_failed", None, stdout, stderr, truncated=truncated)
    if proc.returncode in SANDBOX_EXEC_FAILURES and stderr.startswith(b"sandbox-exec:"):
        log.warning("exec sandbox-exec failed exit=%s", proc.returncode)
        return Outcome("exec.launch_failed", None, stdout, stderr, truncated=truncated)
    if not ended:
        code = "exec.launch_failed"
    code = code or _SIGNAL_CODES.get(proc.returncode)
    return Outcome(code, proc.returncode, stdout, stderr, truncated=truncated)


def _launch(
    policy: Any,
    argv: Any,
    env: Any,
    stdin: Any,
    facts: Mapping[str, Any] | None,
    capture: bool,
) -> Outcome:
    digest = policy.digest() if isinstance(policy, Policy) else "none"
    log = _log(digest)
    try:
        call = _prepare(policy, argv, env, facts)
        if isinstance(call, Refusal):
            log.info("exec refused code=%s", call.code)
            return Outcome(call.code, None, refusal=call)
        data = _checked_stdin(stdin)
    except Exception as error:
        log.warning("exec launch failed before start: %s", type(error).__name__)
        return Outcome("exec.launch_failed", None)
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


def _run(
    policy: Policy,
    argv: Sequence[str],
    env: Mapping[str, str],
    stdin: bytes | None = None,
    *,
    facts: Mapping[str, Any] | None = None,
    capture: bool = True,
) -> Outcome:
    return _launch(policy, argv, env, stdin, facts, capture)


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
