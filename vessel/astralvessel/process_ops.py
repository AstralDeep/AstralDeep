# The operator's commands for a deployment that runs as host processes: up installs the pack and starts the roles,
# check and down act on them, and pair reads the node's secret from its data folder.
# operate.py routes a command here when the deployment file says mode = "process", so Docker is never involved.

from __future__ import annotations

import argparse
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from . import bundle as bundles
from . import node
from .deployment import Deployment
from .probes import run_process_probes
from .runtime import Layout, Runtime, RuntimeProblem, ports_of, roles_of, run_command
from .stack import Stack
from .units import Manager

LEGACY_OWNER = "node.legacy_owner"
PIDFILE = "vessel.pid"


def bundle_for(args: argparse.Namespace, deployment: Deployment, root: Path | None = None) -> bundles.InstalledBundle:
    section = deployment.runtime
    try:
        platform = bundles.platform_name()
        location, sha = bundles.resolve(getattr(args, "bundle", None) or (section.bundle if section else None),
                                        getattr(args, "bundle_sha256", None)
                                        or (section.bundle_sha256 if section else None), platform)
        return bundles.install(location, sha, root, platform=platform)
    except bundles.BundleError as exc:
        raise SystemExit(str(exc)) from None


def _health(url: str) -> dict | None:
    try:
        response = httpx.get(url, timeout=5)
        return response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def role_state(runtime: Runtime) -> tuple[str, dict[str, bool]]:
    ports = runtime.ports
    state = node.health(runtime.deployment.node)
    ready: dict[str, bool] = {}
    for agent_id, agent in runtime.deployment.agents.items():
        body = _health(f"http://127.0.0.1:{ports[f'sandbox-{agent_id}']}/health") or {}
        ready[f"sandbox-{agent_id}"] = bool(body.get("confined")) and body.get("profile") == agent.fingerprint
    if runtime.deployment.agents:
        body = _health(f"http://127.0.0.1:{ports['executor']}/health") or {}
        ready["executor"] = body.get("ok") is True
    return state, ready


