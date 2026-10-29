"""Verifies privacy screening distinguishes current project routing targets from action content.
Dated targets cross the real consent gates while revocation, forged targets and sensitive payloads fail closed.
"""
# ruff: noqa: F811

from types import SimpleNamespace

import pytest

from orchestrator import hitl_confirmation as hc, project_authority, remote_confirmation
from orchestrator.computer_hosts import ComputerHostError
from shared.project_workspaces import ProjectManifest
from tests.test_computer_grant_flow_102 import _call, _mac
from tests.test_hitl_public_research import runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401
from tests.test_project_standing_dispatch import PROJECT, project_runtime

ROOT = "/tmp/project-20261027"


async def dated_runtime(task_runtime, monkeypatch):
    session, host = await project_runtime(task_runtime, monkeypatch)
    orch = task_runtime[0]
    host.project_manifest = ProjectManifest.from_dict({"generation": 2, "projects": [{"project_id": PROJECT,
        "grant_revision": 2, "roots": [{"name": "Primary", "path": ROOT}]}]})
    host.project_manifest_highwater = 2
    session.project_origin = await project_authority.resolve_project_origin(orch, "owner-a", "chat-a", host)
    return session, host


@pytest.mark.parametrize("tool,args,key,target", [
    ("write_file", {"path": "note.md", "content": "hello", "if_exists": "refuse"}, "path", ROOT + "/note.md"),
    ("delete_path", {"path": ROOT + "/note.md"}, "path", ROOT + "/note.md"),
    ("run_command", {"argv": ["pwd"]}, "cwd", ROOT),
    ("run_command", {"argv": ["pwd"], "cwd": ROOT}, "cwd", ROOT),
])
async def test_validated_dated_targets_keep_standing_consent(task_runtime, monkeypatch, tool, args, key, target):
    orch, _ws, effects, *_ = task_runtime
    session, _host = await dated_runtime(task_runtime, monkeypatch)
    screened = []
    monkeypatch.setattr("personalization.phi_gate.get_phi_gate", lambda: SimpleNamespace(
        contains_phi=lambda text: screened.append(text) or False))
    response = await _call(task_runtime, tool, **args)
    assert response.error is None, response.error
    assert response.result["_data"]["approval_source"] == "project_grant"
    assert len(effects) == 1 and effects[0][1][key] == target
    assert all(ROOT not in text for text in screened)
    if tool != "delete_path":
        assert screened
    if tool == "write_file":
        assert any("hello" in text for text in screened)
    assert not getattr(orch, "_hitl_pending_calls", {}) and not remote_confirmation._PENDING_CARDS
    await orch.computer_sessions.end(session, "consent_revoked")


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "note.md", "content": "12345678"}),
    ("write_file", {"path": "note.md", "content": "password: hidden"}),
    ("write_file", {"path": "credentials-20261027.md", "content": "hello"}),
    ("write_file", {"path": "note.md", "content": "hello", "project_targets": ["12345678"]}),
    ("run_command", {"argv": ["echo", "12345678"]}),
    ("run_command", {"command": "echo 12345678"}),
    ("run_command", {"argv": ["pwd"], "env": {"APP_VALUE": "12345678"}}),
    ("run_command", {"argv": ["sudo", "pwd"]}),
])
async def test_routing_exception_does_not_suppress_content_secrets_or_floors(task_runtime, monkeypatch, tool, args):
    orch, _ws, effects, *_ = task_runtime
    session, _host = await dated_runtime(task_runtime, monkeypatch)
    response = await _call(task_runtime, tool, **args)
    assert response.error is not None
    assert not effects and not getattr(orch, "_hitl_pending_calls", {})
    await orch.computer_sessions.end(session, "consent_revoked")


