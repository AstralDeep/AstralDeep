"""Command line entry for confined processes, run as python -I -B <dir>/astral_exec/run.py followed by launch, rlimit or conformance.
Under -I the script's own folder is not on sys.path, so the script inserts only its parent folder before it dispatches.
The rlimit command is the launcher's trampoline: it reads the child environment from a pipe, sets the call's limits as soft and hard, restores default signals, reports readiness on a status descriptor and execs sandbox-exec with exactly that environment.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

USAGE = "usage: run.py {launch,rlimit,conformance} ..."
LIMIT_FLAGS = (
    ("core", "RLIMIT_CORE"),
    ("fsize", "RLIMIT_FSIZE"),
    ("nofile", "RLIMIT_NOFILE"),
    ("cpu", "RLIMIT_CPU"),
)
READY = b"exec"
RESTORED_SIGNALS = ("SIGPIPE", "SIGXFSZ")


def _limit(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("a limit is a non-negative integer")
    return value


def _read_env(fd: int) -> dict[str, str]:
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    os.close(fd)
    env = json.loads(b"".join(chunks))
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise ValueError("the environment must be an object of strings")
    return env


def rlimit(
    args: Sequence[str],
    api: Any = resource,
    signals: Any = signal,
    execve: Callable[[str, list[str], dict[str, str]], Any] = os.execve,
) -> int:
    args = list(args)
    split = args.index("--") if "--" in args else len(args)
    parser = argparse.ArgumentParser(prog="run.py rlimit")
    for flag, _name in LIMIT_FLAGS:
        parser.add_argument(f"--{flag}", type=_limit, required=True)
    parser.add_argument("--status-fd", type=int, required=True)
    parser.add_argument("--env-fd", type=int, required=True)
    options = parser.parse_args(args[:split])
    command = args[split + 1 :]
    try:
        env = _read_env(options.env_fd)
        if not command:
            raise ValueError("rlimit needs a command after --")
        for flag, name in LIMIT_FLAGS:
            value = getattr(options, flag)
            api.setrlimit(getattr(api, name), (value, value))
        for name in RESTORED_SIGNALS:
            signals.signal(getattr(signals, name), signals.SIG_DFL)
        os.set_inheritable(options.status_fd, False)
        os.write(options.status_fd, READY)
        execve(command[0], command, env)
    except (OSError, ValueError, OverflowError) as error:
        os.write(options.status_fd, b"fail " + type(error).__name__.encode())
        return 1
    return 0


def main(args: Sequence[str]) -> int:
    command, rest = (args[0], list(args[1:])) if args else ("", [])
    if command == "launch":
        from astral_exec.launch import main as launch

        return launch(rest)
    if command == "rlimit":
        return rlimit(rest)
    if command == "conformance":
        print(
            "run.py conformance: the conformance suite is not yet available",
            file=sys.stderr,
        )
        return 2
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main(sys.argv[1:]))