def log_tail(layout: Layout, role: str, lines: int = 30) -> str:
    try:
        return "\n".join((layout.logs / f"{role}.log").read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def wait_ready(runtime: Runtime, timeout: float, sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + timeout
    print("waiting for the node, the executor and every agent's sandbox", end="", flush=True)
    state, ready = "", {}
    while time.monotonic() < deadline:
        state, ready = role_state(runtime)
        if state == "ok" and all(ready.values()):
            print(" ready")
            return True
        dead = [role for role, up in runtime.status().items() if not up]
        if dead:
            print(f"\nstopped: {', '.join(dead)} is not running")
            for role in dead:
                print(f"--- {role} ---\n{log_tail(runtime.layout, role)}")
            return False
        print(".", end="", flush=True)
        sleep(1)
    late = ([] if state == "ok" else ["node"]) + [role for role, up in ready.items() if not up]
    print(f"\nnot ready after {timeout:.0f}s: {', '.join(late)}")
    for role in late:
        print(f"--- {role} ---\n{log_tail(runtime.layout, role)}")
    return False


def node_checks(deployment: Deployment, layout: Layout) -> list[str]:
    failed: list[str] = []

    def report(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  node: {name}" + (f" ({detail})" if detail and not ok else ""))
        if not ok:
            failed.append(name)

    state = node.health(deployment.node)
    report("answers /readyz", state == "ok", state)
    document = node.fetch_descriptor(deployment.node)
    problem = node.descriptor_problem(document)
    report("publishes a v2 descriptor with an Ed25519 key", problem is None, problem or "")
    report("carries no legacy owner", node.owner_binding(document).get("code") != LEGACY_OWNER,
           "its data belongs to an owner who signed in through an identity provider; nothing was changed or deleted")
    root = layout.node_data / "node"

    def mode(path: Path) -> str | None:
        try:
            return oct(stat.S_IMODE(path.stat().st_mode))
        except OSError:
            return None

    report("keeps its data directory private", mode(root) == "0o700", str(mode(root)))
    report("keeps its database in a SQLite file readable only by the node", mode(root / "plane.sqlite3") == "0o600",
           str(mode(root / "plane.sqlite3")))
    key = mode(root / "node-ed25519.pem")
    report("keeps its signing key readable only by the node", key in (None, "0o600"), str(key))
    return failed


def _signalled() -> threading.Event:
    stop = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stop.set())
    return stop


def hold_until_signalled(runtime: Runtime) -> None:
    _signalled().wait()


def up(args: argparse.Namespace, stack: Stack, deployment: Deployment, *,
       runtime_for: Callable[..., Runtime] = Runtime, hold: Callable[[Runtime], None] = hold_until_signalled,
       install_root: Path | None = None) -> None:
    stack.load_env()
    pack = bundle_for(args, deployment, install_root)
    runtime = runtime_for(deployment, stack.deployment.parent, pack)
    try:
        runtime.prepare(os.environ.get("VESSEL_SANDBOX_TOKEN", ""))
    except RuntimeProblem as exc:
        raise SystemExit(str(exc)) from None
    foreground = bool(getattr(args, "foreground", False))
    pidfile = runtime.layout.runtime / PIDFILE
    runtime.start(foreground=foreground)
    try:
        if foreground:
            pidfile.write_text(f"{os.getpid()}\n")
        if not wait_ready(runtime, args.timeout):
            raise SystemExit("the deployment is running but not ready; fix the problem above and run astralvessel up")
        report = run_process_probes(runtime)
        failed = node_checks(deployment, runtime.layout)
        print(f"{report.passed} probes passed, {len(report.failed)} failed, {report.skipped} skipped")
        if report.failed or failed:
            raise SystemExit("the deployment is running, but a check failed; it is not ready for use")
        print(f"\nready: node at {node.base_url(deployment.node)}\n  astralvessel pair\n"
              "  astralvessel ask \"your question\"")
        if foreground:
            hold(runtime)
    finally:
        if foreground:
            runtime.stop()
            pidfile.unlink(missing_ok=True)


def check(args: argparse.Namespace, stack: Stack, deployment: Deployment, *,
          install_root: Path | None = None) -> None:
    stack.load_env()
    pack = bundle_for(args, deployment, install_root)
    runtime = Runtime(deployment, stack.deployment.parent, pack)
    report = run_process_probes(runtime)
    failed = [] if args.isolation_only else node_checks(deployment, runtime.layout)
    print(f"{report.passed} probes passed, {len(report.failed)} failed, {report.skipped} skipped")
    if report.failed or failed:
        raise SystemExit(1)


def _supervisor_pid(layout: Layout) -> int | None:
    try:
        pid = int((layout.runtime / PIDFILE).read_text().strip())
        os.kill(pid, 0)
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except (OSError, ValueError):
        return None
    return pid if "astralvessel" in command or "vessel" in command else None


def down(args: argparse.Namespace, stack: Stack, deployment: Deployment, *, runner=run_command) -> None:
    layout = Layout(stack.deployment.parent)
    pid = _supervisor_pid(layout)
    if pid is not None:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
    manager = Manager(sys.platform, deployment.node.project, runner, None, os.getuid())
    if any(manager._file(role).exists() for role in roles_of(deployment)):
        manager.stop(roles_of(deployment))
    if args.volumes:
        shutil.rmtree(layout.node_data, ignore_errors=True)  # only the node's data; keys and agents stay


def node_offer(deployment: Deployment, layout: Layout) -> dict:
    # The secret is read from the node's data folder and sent only to the node's own loopback address.
    reply: dict = {}
    for attempt in range(2):
        try:
            secret = (layout.node_data / "node" / "pairing-secret").read_text().strip() or "absent"
        except OSError:
            secret = "absent"
        try:
            response = httpx.post(f"http://127.0.0.1:{deployment.node.port}/api/node/mesh/offer",
                                  headers={"Astral-Pairing-Secret": secret}, content=b"", timeout=20)
            reply = {"status": response.status_code, **(response.json() if response.content else {})}
        except (httpx.HTTPError, ValueError):
            raise SystemExit("the node did not answer; check that the deployment is running") from None
        if attempt == 0 and reply.get("status") == 401 and reply.get("detail") == "node.pairing_secret_required":
            time.sleep(1)
            continue
        break
    return reply


def executor_offer(deployment: Deployment, layout: Layout) -> dict:
    try:
        token = next(line.partition("=")[2] for line in (layout.generated / "executor.env").read_text().splitlines()
                     if line.startswith("VESSEL_EXECUTOR_TOKEN="))
        response = httpx.get(f"http://127.0.0.1:{ports_of(deployment)['executor']}/pairing",
                             headers={"Authorization": f"Bearer {token}"}, timeout=20)
        return {"status": response.status_code, **(response.json() if response.content else {})}
    except (OSError, StopIteration, httpx.HTTPError, ValueError):
        raise SystemExit("the executor did not answer; check that the deployment is running") from None


def pair_executor(deployment: Deployment, layout: Layout, waiting: dict[str, str]) -> None:
    reply = executor_offer(deployment, layout)
    if reply.get("status") != 200:
        raise SystemExit(waiting.get(reply.get("code", ""), f"the executor refused: {reply.get('code', 'unknown')}"))
    print("\n".join(["Add the executor from the AstralDeep app the same way, so it can check the owner's tickets.",
                      "", f"  code     {reply['code']}", f"  payload  {reply['payload']}"]))