@pytest.mark.parametrize("failure", ["positive", "unavailable"])
async def test_actual_content_phi_analyzer_stays_fail_closed(task_runtime, monkeypatch, failure):
    orch, _ws, effects, *_ = task_runtime
    session, _host = await dated_runtime(task_runtime, monkeypatch)
    def contains_phi(text):
        assert "hello" in text and ROOT not in text
        if failure == "unavailable":
            raise RuntimeError("privacy unavailable")
        return True
    monkeypatch.setattr("personalization.phi_gate.get_phi_gate", lambda: SimpleNamespace(contains_phi=contains_phi))
    response = await _call(task_runtime, "write_file", path="note.md", content="hello")
    assert response.error is not None and not effects
    await orch.computer_sessions.end(session, "consent_revoked")


@pytest.mark.parametrize("path", ["/tmp/elsewhere-20261027/note.md", "../elsewhere-20261027/note.md"])
async def test_forged_outside_target_never_receives_routing_authority(task_runtime, monkeypatch, path):
    orch, _ws, effects, *_ = task_runtime
    session, _host = await dated_runtime(task_runtime, monkeypatch)
    response = await _call(task_runtime, "write_file", path=path, content="hello")
    assert response.error["code"] == "path_outside_workspace"
    assert not effects
    await orch.computer_sessions.end(session, "consent_revoked")


async def test_revoked_grant_during_privacy_screening_cannot_dispatch(task_runtime, monkeypatch):
    orch, _ws, effects, *_ = task_runtime
    session, host = await dated_runtime(task_runtime, monkeypatch)
    review = hc._review_arguments
    def revoke(args, **kwargs):
        result = review(args, **kwargs)
        host.project_manifest = ProjectManifest.from_dict({"generation": 3, "projects": []})
        return result
    monkeypatch.setattr(hc, "_review_arguments", revoke)
    response = await _call(task_runtime, "write_file", path="note.md", content="hello")
    assert response.error is not None and not effects
    await orch.computer_sessions.end(session, "consent_revoked")


async def test_only_exact_top_level_current_project_targets_are_exempt(task_runtime, monkeypatch):
    orch, ws, *_ = task_runtime
    session, host = await dated_runtime(task_runtime, monkeypatch)
    path = ROOT + "/note.md"
    assert not project_authority.project_review_targets(orch, "owner-a", "chat-a", host, "write_file", {"path": path})
    async with project_authority.project_dispatch_scope(orch, ws, "owner-a", "chat-a", "computer-use-1", "write_file", {"path": path}):
        targets = project_authority.project_review_targets(orch, "owner-a", "chat-a", host, "write_file", {"path": path})
        assert targets == frozenset({("path", path)})
        assert not project_authority.project_review_targets(orch, "owner-a", "chat-a", host, "write_file", {"path": "note.md"})
        with pytest.raises(ComputerHostError):
            project_authority.project_review_targets(orch, "owner-a", "chat-a", host, "write_file", {"path": None})
        assert not project_authority.project_review_targets(orch, "owner-a", "chat-a", host, "wait", {})
        with pytest.raises(ComputerHostError):
            project_authority.project_review_targets(orch, "owner-a", "chat-a", host, "write_file", {"path": "/tmp/outside"})
        _, allowed = hc._review_arguments({"path": path, "content": "hello"}, project_targets=targets)
        assert allowed
        _, nested = hc._review_arguments({"content": {"path": path}}, project_targets=targets)
        assert not nested
        host.project_manifest = ProjectManifest.from_dict({"generation": 3, "projects": []})
        assert not project_authority.project_review_targets(orch, "owner-a", "chat-a", host, "write_file", {"path": path})
    await orch.computer_sessions.end(session, "consent_revoked")


async def test_legacy_bypass_keeps_existing_preview_privacy_rules(task_runtime, monkeypatch):
    orch, _ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime, remote=True)
    await orch.computer_sessions.set_bypass(host, True)
    response = await _call(task_runtime, "write_file", path="note-20261027.md", content="hello")
    assert response.error is not None and not effects
    await orch.computer_sessions.end(session, "consent_revoked")
