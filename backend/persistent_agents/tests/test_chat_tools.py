"""Tests owner-scoped status, control and retired-command handling in the assignment meta-tool."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from persistent_agents import chat_tools


@pytest.fixture
def host(monkeypatch):
    record = SimpleNamespace(assignment_id=str(uuid4()), definition=SimpleNamespace(name="Releases"),
                             instruction_revision=1, control_epoch=2)
    service = SimpleNamespace(_owner=Mock(), list=AsyncMock(return_value=[record]),
                              activity=AsyncMock(return_value=[]), control=AsyncMock(return_value=record))
    socket = object()
    orch = SimpleNamespace(persistent_assignments=service, ui_sessions={socket: {"sub": "owner"}},
                           _current_request_text=Mock(return_value="please stop Releases"))
    monkeypatch.setattr(chat_tools, "public_record", lambda entry: {"id": entry.assignment_id})
    return orch, socket, record


def test_meta_schema_lists_only_available_owner_commands():
    properties = chat_tools.meta_tool_definitions()[0]["function"]["parameters"]["properties"]
    assert set(properties) == {"command", "assignment"}
    assert set(properties["command"]["enum"]) == {"list", "status", "pause", "stop", "revoke"}


@pytest.mark.parametrize("command", ["create", "revise", "resume", "run-now"])
async def test_retired_commands_cannot_offer_a_consent_card_or_start_work(host, command):
    orch, socket, _ = host
    response = await chat_tools.handle_meta_tool(orch, "ongoing_agent", {"command": command},
                                                user_id="owner", websocket=socket)
    assert response.error["code"] == "turn_engine_retired"
    assert not response.ui_components
    orch.persistent_assignments._owner.assert_called_once_with("owner", {"sub": "owner"})
    orch.persistent_assignments.list.assert_not_awaited()
    orch.persistent_assignments.control.assert_not_awaited()


@pytest.mark.parametrize("command", ["list", "status", "stop"])
async def test_existing_assignment_remains_readable_and_can_be_stopped(host, command):
    orch, socket, record = host
    response = await chat_tools.handle_meta_tool(orch, "ongoing_agent", {"command": command, "assignment": "Releases"},
                                                user_id="owner", websocket=socket)
    expected = {"id": record.assignment_id}
    if command == "list":
        assert response.result["assignments"] == [expected]
    else:
        assert response.result["assignment"] == expected
    assert orch.persistent_assignments.control.await_count == (1 if command == "stop" else 0)


@pytest.mark.parametrize("target,original,expected", [
    ("other", "stop Releases", "selection_required"),
    ("Releases", "release notes", "assignment_explicit_chat_command_required"),
    ("Releases", None, "assignment_explicit_chat_command_required"),
    (None, "stop Releases", "assignment_request_invalid"),
])
async def test_missing_selection_and_unmatched_owner_commands_cannot_stop_work(host, target, original, expected):
    orch, socket, _ = host
    orch._current_request_text.return_value = original
    response = await chat_tools.handle_meta_tool(orch, "ongoing_agent", {"command": "stop", "assignment": target},
                                                user_id="owner", websocket=socket)
    if expected == "selection_required":
        assert response.result["selection_required"] is True
    else:
        assert response.error["code"] == expected
    orch.persistent_assignments.control.assert_not_awaited()
