"""MCP 2026-07-28 wire adapter shared by astral_sdk.mcp_bridge's stdio and official-SDK
serving paths: it converts the framework function catalog into protocol tool
descriptors and operation dictionaries into protocol result envelopes, and carries
the JSON-RPC error codes and per-request version metadata rules of the declared
protocol version.
"""

from __future__ import annotations

import copy
import json
from importlib import metadata
from typing import Any

from astral_sdk.tools import (
    ASTRAL_TOOLS,
    MCP_SUPPORTED_PROTOCOL_VERSIONS,
    TOOL_NAMES,
)

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SERVER_NAME = "astral-sdk-bridge"
WORK_AGENT_ID = "__work__"
SERVER_INSTRUCTIONS = (
    "Astral exposes the requesting framework credential's authorized Work tools; "
    "human-only commands are never projected."
)
RESULT_TTL_MS = 60_000
CACHE_SCOPE = "private"
CONTENT_TEXT_LIMIT = 65_536

PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022

try:
    SERVER_VERSION = metadata.version("astral-sdk")
except metadata.PackageNotFoundError:
    SERVER_VERSION = "0.1.0"


class ProtocolError(Exception):
    def __init__(self, code: int, message: str, *, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def server_info() -> dict[str, str]:
    return {"name": SERVER_NAME, "version": SERVER_VERSION}


def tool_descriptor(name: str) -> dict[str, Any]:
    spec = ASTRAL_TOOLS[name]
    input_schema = copy.deepcopy(spec["input_schema"])
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}}
    input_schema.setdefault("$schema", JSON_SCHEMA_DIALECT)
    return {
        "name": name,
        "description": spec["description"],
        "inputSchema": input_schema,
        "annotations": {"readOnlyHint": bool(spec["read_only"]), "destructiveHint": False},
        "_meta": {"astral/agentId": WORK_AGENT_ID, "astral/requiredScope": spec["scope"]},
    }


def tool_descriptors() -> list[dict[str, Any]]:
    return [tool_descriptor(name) for name in TOOL_NAMES]


def discover_result() -> dict[str, Any]:
    return {
        "resultType": "complete",
        "supportedVersions": list(MCP_SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {}},
        "instructions": SERVER_INSTRUCTIONS,
        "ttlMs": RESULT_TTL_MS,
        "cacheScope": CACHE_SCOPE,
        "_meta": {SERVER_INFO_META_KEY: server_info()},
    }


def tools_list_result() -> dict[str, Any]:
    return {
        "resultType": "complete",
        "tools": tool_descriptors(),
        "ttlMs": RESULT_TTL_MS,
        "cacheScope": CACHE_SCOPE,
        "_meta": {SERVER_INFO_META_KEY: server_info()},
    }


def _text_content(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text[:CONTENT_TEXT_LIMIT]}]


def tool_success_result(structured: dict[str, Any]) -> dict[str, Any]:
    return {
        "resultType": "complete",
        "content": _text_content(json.dumps(structured, ensure_ascii=False, default=str)),
        "structuredContent": structured,
        "isError": False,
        "_meta": {SERVER_INFO_META_KEY: server_info()},
    }


def tool_error_result(message: str) -> dict[str, Any]:
    return {
        "resultType": "complete",
        "content": _text_content(message),
        "structuredContent": {},
        "isError": True,
        "_meta": {SERVER_INFO_META_KEY: server_info()},
    }


def error_response(request_id: Any, error: ProtocolError) -> dict[str, Any]:
    body: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.data is not None:
        body["data"] = error.data
    return {"jsonrpc": "2.0", "id": request_id, "error": body}


def require_request_envelope(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ProtocolError(INVALID_PARAMS, "params must be an object")
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        raise ProtocolError(INVALID_PARAMS, "params._meta is required")
    declared = meta.get(PROTOCOL_VERSION_META_KEY)
    if not isinstance(declared, str):
        raise ProtocolError(INVALID_PARAMS, "params._meta protocolVersion is required")
    if declared not in MCP_SUPPORTED_PROTOCOL_VERSIONS:
        raise ProtocolError(
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported MCP protocol version",
            data={
                "requested": declared,
                "supported": list(MCP_SUPPORTED_PROTOCOL_VERSIONS),
            },
        )
    capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    if not isinstance(capabilities, dict):
        raise ProtocolError(INVALID_PARAMS, "params._meta clientCapabilities must be an object")
    return params


def negotiated_call_arguments(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise ProtocolError(INVALID_PARAMS, "tools/call requires a tool name")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ProtocolError(INVALID_PARAMS, "tools/call arguments must be an object")
    return name, arguments


def valid_request_id(request_id: Any) -> bool:
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        return False
    return True


__all__ = [
    "CACHE_SCOPE",
    "CONTENT_TEXT_LIMIT",
    "CLIENT_CAPABILITIES_META_KEY",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSON_SCHEMA_DIALECT",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "PROTOCOL_VERSION_META_KEY",
    "ProtocolError",
    "RESULT_TTL_MS",
    "SERVER_INSTRUCTIONS",
    "SERVER_INFO_META_KEY",
    "SERVER_NAME",
    "SERVER_VERSION",
    "UNSUPPORTED_PROTOCOL_VERSION",
    "WORK_AGENT_ID",
    "discover_result",
    "error_response",
    "negotiated_call_arguments",
    "require_request_envelope",
    "server_info",
    "tool_descriptors",
    "tool_descriptor",
    "tool_error_result",
    "tool_success_result",
    "tools_list_result",
    "valid_request_id",
]
