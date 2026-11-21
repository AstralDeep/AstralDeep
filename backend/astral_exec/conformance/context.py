"""The conformance harness around one backend: the temporary directories and canary files, the policy builder, the launch of the probe tool through astral_exec.launch and the parsed result of each launch.
cases.py asks it for runs, and each run is isolated by its own workspace so a case never sees another case's files.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import sysconfig
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astral_exec import Policy, launch
from astral_exec.constants import LINUX_BASE
from astral_exec.fold import contains
from astral_exec.launch import Outcome
from .fixtures import Fixtures

PROBE_SOURCE = (Path(__file__).resolve().parent / "probe_tool.py").read_text(
    encoding="utf-8"
)
SECRET_NAME = "ASTRAL_CONFORMANCE_SECRET"
LIMITS = {
    "memory_bytes": 1 << 30,
    "processes": 1,
    "open_files": 256,
    "file_bytes": 1 << 24,
}
PASSED_ENV = {"CONFORMANCE_PASSED": "1"}
SKIPPED_LIBRARIES = ("_test", "_tkinter", "xx", "_ctypes_test")
LOADERS = (
    "/lib64/ld-linux-x86-64.so.2",
    "/lib/ld-linux-aarch64.so.1",
    "/lib/ld-linux-x86-64.so.2",
)
DETAIL_LIMIT = 300


class Failed(Exception):
    pass


class Refused(Exception):
    pass


@dataclass
class Run:
    outcome: Outcome
    checks: dict[str, Any] = field(default_factory=dict)
    extra: list[str] = field(default_factory=list)
    workspace: str = ""
    seen: dict[str, int] = field(default_factory=dict)
    upstream: Any = None

    @property
    def completed(self) -> bool:
        return self.outcome.code is None and self.outcome.exit_code == 0

    def describe(self) -> str:
        tail = self.outcome.stderr.decode("utf-8", "replace")[-DETAIL_LIMIT:]
        return f"code={self.outcome.code} exit={self.outcome.exit_code} stderr={tail!r}"

    def require(self) -> Run:
        if not self.completed:
            raise Failed(f"the probe tool did not complete: {self.describe()}")
        return self

    def get(self, check: str) -> Any:
        self.require()
        if check not in self.checks:
            raise Failed(f"the probe tool reported no {check}")
        return self.checks[check]


def parse(stdout: bytes) -> tuple[dict[str, Any], list[str]]:
    checks: dict[str, Any] = {}
    extra: list[str] = []
    for line in stdout.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
            checks[row["check"]] = row["outcome"]
        except (ValueError, KeyError, TypeError):
            extra.append(line)
    return checks, extra


def process_table() -> list[tuple[int, str]]:
    if sys.platform == "linux":
        found = []
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                try:
                    with open(f"/proc/{entry}/cmdline", "rb") as handle:
                        text = (
                            handle.read()
                            .replace(b"\0", b" ")
                            .decode("utf-8", "replace")
                        )
                    found.append((int(entry), text))
                except OSError:
                    continue
        return found
    listing = subprocess.run(
        ["/bin/ps", "-axww", "-o", "pid=,command="],
        capture_output=True,
        timeout=30,
        check=True,
    )
    rows = (
        line.strip().split(None, 1)
        for line in listing.stdout.decode("utf-8", "replace").splitlines()
    )
    return [
        (int(row[0]), row[1] if len(row) > 1 else "")
        for row in rows
        if row and row[0].isdigit()
    ]


def processes_matching(token: str) -> list[int]:
    return [
        pid
        for pid, command in process_table()
        if token in command and pid != os.getpid()
    ]


class Context:
    def __init__(
        self, facts: Mapping[str, Any], workdir: str, fixtures: Fixtures
    ) -> None:
        self.facts = facts
        self.platform = facts["platform"]
        self.workdir = workdir
        self.fixtures = fixtures
        self.data = f"{workdir}/data"
        self.home = f"{workdir}/home"
        self.outside = f"{workdir}/outside"
        self.readable = f"{workdir}/readable"
        self.python = os.path.realpath(sys.executable)
        self._counter = 0
        self._memo: dict[str, Run] = {}
        self._build()

    @property
    def linux(self) -> bool:
        return self.platform == "linux"

    def _build(self) -> None:
        for directory in (
            self.data,
            f"{self.data}/exec",
            f"{self.home}/.ssh",
            f"{self.home}/Library/Keychains",
            self.outside,
            self.readable,
        ):
            os.makedirs(directory, exist_ok=True)
        canaries = {
            f"{self.data}/node.db": "node data canary",
            f"{self.home}/.zshrc": "shell startup canary",
            f"{self.home}/.ssh/id_ed25519": "key canary",
            f"{self.home}/Library/Keychains/login.keychain-db": "keychain canary",
            f"{self.outside}/secret.txt": "outside canary",
            f"{self.readable}/readable.txt": "readable canary",
        }
        for path, text in canaries.items():
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)

    def denied_files(self) -> frozenset[str]:
        base = {"EPERM", "EACCES"}
        return frozenset(base | {"ENOENT", "EROFS"} if self.linux else base)

    def interpreter_reads(self) -> list[str]:
        prefix = os.path.realpath(sys.base_prefix)
        if self.linux and any(contains(root, prefix, "linux") for root in LINUX_BASE):
            return []
        return [prefix]

    def workspace(self) -> str:
        self._counter += 1
        path = f"{self.data}/exec/call-{self._counter}"
        os.mkdir(path)
        return path

    def policy(
        self,
        workspace: str,
        *,
        reads: Sequence[str] = (),
        hosts: Sequence[Mapping[str, Any]] = (),
        limits: Mapping[str, int] | None = None,
        wall_ms: int = 60000,
    ) -> Policy:
        document = {
            "v": 1,
            "reads": sorted({*self.interpreter_reads(), *reads}),
            "workspace": workspace,
            "hosts": sorted(hosts, key=lambda entry: (entry["host"], entry["port"])),
            "limits": {**LIMITS, **(limits or {})},
            "time": {"wall_ms": wall_ms},
        }
        return Policy.load(document, self.platform, self.data, self.home)

    def config(
        self, mode: str, workspace: str, extra: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "mode": mode,
            "workspace": workspace,
            "outside": self.outside,
            "readable": self.readable,
            "home": self.home,
            "data": self.data,
            "tcp_port": self.fixtures.tcp_port,
            "echo_port": self.fixtures.echo_port,
            "udp_port": self.fixtures.udp_port,
            "unix_path": self.fixtures.unix_path,
            "cert": str(self.fixtures.cert) if self.fixtures.cert else None,
            "python": self.python,
            "loader": self.loader(),
            "library": self.library(),
            "parent_keys": sorted(os.environ),
            "passed": sorted(PASSED_ENV),
            **extra,
        }

    def launch(
        self,
        policy: Policy,
        mode: str,
        workspace: str,
        *,
        extra: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        options: Mapping[str, Any] | None = None,
        facts: Mapping[str, Any] | None = None,
    ) -> Run:
        argv = [
            self.python,
            "-I",
            "-B",
            "-S",
            "-c",
            PROBE_SOURCE,
            json.dumps(self.config(mode, workspace, extra or {})),
        ]
        child_env = {**PASSED_ENV, **(env or {})}
        before = self.fixtures.snapshot()
        if options is None and facts is None:
            outcome = launch.run(policy, argv, child_env)
        else:
            outcome = launch._run(
                policy, argv, child_env, facts=facts, proxy_options=options
            )
        after = self.fixtures.snapshot()
        checks, extra_lines = parse(outcome.stdout)
        seen = {name: after[name] - before[name] for name in after}
        return Run(outcome, checks, extra_lines, workspace, seen)

    def run(
        self,
        mode: str,
        *,
        reads: Sequence[str] = (),
        hosts: Sequence[Mapping[str, Any]] = (),
        limits: Mapping[str, int] | None = None,
        wall_ms: int = 60000,
        extra: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        options: Mapping[str, Any] | None = None,
        facts: Mapping[str, Any] | None = None,
        prepare: Callable[[str], None] | None = None,
    ) -> Run:
        workspace = self.workspace()
        if prepare is not None:
            prepare(workspace)
        policy = self.policy(
            workspace, reads=reads, hosts=hosts, limits=limits, wall_ms=wall_ms
        )
        return self.launch(
            policy, mode, workspace, extra=extra, env=env, options=options, facts=facts
        )

    def control(self, mode: str, extra: Mapping[str, Any]) -> Run:
        workspace = self.workspace()
        argv = [
            self.python,
            "-I",
            "-B",
            "-S",
            "-c",
            PROBE_SOURCE,
            json.dumps(self.config(mode, workspace, extra)),
        ]
        done = subprocess.run(argv, capture_output=True, timeout=120, check=False)
        checks, extra_lines = parse(done.stdout)
        return Run(
            Outcome(None, done.returncode, done.stdout, done.stderr),
            checks,
            extra_lines,
            workspace,
        )

    def remembered(self, name: str, make) -> Run:
        if name not in self._memo:
            self._memo[name] = make()
        return self._memo[name]

    def library(self) -> str | None:
        folder = os.path.join(sysconfig.get_path("stdlib"), "lib-dynload")
        suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
        names = (
            sorted(name for name in os.listdir(folder) if name.endswith(suffix))
            if os.path.isdir(folder)
            else []
        )
        for name in names:
            if name.startswith(SKIPPED_LIBRARIES):
                continue
            path = os.path.realpath(os.path.join(folder, name))
            try:
                ctypes.CDLL(path)
            except OSError:
                continue
            return path
        return None

    def loader(self) -> str | None:
        return next((path for path in LOADERS if os.path.exists(path)), None)
