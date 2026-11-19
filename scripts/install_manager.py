#!/usr/bin/env python3
"""AstralDeep installation manager.

Provides commands to **resume** an interrupted installation, **upgrade** an existing
deployment while preserving keys and data, and **remove** the node pack safely.

The manager works with the Mac app bundle and the `astralvessel` CLI. It tracks
installation state in a hidden JSON file `.astralvessel_state.json` located in
the repository root.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = REPO_ROOT / ".astralvessel_state.json"
ASTRALVESSEL_CMD = "astralvessel"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def load_state() -> Dict[str, Any]:
    """Load the installation state file if it exists.

    Returns an empty dict when the file is missing or malformed.
    """
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    """Write the given state dictionary to the state file atomically."""
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def run_cmd(args: list[str]) -> subprocess.CompletedProcess:
    """Execute a subprocess command, streaming output to the console.

    Raises ``RuntimeError`` on non‑zero exit status.
    """
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"Command {' '.join(args)} failed with exit code {result.returncode}")
    return result


def ensure_keystore_exists() -> None:
    """Verify that the mesh keystore directory exists.

    The keystore is expected at ``vessel/keystore`` relative to the repo root.
    """
    keystore = REPO_ROOT / "vessel" / "keystore"
    if not keystore.is_dir():
        raise RuntimeError(
            "Keystore not found. Ensure a previous successful install created the mesh keys."
        )

# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

def cmd_resume(args: argparse.Namespace) -> int:
    state = load_state()
    if state.get("status") != "incomplete":
        print("No incomplete installation detected. Nothing to resume.")
        return 0
    print("Resuming interrupted installation...")
    ensure_keystore_exists()
    # Bring up services without recreating volumes or secrets
    run_cmd([ASTRALVESSEL_CMD, "up", "--no-build"])
    # Update state
    state["status"] = "ready"
    save_state(state)
    print("Installation resumed successfully.")
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    state = load_state()
    if state.get("status") != "ready":
        print("Cannot upgrade: installation is not in 'ready' state.")
        return 1
    print("Upgrading AstralDeep...")
    ensure_keystore_exists()
    # Pull latest images
    run_cmd([ASTRALVESSEL_CMD, "pull"])
    # Restart services with force-recreate to apply changes
    run_cmd([ASTRALVESSEL_CMD, "up", "--no-build", "--force-recreate"])
    print("Upgrade completed successfully.")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    state = load_state()
    if state.get("status") != "ready":
        print("Cannot remove: installation is not in 'ready' state.")
        return 1
    print("Removing AstralDeep node pack...")
    # Stop and remove containers
    run_cmd([ASTRALVESSEL_CMD, "down"])
    # Remove state file
    if STATE_FILE.is_file():
        STATE_FILE.unlink()
    print("Removal completed successfully.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AstralDeep Installation Manager")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("resume", help="Resume an interrupted installation")
    subparsers.add_parser("upgrade", help="Upgrade an existing installation")
    subparsers.add_parser("remove", help="Safely remove the node pack")

    args = parser.parse_args()

    if args.command == "resume":
        return cmd_resume(args)
    elif args.command == "upgrade":
        return cmd_upgrade(args)
    elif args.command == "remove":
        return cmd_remove(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
