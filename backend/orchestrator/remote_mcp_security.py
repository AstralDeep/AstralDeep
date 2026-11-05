"""Defines server-owned security notices for remote MCP and A2A connections shown to clients.
The orchestrator attaches these notices from its fixed gateway or successful transport registration.
"""

from __future__ import annotations

from typing import Any

REMOTE_MCP_SECURITY_KIND = "standard_mcp"
REMOTE_MCP_SECURITY_TITLE = "Standard MCP connection"
REMOTE_MCP_AGENT_ID = "remote-mcp-1"
REMOTE_MCP_SECURITY_NOTICE = (
    "This is a standard MCP connection. AstralDeep still checks permissions, HTTPS destinations, "
    "PHI and policy, and records local calls. This connection does not carry Astral’s agent "
    "identity and delegation protocol, and local records do not prove what the remote server "
    "executed. The provider controls its data handling."
)
STANDARD_A2A_SECURITY_KIND = "standard_a2a"
STANDARD_A2A_SECURITY_TITLE = "Standard A2A connection"
STANDARD_A2A_SECURITY_NOTICE = (
    "This is a standard A2A connection. AstralDeep still applies local permissions, policy checks, "
    "and audit records. The native Astral handshake and the remote enforcement of delegated "
    "permissions have not been verified for this connection. Local records do not prove what the "
    "remote server executed. The provider controls its data handling."
)


def connection_security_for_agent(
    agent_id: str, *, standard_mcp: bool = False, standard_a2a: bool = False,
) -> dict | None:
    if standard_a2a:
        return {
            "kind": STANDARD_A2A_SECURITY_KIND,
            "title": STANDARD_A2A_SECURITY_TITLE,
            "message": STANDARD_A2A_SECURITY_NOTICE,
        }
    if agent_id == REMOTE_MCP_AGENT_ID and standard_mcp:
        return {
            "kind": REMOTE_MCP_SECURITY_KIND,
            "title": REMOTE_MCP_SECURITY_TITLE,
            "message": REMOTE_MCP_SECURITY_NOTICE,
        }
    return None


def connection_security_for_card(
    agent_id: str, card: Any, standard_a2a_cards: Any, local_agents: Any = None,
) -> dict | None:
    return connection_security_for_agent(
        agent_id,
        standard_mcp=(card is not None and agent_id == REMOTE_MCP_AGENT_ID
                      and isinstance(local_agents, dict) and agent_id in local_agents),
        standard_a2a=(card is not None and isinstance(standard_a2a_cards, dict)
                      and standard_a2a_cards.get(agent_id) is card),
    )
