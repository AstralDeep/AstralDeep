"""Hosts the publish_artifact and update_artifact tools in the normal in-process agent path.
Its MCP server derives the owner and conversation only from the orchestrator's dispatch binding.
"""

from agents.artifacts.mcp_server import MCPServer
from orchestrator.artifact_html import AGENT_ID
from shared.base_agent import BaseA2AAgent


class ArtifactsAgent(BaseA2AAgent):
    agent_id = AGENT_ID
    service_name = "Artifacts"
    description = "Publish and revise self-contained HTML pages that clients show as versioned canvas artifacts."
    skill_tags = ["artifacts", "canvas", "pages"]

    def __init__(self, orchestrator, port: int | None = None):
        super().__init__(MCPServer(orchestrator), port=port, port_env_var="ARTIFACTS_AGENT_PORT")
