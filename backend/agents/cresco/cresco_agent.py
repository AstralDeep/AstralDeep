"""Read-only Cresco topology agent exposing bounded fabric reads through the standard A2A
runtime. orchestrator/local_agents.py registers it in-process only when the cresco feature
flag is on; the fabric is reached exclusively through wsapi_client.py.
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from shared.feature_flags import flags

from agents.cresco.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

DISABLED_EXIT_CODE = 78


class CrescoAgent(BaseA2AAgent):
    agent_id = "cresco-1"
    service_name = "Cresco Fabric"
    description = (
        "Read-only view of the configured Cresco edge fabric: regions, agents, per-agent "
        "plugins and host system information. Topology reads only; the fabric is operator-run "
        "external infrastructure."
    )
    examples = [
        {"title": "Fabric regions",
         "prompt": "Which regions does the Cresco fabric report?"},
        {"title": "Fabric agents",
         "prompt": "List the agents registered in the Cresco fabric"},
        {"title": "Agent detail",
         "prompt": "Show the plugins and status of the Cresco agent 'agent-1' in region 'global'"},
        {"title": "Host information",
         "prompt": "Show the host system information for the Cresco agent 'agent-1' in region 'global'"},
    ]
    skill_tags = ["cresco", "edge", "topology", "read-only"]

    def __init__(self, port: int = None):
        super().__init__(MCPServer(), port=port, port_env_var="CRESCO_AGENT_PORT")


def main() -> None:
    import argparse

    if not flags.is_enabled("cresco"):
        raise SystemExit(DISABLED_EXIT_CODE)
    parser = argparse.ArgumentParser(description='Cresco Fabric agent')
    parser.add_argument('--port', type=int, default=None, help='Port to run the agent on')
    args = parser.parse_args()
    asyncio.run(CrescoAgent(port=args.port).run())


if __name__ == "__main__":
    main()
