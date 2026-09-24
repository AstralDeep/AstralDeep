"""Web Research agent package: exposes WebResearchAgent and its MCP dispatch
server/tools for DuckDuckGo/provider search, egress-gated page fetches, and cited
research briefs.
"""

from agents.web_research.web_research_agent import WebResearchAgent
from agents.web_research.mcp_server import MCPServer
from agents.web_research.mcp_tools import (
    web_search,
    fetch_page,
    research_brief,
    TOOL_REGISTRY,
)

__all__ = [
    'WebResearchAgent',
    'MCPServer',
    'web_search',
    'fetch_page',
    'research_brief',
    'TOOL_REGISTRY',
]

__version__ = '1.0.0'
