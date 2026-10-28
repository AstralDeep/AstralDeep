"""Exercises owner project policy and atomic chat creation through real Plane storage.
The same service backs authenticated REST and negotiated WebSocket clients.
"""

import uuid

import pytest

from tests.helpers.voice_plane_runtime import (
    history_manager,
    isolated_voice_plane_runtime,
)


def identity():
    return str(uuid.uuid4())


@pytest.fixture
def history():
    with isolated_voice_plane_runtime("ad_test_projects") as runtime:
        yield history_manager(runtime)


def test_project_chat_creation_is_atomic_and_idempotent(history):
    from orchestrator.projects import ProjectService

    service = ProjectService(history)
    project_id, chat_id = identity(), identity()
    project = service.create("owner", project_id, "Research", 0)
    assert project["revision"] == 1
    assert service.create("owner", project_id, "Research", 0) == project
    assert service.create_chat("owner", project_id, chat_id) == chat_id
    assert service.create_chat("owner", project_id, chat_id) == chat_id
    assert service.project_for_chat("owner", chat_id) == project_id
    assert history.get_chat(chat_id, "owner")["project_id"] == project_id
    assert service.list_chats("owner", project_id)["chats"][0]["id"] == chat_id
    assert service.list("other")["projects"] == []
    assert service.project_for_chat("other", chat_id) is None


def test_invalid_project_never_leaves_an_empty_chat(history):
    from orchestrator.projects import ProjectError, ProjectService

    service = ProjectService(history)
    foreign = identity()
    service.create("other", foreign, "Foreign", 0)
    for project_id in (foreign, identity()):
        chat_id = identity()
        with pytest.raises(ProjectError, match="project_not_found"):
            service.create_chat("owner", project_id, chat_id)
        assert history.get_chat(chat_id, "owner") is None


def test_attachment_failure_rolls_back_conversation(history, monkeypatch):
    from orchestrator.projects import ProjectService

    service = ProjectService(history)
    project_id, chat_id = identity(), identity()
    service.create("owner", project_id, "Research", 0)

    def fail(*args, **kwargs):
        raise RuntimeError("storage failed")

    monkeypatch.setattr(type(history.plane_repositories.projects), "attach_chat", fail)
    with pytest.raises(RuntimeError, match="storage failed"):
        service.create_chat("owner", project_id, chat_id)
    assert history.get_chat(chat_id, "owner") is None


def test_pagination_authenticates_owner_and_scope_and_includes_empty_chats(history):
    from orchestrator.projects import ProjectError, ProjectService

    service = ProjectService(history)
    projects = [identity() for _ in range(3)]
    for index, project in enumerate(projects):
        service.create("owner", project, f"Project {index}", index)
    page = service.list("owner", limit=1)
    assert [row["project_id"] for row in page["projects"]] == projects[:1]
    assert (
        service.list("owner", cursor=page["next_cursor"], limit=2)["next_cursor"]
        is None
    )
    with pytest.raises(ProjectError, match="invalid_project_cursor"):
        service.list("other", cursor=page["next_cursor"])
    chats = sorted(identity() for _ in range(3))
    for chat in chats:
        service.create_chat("owner", projects[0], chat)
    page = service.list_chats("owner", projects[0], limit=2)
    assert [row["id"] for row in page["chats"]] == chats[:2]
    assert (
        service.list_chats("owner", projects[0], cursor=page["next_cursor"])["chats"][
            0
        ]["id"]
        == chats[2]
    )
    for cursor in (page["next_cursor"] + "X", "invalid", "é", "x" * 4097):
        with pytest.raises(ProjectError, match="invalid_project_cursor"):
            service.list("owner", cursor=cursor)
    with pytest.raises(ProjectError, match="invalid_project_cursor"):
        service.list_chats("owner", projects[1], cursor=page["next_cursor"])
    assert service.list_chats("owner", projects[1])["chats"] == []


