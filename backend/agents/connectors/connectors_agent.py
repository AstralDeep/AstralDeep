"""
Claude Connectors Agent — US-22

Bundles connector tools as MCP tools:
- Office: Excel, PowerPoint, Word, Outlook, Pitch Templates
- Dev: Code Review, Constitution Critique
- Runtime: Adaptive Intelligence
- Creative: Blender, Adobe CC, Canva, Dashboards, Graphs, Design

Optional external-service credentials (declared in ``card_metadata`` so the
orchestrator's settings UI can surface them) are all ``required: False`` —
the agent is fully usable with none of them; individual tools degrade to
their preview/stub behavior when their credential is absent.
"""
import asyncio
import os
import sys
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.connectors.mcp_server import ConnectorsMCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class ConnectorsAgent(BaseA2AAgent):
    agent_id = "connectors-1"
    service_name = "Claude Connectors Agent"
    description = (
        "Builds the office documents you ask for — spreadsheets, decks, letters, "
        "email drafts and pitch templates — and reviews code or a written "
        "argument against a standard you name."
    )
    examples = [
        {"title": "Build a deck",
         "prompt": "Draft a six-slide deck outlining a quarterly research update, "
                   "then give me the PowerPoint"},
        {"title": "Make a spreadsheet",
         "prompt": "Build an Excel workbook tracking grant spend by month with a "
                   "summary sheet"},
        {"title": "Review some code",
         "prompt": "Review the file I attached for correctness and error handling, "
                   "most serious findings first"},
    ]
    skill_tags = [
        "office", "productivity", "documents", "email",
        "dev-tools", "code-review",
        "creative", "design", "routing",
    ]

    card_metadata = {
        "required_credentials": [
            {
                "key": "MS_GRAPH_ACCESS_TOKEN",
                "label": "Microsoft Graph Access Token",
                "description": (
                    "OAuth bearer token with Mail.Send scope. Enables outlook_email "
                    "to actually send (instead of returning a preview only)."
                ),
                "required": False,
                "type": "api_key",
            },
            {
                "key": "CANVA_API_KEY",
                "label": "Canva Connect API Key",
                "description": (
                    "Canva Connect API bearer token. Enables canva_design to create "
                    "real designs in your Canva workspace."
                ),
                "required": False,
                "type": "api_key",
            },
            {
                "key": "ADOBE_CLIENT_ID",
                "label": "Adobe Client ID",
                "description": (
                    "Adobe IMS server-to-server client ID. Paired with ADOBE_CLIENT_SECRET. "
                    "Enables adobe_cc to validate Firefly credentials."
                ),
                "required": False,
                "type": "api_key",
            },
            {
                "key": "ADOBE_CLIENT_SECRET",
                "label": "Adobe Client Secret",
                "description": "Adobe IMS server-to-server client secret. Paired with ADOBE_CLIENT_ID.",
                "required": False,
                "type": "api_key",
            },
        ],
    }

    def __init__(self, port: int = None):
        super().__init__(
            ConnectorsMCPServer(),
            port=port,
            port_env_var="CONNECTORS_AGENT_PORT",
        )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Claude Connectors Agent')
    parser.add_argument('--port', type=int, default=None, help='Port to run the agent on')
    args = parser.parse_args()
    agent = ConnectorsAgent(port=args.port)
    asyncio.run(agent.run())
