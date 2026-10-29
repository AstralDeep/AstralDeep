"""Checks malformed file writes stop before local-project authorization gates."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest_plugins = ("tests.test_turn_seams_loop",)


@pytest.mark.asyncio
async def test_missing_path_is_rejected_before_authorization(orch):
    websocket = object()
    orch._authorize_and_prepare = AsyncMock(side_effect=AssertionError("authorization gate must not run"))
    call = SimpleNamespace(
        function=SimpleNamespace(
            name="computer__write",
            arguments=json.dumps({"content": "print('ready')"}),
        )
    )

    response = await orch.execute_single_tool(
        websocket,
        call,
        {"computer__write": "computer-use-1"},
        "chat-a",
        "owner-a",
        tool_to_unqualified={"computer__write": "write_file"},
    )

    assert response.error["code"] == "invalid_tool_arguments"
    assert response.error["effect_state"] == "not_started"
    assert response.error["data"]["missing_keys"] == ["path"]
    orch._authorize_and_prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_write_is_rejected_before_authorization(orch):
    websocket = object()
    orch._authorize_and_prepare = AsyncMock(side_effect=AssertionError("authorization gate must not run"))
    call = SimpleNamespace(
        function=SimpleNamespace(
            name="computer__write",
            arguments=json.dumps({"path": "/workspace/project.py", "content": "x" * 262145}),
        )
    )

    response = await orch.execute_single_tool(
        websocket,
        call,
        {"computer__write": "computer-use-1"},
        "chat-a",
        "owner-a",
        tool_to_unqualified={"computer__write": "write_file"},
    )

    assert response.error["code"] == "invalid_tool_arguments"
    assert response.error["effect_state"] == "not_started"
    assert "content" not in response.error["data"]
    orch._authorize_and_prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_raw_arguments_are_rejected_before_json_parsing(orch):
    websocket = object()
    orch._authorize_and_prepare = AsyncMock(side_effect=AssertionError("authorization gate must not run"))
    call = SimpleNamespace(
        function=SimpleNamespace(
            name="computer__write",
            arguments='{"content":"' + "x" * (262144 * 6 + 4097) + '"}',
        )
    )

    response = await orch.execute_single_tool(
        websocket,
        call,
        {"computer__write": "computer-use-1"},
        "chat-a",
        "owner-a",
        tool_to_unqualified={"computer__write": "write_file"},
    )

    assert response.error["code"] == "invalid_tool_arguments"
    assert response.error["effect_state"] == "not_started"
    assert "content" not in response.error["data"]
    orch._authorize_and_prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_write_json_is_rejected_before_authorization(orch):
    websocket = object()
    orch._authorize_and_prepare = AsyncMock(side_effect=AssertionError("authorization gate must not run"))
    call = SimpleNamespace(
        function=SimpleNamespace(name="computer__write", arguments='{"path":'),
    )

    response = await orch.execute_single_tool(
        websocket,
        call,
        {"computer__write": "computer-use-1"},
        "chat-a",
        "owner-a",
        tool_to_unqualified={"computer__write": "write_file"},
    )

    assert response.error["code"] == "invalid_tool_arguments"
    assert response.error["effect_state"] == "not_started"
    orch._authorize_and_prepare.assert_not_awaited()