def test_rename_revision_fences_delete_tombstone_and_no_implicit_legacy_attach(
    history,
):
    from orchestrator.projects import ProjectError, ProjectService

    service = ProjectService(history)
    project_id = identity()
    service.create("owner", project_id, "Original", 0)
    updated = service.rename("owner", project_id, "Renamed", 1)
    assert updated["revision"] == 2
    assert service.create("owner", project_id, "Original", 0) == updated
    for operation in (
        lambda: service.rename("owner", project_id, "stale", 1),
        lambda: service.delete("owner", project_id, 1),
        lambda: service.create("owner", project_id, "Changed", 0),
    ):
        with pytest.raises(ProjectError, match="project_conflict"):
            operation()
    legacy_id = history.create_chat(user_id="owner")
    with pytest.raises(ProjectError, match="chat_project_conflict"):
        service.create_chat("owner", project_id, legacy_id)
    assert service.delete("owner", project_id, 2).chats == ()
    with pytest.raises(ProjectError):
        service.create("owner", project_id, "Original", 0)
    with pytest.raises(ProjectError, match="project_not_found"):
        service.get("owner", project_id)
    with pytest.raises(ProjectError, match="project_selection_required"):
        service.require_chat("owner", legacy_id)


@pytest.fixture
def api_client(history):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from orchestrator.api import chat_router, project_router
    from orchestrator.auth import require_user_id

    app = FastAPI()
    orch = SimpleNamespace(
        history=history, _broadcast_user_history=AsyncMock(), ui_sessions={}
    )
    app.state.orchestrator = orch
    app.include_router(chat_router)
    app.include_router(project_router)
    app.dependency_overrides[require_user_id] = lambda: "owner"
    with TestClient(app) as client:
        yield client, orch


def test_rest_project_and_chat_contract(api_client):
    client, orch = api_client
    project_id, chat_id = identity(), identity()
    create = {"project_id": project_id, "name": "Research", "position": 0}
    assert client.post("/api/projects", json=create).status_code == 201
    assert client.post("/api/projects", json=create).json()["project_id"] == project_id
    assert client.get("/api/projects").json()["projects"][0]["project_id"] == project_id
    body = {"project_id": project_id, "chat_id": chat_id}
    for _ in range(2):
        response = client.post("/api/chats", json=body)
        assert response.status_code == 201, response.text
        assert response.json()["project_id"] == project_id
        assert response.json()["chat_id"] == chat_id
    assert (
        client.get(f"/api/chats/{chat_id}").json()["chat"]["project_id"] == project_id
    )
    assert (
        client.get(f"/api/projects/{project_id}/chats").json()["chats"][0]["id"]
        == chat_id
    )
    assert (
        client.patch(
            f"/api/projects/{project_id}", json={"name": "New", "revision": 1}
        ).json()["revision"]
        == 2
    )
    assert client.delete(f"/api/projects/{project_id}?revision=1&delete_chats=true").status_code == 200
    assert client.get(f"/api/chats/{chat_id}").status_code == 404
    assert client.get("/api/projects").json()["projects"] == []
    assert (
        client.post("/api/projects", json={**create, "folder": "/private"}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/chats", json={"project_id": identity(), "chat_id": identity()}
        ).status_code
        == 404
    )
    orch._broadcast_user_history.assert_awaited_with(
        user_id="owner", projects_changed=True
    )


@pytest.fixture
def socket_runtime(history, monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from orchestrator.orchestrator import Orchestrator
    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", AsyncMock())
    orch = object.__new__(Orchestrator)
    socket = object()
    orch.history = history
    orch.ui_sessions = {socket: {"sub": "owner", "_project_workspaces_v1": True}}
    orch.ui_clients = [socket]
    orch._ws_active_chat = {}
    orch._ws_timeline_mode = {}
    orch._ws_welcome = {}
    orch._conversation_scopes = {}
    orch._connection_contexts = {}
    orch.compute_tools_available_for_user = Mock(return_value=True)
    orch.send_ui_render = AsyncMock()
    orch._broadcast_user_history = AsyncMock()
    orch._retire_welcome_canvas = AsyncMock()
    orch._serialized_chat = AsyncMock()
    orch._adopt_operation_chat = AsyncMock()
    orch.cancelled_sessions = {}
    frames = []

    async def send(ws, raw):
        frames.append(json.loads(raw))
        return True

    orch._safe_send = send
    return SimpleNamespace(orch=orch, socket=socket, frames=frames)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,payload",
    [
        ("new_chat", {}),
        ("chat_message", {"message": "first message"}),
        ("chat_message", {"message": "component action", "component_id": "button"}),
        ("chat_message", {"message": "scenario", "scenario_id": "example"}),
    ],
)
async def test_negotiated_creation_requires_project_without_creating_legacy_chat(
    socket_runtime, action, payload
):
    import json

    runtime = socket_runtime
    await runtime.orch.handle_ui_message(
        runtime.socket,
        json.dumps(
            {
                "type": "ui_event",
                "action": action,
                "payload": payload,
            }
        ),
    )
    assert runtime.frames[-1]["code"] == "project_selection_required"
    runtime.orch._serialized_chat.assert_not_awaited()
    with runtime.orch.history.plane_runtime.transaction() as transaction:
        page = runtime.orch.history.plane_repositories.projects.list_owner_chats_before(
            transaction, owner_id="owner", cutoff=2**53 - 1
        )
    assert page.records == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["new_chat", "chat_message"])
