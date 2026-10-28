"""Registers the fixed diagnostic tool only inside its owning orchestrator.
Authorization, human review and normal LETS admission remain server-owned.
"""

from agents.sandbox_qualification.mcp_server import MCPServer
from orchestrator.sandbox_tool_sessions import AGENT_ID, QualificationToolSessions
from shared.base_agent import BaseA2AAgent


class SandboxQualificationAgent(BaseA2AAgent):
    agent_id = AGENT_ID
    service_name = "Sandbox qualification"
    description = "Run fixed harmless containment diagnostics with explicit human review and current owner authority."
    skill_tags = ["sandbox", "qualification", "diagnostic"]

    def __init__(self, orchestrator, port=None):
        self.sessions = QualificationToolSessions(orchestrator)
        super().__init__(
            MCPServer(orchestrator),
            port=port,
            port_env_var="SANDBOX_QUALIFICATION_AGENT_PORT",
        )


_CANONICAL_AGENT_INIT = SandboxQualificationAgent.__init__
