"""A stateless MCP endpoint for protocol 2026-07-28 whose tools need a bearer token, with a switch that changes one tool definition.
`oauth_server.py` mounts it behind the resource server's token check and `test_connection_fixtures.py` drives it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PROTOCOL_VERSION = "2026-07-28"
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"


@dataclass
class ToolDefinition:
    name: str = "search"
    description: str = "Search"
    input_schema: dict = field(default_factory=lambda: {"type": "object"})

    def listed(self) -> dict:
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}


@dataclass
class McpSeen:
    method: str
    tool: str | None
    bearer: str | None


class McpEndpoint:
    def __init__(self, tools: list[ToolDefinition] | None = None) -> None:
        self.tools = tools or [ToolDefinition()]
        self.seen: list[McpSeen] = []

    def redefine(self, name: str, **changes: object) -> None:
        for tool in self.tools:
            if tool.name == name:
                for key, value in changes.items():
                    setattr(tool, key, value)
                return
        raise KeyError(name)

    def record(self, request: dict, bearer: str | None) -> tuple[str, str | None]:
        params = request.get("params") or {}
        tool = params.get("name") if isinstance(params.get("name"), str) else None
        method = str(request.get("method", ""))
        self.seen.append(McpSeen(method, tool, bearer))
        return method, tool

    def handle(
        self, request: dict, bearer: str | None, version_header: str | None = None, enforce_version: bool = False
    ) -> tuple[int, dict]:
        method, tool = self.record(request, bearer)
        params = request.get("params") or {}
        ident = request.get("id")
        meta = params.get("_meta") or {}
        stated = (meta.get(VERSION_KEY), version_header)
        if enforce_version and method != "server/discover" and stated != (PROTOCOL_VERSION, PROTOCOL_VERSION):
            return 400, {"error": "UnsupportedProtocolVersionError", "supportedVersions": [PROTOCOL_VERSION]}
        if method == "server/discover":
            result: dict = {
                "resultType": "complete",
                "supportedVersions": [PROTOCOL_VERSION],
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            result = {"resultType": "complete", "tools": [item.listed() for item in self.tools]}
        elif method == "tools/call":
            if tool not in {item.name for item in self.tools}:
                return 200, {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "unknown tool"}}
            result = {
                "resultType": "complete",
                "content": [{"type": "text", "text": f"called {tool}"}],
                "isError": False,
            }
        else:
            return 200, {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "unknown method"}}
        return 200, {"jsonrpc": "2.0", "id": ident, "result": result}
