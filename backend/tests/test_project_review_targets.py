"""Checks that project membership and a host bypass switch cannot release a reviewed operation or expose sensitive arguments."""
# ruff: noqa: F811

import pytest

from orchestrator import project_authority
from shared.project_workspaces import ProjectManifest
from tests.test_computer_grant_flow_102 import _call, _mac
from tests.test_hitl_public_research import runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401
from tests.test_project_standing_dispatch import PROJECT, project_runtime

ROOT = "/tmp/project-20261027"
SYNTHETIC_SSN = "-".join(("123", "45", "6789"))
SYNTHETIC_TOKEN = "gh" + "p_" + "a" * 36
SYNTHETIC_PRIVATE_KEY = "\n".join((
    "".join(("-----", "BEGIN ", "PRIVATE KEY", "-----")),
    "A" * 32,
    "".join(("-----", "END ", "PRIVATE KEY", "-----")),
))


async def dated_runtime(task_runtime, monkeypatch):
    session, host = await project_runtime(task_runtime, monkeypatch)
    orch = task_runtime[0]
    host.project_manifest = ProjectManifest.from_dict({"generation": 2, "projects": [{"project_id": PROJECT,
        "grant_revision": 2, "roots": [{"name": "Primary", "path": ROOT}]}]})
    host.project_manifest_highwater = 2
    session.project_origin = await project_authority.resolve_project_origin(orch, "owner-a", "chat-a", host)
    return session, host


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "note.md", "content": "hello", "if_exists": "refuse"}),
    ("delete_path", {"path": ROOT + "/note.md"}),
    ("run_command", {"argv": ["pwd"]}),
    ("run_command", {"argv": ["pwd"], "cwd": ROOT}),
])
async def test_dated_project_paths_do_not_grant_effect_authority(task_runtime, monkeypatch, tool, args):
    orch, _ws, effects, *_ = task_runtime
    session, _host = await dated_runtime(task_runtime, monkeypatch)
    response = await _call(task_runtime, tool, **args)
    assert response.error["code"] == "refused" and "requires attended authorization" in response.error["message"]
    assert not response.ui_components and not effects
    await orch.computer_sessions.end(session, "consent_revoked")


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "note.md", "content": "Synthetic SSN " + SYNTHETIC_SSN}),
    ("write_file", {"path": "note.md", "content": SYNTHETIC_PRIVATE_KEY}),
    ("write_file", {"path": "credentials-" + SYNTHETIC_TOKEN + ".md", "content": "hello"}),
    ("write_file", {"path": "note.md", "content": "hello", "project_targets": ["MRN " + "987654"]}),
    ("run_command", {"argv": ["echo", SYNTHETIC_SSN]}),
    ("run_command", {"command": "echo " + SYNTHETIC_SSN}),
    ("run_command", {"argv": ["pwd"], "env": {"APP_VALUE": SYNTHETIC_SSN}}),
    ("run_command", {"argv": ["sudo", "pwd"]}),
], ids=["write-phi", "write-private-key", "filename-token", "forged-target-phi", "argv-phi",
        "command-phi", "environment-phi", "sudo-floor"])
async def test_refused_project_effect_never_echoes_the_sensitive_argument(task_runtime, monkeypatch, tool, args):
    orch, _ws, effects, *_ = task_runtime
    session, _host = await dated_runtime(task_runtime, monkeypatch)
    response = await _call(task_runtime, tool, **args)
    assert response.error["code"] == "refused"
    assert not response.ui_components and not effects
    for value in (SYNTHETIC_SSN, SYNTHETIC_TOKEN, SYNTHETIC_PRIVATE_KEY, "987654"):
        assert value not in str(response.error)
    await orch.computer_sessions.end(session, "consent_revoked")


@pytest.mark.parametrize("path", ["/tmp/elsewhere-20261027/note.md", "../elsewhere-20261027/note.md"])
async def test_forged_outside_target_never_receives_routing_authority(task_runtime, monkeypatch, path):
    orch, _ws, effects, *_ = task_runtime
    session, _host = await dated_runtime(task_runtime, monkeypatch)
    response = await _call(task_runtime, "write_file", path=path, content="hello")
    assert response.error["code"] == "path_outside_workspace"
    assert not effects
    await orch.computer_sessions.end(session, "consent_revoked")


async def test_legacy_host_bypass_cannot_authorize_file_writes(task_runtime, monkeypatch):
    orch, _ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime)
    await orch.computer_sessions.set_bypass(host, True)
    response = await _call(task_runtime, "write_file", path="note-20261027.md", content="hello")
    assert response.error["code"] == "refused" and "requires attended authorization" in response.error["message"]
    assert not effects
    await orch.computer_sessions.end(session, "consent_revoked")
