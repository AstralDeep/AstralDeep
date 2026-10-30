"""Exercises fail-closed independent project review and bounded repair dispatch."""

import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from orchestrator.goal_review import (
    build_review_command,
    parse_verdict,
    review_local_project,
)

PROJECT = {"folder_status": "granted", "primary_root": "/work/demo", "workspace_roots": [{"path": "/work"}]}
CATEGORIES = ("files", "environment", "dependencies", "tests", "startup")


@pytest.fixture(autouse=True)
def isolated_goal_scope(monkeypatch):
    monkeypatch.setitem(sys.modules, "orchestrator.hitl_confirmation", SimpleNamespace(
        goal_call_scope=lambda token: nullcontext()))
    monkeypatch.setitem(sys.modules, "orchestrator.goal_project_context", SimpleNamespace(
        project_goal_context=AsyncMock(return_value=PROJECT)))


def verdict(status="pass", ui=True):
    return {"status": status, "ui": ui, "checks": [
        {"name": name, "status": "fail" if status == "repair" and name == "interactions" else "pass",
         "evidence": "playwright test: clicked New project; observed accessible dialog opening at 390px"}
        for name in ("code", "tests", "interactions", "keyboard_focus", "forms_aria", "mobile")
    ], "findings": ["New project button did not open a dialog"] if status == "repair" else []}


def command_result(value):
    return {"review_value": value}


@pytest.mark.parametrize("mutate", [
    lambda v: v.update(status="unknown"),
    lambda v: v.update(checks=[]),
    lambda v: v["checks"][0].update(evidence=""),
    lambda v: v["checks"][0].update(status="not_applicable"),
    lambda v: v["checks"][1].update(name="code"),
    lambda v: v["checks"][0].update(status="fail"),
    lambda v: v.update(findings=["Observed button remained inert"]),
])
def test_invalid_verdict_fails_closed(mutate):
    value = verdict()
    mutate(value)
    with pytest.raises(ValueError):
        parse_verdict(json.dumps(value))


def test_non_ui_can_explain_ui_checks():
    value = verdict(ui=False)
    for row in value["checks"][2:]:
        row["status"] = "not_applicable"
    assert parse_verdict(json.dumps(value))["status"] == "pass"


def test_review_command_is_fresh_readonly_and_bounded():
    command = build_review_command("Build a project", CATEGORIES, PROJECT)
    assert "--ephemeral" in command["argv"]
    assert "workspace-write" in command["argv"]
    assert "--output-last-message" in command["argv"]
    assert "gpt-6-luna" in command["argv"]
    assert "browser automation" in command["stdin"]
    assert "toolkit-appropriate input/event tests" in command["stdin"]
    attached = build_review_command("Build a project", CATEGORIES, PROJECT,
                                    browser_endpoint="http://localhost:42123")
    assert "connectOverCDP(\"http://localhost:42123\")" in attached["stdin"]
    assert "chromium.launch" in attached["stdin"]
    with pytest.raises(ValueError):
        build_review_command("x" * 16300, CATEGORIES, PROJECT)


async def run_loop(results, *, cancelled=False, approval=False, verify_result=None, objective="Build project"):
    expanded = []
    for result in results:
        if "review_value" in result:
            expanded.extend([{"exit_code": 0}, {"text": json.dumps(result["review_value"]), "truncated": False}])
        elif result.get("exit_code") == 0 and "stdout" in result:
            expanded.extend([{"exit_code": 0}, {"text": result["stdout"], "truncated": result.get("truncated", False)}])
        else:
            expanded.append(result)
    expanded.insert(0, {"entries": [{"name": "pyproject.toml", "is_dir": False}]})
    websocket = object()
    orch = SimpleNamespace(cancelled_sessions={id(websocket): cancelled},
                           execute_single_tool=AsyncMock(side_effect=expanded),
                           task_manager=SimpleNamespace(assert_current_execution=AsyncMock()))
    progress = SimpleNamespace(request=objective, requires_approval=approval, record=lambda *a, **k: None)
    verify = AsyncMock(return_value={"tests": ["proof"]} if verify_result is None else verify_result)
    result = await review_local_project(
        orch, websocket, progress, PROJECT, SimpleNamespace(kind="local_project", required_categories=CATEGORIES),
        {"host_run": "computer-use-1", "host_read": "computer-use-1", "host_list": "computer-use-1",
         "host_interrupt": "computer-use-1"},
        {"host_run": "run_command", "host_read": "read_file", "host_list": "list_dir",
         "host_interrupt": "interrupt_command"}, "chat", "user", task="task", verify=verify)
    return result, orch, verify


