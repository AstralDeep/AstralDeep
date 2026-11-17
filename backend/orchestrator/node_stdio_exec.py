"""The node's real sandbox backend for local stdio MCP servers: it fills the launch policy's workspace, loads and selects it through astral_exec, writes the credential environment to a private file and starts the server through the run.py launch command with its own stdio piped back.
node_stdio_launcher.py drives it through the SandboxBackend protocol, and node_mesh.py and institution_meshes.py install it only when this machine can confine a tool.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional

from astral_exec import ExecRefused, Policy, PolicyError, Refusal, paths, probe, select
from orchestrator.node_stdio_launcher import LaunchSpec

logger = logging.getLogger("Orchestrator.NodeStdioExec")

RUN_PY = Path(__file__).resolve().parents[1] / "astral_exec" / "run.py"
STOP_SECONDS = 5.0
LAUNCHER_ENV = {"LC_ALL": "C.UTF-8"}


class ExecRefusal(OSError):
    pass


def _canonical(path: str, platform: str) -> str:
    return paths.canonical_path(path, platform)


def _document(spec: LaunchSpec, workspace: str, platform: str) -> Dict[str, Any]:
    document = json.loads(json.dumps(spec.policy))
    reads = {_canonical(path, platform) for path in document.get("reads", [])}
    if os.path.isabs(spec.command):
        reads.add(os.path.dirname(_canonical(spec.command, platform)))
    document["reads"] = sorted(reads)
    document["workspace"] = workspace
    hosts = {(entry["host"], entry["port"]) for entry in document.get("hosts", [])}
    document["hosts"] = [{"host": host, "port": port} for host, port in sorted(hosts)]
    return document


class ExecProcess:
    def __init__(self, process: subprocess.Popen, scratch: List[str]):
        self._process = process
        self._scratch = scratch
        self.stdin: BinaryIO = process.stdin  # type: ignore[assignment]
        self.stdout: BinaryIO = process.stdout  # type: ignore[assignment]

    def poll(self) -> Optional[int]:
        return self._process.poll()

    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(STOP_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except OSError:
                    self._process.kill()
                self._process.wait()
        for pipe in (self.stdin, self.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        for path in self._scratch:
            shutil.rmtree(path, ignore_errors=True)


class ExecSandbox:
    def __init__(self, data_dir: Path, *, home: Optional[str] = None, scratch_root: Optional[str] = None):
        self._data_path = Path(data_dir)
        self._data_dir = ""
        self._home = home or str(Path.home())
        self._scratch_root = scratch_root

    def spawn(self, spec: LaunchSpec) -> ExecProcess:
        facts = probe()
        platform = facts["platform"]
        scratch: List[str] = []
        try:
            self._data_dir = _canonical(str(self._data_path), platform)
            control = tempfile.mkdtemp(prefix="astral-stdio-", dir=self._scratch_root)
            scratch.append(control)
            workspace = tempfile.mkdtemp(prefix="astral-stdio-ws-", dir=self._scratch_root)
            scratch.append(workspace)
            workspace = _canonical(workspace, platform)
            policy = Policy.load(
                json.dumps(_document(spec, workspace, platform)).encode(),
                platform,
                self._data_dir,
                _canonical(self._home, platform),
            )
            chosen = select(policy, facts, self._data_dir)
            if isinstance(chosen, Refusal):
                raise ExecRefusal(chosen.code)
            policy_file = os.path.join(control, "policy.json")
            env_file = os.path.join(control, "env.json")
            for path, body in ((policy_file, policy.canonical_bytes()), (env_file, json.dumps(dict(spec.env)).encode())):
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(body)
            argv = [
                sys.executable, "-I", "-B", str(RUN_PY), "launch",
                "--policy", policy_file, "--data-dir", self._data_dir,
                "--home", _canonical(self._home, platform), "--env-file", env_file,
                "--", spec.command, *spec.args,
            ]
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(LAUNCHER_ENV),
                cwd=control,
                start_new_session=True,
            )
        except (ExecRefused, PolicyError) as exc:
            for path in scratch:
                shutil.rmtree(path, ignore_errors=True)
            raise ExecRefusal(getattr(exc, "code", "exec.refused")) from exc
        except BaseException:
            for path in scratch:
                shutil.rmtree(path, ignore_errors=True)
            raise
        logger.info("node_stdio_exec_started id=%s digest=%s", spec.id, policy.digest())
        return ExecProcess(process, scratch)


def default_backend(data_dir: Path) -> Optional[ExecSandbox]:
    try:
        facts = probe()
        platform = facts["platform"]
        with tempfile.TemporaryDirectory(prefix="astral-stdio-probe-") as probe_dir, tempfile.TemporaryDirectory(
            prefix="astral-stdio-probe-data-"
        ) as other:
            workspace = _canonical(probe_dir, platform)
            document = {
                "v": 1, "reads": [], "workspace": workspace, "hosts": [],
                "limits": {"memory_bytes": 1 << 28, "processes": 1, "open_files": 64, "file_bytes": 1 << 20},
                "time": {"wall_ms": 1000},
            }
            canonical_other = _canonical(other, platform)
            policy = Policy.load(json.dumps(document).encode(), platform, canonical_other, _canonical(str(Path.home()), platform))
            chosen = select(policy, facts, canonical_other)
    except (ExecRefused, PolicyError, OSError, ValueError, KeyError):
        return None
    return None if isinstance(chosen, Refusal) else ExecSandbox(data_dir)
