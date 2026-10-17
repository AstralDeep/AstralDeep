#!/usr/bin/env python3
"""Runs owner terminal sessions on explicitly operator-approved isolated SSH runtimes.
The in-process host binds security review, current authority and exact human approval before execution.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.terminal_shell.mcp_server import MCPServer

logger = logging.getLogger("TerminalShellAgent")


class TerminalShellAgent(BaseA2AAgent):
    agent_id = "terminal-shell-1"
    service_name = "Server terminal"
    description = (
        "Manages owner terminal sessions through an operator-approved isolated SSH runtime. "
        "Starting a shell or sending input requires a current owner profile, system permission "
        "and exact human approval; unavailable runtimes refuse without execution."
    )
    examples = [
        {"title": "Retained terminals", "prompt": "List my retained terminal sessions"},
        {"title": "Terminal output", "prompt": "Read the retained output from my terminal"},
        {"title": "Close a terminal", "prompt": "Close my retained terminal session"},
    ]
    skill_tags = ["terminal", "shell", "server", "session", "cli"]

    def __init__(self, port: int = None, *, orchestrator=None):
        if orchestrator is None:
            raise RuntimeError("TerminalShellAgent runs in-process only and needs the orchestrator")
        self._terminal_orchestrator = orchestrator
        super().__init__(MCPServer(), port=port, port_env_var="TERMINAL_SHELL_AGENT_PORT")
        from agents.terminal_shell import mcp_tools
        mcp_tools.register_deps(orchestrator)
