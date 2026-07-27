#!/usr/bin/env python3
"""remote-control-1 — mutating remote-compute agent (feature 063).

Reaches the user's own registered machines/clusters over SSH and performs
CONSEQUENTIAL operations: submit/cancel jobs, create/delete paths, upload files,
control services/packages, signal processes. NEVER safe-seeded (FR-003) — every
verb needs an explicit per-user grant, and every DESTRUCTIVE verb is gated by the
durable confirmation mechanism enforced at the orchestrator dispatch gate
(``orchestrator/remote_confirmation.py``). Runs IN-PROCESS; gated by
FF_REMOTE_COMPUTE (registered only when the flag is on).
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.remote_control.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("RemoteControlAgent")


class RemoteControlAgent(BaseA2AAgent):
    """Mutating remote-compute agent: jobs, filesystem, services, packages, signals."""

    agent_id = "remote-control-1"
    service_name = "Remote Compute (control)"
    description = ("Act on your registered clusters and machines over SSH — submit and "
                   "cancel jobs, create and delete paths, upload files, control services "
                   "and packages, and signal processes. Consequential: destructive "
                   "operations always ask you to confirm first.")
    skill_tags = ["remote", "cluster", "slurm", "hpc", "ssh", "control"]

    def __init__(self, port: int = None):
        super().__init__(MCPServer(), port=port, port_env_var="REMOTE_CONTROL_AGENT_PORT")
        # In-process pattern (mirrors remote_observe): wire a shared Database + a
        # CredentialManager (same CREDENTIAL_ENCRYPTION_KEY) so the verbs can read
        # the owner's remote_machine rows and decrypt per-machine credentials.
        try:
            from shared.database import Database
            from orchestrator.credential_manager import CredentialManager
            from agents.remote_control import mcp_tools
            db = Database()
            mcp_tools.register_deps(db, CredentialManager(db=db))
        except Exception:
            logger.warning("remote-control-1 dependency wiring failed", exc_info=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Remote Control Agent')
    parser.add_argument('--port', type=int, default=None, help='Port to run the agent on')
    args = parser.parse_args()
    agent = RemoteControlAgent(port=args.port)
    asyncio.run(agent.run())
