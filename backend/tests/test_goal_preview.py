"""Checks authorized preview discovery and rejection of malformed or denied evidence."""

import json
from types import SimpleNamespace

import pytest
from orchestrator.goal_preview import _package_manager, start_local_preview
from orchestrator.goal_progress import GoalProgress


def test_package_manager_uses_declared_manager_then_root_lockfiles():
    assert _package_manager({"packageManager": "pnpm@9.1.0"}, {"pnpm-lock.yaml"}) == "pnpm"
    assert _package_manager({"packageManager": "pnpm@9.1.0"}, {"package-lock.json"}) is None
    assert _package_manager({}, {"pnpm-lock.yaml", "package-lock.json"}) is None
    assert _package_manager({}, {"yarn.lock"}) == "yarn"
    assert _package_manager({}, {"bun.lock"}) == "bun"
    assert _package_manager({}, {"package.json"}) is None


@pytest.mark.asyncio
async def test_preview_discovery_dispatches_detected_script_through_authorized_project_tool():
    project = {
        "project_id": "project-1", "computer_id": "computer-1", "computer_name": "Mac",
        "folder_status": "granted", "primary_root": "/workspace/site",
        "workspace_roots": [{"path": "/workspace"}],
    }
    tools = {"list": "computer-use-1", "read": "computer-use-1", "preview": "computer-use-1"}
    canonical = {"list": "list_dir", "read": "read_file", "preview": "project_preview"}
    calls = []
    orch = SimpleNamespace(cancelled_sessions={})

    async def execute(_websocket, call, _owners, _chat_id, **_kwargs):
        arguments = json.loads(call.function.arguments)
        verb = canonical[call.function.name]
        calls.append((verb, arguments))
        if verb == "list_dir":
            data = {"entries": [
                {"name": "package.json", "is_dir": False},
                {"name": "pnpm-lock.yaml", "is_dir": False},
            ]}
        elif verb == "read_file":
            data = {"text": '{"scripts":{"preview":"vite preview"}}', "size": 37, "truncated": False}
        else:
            data = {"job_id": "job-1", "url": "http://localhost:4173/", "state": "running"}
        return SimpleNamespace(error=None, result={"_data": data, "_ui_components": [{"type": "card"}]})

    orch.execute_single_tool = execute
    result = await start_local_preview(
        orch, object(), GoalProgress("Start the completed site"), project,
        tools, canonical, "chat-1", "user-1",
    )
    assert {key: result[key] for key in ("job_id", "url", "state", "status", "ui_components")} == {
        "job_id": "job-1", "url": "http://localhost:4173/", "state": "running",
        "status": "running", "ui_components": [{"type": "card"}],
    }
    assert result["_tool_name"] == "preview"
    assert result["_tool_arguments"]["action"] == "start"
    assert result["_result"].result["_data"]["job_id"] == "job-1"
    assert [verb for verb, _args in calls] == ["list_dir", "read_file", "project_preview"]
    assert calls[-1][1] == {
        "action": "start", "cwd": "/workspace/site", "package_manager": "pnpm", "script": "preview",
    }


@pytest.mark.asyncio
async def test_non_javascript_project_is_explicitly_not_applicable():
    project = {
        "project_id": "project-1", "computer_id": "computer-1", "computer_name": "Mac",
        "folder_status": "granted", "primary_root": "/workspace/game",
        "workspace_roots": [{"path": "/workspace"}],
    }
    orch = SimpleNamespace(cancelled_sessions={})
    calls = []

    async def execute(_websocket, call, _owners, _chat_id, **_kwargs):
        calls.append(call.function.name)
        return SimpleNamespace(error=None, result={"_data": {"entries": [{"name": "main.py", "is_dir": False}]}})

    orch.execute_single_tool = execute
    result = await start_local_preview(
        orch, object(), GoalProgress("Finish game"), project,
        {"list": "computer-use-1", "read": "computer-use-1", "preview": "computer-use-1"},
        {"list": "list_dir", "read": "read_file", "preview": "project_preview"}, "chat-1", "user-1",
    )
    assert result == {"status": "not_applicable", "reason": "The selected project has no JavaScript package manifest."}
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,bad", [
    (0, {"_data": []}),
    (1, {"_data": {"text": '{"scripts":{"preview":"vite preview"}}'}}),
    (2, {"_data": {"state": "running", "job_id": "job", "url": "https://remote.invalid/"}}),
    (2, {"_data": {"state": "running", "url": "http://localhost:4173/"}}),
    (2, {"_data": {"code": "permission_denied"}}),
])
async def test_preview_malformed_or_denied_evidence_never_proves_running(phase, bad):
    project = {
        "project_id": "project", "computer_id": "mac", "computer_name": "Mac",
        "folder_status": "granted", "primary_root": "/workspace/site", "workspace_roots": [{"path": "/workspace"}],
    }
    replies = [
        {"_data": {"entries": [{"name": "package.json"}, {"name": "package-lock.json"}]}},
        {"_data": {"text": '{"scripts":{"preview":"vite preview"}}', "truncated": False}},
        {"_data": {"state": "running", "job_id": "job", "url": "http://localhost:4173/"}},
    ]
    replies[phase] = bad
    calls = []

    async def execute(*args, **kwargs):
        calls.append(args[1].function.name)
        return SimpleNamespace(error=None, result=replies[len(calls) - 1])

    result = await start_local_preview(
        SimpleNamespace(cancelled_sessions={}, execute_single_tool=execute), object(), GoalProgress("Preview site"),
        project, {verb: "computer-use-1" for verb in ("list_dir", "read_file", "project_preview")},
        {}, "chat", "user")
    assert result["status"] in {"blocked", "unavailable"}
    assert len(calls) == phase + 1
