#!/usr/bin/env python3
"""remote-compute-1 — the unified remote-compute agent (feature 063).

Reaches the user's own registered machines/clusters over SSH and exposes BOTH the
read-only verbs (queue, job status/history, host facts, directory/process listing,
reachability) and the mutating verbs (submit/cancel jobs, create/delete paths,
upload files, control services/packages, signal processes). Runs IN-PROCESS;
gated by FF_REMOTE_COMPUTE.

Safe-seeded so the read verbs work out of the box, but every DESTRUCTIVE mutating
verb is still gated per-verb by the durable confirmation mechanism
(``orchestrator/remote_confirmation.py``, keyed on this agent's id) — merging the
read + control agents did NOT merge their safety classes.
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.remote_compute.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("RemoteComputeAgent")


class RemoteComputeAgent(BaseA2AAgent):
    """Unified remote-compute agent: read + mutating verbs, one grantable agent."""

    agent_id = "remote-compute-1"
    service_name = "Remote Compute"
    description = ("Work with your registered clusters and machines over SSH — check "
                   "the queue, job status/history, host facts, files and processes, and "
                   "act: submit and cancel jobs, create and delete paths, upload files, "
                   "control services and packages, signal processes. Destructive "
                   "operations always ask you to confirm first.")
    skill_tags = ["remote", "cluster", "slurm", "hpc", "ssh", "control"]

    def __init__(self, port: int = None):
        super().__init__(MCPServer(), port=port, port_env_var="REMOTE_COMPUTE_AGENT_PORT")
        # In-process pattern: wire a shared Database + a CredentialManager (same
        # CREDENTIAL_ENCRYPTION_KEY) into both verb libraries so the verbs can read
        # the owner's remote_machine rows and decrypt per-machine credentials.
        try:
            from shared.database import Database
            from orchestrator.credential_manager import CredentialManager
            from agents.remote_compute import mcp_tools
            db = Database()
            mcp_tools.register_deps(db, CredentialManager(db=db))
        except Exception:
            logger.warning("remote-compute-1 dependency wiring failed", exc_info=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Remote Compute Agent')
    parser.add_argument('--port', type=int, default=None, help='Port to run the agent on')
    args = parser.parse_args()
    agent = RemoteComputeAgent(port=args.port)
    asyncio.run(agent.run())
