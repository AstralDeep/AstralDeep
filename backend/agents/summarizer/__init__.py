"""Summarizer agent package: exposes SummarizerAgent (summarizer_agent.py) and its MCP
dispatch server/tools (mcp_server.py, mcp_tools.py) for text/URL summarization and
document comparison.
"""

from agents.summarizer.summarizer_agent import SummarizerAgent
from agents.summarizer.mcp_server import MCPServer
from agents.summarizer.mcp_tools import (
    summarize_text,
    summarize_url,
    compare_documents,
    TOOL_REGISTRY,
)

__all__ = [
    'SummarizerAgent',
    'MCPServer',
    'summarize_text',
    'summarize_url',
    'compare_documents',
    'TOOL_REGISTRY',
]

__version__ = '1.0.0'
