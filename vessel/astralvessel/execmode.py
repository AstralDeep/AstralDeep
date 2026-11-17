# Builds the astral_exec policy for one sandboxed call in process mode and the launcher command that runs it.
# tools.py runs the command under its own supervision, and sandbox.py reports the backend this module selects.

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REFUSED_STATUS = 125
FILE_BYTES_DEFAULT = 1 << 30
MIN_MEMORY, MAX_MEMORY = 16 * 1024 * 1024, 64 * 1024**3
_facts_cache: dict[str, Mapping[str, Any]] = {}


class ProcessRefused(Exception):
    def __init__(self, code: str, message: str, reason: str | None = None):
        super().__init__(message)
        self.code, self.reason = code, reason
        self.text = f"{code} ({reason}): {message}" if reason else f"{code}: {message}"


def system_facts() -> Mapping[str, Any]:
    from astral_exec.facts import probe
    if "facts" not in _facts_cache:
        _facts_cache["facts"] = probe()
    return _facts_cache["facts"]


@dataclass(frozen=True)
class ProcessExec:
    # data_dir holds the launcher's private files and every call's workspace under exec/; node_data is the node's own
    # data and must never sit under anything a call can read.
    data_dir: str
    node_data: str
    home: str
    python: str
    run_py: str
    reads: tuple[str, ...] = ()
    path_dirs: tuple[str, ...] = ()
    memory_bytes: int = 2 * 1024**3
    pids: int = 512
    rg: str | None = None
    facts: Callable[[], Mapping[str, Any]] = system_facts

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> ProcessExec:
        import astral_exec
        env = os.environ if env is None else env
        pack = env.get("VESSEL_PACK_ROOT")
        reads = tuple(str(Path(pack) / part) for part in ("python", "bin")) if pack else ()
        dirs = (str(Path(pack) / "python" / "bin"), str(Path(pack) / "bin")) if pack else ()
        return cls(data_dir=env["VESSEL_EXEC_DATA"], node_data=env["VESSEL_NODE_DATA_DIR"],
                   home=str(Path.home()), python=sys.executable,
                   run_py=str(Path(astral_exec.__file__).resolve().parent / "run.py"), reads=reads, path_dirs=dirs,
                   memory_bytes=int(env.get("VESSEL_MEMORY_BYTES") or cls.memory_bytes),
                   pids=int(env.get("VESSEL_PIDS") or cls.pids), rg=env.get("VESSEL_RG_BIN") or None)

    def canonical(self, path: str, platform: str) -> str:
        from astral_exec.paths import canonical_path
        return canonical_path(path, platform)

    def _document(self, platform: str, readable: tuple[str, ...], workspace: str, timeout: int,
                  max_file_bytes: int, processes: int | None = None) -> dict[str, Any]:
        reads = set()
        for root in (*readable, *self.reads):
            try:
                reads.add(self.canonical(root, platform))
            except (OSError, ValueError):
                continue
        node = self.canonical(self.node_data, platform) if os.path.exists(self.node_data) else self.node_data
        reads.discard(workspace)  # a turn's own folder is its workspace, which the policy grants separately
        for root in reads:
            if root == node or root.startswith(node.rstrip("/") + "/") or node.startswith(root.rstrip("/") + "/"):
                raise ProcessRefused("exec.policy_overlaps_data", "A read root contains the node data directory.")
        return {"v": 1, "reads": sorted(reads), "workspace": workspace, "hosts": [],
                "limits": {"memory_bytes": min(max(self.memory_bytes, MIN_MEMORY), MAX_MEMORY),
                           "processes": processes or min(max(self.pids, 1), 4096), "open_files": 1024,
                           "file_bytes": max(max_file_bytes or FILE_BYTES_DEFAULT, 4096)},
                "time": {"wall_ms": max(timeout, 1) * 1000}}

    def load(self, readable: tuple[str, ...], workspace: str, timeout: int, max_file_bytes: int):
        from astral_exec import ContextError, ExecRefused, Policy, PolicyError
        facts = self.facts()
        platform = facts["platform"]
        try:
            data_dir = self.canonical(self.data_dir, platform)
            home = self.canonical(self.home, platform)
            document = self._document(platform, readable, self.canonical(workspace, platform), timeout,
                                      max_file_bytes)
            return Policy.load(document, platform, data_dir, home), facts, data_dir
        except ExecRefused as exc:
            raise ProcessRefused(exc.code, exc.refusal.message, exc.refusal.reason) from None
        except (PolicyError, ContextError, OSError, ValueError) as exc:
            raise ProcessRefused("exec.launch_failed", f"the call's policy was not accepted ({exc})") from None

    def backend(self, workspace: str | None = None):
        # The backend this machine offers for an empty policy; None and a refusal when there is none.
        from astral_exec import select
        probe_dir = Path(self.data_dir) / "exec" / "probe"
        probe_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            policy, facts, data_dir = self.load((), str(probe_dir), 1, 0)
        except ProcessRefused as exc:
            return None, exc
        chosen = select(policy, facts, data_dir)
        if hasattr(chosen, "code"):
            return None, ProcessRefused(chosen.code, chosen.message, chosen.reason)
        return chosen, None

    def command(self, argv: list[str], env: Mapping[str, str], readable: tuple[str, ...], workspace: str,
                timeout: int, max_file_bytes: int) -> tuple[list[str], Callable[[], None]]:
        # Selects the backend first so a refusal never reaches the launcher; the policy and environment go in
        # private files, and the returned cleanup removes them.
        from astral_exec import select
        policy, facts, data_dir = self.load(readable, workspace, timeout, max_file_bytes)
        chosen = select(policy, facts, data_dir)
        if hasattr(chosen, "code"):
            raise ProcessRefused(chosen.code, chosen.message, chosen.reason)
        folder = Path(data_dir) / "launch"
        folder.mkdir(mode=0o700, exist_ok=True)
        token = secrets.token_hex(8)
        policy_file, env_file = folder / f"policy-{token}.json", folder / f"env-{token}.json"
        for path, body in ((policy_file, policy.canonical_bytes()), (env_file, json.dumps(dict(env)).encode())):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)

        def cleanup() -> None:
            for path in (policy_file, env_file):
                path.unlink(missing_ok=True)

        launch = [self.python, "-I", "-B", self.run_py, "launch", "--policy", str(policy_file), "--data-dir", data_dir,
                  "--home", self.canonical(self.home, facts["platform"]), "--env-file", str(env_file), "--", *argv]
        return launch, cleanup

    def child_path(self) -> str:
        return os.pathsep.join([*self.path_dirs, "/usr/local/bin", "/usr/bin", "/bin"])

    def resolve(self, name: str) -> str:
        if os.path.isabs(name):
            return name
        return shutil.which(name, path=self.child_path()) or name


def refusal_text(code: int, output: str) -> str | None:
    # The launcher reports a refusal or a limit as one JSON line on stderr and exit status 125.
    if code != REFUSED_STATUS:
        return None
    for line in reversed(output.strip().splitlines()):
        try:
            body = json.loads(line)
        except ValueError:
            continue
        if isinstance(body, dict) and "code" in body:
            return f"{body['code']}: {body.get('message') or ''}".strip()
    return None
