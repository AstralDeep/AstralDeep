# Runs a deployment as host processes from an installed node pack: it copies Vessel's code beside the pack's code and
# checks the pack holds what that code needs, then writes each role's private environment file and starts the roles.
# units.py renders the systemd and launchd units and the foreground supervisor that start and stop them.

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

from . import units
from .bundle import InstalledBundle
from .compose import write_secrets
from .deployment import Deployment, size_bytes

RUNTIME = ".runtime"
CODE = "vessel"
EXECUTOR_PORT = 8700
SANDBOX_FIRST_PORT = 8710
EGRESS_PORT = 8080
EGRESS_PROXY_PORT = 3128
HOST_ONLY = frozenset({"packaging"})
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "example", "config")
RUN_PY = '''# Starts one Vessel role on the pack interpreter with only this folder on the import path.
# The role's settings come from a private environment file named on the command line, never from arguments.
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROLES = {"node", "executor", "sandbox", "egress", "probe"}


def load(path):
    with open(path) as handle:
        for line in handle.read().splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and not key.startswith("#"):
                os.environ[key.strip()] = value.strip().strip("'\\"")


def main(argv):
    role = argv[1].split("-", 1)[0] if len(argv) > 1 else ""
    if role not in ROLES:
        raise SystemExit("usage: run.py node|executor|sandbox-NAME|egress --env-file FILE [role arguments]")
    rest = argv[2:]
    sys.dont_write_bytecode = True
    sys.path.insert(0, HERE)
    if "--env-file" in rest:
        load(rest[rest.index("--env-file") + 1])
    if role == "node":
        launcher = os.path.join(os.environ["VESSEL_PACK_ROOT"], "launcher", "astral_node_launch.py")
        sys.argv = [launcher, *rest]
        runpy.run_path(launcher, run_name="__main__")
        return
    if role == "egress":
        runpy.run_module("egress.main", run_name="__main__")
        return
    if role == "probe":
        import json
        from astralvessel import execmode
        for line in execmode.agent_probe():
            print(json.dumps(line))
        return
    sys.argv = ["astralvessel", role]
    from astralvessel import cli
    cli.main()


main(sys.argv)
'''


