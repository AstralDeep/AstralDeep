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
import os
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
        print("Installation not in a ready state; cannot upgrade.")
        return 1
    print("Upgrading AstralDeep deployment...")
    ensure_keystore_exists()
    # Pull latest images
    run_cmd([ASTRALVESSEL_CMD, "pull"])
    # Recreate containers while preserving volumes
    run_cmd([ASTRALVESSEL_CMD, "up", "--no-build", "--force-recreate"])
    print("Upgrade completed. Data and mesh keys preserved.")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    preserve = args.preserve_data
    print("Removing AstralDeep node pack...")
    cmd = [ASTRALVESSEL_CMD, "down"]
    if not preserve:
        cmd.append("--volumes")
    run_cmd(cmd)
    # Clean up state file
    if STATE_FILE.is_file():
        STATE_FILE.unlink()
    print("Removal completed. " + ("Data volumes preserved." if preserve else "All data removed."))
    return 0

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AstralDeep installation manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    resume_parser = subparsers.add_parser("resume", help="Resume an interrupted install")
    resume_parser.set_defaults(func=cmd_resume)

    upgrade_parser = subparsers.add_parser("upgrade", help="Upgrade an existing installation")
    upgrade_parser.set_defaults(func=cmd_upgrade)

    remove_parser = subparsers.add_parser("remove", help="Safely remove the node pack")
    remove_parser.add_argument(
        "--preserve-data",
        action="store_true",
        help="Do not delete Docker volumes (keystore and DB) during removal",
    )
    remove_parser.set_defaults(func=cmd_remove)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        exit_code = args.func(args)
    except RuntimeError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.exit(1)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
