"""Registers owner-scoped remote MCP capabilities as a normal first-party agent so
orchestrator dispatch keeps its permission, policy, HITL, and audit enforcement.
"""

from shared.base_agent import BaseA2AAgent

from agents.remote_mcp.mcp_server import MCPServer


class RemoteMCPAgent(BaseA2AAgent):
    agent_id = "remote-mcp-1"
    service_name = "Remote MCP"
    description = "Discovers and calls owner-enabled tools from remote MCP connectors through normal permission and audit checks."
    skill_tags = ["mcp", "connectors", "integrations"]
    examples = [
        {"title": "Find connected tools", "prompt": "List my enabled remote MCP tools"},
        {"title": "Call a connected tool", "prompt": "Use the enabled remote tool I selected"},
    ]

    def __init__(self, port=None, *, orchestrator=None):
        if orchestrator is None:
            raise RuntimeError("Remote MCP runs in-process and requires the orchestrator")
        super().__init__(MCPServer(), port=port, port_env_var="REMOTE_MCP_AGENT_PORT")
        from agents.remote_mcp import mcp_tools
        mcp_tools.register_deps(orchestrator)
