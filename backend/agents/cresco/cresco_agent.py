"""Registers the Cresco bridge through the standard A2A runtime as a first-party in-process
agent; mcp_server.MCPServer owns every tool gate and orchestrator/local_agents.py gates
registration behind FF_CRESCO.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.base_agent import BaseA2AAgent
from shared.feature_flags import flags

from agents.cresco.mcp_server import MCPServer

DISABLED_EXIT_CODE = 78


class CrescoAgent(BaseA2AAgent):
    agent_id = "cresco-1"
    service_name = "Cresco Bridge"
    description = (
        "Reads bounded topology from the operator-configured Cresco fabric. "
        "Generic work dispatch and remote command results remain unavailable until "
        "their supported bounded contracts are verified; permission, confirmation "
        "and audit gates remain in force."
    )
    skill_tags = ["cresco", "edge", "fabric", "topology"]
    examples = [
        {"title": "Fabric regions", "prompt": "List the regions on the Cresco fabric."},
        {"title": "Region agents", "prompt": "Show the agents in region r1 on the Cresco fabric."},
        {"title": "Target status", "prompt": "Show the Cresco agent info for target r1_a1."},
    ]

    def __init__(self, port: int | None = None, *, orchestrator=None):
        self.orchestrator = orchestrator
        super().__init__(MCPServer(orchestrator), port=port, port_env_var="CRESCO_AGENT_PORT")
        self.host = "127.0.0.1"


def main() -> None:
    import argparse

    if not flags.is_enabled("cresco"):
        raise SystemExit(os.EX_CONFIG if hasattr(os, "EX_CONFIG") else DISABLED_EXIT_CODE)
    parser = argparse.ArgumentParser(description="Cresco bridge agent")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    agent = CrescoAgent(port=args.port)

    import asyncio

    asyncio.run(agent.run())


if __name__ == "__main__":
    main()
