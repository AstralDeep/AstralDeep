"""Defines server-owned security notices for the standard MCP gateway shown to clients.
The orchestrator attaches this metadata only to its fixed in-process Remote MCP agent.
"""

from __future__ import annotations

REMOTE_MCP_SECURITY_KIND = "standard_mcp"
REMOTE_MCP_SECURITY_TITLE = "Standard MCP connection"
REMOTE_MCP_AGENT_ID = "remote-mcp-1"
REMOTE_MCP_SECURITY_NOTICE = (
    "This is a standard MCP connection. AstralDeep still checks permissions, HTTPS destinations, "
    "PHI and policy, and records local calls. This connection does not carry Astral’s agent "
    "identity and delegation protocol, and local records do not prove what the remote server "
    "executed. The provider controls its data handling."
)


def connection_security_for_agent(agent_id: str) -> dict | None:
    if agent_id != REMOTE_MCP_AGENT_ID:
        return None
    return {
        "kind": REMOTE_MCP_SECURITY_KIND,
        "title": REMOTE_MCP_SECURITY_TITLE,
        "message": REMOTE_MCP_SECURITY_NOTICE,
    }
