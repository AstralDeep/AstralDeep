"""Exercises preview composition through real project leases, ordered child operations and host dispatch."""

import asyncio
import json

import pytest

from agents.computer_use import mcp_tools
from orchestrator.computer_sessions import ComputerSessionManager
from tests.test_project_host_authority import OWNER, manifest, setup

JOB = "11111111-1111-4111-8111-111111111111"
ROOT = "/tmp/project"


@pytest.fixture
async def preview_runtime(monkeypatch):
    api, orch, ws, host, _, _ = setup(monkeypatch)
    host.descriptor["verbs"] = ["read_file", "run_command", "read_output", "interrupt_command"]
    manager = ComputerSessionManager(orch, orch.computer_hosts)
    orch.computer_sessions = manager
    orch.ui_clients = [ws]
    orch.socket_for_chat = lambda *_: ws
    orch._ws_active_chat = {id(ws): "chat"}
    orch.cancelled_sessions = {}

    async def nothing(*args, **kwargs):
        pass

    manager._audit = nothing
    manager._push = nothing
    manager._ensure_sweeper = lambda: None
    session = await manager.start(OWNER, host, ws, "chat", wait_for_ack=False)
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_LOOP", asyncio.get_running_loop())
    monkeypatch.setattr("orchestrator.terminal_host_review.computer_use_review_current", lambda _orch: True)
    sent = []

    async def send(socket, raw):
        frame = json.loads(raw)
        sent.append(frame)
        verb = frame["verb"]
        if verb == "read_file":
            result = {"text": '{"scripts":{"dev":"vite"}}', "size": 26, "truncated": False}
        elif verb == "run_command" and frame["args"].get("background"):
            result = {"job_id": JOB, "started_at": "today"}
        elif verb == "run_command":
            result = {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}
        else:
            result = {"job_id": JOB, "state": "interrupted" if verb == "interrupt_command" else "running"}
        orch.computer_hosts.handle_response(socket, OWNER, {"request_id": frame["request_id"], "ok": True, "result": result})
        return True

    orch._safe_send = send
    return api, orch, ws, host, session, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "status", "stop"])
@pytest.mark.parametrize("restart", [False, True])
async def test_preview_runs_only_its_children_under_the_bound_project_lease(preview_runtime, action, restart):
    api, orch, ws, host, session, sent = preview_runtime
    request = {"action": action, "cwd": ROOT, "package_manager": "npm", "script": "dev"}
    if restart and action == "start":
        old_job = "22222222-2222-4222-8222-222222222222"
        request["job_id"] = old_job
        session.command_jobs[old_job] = {"state": "exited"}
    if action != "start":
        request["job_id"] = JOB
        session.command_jobs[JOB] = {"state": "running"}
    async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", "project_preview", request):
        result = await asyncio.to_thread(mcp_tools.project_preview, user_id=OWNER, session_id="chat", **request)
    assert "code" not in result["_data"], result
    assert result["_data"]["job_id"] == JOB
    expected = ["read_file", "run_command", "read_output", "run_command", "read_output"] if action == "start" else ["read_output" if action == "status" else "interrupt_command"]
    assert [frame["verb"] for frame in sent] == expected
    assert all(frame["project_context"] == session.project_origin.context.to_dict() for frame in sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["path", "verb", "generation", "stale", "args", "changed_request", "grant"])
async def test_composite_scope_does_not_authorize_unrelated_or_stale_effects(preview_runtime, change):
    api, orch, ws, host, session, sent = preview_runtime
    request = {"action": "start", "cwd": ROOT, "package_manager": "npm", "script": "dev"}
    async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", "project_preview", request):
        def attempt():
            verb, args = "read_file", {"path": ROOT + "/package.json", "max_bytes": 32768}
            if change == "path":
                args["path"] = ROOT + "/other.json"
            elif change == "verb":
                verb = "list_dir"
            elif change == "args":
                args["max_bytes"] = 65536
            elif change == "stale":
                orch.cancelled_sessions[id(ws)] = True
            elif change == "generation":
                session.authority_generation += 1
            elif change == "grant":
                host.project_manifest = manifest(generation=2, revision=2)
            elif change == "changed_request":
                return None, mcp_tools.project_preview(user_id=OWNER, session_id="chat", **{**request, "script": "preview"})
            return mcp_tools._run(host, session, verb, args)
        result, error = await asyncio.to_thread(attempt)
        assert result is None and error["_data"]["code"] in {"confirmation_required", "no_session", "stale_project_grant"}
        assert not sent


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["cwd", "argv", "job", "replay", "expired_scope"])
async def test_ordered_children_cannot_widen_commands_jobs_or_outlive_scope(preview_runtime, mutation):
    import contextvars

    api, orch, ws, host, session, sent = preview_runtime
    request = {"action": "start", "cwd": ROOT, "package_manager": "npm", "script": "dev"}
    async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", "project_preview", request):
        def attempt():
            read = {"path": ROOT + "/package.json", "max_bytes": 32768}
            assert mcp_tools._run(host, session, "read_file", read)[1] is None
            if mutation == "expired_scope":
                return contextvars.copy_context()
            command = {"argv": mcp_tools._preview_argv("npm", "dev"), "cwd": ROOT,
                       "timeout_s": 3600, "background": True}
            if mutation == "cwd":
                command["cwd"] = ROOT + "/subfolder"
            elif mutation == "argv":
                command["argv"] = ["sh", "-c", "echo arbitrary"]
            elif mutation == "replay":
                return mcp_tools._run(host, session, "read_file", read)
            elif mutation == "job":
                assert mcp_tools._run(host, session, "run_command", command)[1] is None
                return mcp_tools._run(host, session, "read_output", {
                    "job_id": "22222222-2222-4222-8222-222222222222", "stream": "both", "offset": 0,
                    "max_bytes": 4096, "wait_s": 2})
            return mcp_tools._run(host, session, "run_command", command)

        value = await asyncio.to_thread(attempt)
    if mutation == "expired_scope":
        result, error = await asyncio.to_thread(value.run, mcp_tools._run, host, session, "read_file",
                                               {"path": ROOT + "/package.json", "max_bytes": 32768})
    else:
        result, error = value
    assert result is None and error["_data"]["code"] in {"confirmation_required", "stale_project_grant"}
    assert len(sent) == (2 if mutation == "job" else 1)


@pytest.mark.asyncio
async def test_one_preview_step_runs_once_across_concurrent_calls(preview_runtime):
    api, orch, ws, host, session, sent = preview_runtime
    session.command_jobs[JOB] = {"state": "running"}
    request = {"action": "status", "cwd": ROOT, "job_id": JOB}
    async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", "project_preview", request):
        async def status():
            return await asyncio.to_thread(mcp_tools.project_preview, user_id=OWNER, session_id="chat", **request)

        responses = await asyncio.gather(status(), status())
    assert len(sent) == 1 and sent[0]["verb"] == "read_output"
    assert sorted(item["_data"].get("code", "ok") for item in responses) == ["confirmation_required", "ok"]
