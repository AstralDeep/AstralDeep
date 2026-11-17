"""A stdio MCP server for launcher tests that reads one JSON-RPC message per line and answers through the fixture endpoint, with an extra `environment` tool that reports the names it was started with.
`tests/test_node_stdio_launcher.py` starts it through a test-only sandbox backend and reaches it over the launcher's loopback bridge.
"""

from __future__ import annotations

import json
import os
import sys

from .mcp_server import McpEndpoint, ToolDefinition

INTERPRETER_NAMES = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}


def main() -> int:
    endpoint = McpEndpoint([ToolDefinition(), ToolDefinition(name="environment", description="Environment names")])
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
        except ValueError:
            continue
        if "id" not in request:
            continue
        if request.get("method") == "tools/call" and (request.get("params") or {}).get("name") == "environment":
            names = sorted(name for name in os.environ if name not in INTERPRETER_NAMES)
            reply = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"resultType": "complete", "content": [{"type": "text", "text": json.dumps(names)}], "isError": False},
            }
        else:
            _, reply = endpoint.handle(request, None)
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
