"""MCP 2026-07-28 wire contract for the Astral Work endpoint: JSON-RPC envelopes, the
required _meta metadata, and the mirror headers a qualified client must send on every
request. Consumed by client.py and verified against the real server by the tests in
this package and by scripts/tests/test_openai_agents_example.py.
"""

from __future__ import annotations

import base64
import re
from typing import Any

MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_CONTENT_TYPE = "application/json"
MCP_HEADER_PROTOCOL_VERSION = "MCP-Protocol-Version"
MCP_HEADER_METHOD = "Mcp-Method"
MCP_HEADER_NAME = "Mcp-Name"
META_PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
CLIENT_NAME = "astral-openai-agents-example"
CLIENT_VERSION = "1.0.0"
CLIENT_INFO = {"name": CLIENT_NAME, "version": CLIENT_VERSION}

DISPOSITIONS = frozenset({
    "queued", "active", "awaiting_approval", "awaiting_authority", "budget_blocked",
    "reconciliation_required", "paused", "awaiting_event", "retry_eligible",
    "completed", "failed", "cancelled", "unsupported_version",
})
TERMINAL_DISPOSITIONS = frozenset({"completed", "failed", "cancelled"})

_PLAIN_NAME = re.compile(r"^[\x21-\x7e]+$")


def encode_mirror_name(name: str) -> str:
    if isinstance(name, str) and name == name.strip() and _PLAIN_NAME.fullmatch(name) \
            and not name.startswith("=?base64?") and not name.endswith("?="):
        return name
    encoded = base64.b64encode((name or "").encode("utf-8")).decode("ascii")
    return f"=?base64?{encoded}?="


def request_envelope(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: int | str,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = {
        META_PROTOCOL_VERSION_KEY: MCP_PROTOCOL_VERSION,
        META_CLIENT_CAPABILITIES_KEY: dict(capabilities or {}),
        META_CLIENT_INFO_KEY: dict(CLIENT_INFO),
    }
    merged = dict(params or {})
    merged["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": merged}


def request_headers(
    *,
    bearer: str,
    method: str,
    tool_name: str | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": MCP_CONTENT_TYPE,
        MCP_HEADER_PROTOCOL_VERSION: MCP_PROTOCOL_VERSION,
        MCP_HEADER_METHOD: method,
    }
    if tool_name is not None:
        headers[MCP_HEADER_NAME] = encode_mirror_name(tool_name)
    return headers


__all__ = [
    "CLIENT_INFO",
    "CLIENT_NAME",
    "CLIENT_VERSION",
    "DISPOSITIONS",
    "META_CLIENT_CAPABILITIES_KEY",
    "META_CLIENT_INFO_KEY",
    "META_PROTOCOL_VERSION_KEY",
    "MCP_CONTENT_TYPE",
    "MCP_HEADER_METHOD",
    "MCP_HEADER_NAME",
    "MCP_HEADER_PROTOCOL_VERSION",
    "MCP_PROTOCOL_VERSION",
    "TERMINAL_DISPOSITIONS",
    "encode_mirror_name",
    "request_envelope",
    "request_headers",
]
