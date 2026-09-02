#!/usr/bin/env python3
"""computer-use-1 — drive the user's OWN desktop from any of their clients (feature 076).

The user's Windows PC runs the AstralDeep desktop client with "Allow remote
control" switched on; this agent looks at its screen (screenshots the model
sees as images), moves and clicks the mouse, types, presses keys, opens
applications, reads the clipboard and files, and — only after the user approves
on the device they are holding — runs commands or writes/deletes files.

Runs IN-PROCESS only (it needs the orchestrator's host registry); gated by
FF_COMPUTER_USE. Safe-seeded so the observe verbs work out of the box, while
every consequential verb is gated per-reach by the durable confirmation
mechanism (``orchestrator/remote_confirmation.py`` + ``computer_use_policy``).
"""
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.computer_use.mcp_server import MCPServer

logger = logging.getLogger("ComputerUseAgent")


class ComputerUseAgent(BaseA2AAgent):
    """The bundled computer-use agent: one grantable agent, verb tiers inside."""

    agent_id = "computer-use-1"
    service_name = "My computer"
    description = (
        "Control your own computer that runs the AstralDeep desktop client — from your "
        "phone or any other signed-in device. Start a session, look at the screen, click, "
        "type, press keys, scroll, open apps, switch windows, use the clipboard and read "
        "files. Running commands and writing or deleting files always ask you to approve "
        "first, and whoever is sitting at the computer can pause or stop at any time."
    )
    skill_tags = ["computer", "desktop", "remote-control", "screen", "automation", "windows"]

    def __init__(self, port: int = None, *, orchestrator=None):
        if orchestrator is None:
            raise RuntimeError("ComputerUseAgent runs in-process only and needs the orchestrator")
        super().__init__(MCPServer(), port=port, port_env_var="COMPUTER_USE_AGENT_PORT")
        from agents.computer_use import mcp_tools
        mcp_tools.register_deps(orchestrator)
