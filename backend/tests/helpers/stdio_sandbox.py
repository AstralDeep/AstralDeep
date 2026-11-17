"""A test-only sandbox backend that runs the stdio fixture as a plain child process with exactly the environment the launch record names, and records every spec it was given.
Production has no backend, so this lives under tests and implements the same SandboxBackend protocol a real backend will.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from orchestrator.node_stdio_launcher import LaunchSpec

BACKEND_DIR = Path(__file__).resolve().parents[2]
FIXTURE_MODULE = "tests.fixtures.connections.stdio_server"


class Child:
    def __init__(self, process: subprocess.Popen):
        self._process = process
        self.stdin = process.stdin
        self.stdout = process.stdout

    def poll(self) -> Optional[int]:
        return self._process.poll()

    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
        self._process.wait()
        for pipe in (self.stdin, self.stdout):
            try:
                pipe.close()
            except OSError:
                pass


class FixtureSandbox:
    def __init__(self, *, argv: Optional[List[str]] = None, fail: bool = False):
        self.specs: List[LaunchSpec] = []
        self.children: List[Child] = []
        self._argv = argv
        self._fail = fail

    def spawn(self, spec: LaunchSpec) -> Child:
        self.specs.append(spec)
        if self._fail:
            raise OSError("no sandbox")
        process = subprocess.Popen(
            self._argv or [sys.executable, "-m", FIXTURE_MODULE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(spec.env),
            cwd=BACKEND_DIR,
        )
        child = Child(process)
        self.children.append(child)
        return child
