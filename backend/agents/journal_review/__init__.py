"""Journal Review agent package: evaluates scientific journals for publication fit using
OpenAlex/CrossRef data; aggregates journal_review_agent.py, mcp_server.py, and
mcp_tools.py.
"""

from agents.journal_review.journal_review_agent import JournalReviewAgent
from agents.journal_review.mcp_server import MCPServer
from agents.journal_review.mcp_tools import (
    find_matching_journals,
    get_journal_profile,
    compare_journals,
    analyze_paper_fit,
    get_field_landscape,
    TOOL_REGISTRY,
)

__all__ = [
    'JournalReviewAgent',
    'MCPServer',
    'find_matching_journals',
    'get_journal_profile',
    'compare_journals',
    'analyze_paper_fit',
    'get_field_landscape',
    'TOOL_REGISTRY',
]
__version__ = '1.0.0'
