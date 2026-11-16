"""Probes the machine for the facts select.py needs: platform, Landlock ABI, bubblewrap, user namespaces, sandbox-exec and the App Sandbox.
Every probe is injectable through Hooks, and a probe that errors reports its fact as absent so selection fails closed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

SYS_LANDLOCK_CREATE_RULESET = 444
LANDLOCK_CREATE_RULESET_VERSION = 1
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
TRUE_BINARIES = ("/usr/bin/true", "/bin/true")
BWRAP_DIRECTORIES = ("/usr/bin", "/bin", "/usr/local/bin")
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


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _which(name: str) -> str | None:
    search = os.pathsep.join([os.environ.get("PATH", ""), *BWRAP_DIRECTORIES])
    return shutil.which(name, path=search)


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
    which: Callable[[str], str | None] = _which
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


def _bwrap_path(hooks: Hooks) -> str | None:
    try:
        found = hooks.which("bwrap")
    except Exception:
        return None
    return found if isinstance(found, str) and found else None


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
    try:
        true_binary = next(path for path in TRUE_BINARIES if hooks.executable(path))
    except (StopIteration, Exception):
        return False
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
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        true_binary,
    ]
    try:
        return hooks.run(argv) == 0
    except Exception:
        return False


def _userns(hooks: Hooks, bwrap: str | None) -> bool:
    if not _sysctls_allow_userns(hooks):
        return False
    return True if bwrap is None else _trial_confines(hooks, bwrap)


def _sandbox_exec(hooks: Hooks) -> bool:
    try:
        return bool(hooks.executable(SANDBOX_EXEC))
    except Exception:
        return False


def _app_sandboxed(hooks: Hooks) -> bool:
    env = hooks.env
    return bool(
        env.get("APP_SANDBOX_CONTAINER_ID")
    ) or "/Library/Containers/" in env.get("HOME", "")


def probe(hooks: Hooks | None = None) -> dict[str, Any]:
    hooks = hooks or Hooks()
    linux = hooks.platform == "linux"
    darwin = hooks.platform == "darwin"
    bwrap = _bwrap_path(hooks) if linux else None
    return {
        "platform": hooks.platform,
        "landlock_abi": _landlock_abi(hooks) if linux else 0,
        "bwrap": bwrap is not None,
        "userns": _userns(hooks, bwrap) if linux else False,
        "sandbox_exec": _sandbox_exec(hooks) if darwin else False,
        "app_sandboxed": _app_sandboxed(hooks) if darwin else False,
    }
