#!/usr/bin/env python3
"""Bundled agent operating the owner's interactive terminal sessions on the server
in-process via the orchestrator's terminal session manager; gated by FF_TERMINAL_SHELL
and the ordinary per-user tool permission gates.
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
        "Reads and cleans up retained owner terminal sessions through governed tools. "
        "Starting a shell or sending command input is unavailable until an isolated "
        "executor is qualified; resource limits alone do not provide isolation."
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
        super().__init__(MCPServer(), port=port, port_env_var="TERMINAL_SHELL_AGENT_PORT")
        from agents.terminal_shell import mcp_tools
        mcp_tools.register_deps(orchestrator)
