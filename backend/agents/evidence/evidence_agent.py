"""Hosts bounded observation recall, deletion, and usage tools in the normal A2A agent path.
Its MCP server derives authority exclusively from the orchestrator's dispatch binding.
"""

from agents.evidence.mcp_server import MCPServer
from shared.base_agent import BaseA2AAgent


class EvidenceAgent(BaseA2AAgent):
    agent_id = "evidence-1"
    service_name = "Captured evidence"
    description = "Inspect permitted captured observations and whole-conversation context usage. References do not grant access."
    skill_tags = ["evidence", "recall", "context"]

    def __init__(self, orchestrator, port: int | None = None):
        super().__init__(MCPServer(orchestrator), port=port, port_env_var="EVIDENCE_AGENT_PORT")
