#!/usr/bin/env python3
"""
Dice Roller — A2A-compliant agent.

Rolls N six-sided dice and reports each roll and the total.
"""
import asyncio
import os
import sys
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.dice_roller.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class DiceRollerAgent(BaseA2AAgent):
    """Rolls N six-sided dice and reports each roll and the total."""

    agent_id = "dice-roller-1"
    service_name = "Dice Roller"
    description = (
        "Rolls dice and reports every roll and the total — the smallest "
        "honest end-to-end test of routing, permissions and rendering."
    )
    examples = [
        {"title": "Six dice",
         "prompt": "Roll exactly six six-sided dice and show the normalized results."},
        {"title": "Many rolls",
         "prompt": "Roll 100 six-sided dice and chart how often each face came up"},
    ]
    skill_tags = []

    def __init__(self, port: int = None):
        super().__init__(MCPServer(), port=port, port_env_var="DICE_ROLLER_AGENT_PORT")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Dice Roller')
    parser.add_argument('--port', type=int, default=None, help='Port to run the agent on')
    args = parser.parse_args()

    agent = DiceRollerAgent(port=args.port)
    asyncio.run(agent.run())
