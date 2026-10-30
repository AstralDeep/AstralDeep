"""Checks that local-project planners cannot drift into unrelated account tools after an execution failure."""

from types import SimpleNamespace

import pytest

from orchestrator.goal_execution import focus_project_catalog, authentication_unavailable
from orchestrator.goal_requirements import build_goal_contract


PROJECT = {
    "project_id": "project", "computer_id": "mac", "computer_name": "Work Mac",
    "folder_status": "granted", "primary_root": "/work/project",
    "workspace_roots": [{"path": "/work/project"}],
}


def _catalog():
    agents = {
        "computer__run": "computer-use-1", "computer__write": "computer-use-1",
        "computer__preview": "computer-use-1",
        "browser__navigate": "computer-use-1", "clipboard": "computer-use-1",
        "devices": "computer-use-1", "screen": "computer-use-1", "windows": "computer-use-1",
        "outlook": "outlook-1", "memory": "__memory__", "scheduler": "__scheduler__",
        "create_agent": "__orchestrator__", "delegate": "__subtasks__",
        "guidance": "__skill_guidance__", "foreign_write": "custom-agent",
    }
    names = {
        "computer__run": "run_command", "computer__write": "write_file",
        "computer__preview": "project_preview",
        "browser__navigate": "browser_navigate", "clipboard": "get_clipboard",
        "foreign_write": "write_file",
        "devices": "list_computers", "screen": "screenshot", "windows": "list_windows",
    }
    tools = [{"type": "function", "function": {"name": name}} for name in agents]
    return tools, agents, names


def test_local_project_catalog_keeps_registered_project_execution_and_guidance_only():
    tools, agents, names = _catalog()
    focused, owners, canonical = focus_project_catalog(
        tools, agents, names, PROJECT, build_goal_contract("local_project", PROJECT))
    assert [item["function"]["name"] for item in focused] == [
        "computer__run", "computer__write", "computer__preview", "guidance"]
    assert set(owners) == {"computer__run", "computer__write", "computer__preview", "guidance"}
    assert canonical == {name: names[name] for name in names if name in owners}
    assert len(tools) == 15
    assert "outlook" in agents


@pytest.mark.parametrize("kind,project,explicit", [
    ("informational", PROJECT, False), ("action", PROJECT, False),
    ("local_project", None, False), ("local_project", PROJECT, True),
])
def test_other_requests_and_explicit_authorized_selections_keep_their_catalog(kind, project, explicit):
    tools, agents, names = _catalog()
    result = focus_project_catalog(
        tools, agents, names, project, build_goal_contract(kind, project), explicit_selection=explicit)
    assert result[0] is tools
    assert result[1] is agents
    assert result[2] is names


def test_focus_cannot_add_a_disabled_or_unregistered_tool():
    tools = [{"type": "function", "function": {"name": "foreign_write"}}]
    result = focus_project_catalog(
        tools, {"foreign_write": "outlook-1"}, {"foreign_write": "write_file"},
        PROJECT, build_goal_contract("local_project", PROJECT))
    assert result == ([], {}, {})


@pytest.mark.parametrize("result,expected", [
    (SimpleNamespace(error={"code": "session_auth_unavailable"}), True),
    (SimpleNamespace(error={"code": "permission_denied"}), False),
    (SimpleNamespace(error={"message": "session_auth_unavailable"}), False),
    (SimpleNamespace(error=None, result={"_data": {"code": "session_auth_unavailable"}}), False),
    (None, False),
])
def test_authentication_interlock_requires_the_typed_error_envelope(result, expected):
    assert authentication_unavailable(result) is expected
