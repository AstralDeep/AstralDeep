"""Isolated OpenAI Agents SDK example consuming Astral Work through the MCP
2026-07-28 endpoint: wire contract, redaction, async client, SDK binding, and the
live qualification runner. The OpenAI Agents SDK is imported lazily so this package
stays importable in environments where that external SDK is not installed.
"""

from scripts.openai_agents_example.agent import AstralAgentSession, AGENT_TOOL_NAMES
from scripts.openai_agents_example.client import (
    AstralWorkClient,
    CredentialRefused,
    MCPProtocolError,
    OperationPendingTimeout,
    ToolCallError,
)
from scripts.openai_agents_example.contract import (
    MCP_PROTOCOL_VERSION,
    encode_mirror_name,
    request_envelope,
    request_headers,
)
from scripts.openai_agents_example.redaction import sanitize_line, sanitize_mapping, scrub_text

__all__ = [
    "AGENT_TOOL_NAMES",
    "AstralAgentSession",
    "AstralWorkClient",
    "CredentialRefused",
    "MCPProtocolError",
    "MCP_PROTOCOL_VERSION",
    "OperationPendingTimeout",
    "ToolCallError",
    "encode_mirror_name",
    "request_envelope",
    "request_headers",
    "sanitize_line",
    "sanitize_mapping",
    "scrub_text",
]