@pytest.mark.asyncio
async def test_defect_repair_fresh_review_and_verify():
    result, orch, verify = await run_loop([
        command_result(verdict("repair")), {"exit_code": 0}, command_result(verdict())])
    assert result.status == "passed"
    assert result.rounds == 2
    assert orch.execute_single_tool.await_count == 6
    assert verify.await_count == 2
    calls = orch.execute_single_tool.await_args_list
    assert "workspace-write" in json.loads(calls[1].args[1].function.arguments)["argv"]
    assert "workspace-write" in json.loads(calls[3].args[1].function.arguments)["argv"]
    assert "workspace-write" in json.loads(calls[4].args[1].function.arguments)["argv"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    {"exit_code": 0, "stdout": "not JSON"},
    {"exit_code": 0}, {"exit_code": 0, "stdout": "{}", "truncated": True},
    {"status": "permission_denied"}, {"exit_code": 3},
])
async def test_invalid_missing_denied_outputs_block(result):
    outcome, orch, verify = await run_loop([result, {}] if result == {"exit_code": 0} else [result])
    assert outcome.status == "blocked"
    assert orch.execute_single_tool.await_count in {2, 3}
    verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhaustion_is_bounded():
    result, orch, _ = await run_loop([
        command_result(verdict("repair")), {"exit_code": 0},
        command_result(verdict("repair")), {"exit_code": 0}, command_result(verdict("repair"))])
    assert result.status == "blocked"
    assert orch.execute_single_tool.await_count == 9


@pytest.mark.asyncio
async def test_cancel_before_dispatch():
    result, orch, _ = await run_loop([], cancelled=True)
    assert result.status == "cancelled"
    orch.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_repair_denial_and_failed_verification():
    result, orch, _ = await run_loop([command_result(verdict("repair")), {"status": "permission_denied"}])
    assert result.status == "blocked"
    assert orch.execute_single_tool.await_count == 4
    result, _, _ = await run_loop([command_result(verdict())], verify_result={})
    assert result.status == "blocked"
    result, _, _ = await run_loop([command_result(verdict("repair")), {"exit_code": 0}], verify_result={})
    assert result.status == "blocked"


@pytest.mark.parametrize("value", [None, "x" * 12001, json.dumps({**verdict("repair"), "findings": []}),
                                      json.dumps({**verdict(), "findings": [4]})])
def test_missing_or_invalid_findings(value):
    with pytest.raises(ValueError):
        parse_verdict(value)


@pytest.mark.asyncio
async def test_authority_change_blocks_before_dispatch(monkeypatch):
    context = sys.modules["orchestrator.goal_project_context"]
    context.project_goal_context.return_value = {**PROJECT, "folder_status": "ungranted"}
    result, orch, _ = await run_loop([])
    assert result.status == "blocked"
    orch.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_verifier_and_active_command_scope(monkeypatch):
    from orchestrator.goal_review import is_active_review_command

    verification = AsyncMock(return_value={"tests": ["proof"]})
    monkeypatch.setitem(sys.modules, "orchestrator.goal_auto_verify", SimpleNamespace(verify_local_project=verification))
    websocket = object()
    commands = []

    async def execute(ws, call, *args, **kwargs):
        arguments = json.loads(call.function.arguments)
        commands.append(arguments)
        if call.function.name == "list_dir":
            return {"entries": [{"name": "pyproject.toml", "is_dir": False}]}
        if call.function.name == "run_command":
            assert is_active_review_command(arguments)
            return {"exit_code": 0}
        assert not is_active_review_command(arguments)
        return {"text": json.dumps(verdict()), "truncated": False}

    result = await review_local_project(
        SimpleNamespace(cancelled_sessions={}, execute_single_tool=execute), websocket,
        SimpleNamespace(request="Build project", requires_approval=False, record=lambda *a, **k: None),
        PROJECT, SimpleNamespace(kind="local_project", required_categories=CATEGORIES),
        {"run_command": "computer-use-1", "read_file": "computer-use-1", "list_dir": "computer-use-1",
         "interrupt_command": "computer-use-1"}, {}, "chat", "user")
    assert result.status == "passed"
    verification.assert_awaited_once()
    assert not is_active_review_command(commands[1])


@pytest.mark.asyncio
async def test_oversized_complete_review_preview_blocks_before_launch():
    result, orch, _ = await run_loop([], objective="x" * 3000)
    assert result.status == "blocked"
    orch.execute_single_tool.assert_not_awaited()