async def test_negotiated_socket_create_replays_and_reports_stored_project(
    socket_runtime, action
):
    import json
    from orchestrator.projects import ProjectService

    runtime = socket_runtime
    service = ProjectService(runtime.orch.history)
    project_id, chat_id = identity(), identity()
    service.create("owner", project_id, "Research")
    frame = {
        "type": "ui_event",
        "action": action,
        "payload": {"project_id": project_id, "chat_id": chat_id, "message": "hello"},
    }
    for _ in range(2):
        await runtime.orch.handle_ui_message(runtime.socket, json.dumps(frame))
    created = [frame for frame in runtime.frames if frame["type"] == "chat_created"]
    assert created[0]["payload"]["chat_id"] == chat_id
    assert created[0]["payload"]["project_id"] == project_id
    assert service.list_chats("owner", project_id)["chats"][0]["id"] == chat_id
    runtime.orch._broadcast_user_history.assert_awaited_with(
        user_id="owner", projects_changed=True
    )


@pytest.mark.asyncio
async def test_negotiated_load_refuses_legacy_chat_without_adopting_it(socket_runtime):
    import json

    runtime = socket_runtime
    chat_id = runtime.orch.history.create_chat(user_id="owner")
    await runtime.orch.handle_ui_message(
        runtime.socket,
        json.dumps(
            {"type": "ui_event", "action": "load_chat", "payload": {"chat_id": chat_id}}
        ),
    )
    assert runtime.frames[-1]["code"] == "project_selection_required"
    assert runtime.orch._ws_active_chat == {}


@pytest.mark.asyncio
async def test_correlated_voice_new_chat_keeps_project_identity(socket_runtime):
    import json
    from orchestrator.projects import ProjectService
    from shared.protocol import ChatCreated, Message
    from tests.test_voice_new_chat_activation_065 import _correlated_new_chat

    runtime = socket_runtime
    project_id, chat_id = identity(), identity()
    ProjectService(runtime.orch.history).create("owner", project_id, "Voice")
    frame = _correlated_new_chat()
    frame["payload"].update({"project_id": project_id, "chat_id": chat_id})
    await runtime.orch.handle_ui_message(runtime.socket, json.dumps(frame))
    result = Message.from_json(json.dumps(runtime.frames[-1]))
    assert isinstance(result, ChatCreated)
    assert result.payload["project_id"] == project_id
    assert result.payload["chat_id"] == chat_id


def test_rest_negotiation_binds_owner_device_and_current_connection(api_client):
    from types import SimpleNamespace

    client, orch = api_client
    socket = object()
    generation, device = identity(), identity()
    orch.ui_sessions = {
        socket: {
            "sub": "owner",
            "_project_workspaces_v1": True,
            "_project_device_id": device,
            "_project_connection_generation": generation,
        }
    }
    orch._connection_contexts = {
        id(socket): SimpleNamespace(closing=False, connection_generation=generation)
    }
    headers = {
        "X-Astral-Device-Id": device,
        "X-Astral-Connection-Generation": generation,
    }
    assert client.post("/api/chats", headers=headers).status_code == 409
    legacy = client.post("/api/chats").json()["chat_id"]
    assert client.get(f"/api/chats/{legacy}", headers=headers).status_code == 409
    assert client.get(f"/api/chats/{legacy}").status_code == 200
    assert (
        client.post(
            "/api/chats", headers={**headers, "X-Astral-Device-Id": identity()}
        ).status_code
        == 201
    )
    orch.ui_sessions[socket]["sub"] = "other"
    assert client.post("/api/chats", headers=headers).status_code == 201


