"""Loopback OAuth, MCP, model and A2A servers that the connection tests and the standalone fixture runner share.
Everything binds 127.0.0.1 and needs no network; `serve.py` runs them outside pytest, optionally over TLS.
"""

from .a2a_agent import AgentConfig, FixtureAgent
from .mcp_server import McpEndpoint, ToolDefinition
from .model_server import ModelServer
from .oauth_server import FixtureAuth, FixtureConfig, FixtureRefusal, Urls

__all__ = [
    "AgentConfig",
    "FixtureAgent",
    "FixtureAuth",
    "FixtureConfig",
    "FixtureRefusal",
    "McpEndpoint",
    "ModelServer",
    "ToolDefinition",
    "Urls",
]
