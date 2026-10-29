"""Checks project folder guidance is derived from the authenticated chat host and remains advisory."""

from types import SimpleNamespace

import pytest
from orchestrator import project_authority
from orchestrator.computer_hosts import ComputerHostRegistry
from shared.project_workspaces import ProjectManifest

PROJECT = "12345678-1234-1234-9234-123456789abc"


class ProjectCatalog:
    def __init__(self, history):
        self.history = history

    def require_chat(self, owner, chat):
        assert (owner, chat) == ("owner-a", "chat-a")
        return PROJECT


@pytest.fixture
def project_orchestrator(monkeypatch):
    monkeypatch.setattr(project_authority, "ProjectService", ProjectCatalog)
    socket = object()
    registry = ComputerHostRegistry(None)
    host, _ = registry.register("owner-a", socket, {
        "host_id": "host-a", "name": "Sam's Mac", "platform": "macos", "verbs": [],
    })
    host.project_mode = True
    host.project_manifest = ProjectManifest.from_dict({
        "generation": 1,
        "projects": [{
            "project_id": PROJECT,
            "grant_revision": 2,
            "roots": [{"name": "AstralDeep", "path": "/work/AstralDeep"}],
        }],
    })
    connection = SimpleNamespace(
        websocket=socket, registered=True, closing=False, connection_generation="g1"
    )
    orch = SimpleNamespace(
        computer_hosts=registry,
        ui_sessions={socket: {"sub": "owner-a", "_project_workspaces_v1": True}},
        _connection_contexts={id(socket): connection},
        history=object(),
    )
    return orch, socket, host


@pytest.mark.asyncio
async def test_context_uses_only_current_socket_project_roots(project_orchestrator):
    from orchestrator.goal_project_context import project_goal_context

    orch, socket, host = project_orchestrator
    result = await project_goal_context(orch, socket, "owner-a", "chat-a")

    assert result == {
        "project_id": PROJECT,
        "computer_id": "host-a",
        "computer_name": "Sam's Mac",
        "workspace_roots": [{"name": "AstralDeep", "path": "/work/AstralDeep"}],
        "primary_root": "/work/AstralDeep",
        "folder_status": "granted",
        "advisory": "Run project setup, dependency installation, tests, and startup from this local project folder.",
    }
    assert host.project_manifest.project(PROJECT).roots[0].path == "/work/AstralDeep"


@pytest.mark.asyncio
async def test_validated_durable_work_wrapper_uses_its_original_socket(project_orchestrator):
    from orchestrator.async_tasks import DurableUserTurnWebSocket
    from orchestrator.goal_project_context import project_goal_context

    orch, socket, _ = project_orchestrator
    wrapper = DurableUserTurnWebSocket(socket, user_id="owner-a")

    result = await project_goal_context(orch, wrapper, "owner-a", "chat-a")

    assert result["primary_root"] == "/work/AstralDeep"


@pytest.mark.asyncio
async def test_closed_durable_work_wrapper_has_no_project_context(project_orchestrator):
    from orchestrator.async_tasks import DurableUserTurnWebSocket
    from orchestrator.goal_project_context import project_goal_context

    orch, socket, _ = project_orchestrator
    wrapper = DurableUserTurnWebSocket(socket, user_id="owner-a")
    wrapper.scrub()

    assert await project_goal_context(orch, wrapper, "owner-a", "chat-a") is None


@pytest.mark.asyncio
async def test_nonproject_chat_returns_no_project_context(project_orchestrator):
    from orchestrator.goal_project_context import project_goal_context

    orch, socket, host = project_orchestrator
    host.project_mode = False

    assert await project_goal_context(orch, socket, "owner-a", "chat-a") is None


@pytest.mark.asyncio
async def test_wrong_owner_does_not_resolve_another_owners_socket(project_orchestrator):
    from orchestrator.goal_project_context import project_goal_context

    orch, socket, _ = project_orchestrator

    assert await project_goal_context(orch, socket, "owner-b", "chat-a") is None


@pytest.mark.asyncio
async def test_registry_mismatch_does_not_return_unbound_project_host(project_orchestrator):
    from orchestrator.goal_project_context import project_goal_context

    orch, socket, _ = project_orchestrator
    orch.computer_hosts.host_for_socket = lambda _: SimpleNamespace(
        websocket=object(), owner_sub="owner-a", host_id="other", name="Other Mac", project_mode=True
    )

    assert await project_goal_context(orch, socket, "owner-a", "chat-a") is None


@pytest.mark.asyncio
async def test_missing_project_folder_grant_returns_setup_advisory(project_orchestrator):
    from orchestrator.goal_project_context import project_goal_context

    orch, socket, host = project_orchestrator
    host.project_manifest = ProjectManifest.from_dict({"generation": 1, "projects": []})

    result = await project_goal_context(orch, socket, "owner-a", "chat-a")

    assert result["project_id"] == PROJECT
    assert result["workspace_roots"] == []
    assert result["primary_root"] is None
    assert result["folder_status"] == "folder_grant_required"
    assert "choose folders" in result["advisory"].lower()


@pytest.mark.asyncio
async def test_stale_host_authority_returns_advisory_without_roots(project_orchestrator, monkeypatch):
    from orchestrator.goal_project_context import project_goal_context

    orch, socket, _ = project_orchestrator
    class StalingProjectCatalog:
        def __init__(self, history):
            pass

        def require_chat(self, owner, chat):
            orch._connection_contexts[id(socket)] = SimpleNamespace(
                websocket=socket, registered=True, closing=False, connection_generation="changed"
            )
            return PROJECT

    monkeypatch.setattr(project_authority, "ProjectService", StalingProjectCatalog)

    result = await project_goal_context(orch, socket, "owner-a", "chat-a")

    assert result["folder_status"] == "authority_unavailable"
    assert result["workspace_roots"] == []
    assert result["primary_root"] is None


@pytest.mark.asyncio
async def test_unnegotiated_project_host_returns_authority_advisory(project_orchestrator):
    from orchestrator.goal_project_context import project_goal_context

    orch, socket, _ = project_orchestrator
    orch.ui_sessions[socket]["_project_workspaces_v1"] = False

    result = await project_goal_context(orch, socket, "owner-a", "chat-a")

    assert result["folder_status"] == "authority_unavailable"
    assert result["workspace_roots"] == []


@pytest.mark.asyncio
async def test_origin_that_goes_stale_is_never_returned_as_folder_guidance(
    project_orchestrator, monkeypatch
):
    from orchestrator import goal_project_context

    orch, socket, _ = project_orchestrator
    monkeypatch.setattr(goal_project_context, "origin_current", lambda *_: False)

    result = await goal_project_context.project_goal_context(orch, socket, "owner-a", "chat-a")

    assert result["folder_status"] == "authority_unavailable"
    assert result["workspace_roots"] == []


@pytest.mark.asyncio
async def test_virtual_work_socket_does_not_fall_back_to_other_owner_host(project_orchestrator):
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    from orchestrator.goal_project_context import project_goal_context

    orch, _, _ = project_orchestrator
    work_socket = VirtualWebSocket(BackgroundTask(
        task_id="task-a", chat_id="chat-a", user_id="owner-a"
    ))

    assert await project_goal_context(orch, work_socket, "owner-a", "chat-a") is None


@pytest.mark.asyncio
async def test_unregistered_socket_does_not_use_other_active_project_host(project_orchestrator):
    from orchestrator.goal_project_context import project_goal_context

    orch, _, _ = project_orchestrator

    assert await project_goal_context(orch, object(), "owner-a", "chat-a") is None
