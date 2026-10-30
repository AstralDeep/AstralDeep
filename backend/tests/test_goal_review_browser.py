"""Checks isolated host Chrome launch arguments, project detection and DevTools validation."""

import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import goal_review_browser as browser
from orchestrator.goal_progress import GoalProgress
from orchestrator.goal_review import review_local_project

PROJECT = {"project_id": "p", "computer_id": "h", "computer_name": "Mac", "folder_status": "granted",
           "primary_root": "/work/site", "workspace_roots": [{"path": "/work"}]}
CATEGORIES = ("files", "environment", "dependencies", "tests", "startup")


def test_detects_browser_projects_only_from_authorized_manifest_observations():
    entries = [{"name": "package.json", "is_dir": False}, {"name": "src", "is_dir": True}]
    assert browser.is_web_project(entries, json.dumps({"scripts": {"dev": "vite --host"}}))
    assert browser.is_web_project(entries, json.dumps({"dependencies": {"react": "^19"}}))
    assert not browser.is_web_project(entries, json.dumps({"scripts": {"start": "node cli.js"}}))
    assert not browser.is_web_project([{"name": "pyproject.toml", "is_dir": False}], "{}")
    assert not browser.is_web_project(entries, "invalid json")


def test_launch_is_host_owned_bounded_and_uses_unique_granted_profile():
    args = browser.launch_arguments("/work/site", ".astral-review-browser-abc123")
    assert args["cwd"] == "/work/site"
    assert args["timeout_s"] == 660 and args["background"] is True
    assert args["argv"][0] == browser.CHROME_PATH
    assert "--headless=new" in args["argv"]
    assert "--remote-debugging-port=0" in args["argv"]
    assert "--no-first-run" in args["argv"]
    assert "--no-default-browser-check" in args["argv"]
    assert "--disable-background-networking" in args["argv"]
    assert "--user-data-dir=/work/site/.astral-review-browser-abc123" in args["argv"]


@pytest.mark.parametrize("text,expected", [
    ("42123\n/devtools/browser/0123456789abcdef\n", (42123, "/devtools/browser/0123456789abcdef")),
    ("0\n/devtools/browser/0123456789abcdef", None),
    ("65536\n/devtools/browser/0123456789abcdef", None),
    ("42123\n/devtools/page/0123456789abcdef", None),
    ("42123\n/devtools/browser/no", None),
    ("42123", None),
])
def test_active_port_is_strictly_validated(text, expected):
    assert browser.parse_active_port(text) == expected


def test_reviewer_hint_uses_localhost_and_probe_requires_chrome_protocol():
    assert browser.endpoint(42123) == "http://localhost:42123"
    with pytest.raises(ValueError):
        browser.endpoint(0)
    with pytest.raises(ValueError):
        browser.reviewer_browser_hint("http://127.0.0.1:42123")
    assert "connectOverCDP" in browser.reviewer_browser_hint("http://localhost:42123")
    assert "startsWith('Chrome/')" in browser.endpoint_probe_script()
    assert "ws.pathname!==process.argv[2]" in browser.endpoint_probe_script()
    with pytest.raises(ValueError):
        browser.reviewer_browser_hint("http://localhost:99999")


