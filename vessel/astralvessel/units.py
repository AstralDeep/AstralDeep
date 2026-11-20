# Renders the systemd user units and launchd agents that keep each role running, and runs the foreground supervisor
# for hosts with no user manager. runtime.py supplies one RoleSpec per role and calls Manager or Supervisor with it.

from __future__ import annotations

import os
import plistlib
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

LABEL_PREFIX = "dev.astraldeep.vessel"
CHILD_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class Limits:
    memory_bytes: int
    pids: int
    cpus: float


@dataclass(frozen=True)
class RoleSpec:
    role: str
    argv: list[str]
    cwd: Path
    log: Path
    pack_bin: str
    limits: Limits | None = None

    @property
    def path(self) -> str:
        return f"{self.pack_bin}:{BASE_PATH}"


def unit_name(project: str, role: str) -> str:
    return f"astralvessel-{project}-{role}.service"


def label(project: str, role: str) -> str:
    return f"{LABEL_PREFIX}.{project}.{role}"


def _quote(word: str) -> str:
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def systemd_unit(project: str, spec: RoleSpec) -> str:
    lines = ["[Unit]", f"Description=AstralVessel {project} {spec.role}", "", "[Service]",
             f"ExecStart={' '.join(_quote(a) for a in spec.argv)}", f"WorkingDirectory={_quote(str(spec.cwd))}",
             f"Environment=PATH={_quote(spec.path)}", "Environment=PYTHONDONTWRITEBYTECODE=1",
             "Restart=on-failure", "RestartSec=2", "NoNewPrivileges=yes",
             f"StandardOutput=append:{spec.log}", f"StandardError=append:{spec.log}"]
    if spec.limits is not None:
        lines += [f"MemoryMax={spec.limits.memory_bytes}", f"TasksMax={spec.limits.pids}",
                  f"CPUQuota={round(spec.limits.cpus * 100)}%"]
    return "\n".join([*lines, "", "[Install]", "WantedBy=default.target", ""])


def launchd_plist(project: str, spec: RoleSpec, keep_alive: bool = True) -> bytes:
    document = {"Label": label(project, spec.role), "ProgramArguments": spec.argv,
                "WorkingDirectory": str(spec.cwd),
                "EnvironmentVariables": {"PATH": spec.path, "PYTHONDONTWRITEBYTECODE": "1"},
                "RunAtLoad": True, "StandardOutPath": str(spec.log), "StandardErrorPath": str(spec.log)}
    if keep_alive:
        document["KeepAlive"] = {"SuccessfulExit": False}
    return plistlib.dumps(document, sort_keys=True)


class Manager:
    # The user's service manager, driven only through the injected runner, so tests record every call.
    def __init__(self, platform: str, project: str, runner: Callable, unit_dir: Path | None, uid: int):
        self.darwin = platform == "darwin"
        self.project, self.runner, self.uid = project, runner, uid
        default = Path.home() / ("Library/LaunchAgents" if self.darwin else ".config/systemd/user")
        self.unit_dir = unit_dir or default

    def _file(self, role: str) -> Path:
        return self.unit_dir / (f"{label(self.project, role)}.plist" if self.darwin else unit_name(self.project, role))

    def _target(self, role: str) -> str:
        return f"gui/{self.uid}/{label(self.project, role)}"

    def install(self, specs: list[RoleSpec]) -> None:
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        for spec in specs:
            spec.log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self._file(spec.role)
            if self.darwin:
                path.write_bytes(launchd_plist(self.project, spec))
            else:
                path.write_text(systemd_unit(self.project, spec))
        if not self.darwin:
            self.runner(["systemctl", "--user", "daemon-reload"])

    def start(self, roles: list[str]) -> None:
        for role in roles:
            if self.darwin:
                self.runner(["launchctl", "bootout", self._target(role)])
                self.runner(["launchctl", "bootstrap", f"gui/{self.uid}", str(self._file(role))])
                self.runner(["launchctl", "kickstart", "-k", self._target(role)])
            else:
                self.runner(["systemctl", "--user", "enable", "--now", unit_name(self.project, role)])
                self.runner(["systemctl", "--user", "restart", unit_name(self.project, role)])

    def stop(self, roles: list[str]) -> None:
        for role in reversed(roles):
            if self.darwin:
                self.runner(["launchctl", "bootout", self._target(role)])
            else:
                self.runner(["systemctl", "--user", "disable", "--now", unit_name(self.project, role)])

    def status(self, roles: list[str]) -> dict[str, bool]:
        found = {}
        for role in roles:
            if self.darwin:
                found[role] = self.runner(["launchctl", "print", self._target(role)]).returncode == 0
            else:
                done = self.runner(["systemctl", "--user", "is-active", unit_name(self.project, role)])
                found[role] = done.stdout.strip() == "active"
        return found


class Supervisor:
    # Starts every role as its own session and restarts one that exits; stop ends each role's whole process group.
    def __init__(self, specs: list[RoleSpec], home: str, popen=subprocess.Popen, backoff: float = 2.0,
                 grace: float = 5.0):
        self.specs, self.home, self.popen, self.backoff, self.grace = specs, home, popen, backoff, grace
        self.children: dict[str, subprocess.Popen] = {}
        self.restarts: dict[str, int] = {}
        self.stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def _spawn(self, spec: RoleSpec) -> None:
        spec.log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        env = {"PATH": spec.path, "HOME": self.home, **CHILD_ENV}
        with open(spec.log, "ab") as log:
            self.children[spec.role] = self.popen(spec.argv, cwd=spec.cwd, env=env, stdin=subprocess.DEVNULL,
                                                  stdout=log, stderr=log, start_new_session=True)

    def start_all(self) -> None:
        for spec in self.specs:
            self._spawn(spec)
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def poll(self) -> list[str]:
        again = []
        for spec in self.specs:
            child = self.children.get(spec.role)
            if child is not None and child.poll() is not None and not self.stopping.is_set():
                self.restarts[spec.role] = self.restarts.get(spec.role, 0) + 1
                self._spawn(spec)
                again.append(spec.role)
        return again

    def _watch(self) -> None:
        while not self.stopping.wait(self.backoff / 4 if self.backoff else 0.05):
            self.poll()

    def status(self) -> dict[str, bool]:
        return {spec.role: (c := self.children.get(spec.role)) is not None and c.poll() is None for spec in self.specs}

    def wait(self) -> None:
        # Blocks the foreground command until stop_all runs or a signal arrives.
        while not self.stopping.wait(0.5):
            pass

    @staticmethod
    def _signal(child: subprocess.Popen, number: int) -> None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(child.pid, number)
            else:
                child.terminate()  # Windows: the child is the whole group the supervisor started
        except (ProcessLookupError, PermissionError):
            pass

    def stop_all(self) -> None:
        self.stopping.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        for child in reversed(list(self.children.values())):
            self._signal(child, signal.SIGTERM)
        deadline = time.monotonic() + self.grace
        for child in self.children.values():
            try:
                child.wait(max(deadline - time.monotonic(), 0.05))
            except subprocess.TimeoutExpired:
                self._signal(child, signal.SIGKILL)
                child.wait()
        self.children.clear()
