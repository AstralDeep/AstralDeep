#!/usr/bin/env python3
"""remote-observe-1 — read-only remote-compute agent (feature 063).

Reaches the user's own registered machines/clusters over SSH and reports typed,
structured facts (queue, host facts, reachability). Every verb is incapable of
changing remote state. Runs IN-PROCESS in the orchestrator; gated by
FF_REMOTE_COMPUTE (registered only when the flag is on).
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.remote_observe.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("RemoteObserveAgent")


class RemoteObserveAgent(BaseA2AAgent):
    """Read-only remote-compute agent: queue, job status, host facts, reachability."""

    agent_id = "remote-observe-1"
    service_name = "Remote Compute (read-only)"
    description = ("Check your registered clusters and machines over SSH — queue "
                   "contents, host facts, and reachability. Read-only: it cannot "
                   "change anything on any machine.")
    skill_tags = ["remote", "cluster", "slurm", "hpc", "ssh"]

    def __init__(self, port: int = None):
        super().__init__(MCPServer(), port=port, port_env_var="REMOTE_OBSERVE_AGENT_PORT")
        # In-process pattern (mirrors general_agent): wire a shared Database + a
        # CredentialManager (same CREDENTIAL_ENCRYPTION_KEY) so the verbs can read
        # the owner's remote_machine rows and decrypt per-machine credentials.
        try:
            from shared.database import Database
            from orchestrator.credential_manager import CredentialManager
            from agents.remote_observe import mcp_tools
            db = Database()
            mcp_tools.register_deps(db, CredentialManager(db=db))
        except Exception:
            logger.warning("remote-observe-1 dependency wiring failed", exc_info=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Remote Observe Agent')
    parser.add_argument('--port', type=int, default=None, help='Port to run the agent on')
    args = parser.parse_args()
    agent = RemoteObserveAgent(port=args.port)
    asyncio.run(agent.run())
