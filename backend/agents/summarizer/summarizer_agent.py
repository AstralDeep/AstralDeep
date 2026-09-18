#!/usr/bin/env python3
"""
Summarizer Agent — A2A-compliant specialist for digesting and comparing text.

Provides tools for:
- Summarizing provided text into TL;DR / key points / notable quotes
- Summarizing a URL after an egress-gated fetch
- Comparing two documents side by side with a key-differences table
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.summarizer.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

PORT_ENV_VAR = "SUMMARIZER_AGENT_PORT"


class SummarizerAgent(BaseA2AAgent):
    """Specialist agent for summarizing and comparing text and web pages."""

    agent_id = "summarizer-1"
    service_name = "Summarizer"
    description = (
        "Turns a page or a block of text into a TL;DR, key points and quotable "
        "lines, and puts two documents side by side with a table of what "
        "differs. Long inputs are cut with a notice, never silently."
    )
    examples = [
        {"title": "Summarize a page",
         "prompt": "Summarize https://en.wikipedia.org/wiki/Dog_grooming — give me "
                   "a TL;DR and key points"},
        {"title": "Compare two documents",
         "prompt": "Compare the two documents I attached and table the key differences"},
    ]
    skill_tags = ["summarize", "digest", "compare", "tldr"]

    def __init__(self, port: int = None):
        super().__init__(MCPServer(), port=port, port_env_var=PORT_ENV_VAR)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Summarizer Agent')
    parser.add_argument('--port', type=int, default=None,
                        help='Port to run the agent on (overrides dynamic discovery)')
    args = parser.parse_args()

    agent = SummarizerAgent(port=args.port)
    asyncio.run(agent.run())
