"""Checks isolated host Chrome launch arguments, project detection and DevTools validation."""

import json
import shutil
import subprocess
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
    assert not browser.is_web_project(entries, json.dumps({"dependencies": {"react-native": "^0.80"}}))
    assert browser.vite_script(json.dumps({"scripts": {"dev": "vite --host"}})) == "dev"
    assert browser.vite_script(json.dumps({"scripts": {"start": "node app.js"}})) is None
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
    assert "Date.now()+8000" in browser.active_port_wait_script()
    assert "setTimeout(poll,100)" in browser.active_port_wait_script()


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
    assert "ws.hostname!==u.hostname" in browser.endpoint_probe_script()
    assert len(browser.endpoint_probe_script()) <= 512
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
@pytest.mark.parametrize("static_site", [False, True])
@pytest.mark.parametrize("active_file,wait_ready,interrupt_state,expected,active_truncated,preview_denied", [
    ("42123\n/devtools/browser/0123456789abcdef\n", True, "interrupted", "passed", False, False),
    ("invalid", True, "interrupted", "blocked", False, False),
    ("42123\n/devtools/browser/0123456789abcdef\n", False, "interrupted", "blocked", False, False),
    ("invalid", True, "uncertain", "blocked", False, False),
    ("42123\n/devtools/browser/0123456789abcdef\n", True, "permission_denied", "blocked", False, False),
    ("42123\n/devtools/browser/0123456789abcdef\n", True, "interrupted", "blocked", True, False),
    ("42123\n/devtools/browser/0123456789abcdef\n", True, "interrupted", "blocked", False, True),
])
async def test_web_review_uses_isolated_host_browser_and_cleans_owned_job(
    monkeypatch, active_file, wait_ready, interrupt_state, expected, active_truncated, preview_denied, static_site,
):
    if preview_denied and static_site:
        pytest.skip("preview denial applies only to manifest-based Vite projects")
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
            if static_site:
                return {"entries": [{"name": "index.html", "is_dir": False}]}
            return {"entries": [{"name": "package.json", "is_dir": False},
                                {"name": "index.html", "is_dir": False},
                                {"name": "package-lock.json", "is_dir": False}]}
        if name == "read_file" and args["path"].endswith("package.json"):
            return {"text": json.dumps({"packageManager": "npm@10", "scripts": {"dev": "vite --host"},
                                         "devDependencies": {"vite": "^6"}}), "truncated": False}
        if name == "read_file" and args["path"].endswith("DevToolsActivePort"):
            return {"text": active_file, "truncated": active_truncated}
        if name == "read_file":
            return {"text": browser_verdict(), "truncated": False}
        if name == "interrupt_command":
            if interrupt_state == "permission_denied":
                return {"code": "permission_denied", "message": "Job control denied.",
                        "next_action": "Review the active host session."}
            return {"job_id": args["job_id"], "state": interrupt_state, "exit_code": 0}
        if name == "project_preview":
            if preview_denied:
                return {"code": "permission_denied", "message": "Preview denied.",
                        "next_action": "Review the project grant."}
            return {"_data": {"state": "running", "job_id": "preview-owned-job",
                               "url": "http://localhost:4173/"}}
        if args.get("background") is True:
            return {"job_id": "chrome-owned-job", "background": True}
        if args["argv"][0] == "node" and "rmSync" in args["argv"][2]:
            return {"exit_code": 0}
        if args["argv"][0] == "node":
            if "Date.now()+8000" in args["argv"][2]:
                return {"exit_code": 0 if wait_ready else 1}
            return {"exit_code": 0, "stdout": "Chrome DevTools ready"}
        assert "--output-last-message" in args["argv"]
        assert "connectOverCDP(\"http://localhost:42123\")" in args["stdin"]
        assert "Do not call chromium.launch" in args["stdin"]
        if static_site:
            assert "--strictPort" in args["stdin"]
            assert "Use the existing app at http://localhost:4173/" not in args["stdin"]
        else:
            assert "Use the existing app at http://localhost:4173/" in args["stdin"]
        return {"exit_code": 0}

    progress = GoalProgress("Build a website")
    result = await review_local_project(
        SimpleNamespace(cancelled_sessions={}, execute_single_tool=execute), websocket, progress, PROJECT,
        SimpleNamespace(kind="local_project", required_categories=CATEGORIES),
        {name: "computer-use-1" for name in ("run_command", "read_file", "list_dir", "interrupt_command", "project_preview")},
        {}, "chat", "user", verify=AsyncMock(return_value={"tests": ["reviewed"]}),
    )
    assert result.status == expected
    attested = [row for row in progress.private_observations() if row.get("attested_worker")]
    reviewer_started = any(name == "run_command" and "--output-last-message" in args.get("argv", [])
                           for name, args in commands)
    assert len(attested) == (1 if reviewer_started else 0)
    if preview_denied and not static_site:
        assert not reviewer_started
        assert not any(name == "interrupt_command" for name, _ in commands)
        return
    launch = next(args for name, args in commands if name == "run_command" and args.get("background"))
    assert launch["timeout_s"] == 660
    assert any(name == "interrupt_command" and args["job_id"] == "chrome-owned-job" for name, args in commands)
    if not static_site:
        assert any(name == "project_preview" and args.get("action") == "start" for name, args in commands)
        assert any(name == "interrupt_command" and args["job_id"] == "preview-owned-job"
                   for name, args in commands)
    wait_index = next(i for i, (name, args) in enumerate(commands)
                      if name == "run_command" and args.get("argv", [None, None, ""])[0] == "node"
                      and "Date.now()+8000" in args.get("argv", [None, None, ""])[2])
    active_read_index = next((i for i, (name, args) in enumerate(commands)
                              if name == "read_file" and args["path"].endswith("DevToolsActivePort")), None)
    assert (active_read_index is not None) == wait_ready
    if active_read_index is not None:
        assert wait_index < active_read_index
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
        {name: "computer-use-1" for name in ("run_command", "read_file", "list_dir", "interrupt_command", "project_preview")},
        {}, "chat", "user", verify=AsyncMock(return_value={"tests": ["reviewed"]}),
    )
    assert result.status == "passed"
    assert not any(args.get("background") is True for _, args in commands)


@pytest.mark.parametrize("hostname,expected", [("localhost", 0), ("remote.invalid", 2)])
def test_endpoint_probe_rejects_websocket_host_redirect(hostname, expected):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the host endpoint probe")
    path = "/devtools/browser/0123456789abcdef"
    response = {"Browser": "Chrome/123", "webSocketDebuggerUrl": f"ws://{hostname}:42123{path}"}
    source = "global.fetch=async()=>({ok:true,json:async()=>(" + json.dumps(response) + ")});" + browser.endpoint_probe_script()
    result = subprocess.run([node, "-e", source, "http://localhost:42123/json/version", path],
                            capture_output=True, timeout=15, check=False)
    assert result.returncode == expected
