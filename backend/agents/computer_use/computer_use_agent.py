#!/usr/bin/env python3
"""Bundled agent driving the user's own desktop (screenshots, mouse/keyboard, files)
in-process via the orchestrator's host registry; consequential verbs are gated
per-reach by orchestrator/remote_confirmation.py and computer_use_policy.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.computer_use.mcp_server import MCPServer

logger = logging.getLogger("ComputerUseAgent")


class ComputerUseAgent(BaseA2AAgent):
    agent_id = "computer-use-1"
    service_name = "My computer"
    description = (
        "Drives your own computer from any device you are signed in on: look at "
        "the screen, click, type, switch windows, use the clipboard, read files. "
        "Commands and writes ask you to approve first, and whoever is sitting at "
        "that computer can stop it at any time."
    )
    examples = [
        {"title": "What is on screen",
         "prompt": "Start a session on my computer and show me what is on the screen"},
        {"title": "Find a window",
         "prompt": "List the open windows on my computer and bring the browser to the front"},
        {"title": "Fetch something",
         "prompt": "Read the contents of the file on my desktop called notes.txt"},
    ]
    skill_tags = ["computer", "desktop", "remote-control", "screen", "automation", "windows"]

    def __init__(self, port: int = None, *, orchestrator=None):
        if orchestrator is None:
            raise RuntimeError("ComputerUseAgent runs in-process only and needs the orchestrator")
        super().__init__(MCPServer(), port=port, port_env_var="COMPUTER_USE_AGENT_PORT")
        from agents.computer_use import mcp_tools
        mcp_tools.register_deps(orchestrator)
