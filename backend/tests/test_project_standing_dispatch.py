"""Exercises project host grants through the physical dispatch gate for owner-scoped reads and reviewed effects."""
# ruff: noqa: F811

from types import SimpleNamespace

import pytest

from orchestrator import project_authority
from orchestrator.computer_use_policy import SCOPES
from shared.project_workspaces import ProjectManifest
from tests.test_computer_grant_flow_102 import _mac, _call
from tests.test_hitl_public_research import runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401

PROJECT = "12345678-1234-1234-9234-123456789abc"


async def project_runtime(task_runtime, monkeypatch):
    orch, ws, *_ = task_runtime
    session, host = await _mac(task_runtime)
    orch.tool_permissions.get_tool_scope_map.return_value = dict(SCOPES)
    host.project_mode = True
    host.descriptor["workspace_roots"] = []
    host.project_manifest = ProjectManifest.from_dict({"generation": 1, "projects": [{"project_id": PROJECT,
        "grant_revision": 1, "roots": [{"name": "Primary", "path": "/tmp/project-a"},
                                      {"name": "Second", "path": "/tmp/second"}]}]})
    orch.ui_sessions[host.websocket] = {"sub": "owner-a", "_project_workspaces_v1": True}
    orch._connection_contexts[id(host.websocket)] = SimpleNamespace(websocket=host.websocket, registered=True,
        closing=False, connection_generation="host-connection")
    host.project_manifest_highwater = 1
    class Service:
        def __init__(self, history):
            pass
        def require_chat(self, owner, chat):
            assert owner == "owner-a" and chat == "chat-a"
            return PROJECT
    monkeypatch.setattr(project_authority, "ProjectService", Service)
    session.project_origin = await project_authority.resolve_project_origin(orch, "owner-a", "chat-a", host)
    return session, host


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "hello.md", "content": "hello"}),
    ("delete_path", {"path": "hello.md"}),
    ("run_command", {"argv": ["pwd"]}),
])
async def test_project_grant_alone_cannot_release_an_effect(task_runtime, monkeypatch, tool, args):
    orch, _, effects, *_ = task_runtime
    session, _host = await project_runtime(task_runtime, monkeypatch)
    response = await _call(task_runtime, tool, **args)
    assert response.error["code"] == "refused" and "requires attended authorization" in response.error["message"]
    assert not response.ui_components and not effects
    await orch.computer_sessions.end(session, "consent_revoked")


@pytest.mark.parametrize("tool,args", [("read_file", {"path": "hello.md"}), ("list_dir", {"path": "."})])
async def test_project_reads_record_standing_source_without_cards(task_runtime, monkeypatch, tool, args):
    orch, ws, effects, *_ = task_runtime
    session, _ = await project_runtime(task_runtime, monkeypatch)
    response = await _call(task_runtime, tool, **args)
    assert response.error is None, response.error
    assert response.result["_data"]["approval_source"] == "project_grant"
    assert [name for name, _ in effects] == [tool] and not response.ui_components
    events = [call.args[0] for call in orch.audit_recorder.record.await_args_list]
    assert [event.action_type for event in events] == [
        "hitl.project_grant", "hitl.effect_started", "hitl.effect_response_success",
    ]
    assert all(event.inputs_meta.get("approval_source") == "project_grant" for event in events)
    assert len({event.correlation_id for event in events}) == 1
    await orch.computer_sessions.end(session, "consent_revoked")


@pytest.mark.parametrize("transition", ["project_grant", "effect_started"])
@pytest.mark.parametrize("change", ["grant", "session", "connection"])
async def test_project_read_rechecks_authority_after_each_audit(task_runtime, monkeypatch, transition, change):
    orch, ws, effects, *_ = task_runtime
    session, host = await project_runtime(task_runtime, monkeypatch)

    async def revoke(event):
        if event.action_type == "hitl." + transition:
            if change == "grant":
                host.project_manifest = ProjectManifest.from_dict({"generation": 2, "projects": []})
            elif change == "session":
                await orch.computer_sessions.pause(session, "manual_pause")
            else:
                orch._connection_contexts[id(ws)].closing = True
        return object()

    orch.audit_recorder.record.side_effect = revoke
    response = await _call(task_runtime, "read_file", path="hello.md")
    assert response.error["code"] == "project_authority_unavailable"
    assert not effects
    await orch.computer_sessions.end(session, "consent_revoked")


@pytest.mark.parametrize("tool,args", [("read_file", {"path": "/tmp/project-a/a"}), ("run_command", {"argv": ["pwd"]})])
async def test_text_only_project_file_tools_refuse_even_with_bypass(task_runtime, monkeypatch, tool, args):
    orch, _, effects, *_ = task_runtime
    session, host = await project_runtime(task_runtime, monkeypatch)
    host.project_manifest = ProjectManifest.from_dict({"generation": 2, "projects": []})
    session.project_origin = await project_authority.resolve_project_origin(orch, "owner-a", "chat-a", host)
    await orch.computer_sessions.set_bypass(host, True)
    response = await _call(task_runtime, tool, **args)
    assert response.error["code"] == "project_grant_required"
    assert not effects
    await orch.computer_sessions.end(session, "consent_revoked")


async def test_gate_wrapper_does_not_reclassify_body_exception_as_admission_denial(task_runtime, monkeypatch):
    from unittest.mock import AsyncMock
    from orchestrator.computer_hosts import ComputerHostError
    orch, ws, *_ = task_runtime
    session, _ = await project_runtime(task_runtime, monkeypatch)
    async def body(*args, **kwargs):
        raise ComputerHostError("outcome_uncertain", "Body effect may have completed")
    monkeypatch.setattr(orch, "_run_project_bound_gate_stack", body)
    denial = AsyncMock()
    monkeypatch.setattr(orch, "_audit_gate_denial", denial)
    with pytest.raises(ComputerHostError, match="may have completed"):
        await orch._run_gate_stack(ws, "computer-use-1", "write_file", {"path": "a", "content": "x"}, "chat-a", "owner-a")
    denial.assert_not_called()
    await orch.computer_sessions.end(session, "consent_revoked")
