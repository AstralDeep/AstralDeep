"""Checks file path resolution, host identity and dispatch denials for computer tools."""

import json

import pytest

from orchestrator import computer_use_policy as policy
from tests.test_computer_bypass_102 import _bypassed
from tests.test_computer_grant_flow_102 import ROOT, SCOPES, _call, _mac
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401


@pytest.mark.parametrize("path,expected", [
    ("hello.md", ROOT + "/hello.md"),
    (ROOT + "/sub/../hello.md", ROOT + "/hello.md"),
    (ROOT + "/hello.md", ROOT + "/hello.md"),
])
def test_file_paths_resolve_only_inside_the_shared_folder(path, expected):
    assert policy.resolve_workspace_path(path, [{"name": "x", "path": ROOT}], "Mac") == expected


@pytest.mark.parametrize("path", ["../hello.md", "/Users/bruh/hello.md", ROOT + "-other/a", "", "a\x00b"])
def test_invalid_file_paths_return_actual_shared_roots(path):
    with pytest.raises(policy.CwdRefusal) as caught:
        policy.resolve_workspace_path(path, [{"name": "x", "path": ROOT}], "Mac")
    assert ROOT in str(caught.value)


def test_no_shared_folder_provides_a_useful_failure():
    with pytest.raises(policy.CwdRefusal, match="shares no"):
        policy.resolve_workspace_path("hello.md", [], "Mac")


@pytest.mark.parametrize("tool", ["read_file", "list_dir", "write_file", "delete_path"])
async def test_outside_file_path_never_creates_an_approval(task_runtime, tool):
    orch, _ws, effects, *_ = task_runtime
    await _mac(task_runtime)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, tool: policy.SCOPES[tool]}
    response = await _call(task_runtime, tool, path="/Users/bruh/hello.md", content="poem")
    assert response.error and ROOT in response.error["message"]
    assert not effects


@pytest.mark.parametrize("tool,args", [
    ("run_command", {"argv": ["sudo", "ls"], "cwd": ROOT}),
    ("type_text", {"text": "secret", "hwnd": 50}),
    ("click", {"x": 1, "y": 1, "hwnd": 52}),
    ("type_text", {"text": "x"}),
])
async def test_bypass_denials_never_fall_through_to_an_approval(task_runtime, tool, args):
    orch, _ws, effects, *_ = task_runtime
    await _bypassed(task_runtime)
    response = await _call(task_runtime, tool, **args)
    assert response.error and not effects
    assert "confirmation_required" not in json.dumps(response.result)


def test_multiple_roots_require_an_explicit_primary_for_relative_paths():
    roots = [{"name": "x", "path": ROOT}, {"name": "other", "path": "/Users/sam/Other"}]
    assert policy.resolve_workspace_path("hello.md", roots, "Mac", primary=ROOT) == ROOT + "/hello.md"
    with pytest.raises(policy.CwdRefusal, match="absolute path"):
        policy.resolve_workspace_path("hello.md", roots, "Mac")


async def test_no_session_never_creates_a_dead_file_card(task_runtime):
    orch, _ws, effects, *_ = task_runtime
    session, _host = await _mac(task_runtime)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "write_file": policy.SCOPES["write_file"]}
    await orch.computer_sessions.end(session, "user_stop", cancel_turn=False)
    response = await _call(task_runtime, "write_file", path=ROOT + "/notes.md", content="safe")
    assert response.error and not effects


async def test_sensitive_file_arguments_are_refused_before_creating_a_review(task_runtime):
    orch, _ws, effects, *_ = task_runtime
    await _mac(task_runtime)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "write_file": policy.SCOPES["write_file"]}
    response = await _call(task_runtime, "write_file", path=ROOT + "/notes.md", content="password: synthetic-secret")
    assert response.error and "synthetic-secret" not in response.error["message"]
    assert not effects


async def test_unknown_computer_file_request_never_creates_a_card(task_runtime):
    orch, _ws, effects, *_ = task_runtime
    await _mac(task_runtime)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "write_file": policy.SCOPES["write_file"]}
    response = await _call(task_runtime, "write_file", computer="missing-mac", path=ROOT + "/notes.md", content="safe")
    assert response.error and "computer_unavailable" in response.error["message"]
    assert not effects


@pytest.mark.parametrize("bypass", [False, True])
async def test_computer_discovery_is_available_without_review_in_both_modes(task_runtime, bypass):
    orch, _ws, effects, *_ = task_runtime
    if bypass:
        await _bypassed(task_runtime)
    else:
        await _mac(task_runtime)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "list_computers": "tools:read"}
    response = await _call(task_runtime, "list_computers")
    assert response.error is None and [tool for tool, _args in effects] == ["list_computers"]


@pytest.mark.parametrize("bypass", [False, True])
async def test_rejected_browser_url_is_an_error_without_a_waiting_state(task_runtime, bypass):
    orch, _ws, effects, *_ = task_runtime
    if bypass:
        await _bypassed(task_runtime)
    else:
        await _mac(task_runtime)
    response = await _call(task_runtime, "browser_navigate", url="file:///tmp/notes.md")
    assert response.error and "url_rejected" in response.error["message"]
    assert "confirmation_required" not in json.dumps(response.result)
    assert not effects
