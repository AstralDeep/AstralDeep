"""
Medical Agent — A2A-compliant specialist agent for medical professionals.
"""
import asyncio
import os
import sys
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.medical.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

DEFAULT_PORT = 8004


class MedicalAgent(BaseA2AAgent):
    """Specialist agent with medical data generation and analysis capabilities."""

    agent_id = "medical-1"
    service_name = "Medical Agent"
    description = (
        "Searches patient records for a cohort, generates synthetic patients "
        "for testing, and runs statistical analysis over clinical tables and "
        "CSV files."
    )
    examples = [
        {"title": "Build a cohort",
         "prompt": "Find patients over 65 with a documented degenerative condition "
                   "and chart the age distribution"},
        {"title": "Synthetic records",
         "prompt": "Generate 200 synthetic patients with realistic vitals and show "
                   "the summary statistics"},
        {"title": "Analyze a table",
         "prompt": "Analyze the CSV I attached and report which variables correlate "
                   "with the outcome column"},
    ]
    skill_tags = ["medical", "analysis", "data"]

    def __init__(self, port: int = DEFAULT_PORT):
        super().__init__(MCPServer(), port=port)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Medical Agent')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    agent = MedicalAgent(port=args.port)
    asyncio.run(agent.run())
