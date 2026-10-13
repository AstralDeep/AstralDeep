"""End-to-end tests driving the bridge subprocess with an independent MCP stdio client:
an official ClientSession discovers, lists and calls authorized Work tools over a real
stdio pipe against both the lightweight and the official-SDK serving paths, and every
wire envelope is validated against the pinned 2026-07-28 protocol schema.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("mcp_types")

from mcp import Client, StdioServerParameters  # noqa: E402

SDK_ROOT = Path(__file__).resolve().parents[1]

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}

_RUNNER_PREFIX = (
    "import os;"
    "from astral_sdk.mcp_bridge import Bridge;"
    "bridge = Bridge.connect(os.environ['ASTRAL_BASE_URL'], os.environ['ASTRAL_TOKEN']);"
)
LIGHT_RUNNER = _RUNNER_PREFIX + "bridge.serve_stdio(); bridge.close()"
OFFICIAL_RUNNER = _RUNNER_PREFIX + "bridge.serve_with_official_sdk()"


def _subprocess_env(fake_server) -> dict[str, str]:
    import astral_sdk

    package_root = Path(astral_sdk.__file__).resolve().parent.parent
    env = dict(os.environ)
    env["ASTRAL_BASE_URL"] = fake_server.base_url
    env["ASTRAL_TOKEN"] = fake_server.state.valid_token
    env["PYTHONPATH"] = str(package_root) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _server_parameters(fake_server, runner: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-c", runner], env=_subprocess_env(fake_server))


async def test_independent_stdio_client_discovers_lists_and_calls(
        fake_server, validate_mcp, pytestconfig):
    for runner in (LIGHT_RUNNER, OFFICIAL_RUNNER):
        async with Client(_server_parameters(fake_server, runner)) as client:
            assert client.protocol_version == "2026-07-28"
            discover = client.session.discover_result
            assert discover is not None
            validate_mcp("DiscoverResult", discover.model_dump(
                by_alias=True, mode="json", exclude_none=True))
            assert "Astral exposes" in discover.instructions

            listing = await client.list_tools()
            validate_mcp("ListToolsResult", listing.model_dump(
                by_alias=True, mode="json", exclude_none=True))
            names = {tool.name for tool in listing.tools}
            assert "astral_submit_operation" in names
            descriptor = next(tool for tool in listing.tools
                              if tool.name == "astral_get_operation")
            validate_mcp("Tool", descriptor.model_dump(
                by_alias=True, mode="json", exclude_none=True))
            assert descriptor.input_schema["required"] == ["operation_id"]
            assert "parameters" not in descriptor.model_dump(
                by_alias=True, mode="json", exclude_none=True)

            operation = await client.call_tool("astral_submit_operation", {
                "idempotency_key": str(uuid.uuid4()), "name": "Interop",
                "instructions": "One paragraph."})
            assert operation.is_error is False
            assert operation.structured_content["created"] is True
            validate_mcp("CallToolResult", operation.model_dump(
                by_alias=True, mode="json", exclude_none=True))
            operation_id = operation.structured_content["id"]

            await client.session.send_notification(
                mcp_types_client_cancelled())

            missing = await client.call_tool(
                "astral_get_operation", {"operation_id": str(uuid.uuid4())})
            assert missing.is_error is True
            assert missing.content[0].text == "work_not_found"

            invalid = await client.call_tool("astral_get_operation", {})
            assert invalid.is_error is True

            read_back = await client.call_tool(
                "astral_get_operation", {"operation_id": operation_id})
            assert read_back.is_error is False
            assert read_back.structured_content["id"] == operation_id
            validate_mcp("CallToolResult", read_back.model_dump(
                by_alias=True, mode="json", exclude_none=True))


def mcp_types_client_cancelled():
    import mcp_types as types

    return types.CancelledNotification(
        params=types.CancelledNotificationParams(request_id="interop-1", reason="host moved on"))


def test_bridge_process_answers_then_exits_cleanly_on_eof(fake_server):
    for runner in (LIGHT_RUNNER, OFFICIAL_RUNNER):
        process = subprocess.Popen(
            [sys.executable, "-c", runner], env=_subprocess_env(fake_server),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}}
        stdout, stderr = process.communicate(json.dumps(request) + "\n", timeout=60)
        assert process.returncode == 0, stderr
        response = json.loads(stdout.splitlines()[-1])
        assert response["id"] == 1
        assert response["result"]["resultType"] == "complete"