def browser_verdict(ui=True):
    return json.dumps({"status": "pass", "ui": ui, "checks": [
        {"name": name, "status": "pass" if ui or name in {"code", "tests"} else "not_applicable",
         "evidence": "Playwright connected to host Chrome and clicked the primary navigation button successfully."
         if ui or name in {"code", "tests"} else "Inspected project files and found no application UI to exercise."}
        for name in ("code", "tests", "interactions", "keyboard_focus", "forms_aria", "mobile")
    ], "findings": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("active_file,interrupt_state,expected", [
    ("42123\n/devtools/browser/0123456789abcdef\n", "interrupted", "passed"),
    ("invalid", "interrupted", "blocked"),
    ("invalid", "uncertain", "blocked"),
])
async def test_web_review_uses_isolated_host_browser_and_cleans_owned_job(
    monkeypatch, active_file, interrupt_state, expected,
):
    monkeypatch.setitem(sys.modules, "orchestrator.hitl_confirmation", SimpleNamespace(
        goal_call_scope=lambda _token: nullcontext()))
    monkeypatch.setitem(sys.modules, "orchestrator.goal_project_context", SimpleNamespace(
        project_goal_context=AsyncMock(return_value=PROJECT)))
    websocket = object()
    commands = []

    async def execute(_ws, call, *_args, **_kwargs):
        name = call.function.name
        args = json.loads(call.function.arguments)
        commands.append((name, args))
        if name == "list_dir":
            return {"entries": [{"name": "package.json", "is_dir": False},
                                {"name": "index.html", "is_dir": False}]}
        if name == "read_file" and args["path"].endswith("package.json"):
            return {"text": json.dumps({"scripts": {"dev": "vite --host"},
                                         "devDependencies": {"vite": "^6"}}), "truncated": False}
        if name == "read_file" and args["path"].endswith("DevToolsActivePort"):
            return {"text": active_file, "truncated": False}
        if name == "read_file":
            return {"text": browser_verdict(), "truncated": False}
        if name == "interrupt_command":
            return {"job_id": args["job_id"], "state": interrupt_state, "exit_code": 0}
        if args.get("background") is True:
            return {"job_id": "chrome-owned-job", "background": True}
        if args["argv"][0] == "node" and "rmSync" in args["argv"][2]:
            return {"exit_code": 0}
        if args["argv"][0] == "node":
            return {"exit_code": 0, "stdout": "Chrome DevTools ready"}
        assert "--output-last-message" in args["argv"]
        assert "connectOverCDP(\"http://localhost:42123\")" in args["stdin"]
        assert "Do not call chromium.launch" in args["stdin"]
        return {"exit_code": 0}

    progress = GoalProgress("Build a website")
    result = await review_local_project(
        SimpleNamespace(cancelled_sessions={}, execute_single_tool=execute), websocket, progress, PROJECT,
        SimpleNamespace(kind="local_project", required_categories=CATEGORIES),
        {name: "computer-use-1" for name in ("run_command", "read_file", "list_dir", "interrupt_command")},
        {}, "chat", "user", verify=AsyncMock(return_value={"tests": ["reviewed"]}),
    )
    assert result.status == expected
    launch = next(args for name, args in commands if name == "run_command" and args.get("background"))
    assert launch["timeout_s"] == 660
    assert any(name == "interrupt_command" and args["job_id"] == "chrome-owned-job" for name, args in commands)
    if interrupt_state == "interrupted":
        assert not progress.pending_jobs


@pytest.mark.asyncio
async def test_nonweb_project_does_not_launch_chrome(monkeypatch):
    monkeypatch.setitem(sys.modules, "orchestrator.hitl_confirmation", SimpleNamespace(
        goal_call_scope=lambda _token: nullcontext()))
    monkeypatch.setitem(sys.modules, "orchestrator.goal_project_context", SimpleNamespace(
        project_goal_context=AsyncMock(return_value=PROJECT)))
    commands = []

    async def execute(_ws, call, *_args, **_kwargs):
        args = json.loads(call.function.arguments)
        commands.append((call.function.name, args))
        if call.function.name == "list_dir":
            return {"entries": [{"name": "pyproject.toml", "is_dir": False}]}
        if call.function.name == "run_command":
            return {"exit_code": 0}
        return {"text": browser_verdict(ui=False), "truncated": False}

    result = await review_local_project(
        SimpleNamespace(cancelled_sessions={}, execute_single_tool=execute), object(), GoalProgress("Review"),
        PROJECT, SimpleNamespace(kind="local_project", required_categories=CATEGORIES),
        {name: "computer-use-1" for name in ("run_command", "read_file", "list_dir", "interrupt_command")},
        {}, "chat", "user", verify=AsyncMock(return_value={"tests": ["reviewed"]}),
    )
    assert result.status == "passed"
    assert not any(args.get("background") is True for _, args in commands)
