#!/usr/bin/env python3
"""A2A-compliant Journal Review agent: matches papers to journals, profiles and compares
journals, and surveys a field's publishing landscape, dispatched through
mcp_server.py.
"""
import asyncio
import os
import sys
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.journal_review.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class JournalReviewAgent(BaseA2AAgent):
    agent_id = "journal-review-1"
    service_name = "Journal Review Agent"
    description = (
        "Finds the right venue for a paper: matching journals, impact and "
        "acceptance figures, review timelines, open-access terms, and a "
        "side-by-side comparison of the shortlist."
    )
    examples = [
        {"title": "Where should this go",
         "prompt": "Find journals that fit a paper on self-supervised learning for "
                   "chest CT and rank them by fit"},
        {"title": "Compare the shortlist",
         "prompt": "Compare Radiology, Medical Image Analysis and IEEE TMI on impact, "
                   "review time and open-access terms"},
        {"title": "Survey the field",
         "prompt": "Show the top venues in medical imaging with their impact and "
                   "acceptance rates"},
    ]
    skill_tags = ["journals", "publishing", "peer-review", "citation-impact",
                  "research", "academic", "science"]

    def __init__(self, port: int = None):
        super().__init__(MCPServer(), port=port, port_env_var="JOURNAL_REVIEW_AGENT_PORT")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Journal Review Agent')
    parser.add_argument('--port', type=int, default=None,
                        help='Port to run the agent on (overrides dynamic discovery)')
    args = parser.parse_args()

    agent = JournalReviewAgent(port=args.port)
    asyncio.run(agent.run())
