"""The monitoring parent of one confined call: it selects the backend, writes the Seatbelt profile, starts sandbox-exec in a new session with the call's rlimits and enforces time, memory and processes until the tool exits.
run() captures the output for in-process callers, and main() is the run.py launch command, which passes its own stdio through.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import resource
import secrets
import signal
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
    REFUSED_EXIT_STATUS,
    SANDBOX_EXEC,
)
from .errors import ContextError, ExecRefused, PolicyError, Refusal
from .facts import probe
from .policy import Policy
from .select import Backend, select
from .watchdog import Watch

POLL_SECONDS = 0.01
DRAIN_SECONDS = 2.0
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
USAGE = "usage: run.py launch --policy FILE --data-dir DIR --home DIR --env-file FILE -- argv..."
_LOG = logging.getLogger("astral_exec.launch")


@dataclass(frozen=True)
class Outcome:
    code: str | None
    exit_code: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    refusal: Refusal | None = None

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


def apply_limits(limits: Mapping[str, int], api: Any = resource) -> None:
    api.setrlimit(api.RLIMIT_CORE, (CORE_LIMIT_BYTES, CORE_LIMIT_BYTES))
    for which, key in (
        (api.RLIMIT_FSIZE, "file_bytes"),
        (api.RLIMIT_NOFILE, "open_files"),
    ):
        _soft, hard = api.getrlimit(which)
        bound = limits[key] if hard == api.RLIM_INFINITY else min(limits[key], hard)
        api.setrlimit(which, (bound, bound))


def _checked_argv(argv: Any) -> list[str]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
        raise ValueError("argv must be a non-empty list of strings")
    if not all(isinstance(arg, str) and "\0" not in arg for arg in argv):
        raise ValueError("argv must hold strings without NUL")
    return list(argv)


def _checked_env(env: Any) -> dict[str, str]:
    if not isinstance(env, Mapping):
        raise ValueError("env must be a mapping of strings")
    for key, value in env.items():
        if not (isinstance(key, str) and isinstance(value, str)):
            raise ValueError("env keys and values must be strings")
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("env holds a malformed entry")
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


def _write_profile(text: str) -> str:
    fd, path = tempfile.mkstemp(prefix="astral-exec-", suffix=".sb")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _spawn(
    call: _Call, profile: str, capture: bool, data: bytes | None
) -> subprocess.Popen:
    limits = call.document["limits"]
    stdin: Any = None
    if capture:
        stdin = subprocess.DEVNULL if data is None else subprocess.PIPE
    output = subprocess.PIPE if capture else None
    return subprocess.Popen(
        [SANDBOX_EXEC, "-f", profile, *call.argv],
        executable=SANDBOX_EXEC,
        env=call.env,
        cwd=call.document["workspace"],
        stdin=stdin,
        stdout=output,
        stderr=output,
        start_new_session=True,
        close_fds=True,
        preexec_fn=lambda: apply_limits(limits),
    )


def _monitor(
    proc: subprocess.Popen,
    watch: Any,
    limits: Mapping[str, int],
    deadline: float,
    data: bytes | None,
) -> tuple[str | None, tuple[bytes | None, bytes | None] | None]:
    pending = data
    while True:
        try:
            return None, proc.communicate(pending, timeout=POLL_SECONDS)
        except subprocess.TimeoutExpired:
            pending = None
        if proc.poll() is not None:
            return None, None
        if time.monotonic() >= deadline:
            return "exec.time_limit", None
        processes, memory = watch.sample()
        if memory > limits["memory_bytes"]:
            return "exec.memory_limit", None
        if processes > limits["processes"]:
            return "exec.process_limit", None


def _drain(proc: subprocess.Popen) -> tuple[bytes | None, bytes | None]:
    try:
        return proc.communicate(timeout=DRAIN_SECONDS)
    except subprocess.TimeoutExpired as error:
        _LOG.warning("exec output stayed open after teardown pid=%d", proc.pid)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        proc.wait()
        return error.output, error.stderr


def _end(proc: subprocess.Popen, watch: Any) -> bool:
    survivors: set[int] | None = None
    if watch is not None:
        try:
            survivors = watch.end()
        except Exception as error:
            _LOG.warning("exec teardown failed: %s", type(error).__name__)
    if survivors is None and proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if survivors:
        _LOG.warning("exec processes survived teardown count=%d", len(survivors))
    return survivors == set()


def _supervise(
    proc: subprocess.Popen,
    limits: Mapping[str, int],
    wall_ms: int,
    marker: str,
    data: bytes | None,
    watch_factory: Callable[[int, bytes | None], Any] = Watch,
) -> Outcome:
    deadline = time.monotonic() + wall_ms / 1000
    entry = f"{LAUNCH_MARKER}={marker}".encode() if limits["processes"] > 1 else None
    watch = None
    code: str | None = None
    output = None
    ended = False
    try:
        watch = watch_factory(proc.pid, entry)
        code, output = _monitor(proc, watch, limits, deadline, data)
    except Exception as error:
        _LOG.warning("exec monitor failed: %s", type(error).__name__)
        code = "exec.launch_failed"
    finally:
        ended = _end(proc, watch)
    if not ended:
        code = "exec.launch_failed"
    stdout, stderr = output if output is not None else _drain(proc)
    if code is None and proc.returncode == -signal.SIGXFSZ:
        code = "exec.file_limit"
    return Outcome(code, proc.returncode, stdout or b"", stderr or b"")


def _launch(
    policy: Any,
    argv: Any,
    env: Any,
    stdin: Any,
    facts: Mapping[str, Any] | None,
    capture: bool,
) -> Outcome:
    digest = policy.digest() if isinstance(policy, Policy) else "none"
    try:
        call = _prepare(policy, argv, env, facts)
        if isinstance(call, Refusal):
            _LOG.info("exec refused digest=%s code=%s", digest, call.code)
            return Outcome(call.code, None, refusal=call)
        data = _checked_stdin(stdin)
        profile = _write_profile(call.profile)
    except Exception as error:
        _LOG.warning("exec launch failed digest=%s error=%s", digest, error)
        return Outcome("exec.launch_failed", None)
    try:
        try:
            proc = _spawn(call, profile, capture, data)
        except Exception as error:
            _LOG.warning("exec launch failed digest=%s error=%s", digest, error)
            return Outcome("exec.launch_failed", None)
        _LOG.info("exec launch digest=%s backend=seatbelt pid=%d", digest, proc.pid)
        outcome = _supervise(
            proc,
            call.document["limits"],
            call.document["time"]["wall_ms"],
            call.marker,
            data,
        )
        _LOG.info(
            "exec outcome digest=%s code=%s exit=%s",
            digest,
            outcome.code,
            outcome.exit_code,
        )
        return outcome
    finally:
        _remove(profile)


def run(
    policy: Policy,
    argv: Sequence[str],
    env: Mapping[str, str],
    stdin: bytes | None = None,
    *,
    facts: Mapping[str, Any] | None = None,
) -> Outcome:
    return _launch(policy, argv, env, stdin, facts, True)


def _stop(signum: int, _frame: Any) -> None:
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
        try:
            facts = probe()
            with open(options.policy, "rb") as handle:
                data = handle.read()
            policy = Policy.load(
                data, facts["platform"], options.data_dir, options.home
            )
            env = _read_env(options.env_file)
        except ExecRefused as error:
            return _refused(error.code, error.refusal.message)
        except PolicyError as error:
            return _refused(error.code, str(error))
        except Exception as error:
            _LOG.warning("exec launch failed before start: %s", error)
            return _refused("exec.launch_failed", MESSAGES["exec.launch_failed"])
        return _status(_launch(policy, args[split + 1 :], env, None, facts, False))
    except _Stopped as stopped:
        return 128 + stopped.signum
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
