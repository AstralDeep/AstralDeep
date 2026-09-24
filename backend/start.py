"""Boots the backend under process_supervision: waits for the orchestrator's health
check, then discovers and spawns each agents/ subdirectory as its own supervised
child, or runs it in-process/remote-compute per feature flags.
"""

import time
import sys
import os
import urllib.request
import uuid

from shared.process_supervision import (
    ProcessOwner,
    ProcessSupervisor,
    TerminationReason,
)

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass


EX_UNAVAILABLE = getattr(os, "EX_UNAVAILABLE", 69)

_NON_AGENT_DIRS = frozenset({"tests", "__pycache__"})


def _agent_entrypoint(agents_dir: str, item: str):
    if item.startswith("__") or item.startswith("test_") or item in _NON_AGENT_DIRS:
        return None
    item_path = os.path.join(agents_dir, item)
    if not os.path.isdir(item_path):
        return None
    script = os.path.join(item_path, f"{item}_agent.py")
    if not os.path.isfile(script):
        return None
    return script


def _remote_compute_enabled() -> bool:
    try:
        from shared.feature_flags import flags
        return bool(flags.is_enabled("remote_compute"))
    except Exception:
        return False


def _wait_for_orchestrator(port: int, process, timeout_s: float = 60.0,
                           interval_s: float = 0.5) -> bool:
    url = f"http://localhost:{port}/healthz"
    started = time.monotonic()
    deadline = started + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(" Orchestrator exited before reporting healthy.")
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    elapsed = time.monotonic() - started
                    print(f" Orchestrator healthy after {elapsed:.1f}s.")
                    return True
        except Exception:
            pass
        time.sleep(interval_s)
    print(f" Orchestrator /healthz not ready after {timeout_s:.0f}s; continuing anyway.")
    return False


def main(process_supervisor=None):
    process_supervisor = (
        process_supervisor
        if process_supervisor is not None
        else ProcessSupervisor()
    )
    if sys.stdout.encoding.lower() != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass

    base_dir = os.path.dirname(os.path.abspath(__file__))
    orchestrator_script = os.path.join(base_dir, "orchestrator", "orchestrator.py")
    os.path.join(base_dir, "agents", "general_agent.py")
    python_exe = sys.executable

    try:
        print("=" * 60)
        print("  AstralDeep  ")
        print("=" * 60)
        print()

        agents_dir = os.path.join(base_dir, "agents")
        valid_agents = []
        if os.path.exists(agents_dir):
            for item in os.listdir(agents_dir):
                if _agent_entrypoint(agents_dir, item) is None:
                    continue
                if os.path.exists(os.path.join(agents_dir, item, ".draft")):
                    continue
                valid_agents.append(item)

        max_agents = max(1, len(valid_agents))
        env = os.environ.copy()
        env["MAX_AGENTS"] = str(max_agents)

        orch_port = int(os.environ.get("ORCHESTRATOR_PORT", 8001))
        print(f"Starting Orchestrator on port {orch_port} (expecting {max_agents} agents)...")
        p_orch = process_supervisor.spawn(
            process_id=uuid.uuid4(),
            owner=ProcessOwner(owner_kind="backend_entrypoint", owner_id="orchestrator"),
            argv=(python_exe, orchestrator_script),
            env=env,
        )
        if not _wait_for_orchestrator(orch_port, p_orch):
            returncode = p_orch.poll()
            if isinstance(returncode, int) and returncode != 0:
                raise SystemExit(returncode)
            raise SystemExit(EX_UNAVAILABLE)

        inprocess_enabled = os.environ.get("FF_INPROCESS_AGENTS", "True").lower() in ("true", "1", "yes")
        try:
            from orchestrator.local_agents import BUILT_IN_AGENT_DIRS
        except Exception:
            BUILT_IN_AGENT_DIRS = ()
        try:
            from orchestrator.local_agents import _REMOTE_COMPUTE_AGENT_DIRS
        except Exception:
            _REMOTE_COMPUTE_AGENT_DIRS = ("remote_compute",)
        remote_compute_enabled = _remote_compute_enabled()

        next_port = int(os.environ.get("AGENT_PORT", 8003))
        for item in os.listdir(agents_dir):
            custom_agent_script = _agent_entrypoint(agents_dir, item)
            if custom_agent_script is None:
                continue
            item_path = os.path.dirname(custom_agent_script)
            if os.path.exists(os.path.join(item_path, ".draft")):
                print(f"Skipping draft agent: {item}")
                continue
            if inprocess_enabled and item in BUILT_IN_AGENT_DIRS:
                print(f"Running {item} in-process (no port)")
                continue
            if item in _REMOTE_COMPUTE_AGENT_DIRS:
                # Off must fully disable the agent — no in-process, no subprocess
                if not remote_compute_enabled:
                    print(f"Skipping {item} (FF_REMOTE_COMPUTE is off)")
                    continue
                if inprocess_enabled:
                    print(f"Running {item} in-process (no port)")
                    continue
            print(f"Starting {item} agent on port {next_port}...")
            process_supervisor.spawn(
                process_id=uuid.uuid4(),
                owner=ProcessOwner(
                    owner_kind="server_agent",
                    owner_id=item,
                ),
                argv=(
                    python_exe,
                    custom_agent_script,
                    "--port",
                    str(next_port),
                ),
                cwd=item_path,
            )
            next_port += 1

        print()
        print("-" * 60)
        print(" System started!")
        print(f"  Orchestrator WS: ws://localhost:{orch_port}")
        agent_start_port = int(os.environ.get("AGENT_PORT", 8003))
        print(f"  Agent APIs start at: http://localhost:{agent_start_port}")
        print("-" * 60)
        print()
        print("Press Ctrl+C to stop.")
        print()

        while True:
            time.sleep(1)
            if p_orch.poll() is not None:
                print(" Orchestrator died!")
                _rc = p_orch.returncode
                if _rc:
                    raise SystemExit(_rc)
                break

    except KeyboardInterrupt:
        print("\n Stopping...")
    finally:
        snapshots = process_supervisor.terminate_all(reason=TerminationReason.QUIT)
        for snapshot in snapshots:
            if snapshot.cleanup_error:
                print(
                    " Process cleanup incomplete for "
                    f"{snapshot.owner.owner_kind}/{snapshot.owner.owner_id}: "
                    f"{snapshot.cleanup_error}"
                )
        print(" System stopped.")


if __name__ == "__main__":
    main()
