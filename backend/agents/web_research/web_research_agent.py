#!/usr/bin/env python3
"""
Web Research Agent — A2A-compliant specialist for searching the web, fetching
pages, and synthesizing cited research briefs.

Provides tools for:
- Web search (keyless DuckDuckGo HTML path, or an optional operator/user
  configured Tavily-compatible search provider)
- Egress-gated page fetching with readable-text extraction
- Research briefs that cite only the sources actually fetched
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.web_research.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

PORT_ENV_VAR = "WEB_RESEARCH_AGENT_PORT"


class WebResearchAgent(BaseA2AAgent):
    """Specialist agent for web search, page fetching, and research briefs."""

    agent_id = "web-research-1"
    service_name = "Web Research"
    description = (
        "Searches the web, fetches pages through the platform's egress-gated "
        "HTTP layer, and synthesizes cited research briefs. Offers keyless "
        "DuckDuckGo search, which may be blocked by the provider; when the "
        "optional SEARCH_API_URL + SEARCH_API_KEY credentials are saved, a "
        "Tavily-compatible search provider is preferred. Briefs cite only "
        "sources that were actually fetched — sources are never fabricated."
    )
    skill_tags = ["research", "web", "search", "sources", "brief"]

    card_metadata = {
        "required_credentials": [
            {
                "key": "SEARCH_API_URL",
                "label": "Search Provider URL",
                "description": (
                    "Optional. URL of a Tavily-compatible JSON search endpoint. "
                    "When absent, keyless search is used and may be blocked."
                ),
                "required": False,
                "type": "api_key",
                "placeholder": "https://api.tavily.com/search",
            },
            {
                "key": "SEARCH_API_KEY",
                "label": "Search Provider API Key",
                "description": (
                    "Optional. Add a key for reliable/higher-limit search. "
                    "Use it with the Search Provider URL above."
                ),
                "required": False,
                "type": "api_key",
            },
        ],
    }

    def __init__(self, port: int = None):
        super().__init__(MCPServer(), port=port, port_env_var=PORT_ENV_VAR)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Web Research Agent')
    parser.add_argument('--port', type=int, default=None,
                        help='Port to run the agent on (overrides dynamic discovery)')
    args = parser.parse_args()

    agent = WebResearchAgent(port=args.port)
    asyncio.run(agent.run())
