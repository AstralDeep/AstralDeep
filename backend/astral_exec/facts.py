"""Probes the machine for the facts select.py needs: platform, Landlock ABI, bubblewrap, user namespaces, effective user, sandbox-exec and the App Sandbox.
Every probe is injectable through Hooks, and a probe that errors reports the safe answer (absent, or sandboxed for the App Sandbox) so selection fails closed.
"""

from __future__ import annotations

import os
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .constants import BWRAP_PATHS, SANDBOX_EXEC

SYS_LANDLOCK_CREATE_RULESET = 444
LANDLOCK_CREATE_RULESET_VERSION = 1
INNER_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inner.py")
USERNS_SYSCTLS = (
    "/proc/sys/user/max_user_namespaces",
    "/proc/sys/kernel/unprivileged_userns_clone",
)
TRIAL_SECONDS = 10


def _syscall(number: int, a: int, b: int, c: int) -> int:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(
        ctypes.c_long(number), ctypes.c_long(a), ctypes.c_long(b), ctypes.c_long(c)
    )
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return int(result)


def _sandbox_check(pid: int) -> int:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    libc.sandbox_check.restype = ctypes.c_int
    return int(libc.sandbox_check(pid, None, 0))


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read(64)


def _run(argv: Sequence[str]) -> int:
    return subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=TRIAL_SECONDS,
        check=False,
    ).returncode


@dataclass(frozen=True)
class Hooks:
    platform: str = field(default_factory=lambda: _platform_name())
    syscall: Callable[[int, int, int, int], Any] = _syscall
    executable: Callable[[str], bool] = _executable
    stat: Callable[[str], Any] = os.stat
    sandbox_check: Callable[[int], Any] = _sandbox_check
    pid: Callable[[], int] = os.getpid
    euid: Callable[[], int] = os.geteuid
    exists: Callable[[str], bool] = os.path.exists
    tid: Callable[[], int] = threading.get_native_id
    read_text: Callable[[str], str] = _read_text
    run: Callable[[Sequence[str]], int] = _run
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))


def _platform_name() -> str:
    return "linux" if sys.platform.startswith("linux") else sys.platform


def _landlock_abi(hooks: Hooks) -> int:
    try:
        value = hooks.syscall(
            SYS_LANDLOCK_CREATE_RULESET, 0, 0, LANDLOCK_CREATE_RULESET_VERSION
        )
    except Exception:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 0
    return value


def resolve_bwrap(hooks: Hooks | None = None) -> str | None:
    hooks = hooks or Hooks()
    for path in BWRAP_PATHS:
        try:
            info = hooks.stat(path)
            mode = info.st_mode
            if (
                stat_module.S_ISREG(mode)
                and info.st_uid == 0
                and not mode & 0o022
                and mode & 0o111
            ):
                return path
        except Exception:
            continue
    return None


def _sysctls_allow_userns(hooks: Hooks) -> bool:
    for path in USERNS_SYSCTLS:
        try:
            text = hooks.read_text(path).strip()
        except Exception:
            continue
        if text.isdigit() and int(text) == 0:
            return False
    return True


def _trial_confines(hooks: Hooks, bwrap: str) -> bool:
    interpreter = sys.executable
    try:
        if not os.path.isabs(interpreter) or not hooks.executable(interpreter):
            return False
        with tempfile.TemporaryDirectory(prefix="astral-trial-") as work:
            argv = [
                bwrap,
                "--unshare-user",
                "--unshare-pid",
                "--unshare-net",
                "--unshare-ipc",
                "--unshare-uts",
                "--unshare-cgroup",
                "--ro-bind",
                "/",
                "/",
                "--bind",
                work,
                work,
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                interpreter,
                "-I",
                "-S",
                "-B",
                INNER_PY,
                "--probe",
                work,
            ]
            return hooks.run(argv) == 0
    except Exception:
        return False


def _userns(hooks: Hooks, bwrap: str | None) -> bool:
    if bwrap is None or not _sysctls_allow_userns(hooks):
        return False
    return _trial_confines(hooks, bwrap)


def _euid(hooks: Hooks) -> int:
    try:
        value = hooks.euid()
    except Exception:
        return 0
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 0
    )


def _proc_children(hooks: Hooks) -> bool:
    try:
        return bool(hooks.exists(f"/proc/self/task/{hooks.tid()}/children"))
    except Exception:
        return False


def _sandbox_exec(hooks: Hooks) -> bool:
    try:
        return bool(hooks.executable(SANDBOX_EXEC))
    except Exception:
        return False


def _app_sandboxed(hooks: Hooks) -> bool:
    try:
        if hooks.sandbox_check(hooks.pid()) != 0:
            return True
    except Exception:
        return True
    env = hooks.env
    return bool(
        env.get("APP_SANDBOX_CONTAINER_ID")
    ) or "/Library/Containers/" in env.get("HOME", "")


class Facts(dict):
    bwrap_path: str | None = None
    proc_children: bool = False


def probe(hooks: Hooks | None = None) -> Facts:
    hooks = hooks or Hooks()
    linux = hooks.platform == "linux"
    darwin = hooks.platform == "darwin"
    bwrap = resolve_bwrap(hooks) if linux else None
    facts = Facts(
        platform=hooks.platform,
        landlock_abi=_landlock_abi(hooks) if linux else 0,
        bwrap=bwrap is not None,
        userns=_userns(hooks, bwrap) if linux else False,
        euid=_euid(hooks),
        sandbox_exec=_sandbox_exec(hooks) if darwin else False,
        app_sandboxed=_app_sandboxed(hooks) if darwin else False,
    )
    facts.bwrap_path = bwrap
    facts.proc_children = linux and _proc_children(hooks)
    return facts