def test_projects_only_negotiation_needs_server_profile_and_explicit_matching_capability():
    from types import SimpleNamespace
    from orchestrator.projects import project_workspaces_negotiated

    values = {
        "device_type": SimpleNamespace(value="macos"),
        "console_contract": "console/v2",
        "project_workspaces_v1": True,
    }
    profile = SimpleNamespace(**values)
    assert project_workspaces_negotiated(
        profile, {"project_workspaces_v1": True}, ["project_workspaces_v1"]
    )
    for changes in (
        {"project_workspaces_v1": False},
        {"project_workspaces_v1": 1},
        {"console_contract": "legacy"},
        {"device_type": SimpleNamespace(value="ios")},
    ):
        assert not project_workspaces_negotiated(
            SimpleNamespace(**{**values, **changes}),
            {"project_workspaces_v1": True},
            ["project_workspaces_v1"],
        )
    assert not project_workspaces_negotiated(profile, {}, ["project_workspaces_v1"])
    assert not project_workspaces_negotiated(
        profile, {"project_workspaces_v1": True}, []
    )


def test_voice_activation_refuses_negotiated_legacy_chat(history):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from orchestrator.auth import require_user_id
    from orchestrator.voice_api import router
    from tests.test_voice_session_api_065 import (
        _Runtime,
        _Orchestrator,
        _headers,
        _capability,
        DEVICE,
        CONNECTION,
        ACTIVATION,
    )

    runtime = _Runtime()
    orch = _Orchestrator(runtime)
    orch.history = history
    socket = object()
    orch.ui_sessions = {
        socket: {
            "sub": "user-a",
            "_project_workspaces_v1": True,
            "_project_device_id": DEVICE,
            "_project_connection_generation": CONNECTION,
        }
    }
    orch._connection_contexts = {
        id(socket): SimpleNamespace(closing=False, connection_generation=CONNECTION)
    }
    chat_id = history.create_chat(user_id="user-a")
    app = FastAPI()
    app.state.orchestrator = orch
    app.dependency_overrides[require_user_id] = lambda: "user-a"
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            "/api/voice/sessions",
            headers=_headers(),
            json={
                "device_id": DEVICE,
                "device_kind": "macos",
                "visible_chat_id": chat_id,
                "activation_id": ACTIVATION,
                "capability": _capability(),
                "foreground_active": True,
            },
        )
    assert response.status_code == 409
    assert response.json()["code"] == "project_selection_required"
    assert runtime.calls == []