class RuntimeProblem(Exception):
    pass


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def run_command(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, text=True, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def vessel_requirements() -> list[Requirement]:
    return [Requirement(text) for text in (metadata.requires("astralvessel") or []) if "extra ==" not in text]


def egress_requirements(root: Path) -> list[Requirement]:
    lines = (root / "requirements.txt").read_text().splitlines()
    return [Requirement(line) for line in lines if line.strip() and not line.startswith("#")]


def dependency_problems(bundle: InstalledBundle, required: list[Requirement], role: str) -> list[str]:
    have = bundle.distributions
    problems = []
    for need in required:
        name = need.name.lower().replace("_", "-")
        if name in HOST_ONLY:
            continue
        if name not in have:
            problems.append(f"{role} needs {need.name}, and the pack has none")
        elif not need.specifier.contains(have[name], prereleases=True):
            problems.append(f"{role} needs {need.name}{need.specifier}, and the pack has {have[name]}")
    return problems


def ports_of(deployment: Deployment) -> dict[str, int]:
    sandboxes = {f"sandbox-{a}": SANDBOX_FIRST_PORT + i for i, a in enumerate(deployment.agents)}
    return {"node": deployment.node.port, "executor": EXECUTOR_PORT, "egress": EGRESS_PORT, **sandboxes}


def roles_of(deployment: Deployment) -> list[str]:
    roles = ["node"]
    if deployment.egress.upstream_url is not None:
        roles.append("egress")
    if deployment.agents:
        roles += [f"sandbox-{a}" for a in deployment.agents] + ["executor"]
    return roles


@dataclass
class Layout:
    folder: Path

    @property
    def runtime(self) -> Path:
        return self.folder / RUNTIME

    @property
    def code(self) -> Path:
        return self.runtime / CODE

    @property
    def node_data(self) -> Path:
        return self.runtime / "node-data"

    @property
    def executor_data(self) -> Path:
        return self.runtime / "executor"

    @property
    def logs(self) -> Path:
        return self.runtime / "logs"

    @property
    def generated(self) -> Path:
        return self.folder / ".generated"

    def exec_data(self, agent_id: str) -> Path:
        return self.runtime / "exec" / agent_id


@dataclass
class Runtime:
    deployment: Deployment
    folder: Path
    bundle: InstalledBundle
    runner: Runner = run_command
    platform: str = sys.platform
    unit_dir: Path | None = None
    out: Callable[[str], None] = print
    user: str = field(default_factory=lambda: getpass.getuser())
    uid: int = field(default_factory=lambda: os.getuid() if hasattr(os, "getuid") else 0)
    supervisor: units.Supervisor | None = None

    def __post_init__(self) -> None:
        self.layout = Layout(self.folder)

    @property
    def project(self) -> str:
        return self.deployment.node.project

    @property
    def ports(self) -> dict[str, int]:
        return ports_of(self.deployment)

    @property
    def gateway(self) -> bool:
        return self.deployment.egress.upstream_url is not None

    @property
    def roles(self) -> list[str]:
        return roles_of(self.deployment)

    def check_dependencies(self) -> None:
        here = Path(__file__).resolve().parent.parent
        problems = dependency_problems(self.bundle, vessel_requirements(), "Vessel")
        if self.gateway:
            problems += dependency_problems(self.bundle, egress_requirements(here / "egress"), "the egress")
        if problems:
            raise RuntimeProblem("this node pack cannot run Vessel: " + "; ".join(problems))

    def copy_code(self) -> None:
        here = Path(__file__).resolve().parent
        fresh = self.layout.runtime / f"{CODE}.new"
        shutil.rmtree(fresh, ignore_errors=True)
        fresh.mkdir(parents=True)
        shutil.copytree(here, fresh / "astralvessel", ignore=COPY_IGNORE)
        shutil.copytree(here.parent / "egress", fresh / "egress", ignore=COPY_IGNORE)
        if not self.bundle.astral_exec.is_dir():
            raise RuntimeProblem("this node pack holds no astral_exec, so it cannot confine agent tools")
        shutil.copytree(self.bundle.astral_exec, fresh / "astral_exec", ignore=COPY_IGNORE)
        (fresh / "run.py").write_text(RUN_PY)
        old = self.layout.runtime / f"{CODE}.old"
        shutil.rmtree(old, ignore_errors=True)
        if self.layout.code.exists():
            self.layout.code.rename(old)
        fresh.rename(self.layout.code)
        shutil.rmtree(old, ignore_errors=True)

    def _private(self, path: Path, values: dict[str, str]) -> None:
        text = "".join(f"{key}={value}\n" for key, value in values.items())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        path.chmod(0o600)

    def _env_values(self, path: Path) -> dict[str, str]:
        found = {}
        for line in path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                found[key] = value
        return found

    def environment(self, role: str, master: str) -> dict[str, str]:
        gen, node = self.layout.generated, self.deployment.node
        ports = self.ports
        pack = str(self.bundle.root)
        base = {"VESSEL_PACK_ROOT": pack}
        if role == "node":
            return {**base, **self._env_values(gen / "node.env"), **node_environment(self), **(
                self._env_values(gen / "node-executor.env") if self.deployment.agents else {})}
        if role == "egress":
            key = self.deployment.egress.upstream_key
            secret = os.environ.get(key[4:], "") if key.startswith("env:") else key
            if not secret:
                raise RuntimeProblem(f"the model key {key} is not set in the environment")
            return {**base, **self._env_values(gen / "egress.env"), "EGRESS_HOST": "127.0.0.1",
                    "EGRESS_PORT": str(ports["egress"]), "EGRESS_PROXY_PORT": str(EGRESS_PROXY_PORT),
                    "LLM_BASE_URL": self.deployment.egress.upstream_url or "", "LLM_API_KEY": secret}
        if role == "executor":
            return {**base, **self._env_values(gen / "executor.env"), "VESSEL_DEPLOYMENT": str(self.deployment.path),
                    "VESSEL_SANDBOX_TOKEN": master, "VESSEL_HOST": "127.0.0.1", "VESSEL_PORT": str(EXECUTOR_PORT),
                    "VESSEL_SANDBOX_URL_TEMPLATE": "http://127.0.0.1:{port}",
                    "VESSEL_SANDBOX_PORTS": json.dumps({a: ports[f"sandbox-{a}"] for a in self.deployment.agents}),
                    "VESSEL_NODE_URL": f"http://127.0.0.1:{node.port}",
                    "VESSEL_EXECUTOR_DIR": str(self.layout.executor_data)}
        agent_id = role.removeprefix("sandbox-")
        agent = self.deployment.agents[agent_id]
        exec_data = self.layout.exec_data(agent_id)
        values = {**base, **self._env_values(gen / f"sandbox-{agent_id}.env"),
                  "VESSEL_PACKAGE": str(agent.host_package), "VESSEL_HOST": "127.0.0.1",
                  "VESSEL_PORT": str(ports[role]), "VESSEL_PROFILE_HASH": agent.fingerprint,
                  "VESSEL_TURN_DISK": str(agent.profile.turn_bytes), "VESSEL_USER_DISK": str(agent.profile.user_bytes),
                  "VESSEL_MODE": "process", "VESSEL_EXEC_DATA": str(exec_data),
                  "VESSEL_NODE_DATA_DIR": str(self.layout.node_data),
                  "VESSEL_MEMORY_BYTES": str(size_bytes(agent.profile.memory)),
                  "VESSEL_PIDS": str(agent.profile.pids)}
        sources = {m.target: self.mount_source(m.source) for m in agent.profile.mounts}
        if sources:
            values["VESSEL_READS"] = os.pathsep.join(sources.values())
        workdir = sources.get(agent.package.manifest.sandbox.workdir)
        if workdir:
            values["VESSEL_WORKDIR"] = workdir
        rg = self.bundle.ripgrep
        if rg is not None:
            values["VESSEL_RG_BIN"] = str(rg)
        return values

    def mount_source(self, source: str) -> str:
        if source.startswith("volume:"):
            raise RuntimeProblem(f"{source} is a Docker volume; process mode reads a folder on this machine")
        if source.startswith("env:"):
            value = os.environ.get(source[4:])
            if not value:
                raise RuntimeProblem(f"{source[4:]} is not set in the environment")
            source = value
        return str((self.deployment.path.parent / source).resolve())

    def prepare(self, master_token: str) -> None:
        if len(master_token) < 16:
            raise RuntimeProblem("VESSEL_SANDBOX_TOKEN must be at least 16 characters")
        self.check_dependencies()
        layout = self.layout
        for folder in (layout.runtime, layout.node_data, layout.executor_data, layout.logs):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            folder.chmod(0o700)
        for agent_id in self.deployment.agents:
            (layout.exec_data(agent_id) / "exec").mkdir(mode=0o700, parents=True, exist_ok=True)
        self.copy_code()
        write_secrets(self.deployment, layout.generated, master_token)
        for role in self.roles:
            name = "node-launch.env" if role == "node" else f"{role}-run.env"
            self._private(layout.generated / name, self.environment(role, master_token))

    def env_file(self, role: str) -> Path:
        return self.layout.generated / ("node-launch.env" if role == "node" else f"{role}-run.env")

    def spec(self, role: str) -> units.RoleSpec:
        run_py = str(self.layout.code / "run.py")
        argv = [str(self.bundle.python), "-I", "-B", run_py, role]
        file = str(self.env_file(role))
        if role == "node":
            argv += ["--env-file", file, "--host", self.deployment.node.bind, "--port", str(self.deployment.node.port),
                     "--log-dir", str(self.layout.logs / "node")]
            if self.deployment.node.bind not in ("127.0.0.1", "::1", "localhost"):
                argv.append("--allow-remote")
        else:
            argv += ["--env-file", file]
        limits = None
        if role.startswith("sandbox-"):
            profile = self.deployment.agents[role.removeprefix("sandbox-")].profile
            limits = units.Limits(size_bytes(profile.memory), profile.pids, profile.cpus)
        return units.RoleSpec(role, argv, self.folder, self.layout.logs / f"{role}.log", str(self.bundle.root / "bin"),
                              limits)

    def specs(self) -> list[units.RoleSpec]:
        return [self.spec(role) for role in self.roles]

    def start(self, *, foreground: bool = False) -> None:
        if foreground:
            self.supervisor = units.Supervisor(self.specs(), home=str(Path.home()))
            self.supervisor.start_all()
            return
        manager = units.Manager(self.platform, self.project, self.runner, self.unit_dir, self.uid)
        if self.platform != "darwin":
            self._linger_note()
        manager.install(self.specs())
        manager.start(self.roles)

    def _linger_note(self) -> None:
        done = self.runner(["loginctl", "show-user", self.user, "-p", "Linger", "--value"])
        if done.returncode == 0 and done.stdout.strip() == "no":
            self.out("note: user services stop when you log out. To keep the node running, run once as an "
                     f"administrator: loginctl enable-linger {self.user}")

    def stop(self) -> None:
        if self.supervisor is not None:
            self.supervisor.stop_all()
            return
        units.Manager(self.platform, self.project, self.runner, self.unit_dir, self.uid).stop(self.roles)

    def status(self) -> dict[str, bool]:
        if self.supervisor is not None:
            return self.supervisor.status()
        return units.Manager(self.platform, self.project, self.runner, self.unit_dir, self.uid).status(self.roles)


def node_environment(runtime: Runtime) -> dict[str, str]:
    # The process twin of compose.node_environment: host paths under .runtime/node-data and loopback service URLs.
    node, data = runtime.deployment.node, runtime.layout.node_data
    public = (node.public_url or f"http://127.0.0.1:{node.port}").rstrip("/")
    env = {
        "ASTRAL_NODE_PROFILE": "personal",
        "ASTRAL_NODE_DATA_DIR": str(data / "node"),
        "ATTACHMENT_UPLOAD_ROOT": str(data / "node" / "blobs"),
        "PERSONAL_AGENT_ARTIFACT_ROOT": str(data / "node" / "agents"),
        "ORCHESTRATOR_HOST": node.bind,
        "ORCHESTRATOR_PORT": str(node.port),
        "PUBLIC_BASE_URL": public,
        "BACKEND_PUBLIC_URL": public,
        "FF_A2A_SERVER": "true",
        "PYTHON_DOTENV_DISABLED": "1",
    }
    merged = {**node.environment, **env}
    if runtime.deployment.agents:
        merged["ASTRAL_VESSEL_EXECUTOR_URL"] = f"http://127.0.0.1:{EXECUTOR_PORT}"
        merged["ASTRAL_VESSEL_EXECUTOR_AGENT_TOKENS_FILE"] = str(runtime.layout.generated / "executor-agents.json")
    return merged
