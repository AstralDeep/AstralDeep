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
        "Opens and operates interactive shell sessions on the server you are signed "
        "in to: start a terminal, send keystrokes, read output, interrupt a running "
        "command, and close it again. Every session belongs to you and is limited to "
        "your own terminal quota."
    )
    examples = [
        {"title": "Check disk space",
         "prompt": "Start a terminal on the server and show me disk usage"},
        {"title": "Follow a long command",
         "prompt": "Open a terminal, run the test suite, and show me the summary"},
        {"title": "What is running",
         "prompt": "List my open terminal sessions"},
    ]
    skill_tags = ["terminal", "shell", "server", "session", "cli"]

    def __init__(self, port: int = None, *, orchestrator=None):
        if orchestrator is None:
            raise RuntimeError("TerminalShellAgent runs in-process only and needs the orchestrator")
        super().__init__(MCPServer(), port=port, port_env_var="TERMINAL_SHELL_AGENT_PORT")
        from agents.terminal_shell import mcp_tools
        mcp_tools.register_deps(orchestrator)