def test_resume_rejection_keeps_registration_ready_and_correlated(history, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tests.test_register_ui_auth_required import (
        _FakeWS,
        _make_fake,
        _register_msg,
        _run_and_drain,
    )
    from orchestrator.orchestrator import Orchestrator
    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_auth_event", AsyncMock())
    fake = _make_fake(validate=AsyncMock(return_value={"sub": "owner"}))
    fake.history = history
    fake._register_ui_operation_refusal = Orchestrator._register_ui_operation_refusal
    fake._emit_hydration_snapshot = AsyncMock()
    fake._issue_voice_control_binding = AsyncMock()
    fake.rote.register_device = lambda ws, device: SimpleNamespace(
        device_type=SimpleNamespace(value="macos"),
        console_contract="console/v2",
        project_workspaces_v1=True,
    )
    socket = _FakeWS()
    fake._registered_events[id(socket)] = asyncio.Event()
    chat_id = history.create_chat(user_id="owner")
    generation, request_id = identity(), identity()
    _run_and_drain(
        fake.handle_ui_message(
            socket,
            _register_msg(
                token="test",
                device_id=identity(),
                connection_generation=generation,
                capabilities=["project_workspaces_v1"],
                device={"project_workspaces_v1": True},
                resume={
                    "schema_version": 1,
                    "active_chat_id": chat_id,
                    "request_generation": request_id,
                },
            ),
        )
    )
    error = next(
        frame
        for _, frame in fake._sent
        if frame.get("code") == "project_selection_required"
    )
    assert fake._registered_events[id(socket)].is_set()
    assert error["request_generation"] == request_id
    assert error["chat_id"] == chat_id
    fake._emit_hydration_snapshot.assert_not_awaited()


@pytest.mark.parametrize(
    "value", [None, "bad", "550E8400-E29B-41D4-A716-446655440000", 3]
)
def test_new_chat_identifiers_are_canonical_and_fail_before_writes(history, value):
    from orchestrator.projects import ProjectError, ProjectService

    service = ProjectService(history)
    project_id = identity()
    service.create("owner", project_id, "Research")
    with pytest.raises(ProjectError, match="invalid_project_request"):
        service.create_chat("owner", project_id, value)
    assert service.list_chats("owner", project_id)["chats"] == []


def test_service_denials_and_owned_delete(history):
    from orchestrator.projects import ProjectError, ProjectService

    service = ProjectService(history)
    project_id = identity()
    service.create("owner", project_id, "Research")
    assert service.get("owner", project_id)["name"] == "Research"
    with pytest.raises(ProjectError, match="invalid_project_request"):
        service.create("owner", identity(), " ")
    with pytest.raises(ProjectError, match="project_not_found"):
        service.rename("other", project_id, "Stolen", 1)
    with pytest.raises(ProjectError, match="invalid_project_request"):
        service.create_chat("owner", project_id, identity(), agent_id="")
    foreign_chat = history.create_chat(user_id="other")
    with pytest.raises(ProjectError, match="chat_project_conflict"):
        service.create_chat("owner", project_id, foreign_chat)
    assert service.delete("owner", project_id, 1)
    assert service.list("owner")["projects"] == []


def test_concurrent_project_chat_retry_has_one_empty_conversation(history):
    from concurrent.futures import ThreadPoolExecutor
    from orchestrator.projects import ProjectService

    service = ProjectService(history)
    project_id, chat_id = identity(), identity()
    with ThreadPoolExecutor(max_workers=4) as executor:
        projects = list(
            executor.map(
                lambda _: service.create("owner", project_id, "Research"), range(4)
            )
        )
        chats = list(
            executor.map(
                lambda _: service.create_chat("owner", project_id, chat_id), range(4)
            )
        )
    assert all(project == projects[0] for project in projects)
    assert chats == [chat_id] * 4
    assert len(service.list_chats("owner", project_id)["chats"]) == 1


@pytest.mark.asyncio
async def test_dispatch_ignores_claimed_project_and_loads_actual_membership(
    socket_runtime,
):
    import json
    from orchestrator.projects import ProjectService

    runtime = socket_runtime
    service = ProjectService(runtime.orch.history)
    project_id, chat_id = identity(), identity()
    service.create("owner", project_id, "Research")
    service.create_chat("owner", project_id, chat_id)
    await runtime.orch.handle_ui_message(
        runtime.socket,
        json.dumps(
            {
                "type": "ui_event",
                "action": "chat_message",
                "payload": {
                    "chat_id": chat_id,
                    "project_id": identity(),
                    "message": "hello",
                },
            }
        ),
    )
    runtime.orch._serialized_chat.assert_awaited_once()
    assert service.project_for_chat("owner", chat_id) == project_id
    assert "project_id" not in runtime.orch._serialized_chat.call_args.kwargs


@pytest.mark.asyncio
async def test_hydration_and_direct_dispatch_refuse_legacy_chat(socket_runtime):
    from orchestrator.projects import ProjectError

    runtime = socket_runtime
    chat_id = runtime.orch.history.create_chat(user_id="owner")
    with pytest.raises(ProjectError, match="project_selection_required"):
        await runtime.orch._emit_hydration_snapshot(
            runtime.socket,
            chat_id=chat_id,
            user_id="owner",
            connection_generation=identity(),
            request_generation=identity(),
        )
    with pytest.raises(ProjectError, match="project_selection_required"):
        await runtime.orch.handle_chat_message(
            runtime.socket, "hello", chat_id, user_id="owner"
        )


@pytest.mark.asyncio
async def test_history_invalidation_is_owner_scoped_and_reports_empty_projects(
    socket_runtime,
):
    from unittest.mock import AsyncMock
    from orchestrator.orchestrator import Orchestrator

    runtime = socket_runtime
    peer, other = object(), object()
    runtime.orch.ui_clients.extend([peer, other])
    runtime.orch.ui_sessions[peer] = {"sub": "owner"}
    runtime.orch.ui_sessions[other] = {"sub": "other"}
    runtime.orch._push_history_surface = AsyncMock()
    await Orchestrator._broadcast_user_history(
        runtime.orch, user_id="owner", projects_changed=True
    )
    assert len(runtime.frames) == 2
    assert all(
        frame == {"type": "history_list", "chats": [], "projects_changed": True}
        for frame in runtime.frames
    )
    assert {
        call.args[0] for call in runtime.orch._push_history_surface.call_args_list
    } == {runtime.socket, peer}


def test_rest_owner_isolation_revision_and_validation(api_client):
    client, orch = api_client
    from orchestrator.projects import ProjectService

    service = ProjectService(orch.history)
    project_id = identity()
    service.create("other", project_id, "Foreign")
    assert client.get(f"/api/projects/{project_id}").status_code == 404
    assert client.get(f"/api/projects/{project_id}/chats").status_code == 404
    assert (
        client.patch(
            f"/api/projects/{project_id}", json={"revision": 1, "name": "Stolen"}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/projects/{project_id}?revision=1").status_code == 404
    assert client.get("/api/projects?limit=0").status_code == 400
    assert client.get("/api/projects?cursor=bad").status_code == 400
    owned = identity()
    client.post("/api/projects", json={"project_id": owned, "name": "Own"})
    assert client.get(f"/api/projects/{owned}").status_code == 200
    assert client.delete(f"/api/projects/{owned}?revision=1").status_code == 200
    client.app.dependency_overrides.clear()
    assert client.get("/api/projects").status_code in {401, 403}


def test_connection_idempotency_digest_binds_project(socket_runtime):
    import copy
    import json
    from types import SimpleNamespace

    runtime = socket_runtime
    frame = {
        "type": "ui_event",
        "action": "new_chat",
        "submission_id": identity(),
        "request_generation": identity(),
        "payload": {"project_id": identity(), "chat_id": identity()},
    }
    context = SimpleNamespace(websocket=runtime.socket, connection_generation=None)
    first = runtime.orch._connection_frame(context, json.dumps(frame), frame)
    changed = copy.deepcopy(frame)
    changed["payload"]["project_id"] = identity()
    second = runtime.orch._connection_frame(context, json.dumps(changed), changed)
    assert first is not None and second is not None
    assert first.normalized_digest != second.normalized_digest


def test_project_chat_summary_preserves_history_flags_agent_and_visible_preview(
    history,
):
    from orchestrator.projects import ProjectService

    service = ProjectService(history)
    project_id, chat_id, empty_id = identity(), identity(), identity()
    service.create("owner", project_id, "Research")
    service.create_chat("owner", project_id, chat_id, agent_id="agent")
    service.create_chat("owner", project_id, empty_id)
    history.add_message(chat_id, "user", "Visible message", user_id="owner")
    history.set_chat_flag(chat_id, "pinned", True, "owner")
    history.set_chat_flag(chat_id, "archived", True, "owner")
    rows = {row["id"]: row for row in service.list_chats("owner", project_id)["chats"]}
    assert rows[chat_id]["agent_id"] == "agent"
    assert rows[chat_id]["pinned"] is True and rows[chat_id]["archived"] is True
    assert rows[chat_id]["preview"] == "Visible message"
    assert rows[empty_id]["preview"] == ""
    assert rows[empty_id]["has_saved_components"] is False


@pytest.mark.parametrize(
    "project_id", ["bad", None, 1, "550E8400-E29B-41D4-A716-446655440000"]
)
def test_correlated_project_protocol_rejects_invalid_identity(project_id):
    from shared.protocol import Message, ProtocolValidationError
    from tests.test_voice_new_chat_activation_065 import _correlated_new_chat
    import json

    frame = _correlated_new_chat()
    frame["payload"].update(project_id=project_id, chat_id=identity())
    with pytest.raises(ProtocolValidationError):
        Message.from_json(json.dumps(frame))


def test_correlated_protocol_accepts_optional_chat_and_agent():
    import json
    from shared.protocol import Message
    from tests.test_voice_new_chat_activation_065 import _correlated_new_chat

    frame = _correlated_new_chat()
    frame["payload"].update(chat_id=identity(), agent_id="agent")
    assert Message.from_json(json.dumps(frame)).payload == frame["payload"]


@pytest.mark.parametrize("chat_id", [" legacy", "legacy ", "legacy\nchat"])
def test_project_lookup_and_history_preserve_legacy_identity_bytes(history, chat_id):
    from orchestrator.projects import ProjectService

    service = ProjectService(history)
    project_id = identity()
    service.create("owner", project_id, "Research")
    history.create_chat(chat_id, user_id="owner")
    with history.plane_runtime.transaction() as transaction:
        history.plane_repositories.projects.attach_chat(
            transaction, owner_id="owner", project_id=project_id, chat_id=chat_id
        )
    assert service.project_for_chat("owner", chat_id) == project_id
    assert history.get_chat(chat_id, "owner")["id"] == chat_id
    assert service.list_chats("owner", project_id)["chats"][0]["id"] == chat_id


def test_invalid_project_ingress_is_rejected(socket_runtime):
    import json
    from types import SimpleNamespace

    frame = {
        "type": "ui_event",
        "action": "new_chat",
        "submission_id": identity(),
        "request_generation": identity(),
        "payload": {"project_id": "bad", "chat_id": identity()},
    }
    context = SimpleNamespace(
        websocket=socket_runtime.socket, connection_generation=None
    )
    assert (
        socket_runtime.orch._connection_frame(context, json.dumps(frame), frame) is None
    )


def test_client_supplied_creation_identity_is_not_silently_replaced(api_client):
    client, _ = api_client
    response = client.post("/api/chats", json={"chat_id": identity()})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "project_selection_required"


@pytest.mark.parametrize(
    "legacy_id",
    ["🧪" * 512, "\x01" * 512, " legacy\n" + "🧪" * 504],
    ids=["emoji", "control", "mixed"],
)
def test_project_chat_cursor_round_trips_maximum_legacy_identity(history, legacy_id):
    from orchestrator.projects import ProjectService

    service = ProjectService(history)
    owner_id = "🧪" * 512
    project_id = identity()
    service.create(owner_id, project_id, "Research")
    chats = sorted([legacy_id, "🫠" * 512])
    for chat_id in chats:
        history.create_chat(chat_id, user_id=owner_id)
        with history.plane_runtime.transaction() as transaction:
            history.plane_repositories.projects.attach_chat(
                transaction, owner_id=owner_id, project_id=project_id, chat_id=chat_id
            )
    page = service.list_chats(owner_id, project_id, limit=1)
    assert page["chats"][0]["id"] == chats[0]
    assert len(page["next_cursor"]) <= 4096
    final = service.list_chats(
        owner_id, project_id, limit=1, cursor=page["next_cursor"]
    )
    assert final["chats"][0]["id"] == chats[1]
    assert final["next_cursor"] is None


def test_project_cursor_bounds_do_not_depend_on_owner_unicode(history):
    from orchestrator.projects import ProjectService

    service = ProjectService(history)
    owner = "🧪" * 512
    projects = [identity(), identity()]
    for index, project_id in enumerate(projects):
        service.create(owner, project_id, "Project", index)
    first = service.list(owner, limit=1)
    assert len(first["next_cursor"]) < 512
    final = service.list(owner, limit=1, cursor=first["next_cursor"])
    assert final["projects"][0]["project_id"] == projects[1]
    assert final["next_cursor"] is None


@pytest.mark.parametrize(
    "ordering",
    [
        {"chat_id_utf8": "!"},
        {"chat_id_utf8": 3},
        {"chat_id_utf8": "_w=="},
        {"chat_id_utf8": "", "owner_id": "other"},
        [],
    ],
)
def test_signed_malformed_cursor_ordering_is_refused(ordering):
    import time
    from orchestrator.external_identity_links import encode_signed_payload
    from orchestrator.projects import ProjectError, ProjectService, _cursor_scope
    from orchestrator.web_auth import _secret

    scope = "chats:" + identity()
    now = int(time.time())
    token = encode_signed_payload(
        {
            "v": 1,
            "type": "project_page",
            "iat": now,
            "exp": now + 300,
            "scope": _cursor_scope("owner", scope),
            "cursor": ordering,
        },
        _secret(),
    )
    with pytest.raises(ProjectError, match="invalid_project_cursor"):
        ProjectService._cursor(token, "owner", scope)


def test_cursor_encoder_never_emits_an_oversized_token():
    from astralplane import ProjectChatCursor
    from orchestrator.projects import ProjectError, ProjectService

    project_id = identity()
    with pytest.raises(ProjectError, match="invalid_project_cursor"):
        ProjectService._next(
            ProjectChatCursor("owner", project_id, None, "🧪" * 600),
            "owner",
            "chats:" + project_id,
        )
