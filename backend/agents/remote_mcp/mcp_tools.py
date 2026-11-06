"""Exposes owner-scoped remote MCP discovery and calls through the ordinary agent tool
permission, confirmation, policy, and audit path.
"""

from __future__ import annotations

from typing import Any

from astralprims import Alert

_ORCH = None


def register_deps(orchestrator) -> None:
    global _ORCH
    _ORCH = orchestrator


def _owner(kwargs: dict[str, Any]) -> str:
    value = kwargs.get("user_id")
    if type(value) is not str or not value:
        raise ValueError("Owner identity is required.")
    return value


def list_remote_tools(**kwargs):
    from orchestrator.remote_mcp_connectors import list_enabled_tools

    owner = _owner(kwargs)
    return {"_ui_components": [], "_data": {"tools": list_enabled_tools(_ORCH, owner)}}


def call_remote_tool(connector_id: str, tool_name: str, arguments: dict[str, Any], **kwargs):
    from orchestrator.remote_mcp_connectors import RemoteMCPError, call_tool

    try:
        result = call_tool(_ORCH, _owner(kwargs), connector_id, tool_name, arguments)
    except (RemoteMCPError, ValueError) as error:
        message = str(error)[:300]
        return {"_ui_components": [Alert(message=message, variant="error").to_dict()],
                "_data": {"error": "remote_mcp_call_refused", "message": message}}
    return {"_ui_components": [], "_data": result}


TOOL_REGISTRY = {
    "list_remote_tools": {
        "function": list_remote_tools,
        "description": "List cached remote MCP tools that the current owner has enabled in settings. Refresh tool definitions from the connector settings surface.",
        "scope": "tools:read",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "call_remote_tool": {
        "function": call_remote_tool,
        "description": "Call one enabled tool from an owner-configured remote MCP connector. The server and tool must remain enabled, and the call is subject to normal execution permissions and audit.",
        "scope": "tools:execute",
        "input_schema": {
            "type": "object", "required": ["connector_id", "tool_name", "arguments"],
            "properties": {
                "connector_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "tool_name": {"type": "string", "minLength": 1, "maxLength": 128},
                "arguments": {"type": "object"},
            }, "additionalProperties": False,
        },
    },
}
