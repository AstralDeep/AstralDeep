"""Verifies confirmed project deletion through real Plane storage and the REST lifecycle.
Owner isolation, complete pagination, rollback and audit retention share the production transaction path.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from audit.repository import AuditRepository
from audit.schemas import AuditEventCreate
from orchestrator.projects import ProjectError, ProjectService
from tests.helpers.voice_plane_runtime import history_manager, isolated_voice_plane_runtime
from tests.test_chat_projects import api_client as api_client


@pytest.fixture
def history():
    with isolated_voice_plane_runtime("ad_test_project_delete") as runtime:
        yield history_manager(runtime)


def project(service, owner="owner", identity=None):
    identity = identity or str(uuid4())
    service.create(owner, identity, "Synthetic project")
    return identity


def chat(service, project_id, owner="owner"):
    identity = str(uuid4())
    service.create_chat(owner, project_id, identity)
    return identity


def test_nonempty_stale_project_delete_preserves_other_owners_projects_and_audit(history, monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-project-deletion-audit")
    service = ProjectService(history)
    selected = project(service)
    target = [chat(service, selected) for _ in range(2)]
    history.add_message(target[0], "user", "Synthetic message", user_id="owner")
    history.set_chat_flag(target[1], "archived", True, "owner")
    kept_project = project(service)
    kept_chat = chat(service, kept_project)
    foreign_project = project(service, "other", selected)
    foreign_chat = chat(service, foreign_project, "other")
    audit = AuditRepository(plane_runtime=history.plane_runtime, plane_repositories=history.plane_repositories)
    now = datetime.now(UTC)
    audit.insert(AuditEventCreate(
        actor_user_id="owner", auth_principal="owner", event_class="settings",
        action_type="fixture.project_delete", description="Synthetic project deletion fixture",
        conversation_id=target[0], correlation_id=str(uuid4()), outcome="success",
        started_at=now, completed_at=now,
    ))
    before = audit.list_for_user("owner")
    service.rename("owner", selected, "Renamed", 1)

    deleted = service.delete("owner", selected, 1)

    assert {mutation.chat_id for mutation in deleted.chats} == set(target)
    assert all(mutation.chat_deleted for mutation in deleted.chats)
    assert all(history.get_chat(identity, "owner") is None for identity in target)
    with pytest.raises(ProjectError, match="project_not_found"):
        service.get("owner", selected)
    assert history.get_chat(kept_chat, "owner")["project_id"] == kept_project
    assert history.get_chat(foreign_chat, "other")["project_id"] == selected
    assert service.get("other", selected)["revision"] == 1
    assert audit.list_for_user("owner") == before
    assert audit.verify_chain("owner") is None


def test_delete_covers_every_chat_page_including_empty_chats(history):
    service = ProjectService(history)
    selected = project(service)
    ids = [str(uuid4()) for _ in range(205)]
    with history.plane_runtime.transaction() as transaction:
        for identity in ids:
            service.conversations.create(
                transaction, owner_id="owner", conversation_id=identity,
                title="Empty fixture", agent_id=None, created_at=1,
            )
            service.repository.attach_chat(
                transaction, owner_id="owner", project_id=selected, chat_id=identity,
            )

    deleted = service.delete("owner", selected, 1)

    assert {mutation.chat_id for mutation in deleted.chats} == set(ids)
    assert service.list("owner")["projects"] == []
    assert all(history.get_chat(identity, "owner") is None for identity in ids)


def test_later_chat_failure_rolls_back_every_chat_and_project(history, monkeypatch):
    service = ProjectService(history)
    selected = project(service)
    ids = [chat(service, selected) for _ in range(2)]
    original = history.delete_chat
    removed = []

    def fail_later(identity, **kwargs):
        mutation = original(identity, **kwargs)
        removed.append(identity)
        if len(removed) == 2:
            raise RuntimeError("synthetic later chat failure")
        return mutation

    monkeypatch.setattr(history, "delete_chat", fail_later)
    with pytest.raises(RuntimeError, match="later chat failure"):
        service.delete("owner", selected, 1)
    assert service.get("owner", selected)["revision"] == 1
    assert {row["id"] for row in service.list_chats("owner", selected)["chats"]} == set(ids)
    assert all(history.get_chat(identity, "owner") is not None for identity in ids)


def test_artifact_cleanup_failure_rolls_back_chat_and_project(history, monkeypatch):
    service = ProjectService(history)
    selected = project(service)
    identity = chat(service, selected)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic version failure")

    monkeypatch.setattr(type(history._artifacts.repository.versions), "delete_for_conversation", fail)
    with pytest.raises(RuntimeError, match="version failure"):
        service.delete("owner", selected, 1)
    assert history.get_chat(identity, "owner") is not None
    assert service.get("owner", selected)["revision"] == 1


def test_other_owner_and_invalid_revision_cannot_delete(history):
    service = ProjectService(history)
    selected = project(service, "other")
    identity = chat(service, selected, "other")
    with pytest.raises(ProjectError, match="project_not_found"):
        service.delete("owner", selected, 1)
    for revision in (0, True, "1", None, 2**53):
        with pytest.raises(ProjectError, match="invalid_project_request"):
            service.delete("other", selected, revision)
    assert history.get_chat(identity, "other") is not None


def test_concurrent_create_and_delete_have_one_serial_outcome(history):
    service = ProjectService(history)
    selected = project(service)
    identity = str(uuid4())
    start = Barrier(2)

    def create():
        start.wait(timeout=10)
        try:
            return service.create_chat("owner", selected, identity)
        except ProjectError as error:
            return error

    def delete():
        start.wait(timeout=10)
        return service.delete("owner", selected, 1)

    with ThreadPoolExecutor(max_workers=2) as workers:
        created = workers.submit(create)
        deleted = workers.submit(delete)
        create_result, delete_result = created.result(timeout=15), deleted.result(timeout=15)
    assert history.get_chat(identity, "owner") is None
    assert service.list("owner")["projects"] == []
    if isinstance(create_result, ProjectError):
        assert create_result.code == "project_not_found"
        assert delete_result.chats == ()
    else:
        assert create_result == identity
        assert [mutation.chat_id for mutation in delete_result.chats] == [identity]


def test_rest_delete_notifies_only_affected_owner_chats_and_voice(api_client):
    import json

    client, orch = api_client
    service = ProjectService(orch.history)
    selected = project(service)
    ids = [chat(service, selected) for _ in range(2)]
    kept = chat(service, project(service))
    sockets = [object() for _ in range(4)]
    orch.ui_clients = sockets
    orch._get_user_id = lambda socket: "other" if socket is sockets[3] else "owner"
    orch._ws_active_chat = dict(zip(map(id, sockets), [*ids, kept, ids[0]]))
    orch._ws_timeline_mode = {id(sockets[0]): 5, id(sockets[2]): 7}
    orch._safe_send = AsyncMock()
    orch.voice_services = SimpleNamespace(handle_chat_unavailable=AsyncMock())

    response = client.delete(f"/api/projects/{selected}?revision=1")

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert set(orch._ws_active_chat) == {id(sockets[2]), id(sockets[3])}
    assert orch._ws_timeline_mode == {id(sockets[2]): 7}
    frames = [(call.args[0], json.loads(call.args[1])) for call in orch._safe_send.await_args_list]
    assert {(socket, frame["chat_id"]) for socket, frame in frames if frame["type"] == "chat_deleted"} == set(zip(sockets, ids))
    assert [frame for _, frame in frames if frame["type"] == "workspace_timeline_mode"] == [{"type": "workspace_timeline_mode", "active": False}]
    assert {call.args[0].chat_id for call in orch.voice_services.handle_chat_unavailable.await_args_list} == set(ids)
    orch._broadcast_user_history.assert_awaited_with(user_id="owner", projects_changed=True)


@pytest.mark.asyncio
async def test_project_revocation_survives_a_failed_postcommit_session_cleanup(monkeypatch, caplog):
    from tests.test_project_host_authority import OWNER, PROJECT, setup

    api, orch, _, host, _, _ = setup(monkeypatch)
    origin = await api.resolve_project_origin(orch, OWNER, "chat", host)
    affected = [SimpleNamespace(project_origin=origin) for _ in range(2)]
    ended = []

    async def end(session, reason):
        assert not api.origin_current(orch, origin)
        ended.append((session, reason))
        if session is affected[0]:
            raise RuntimeError("synthetic session cleanup failure")

    orch.computer_sessions = SimpleNamespace(live_for_owner=lambda _: affected, end=end)
    await api.invalidate_deleted_project(orch, OWNER, PROJECT)
    assert host.project_manifest.project(PROJECT) is None
    assert ended == [(session, "consent_revoked") for session in affected]
    assert "project_deleted_session_cleanup_unavailable" in caplog.text
