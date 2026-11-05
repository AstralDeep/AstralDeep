"""Binds the remote MCP connector tools to the standard in-process MCP request protocol."""

from agents.general.mcp_server import MCPServer as BaseMCPServer
from agents.remote_mcp.mcp_tools import TOOL_REGISTRY


class MCPServer(BaseMCPServer):
    def __init__(self):
        self.tools = TOOL_REGISTRY
