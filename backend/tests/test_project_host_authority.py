"""Exercises origin authority against persisted membership and current connection-bound manifests.
Behavioral doubles isolate scheduling races while preserving registry and session identity checks.
"""

import asyncio
import importlib
from types import SimpleNamespace

import pytest

from orchestrator.computer_hosts import ComputerHostError, ComputerHostRegistry
from shared.project_workspaces import ProjectManifest

OWNER = "owner"
PROJECT = "12345678-1234-1234-9234-123456789abc"
OTHER = "12345678-1234-1234-9234-123456789abd"
HOST = "88888888-8888-4888-8888-888888888888"


def manifest(generation=1, revision=1, path="/tmp/project", project=PROJECT):
    return ProjectManifest.from_dict({"generation": generation, "projects": [{"project_id": project,
        "grant_revision": revision, "roots": [{"name": "Primary", "path": path}]}]})


def setup(monkeypatch):
    api = importlib.import_module("orchestrator.project_authority")
    ws = object()
    orch = SimpleNamespace(ui_sessions={ws: {"sub": OWNER, "_project_workspaces_v1": True}},
        _connection_contexts={id(ws): SimpleNamespace(websocket=ws, registered=True, closing=False,
                                                    connection_generation="connection")}, history=object())
    orch.computer_hosts = ComputerHostRegistry(orch)
    host, _ = orch.computer_hosts.register(OWNER, ws, {"host_id": HOST, "platform": "macos", "name": "Mac",
        "workspace_roots": [], "verbs": ["read_file", "run_command"]})
    host.project_mode = True
    host.project_manifest = manifest()
    chats = {(OWNER, "chat"): PROJECT, (OWNER, "other-chat"): OTHER}
    projects = {(OWNER, PROJECT), (OWNER, OTHER)}

    class Service:
        def __init__(self, history):
            pass

        def require_chat(self, owner, chat):
            from orchestrator.projects import ProjectError
            if (owner, chat) not in chats:
                raise ProjectError("project_selection_required")
            return chats[(owner, chat)]

        def get(self, owner, project):
            from orchestrator.projects import ProjectError
            if (owner, project) not in projects:
                raise ProjectError("project_not_found", 404)
            return {"project_id": project}

    monkeypatch.setattr(api, "ProjectService", Service)
    return api, orch, ws, host, chats, projects


@pytest.mark.asyncio
async def test_origin_uses_membership_and_never_sidebar_or_legacy_roots(monkeypatch):
    api, orch, ws, host, chats, _ = setup(monkeypatch)
    orch._ws_active_chat = {id(ws): "other-chat"}
    origin = await api.resolve_project_origin(orch, OWNER, "chat", host)
    assert origin.context.project_id == PROJECT
    assert origin.roots == [{"name": "Primary", "path": "/tmp/project"}]
    assert origin.primary == "/tmp/project"
    assert host.workspace_roots == []
    other = await api.resolve_project_origin(orch, OWNER, "other-chat", host)
    assert other.context.project_id == OTHER and other.roots == []
    with pytest.raises(ComputerHostError, match="folders"):
        other.require_grant()
    chats.pop((OWNER, "chat"))
    with pytest.raises(ComputerHostError):
        await api.resolve_project_origin(orch, OWNER, "chat", host)


@pytest.mark.asyncio
async def test_immutable_binding_survives_other_project_update_but_not_own_revision(monkeypatch):
    api, orch, _, host, _, _ = setup(monkeypatch)
    origin = await api.resolve_project_origin(orch, OWNER, "chat", host)
    host.project_manifest = ProjectManifest.from_dict({"generation": 2,
        "projects": [manifest().projects[0].to_dict(), manifest(project=OTHER).projects[0].to_dict()]})
    assert api.origin_current(orch, origin)
    host.project_manifest = manifest(generation=3, revision=2)
    assert not api.origin_current(orch, origin)


@pytest.mark.asyncio
async def test_manifest_replacement_order_revision_identity_and_no_partial_admission(monkeypatch):
    api, orch, ws, host, _, projects = setup(monkeypatch)
    host.project_manifest = None
    await api.admit_project_manifest(orch, ws, host, manifest())
    await api.admit_project_manifest(orch, ws, host, manifest())
    for candidate in [manifest(path="/tmp/changed"), manifest(generation=2, path="/tmp/changed")]:
        with pytest.raises(ComputerHostError):
            await api.admit_project_manifest(orch, ws, host, candidate)
    assert host.project_manifest == manifest()
    await api.admit_project_manifest(orch, ws, host, ProjectManifest.from_dict({"generation": 3, "projects": []}))
    with pytest.raises(ComputerHostError):
        await api.admit_project_manifest(orch, ws, host, manifest(generation=4))
    await api.admit_project_manifest(orch, ws, host, manifest(generation=5, revision=2))
    projects.remove((OWNER, PROJECT))
    with pytest.raises(ComputerHostError):
        await api.admit_project_manifest(orch, ws, host, manifest(generation=6, revision=3))
    assert host.project_manifest.generation == 5


@pytest.mark.asyncio
async def test_context_witness_is_scoped_and_expired_in_inherited_task(monkeypatch):
    api, orch, ws, host, _, _ = setup(monkeypatch)
    assert api.current_origin(orch, OWNER, "chat", host) is None
    inherited = asyncio.Event()
    async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", "read_file", {"computer": HOST, "path": "/tmp/project/a"}):
        assert api.current_origin(orch, OWNER, "chat", host) is not None
        assert api.current_origin(orch, OWNER, "other-chat", host) is None
        async def later():
            await inherited.wait()
            return api.current_origin(orch, OWNER, "chat", host)
        task = asyncio.create_task(later())
    inherited.set()
    assert await task is None


def test_project_descriptor_cannot_smuggle_legacy_roots():
    from shared.protocol import ComputerHostDescriptor, ProtocolValidationError

    value = {"host_id": HOST, "name": "Mac", "platform": "macos", "client_version": "2.0.2", "protocol": 1,
        "screens": [{"index": 0, "width": 1000, "height": 800, "scale": 2, "primary": True}],
        "verbs": ["read_file"], "project_manifest": manifest().to_dict()}
    assert ComputerHostDescriptor.from_dict(value).to_dict() == value
    with pytest.raises(ProtocolValidationError):
        ComputerHostDescriptor.from_dict({**value, "workspace_roots": [{"name": "Legacy", "path": "/tmp/legacy"}]})


@pytest.mark.asyncio
async def test_descriptor_owner_validation_precedes_registration_and_ack(monkeypatch):
    api, orch, ws, host, _, projects = setup(monkeypatch)
    from orchestrator.orchestrator import Orchestrator
    from shared.protocol import ComputerHostDescriptor

    sent = []
    async def send(socket, raw):
        import json
        sent.append((socket, json.loads(raw)))
        return True
    async def nothing(*args):
        pass
    orch._safe_send = send
    orch.ui_clients = [ws]
    orch.computer_sessions = SimpleNamespace(clear_bypass=nothing, end_all_for_host=nothing)
    value = {"host_id": HOST, "name": "Mac", "platform": "macos", "client_version": "2.0.2", "protocol": 1,
        "screens": [{"index": 0, "width": 1000, "height": 800, "scale": 2, "primary": True}],
        "verbs": ["read_file"], "project_manifest": manifest().to_dict()}
    projects.clear()
    await Orchestrator._register_computer_host(orch, ws, OWNER, ComputerHostDescriptor.from_dict(value))
    assert orch.computer_hosts.get(OWNER, HOST) is host
    assert sent[-1][1]["project_manifest_ack"]["accepted"] is False
    projects.add((OWNER, PROJECT))
    await Orchestrator._register_computer_host(orch, ws, OWNER, ComputerHostDescriptor.from_dict(value))
    current = orch.computer_hosts.get(OWNER, HOST)
    assert current is not host and current.project_mode and current.project_manifest == manifest()
    assert sent[-1][1]["project_manifest_ack"] == {"generation": 1, "digest": manifest().digest, "accepted": True}


@pytest.mark.asyncio
async def test_sessions_rotate_for_origin_binding_changes_and_requests_are_stamped(monkeypatch):
    api, orch, ws, host, _, _ = setup(monkeypatch)
    from orchestrator.computer_sessions import ComputerSessionManager
    import json

    orch.ui_clients = [ws]
    manager = ComputerSessionManager(orch, orch.computer_hosts)
    orch.computer_sessions = manager
    async def nothing(*args, **kwargs):
        pass
    manager._audit = nothing
    manager._push = nothing
    manager._ensure_sweeper = lambda: None
    first = await manager.start(OWNER, host, ws, "chat", wait_for_ack=False)
    same = await manager.start(OWNER, host, ws, "chat", wait_for_ack=False)
    assert same is first
    assert first.public()["project_context"]["project_id"] == PROJECT
    second = await manager.start(OWNER, host, ws, "other-chat", wait_for_ack=False)
    assert second.session_id != first.session_id and first.state == "ended"
    sent = []
    async def send(socket, raw):
        frame = json.loads(raw)
        sent.append(frame)
        orch.computer_hosts.handle_response(socket, OWNER, {"request_id": frame["request_id"], "ok": True,
                                                          "result": {"text": "ok"}})
        return True
    orch._safe_send = send
    with pytest.raises(ComputerHostError):
        await orch.computer_hosts.request(host, second.session_id, "read_file", {"path": "/tmp/project/a"}, 1)
    fresh = await manager.start(OWNER, host, ws, "chat", wait_for_ack=False)
    result = await orch.computer_hosts.request(host, fresh.session_id, "read_file", {"path": "/tmp/project/a"}, 1)
    assert result == {"text": "ok"}
    assert sent[-1]["project_context"] == fresh.project_origin.context.to_dict()


@pytest.mark.asyncio
async def test_standing_project_consent_with_bypass_off_and_direct_grant_denials(monkeypatch):
    api, orch, ws, host, _, _ = setup(monkeypatch)
    from orchestrator.computer_sessions import ComputerSessionManager
    from orchestrator.hitl_confirmation import consent_decision

    orch.computer_sessions = ComputerSessionManager(orch, orch.computer_hosts)
    args = {"computer": HOST, "path": "/tmp/project/a"}
    assert consent_decision(orch, ws, OWNER, "chat", "computer-use-1", "write_file", args, ())[0] == "refuse"
    async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", "write_file", args):
        for verb in ("write_file", "read_file", "start_session", "end_session"):
            assert consent_decision(orch, ws, OWNER, "chat", "computer-use-1", verb, args, ())[0] == "standing"
        assert consent_decision(orch, ws, OWNER, "chat", "computer-use-1", "click", {"computer": HOST}, ())[0] == "review"
        assert consent_decision(orch, ws, OWNER, "chat", "computer-use-1", "run_command",
            {"computer": HOST, "command": "sudo whoami", "cwd": "/tmp/project"}, ())[0] == "refuse"
    async with api.project_dispatch_scope(orch, ws, OWNER, "other-chat", "computer-use-1", "list_computers", args):
        assert consent_decision(orch, ws, OWNER, "other-chat", "computer-use-1", "write_file", args, ())[0] == "refuse"

@pytest.mark.asyncio
async def test_deleted_project_invalidates_host_entry_and_live_session(monkeypatch):
    api, orch, ws, host, chats, projects = setup(monkeypatch)
    origin = await api.resolve_project_origin(orch, OWNER, "chat", host)
    ended = []
    session = SimpleNamespace(host_id=HOST, project_origin=origin)
    async def end(current, reason):
        ended.append((current, reason))
    orch.computer_sessions = SimpleNamespace(live_for_owner=lambda _: [session], end=end)
    await api.invalidate_deleted_project(orch, OWNER, PROJECT)
    assert host.project_manifest.project(PROJECT) is None
    assert host.project_retirements[PROJECT] == 1
    assert not api.origin_current(orch, origin)
    assert ended == [(session, "consent_revoked")]


@pytest.mark.asyncio
async def test_another_task_cannot_borrow_even_an_active_dispatch_witness(monkeypatch):
    api, orch, ws, host, _, _ = setup(monkeypatch)
    async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", "list_computers", {}):
        async def inherited():
            return api.current_origin(orch, OWNER, "chat", host)
        assert await asyncio.create_task(inherited()) is None
        assert api.current_origin(orch, OWNER, "chat", host) is not None

@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_send", "before_output"])
@pytest.mark.parametrize("transition", ["pause", "end", "pause_resume", "replace"])
async def test_membership_await_cannot_outlive_session_authority(monkeypatch, phase, transition):
    import copy
    import json
    import threading
    from orchestrator.computer_sessions import ComputerSessionManager

    api, orch, ws, host, _, _ = setup(monkeypatch)
    manager = ComputerSessionManager(orch, orch.computer_hosts)
    orch.computer_sessions = manager
    orch.ui_clients = [ws]
    async def nothing(*args, **kwargs):
        pass
    manager._audit = nothing
    manager._push = nothing
    manager._ensure_sweeper = lambda: None
    session = await manager.start(OWNER, host, ws, "chat", wait_for_ack=False)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = api.ProjectService.require_chat
    reads = 0
    def lookup(self, owner, chat):
        nonlocal reads
        reads += 1
        if reads == (1 if phase == "before_send" else 2):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
        return original(self, owner, chat)
    monkeypatch.setattr(api.ProjectService, "require_chat", lookup)
    sent = []
    async def send(socket, raw):
        frame = json.loads(raw)
        sent.append(frame)
        orch.computer_hosts.handle_response(socket, OWNER, {"request_id": frame["request_id"], "ok": True,
                                                            "result": {"text": "private output"}})
        return True
    orch._safe_send = send
    pending = asyncio.create_task(orch.computer_hosts.request(host, session.session_id, "read_file", {"path": "/tmp/project/a"}, 10))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if transition == "end":
            await manager.end(session, "consent_revoked", cancel_turn=False)
        elif transition == "replace":
            manager._sessions[session.session_id] = copy.copy(session)
        else:
            await manager.pause(session, "approval_not_completed", push=False)
            if transition == "pause_resume":
                await manager.resume(session, push=False)
    finally:
        release.set()
    with pytest.raises(ComputerHostError):
        await pending
    assert len(sent) == (0 if phase == "before_send" else 1)

@pytest.mark.asyncio
async def test_updates_ack_digest_revoke_only_changed_grant_and_preserve_bypass(monkeypatch):
    import json
    from orchestrator.computer_sessions import ComputerSessionManager

    api, orch, ws, host, _, _ = setup(monkeypatch)
    manager = ComputerSessionManager(orch, orch.computer_hosts)
    orch.computer_sessions = manager
    orch.ui_clients = [ws]
    async def nothing(*args, **kwargs):
        pass
    manager._audit = nothing
    manager._push = nothing
    manager._ensure_sweeper = lambda: None
    session = await manager.start(OWNER, host, ws, "chat", wait_for_ack=False)
    sent = []
    async def send(socket, raw):
        sent.append((socket, json.loads(raw)))
        return True
    orch._safe_send = send
    manager._bypass[(OWNER, HOST)] = host
    assert await api.replace_project_manifest(orch, ws, host, manifest(generation=2, revision=2).to_dict())
    assert session.state == "ended"
    assert sent[-1][1]["project_manifest_ack"]["accepted"] is True
    assert manager.bypass_on(OWNER, HOST)
    assert not await api.replace_project_manifest(orch, ws, host, manifest().to_dict())
    assert sent[-1][1]["project_manifest_ack"] == {"generation": 1, "digest": manifest().digest,
        "accepted": False, "code": "stale_project_manifest"}


@pytest.mark.asyncio
async def test_current_connection_and_whole_owner_catalog_are_rechecked_after_await(monkeypatch):
    api, orch, ws, host, _, _ = setup(monkeypatch)
    original = api.ProjectService.get
    def replace(self, owner, project):
        value = original(self, owner, project)
        orch._connection_contexts[id(ws)] = SimpleNamespace(websocket=ws, registered=True, closing=False,
                                                         connection_generation="replacement")
        return value
    monkeypatch.setattr(api.ProjectService, "get", replace)
    with pytest.raises(ComputerHostError):
        await api.admit_project_manifest(orch, ws, host, manifest(generation=2, revision=2))
    assert host.project_manifest == manifest()
    with pytest.raises(ComputerHostError):
        await api.admit_project_manifest(orch, object(), host, manifest(generation=3, revision=2))
    with pytest.raises(ComputerHostError):
        await api.prepare_project_advertisement(orch, ws, OWNER, {"host_id": HOST, "project_manifest": manifest().to_dict()})


@pytest.mark.asyncio
async def test_project_host_never_accepts_legacy_roots_or_unnegotiated_manifest(monkeypatch):
    api, orch, ws, host, _, _ = setup(monkeypatch)
    with pytest.raises(ComputerHostError):
        await api.prepare_project_advertisement(orch, ws, OWNER, {"host_id": HOST,
            "workspace_roots": [{"name": "legacy", "path": "/tmp/legacy"}]})
    orch.ui_sessions[ws]["_project_workspaces_v1"] = False
    with pytest.raises(ComputerHostError):
        await api.prepare_project_advertisement(orch, ws, OWNER, {"host_id": HOST, "project_manifest": manifest().to_dict()})
    assert await api.prepare_project_advertisement(orch, ws, OWNER, {"host_id": HOST}) == (False, None)
    assert not api.origin_current(orch, object())
    with pytest.raises(ComputerHostError):
        await api.resolve_project_origin(orch, OWNER, "chat", host)


@pytest.mark.asyncio
async def test_project_paths_outside_grant_and_foreign_controller_fail_before_effect(monkeypatch):
    api, orch, ws, host, _, _ = setup(monkeypatch)
    for args in ({"path": "/tmp/elsewhere/a"}, {"path": "../outside"}):
        with pytest.raises(ComputerHostError):
            async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", "read_file", args):
                pytest.fail("outside path admitted")
    with pytest.raises(ComputerHostError):
        async with api.project_dispatch_scope(orch, object(), OWNER, "chat", "computer-use-1", "list_computers", {}):
            pytest.fail("foreign controller admitted")
    with pytest.raises(ComputerHostError):
        api.project_roots(orch, OWNER, "chat", host)
    host.project_mode = False
    assert api.project_roots(orch, OWNER, "chat", host) == []

@pytest.mark.asyncio
async def test_mcp_worker_bridge_uses_fresh_origin_for_real_host_delivery(monkeypatch):
    import json
    from agents.computer_use import mcp_tools
    from orchestrator.computer_sessions import ComputerSessionManager

    api, orch, ws, host, _, _ = setup(monkeypatch)
    host.descriptor["verbs"] = ["read_file", "list_dir", "run_command"]
    manager = ComputerSessionManager(orch, orch.computer_hosts)
    orch.computer_sessions = manager
    orch.ui_clients = [ws]
    orch.socket_for_chat = lambda *_: ws
    async def nothing(*args, **kwargs):
        pass
    manager._audit = nothing
    manager._push = nothing
    manager._ensure_sweeper = lambda: None
    session = await manager.start(OWNER, host, ws, "chat", wait_for_ack=False)
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_LOOP", asyncio.get_running_loop())
    sent = []
    async def send(socket, raw):
        frame = json.loads(raw)
        sent.append(frame)
        orch.computer_hosts.handle_response(socket, OWNER, {"request_id": frame["request_id"], "ok": True,
            "result": {"text": "owned", "entries": []}})
        return True
    orch._safe_send = send
    for tool in ("read_file", "list_dir"):
        args = {"path": "/tmp/project/a"}
        async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", tool, args):
            result = await asyncio.to_thread(getattr(mcp_tools, tool), user_id=OWNER, session_id="chat", **args)
        assert "code" not in result["_data"], result
        assert sent[-1]["project_context"] == session.project_origin.context.to_dict()
    async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", "list_computers", {}):
        result = await asyncio.to_thread(mcp_tools.list_computers, user_id=OWNER, session_id="chat")
        assert result["_data"]["computers"][0]["primary_root"] == "/tmp/project"
        args = await asyncio.to_thread(mcp_tools._command_args, {"user_id": OWNER, "session_id": "chat", "argv": ["pwd"]}, host)
        assert args["cwd"] == "/tmp/project"
    assert mcp_tools._ctx({"user_id": OWNER, "session_id": "chat"})[-1]["_data"]["code"] == "stale_project_grant"
    monkeypatch.setattr(mcp_tools, "RESUME_SETTLE_SECONDS", 0)
    await manager.pause(session, "approval", push=False)
    async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", "resume_session", {}):
        result = await asyncio.to_thread(mcp_tools.resume_session, user_id=OWNER, session_id="chat")
    assert result["_data"]["state"] == "active"
    async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", "start_session", {}):
        result = await asyncio.to_thread(mcp_tools.start_session, user_id=OWNER, session_id="chat")
    assert result["_data"]["session_id"] == session.session_id
    async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", "end_session", {}):
        result = await asyncio.to_thread(mcp_tools.end_session, user_id=OWNER, session_id="chat")
    assert result["_data"]["ended"] is True

@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["caller_cancel", "takeover", "stop"])
@pytest.mark.parametrize("tool", ["read_file", "start_session", "end_session", "resume_session"])
async def test_queued_worker_bridge_cannot_renew_retired_caller_authority(monkeypatch, retirement, tool):
    import threading
    from agents.computer_use import mcp_tools
    from orchestrator.computer_sessions import ComputerSessionManager

    api, orch, ws, host, _, _ = setup(monkeypatch)
    manager = ComputerSessionManager(orch, orch.computer_hosts)
    orch.computer_sessions = manager
    orch.ui_clients = [ws]
    orch.socket_for_chat = lambda *_: ws
    async def nothing(*args, **kwargs):
        pass
    manager._audit = nothing
    manager._push = nothing
    manager._ensure_sweeper = lambda: None
    session = await manager.start(OWNER, host, ws, "chat", wait_for_ack=False)
    if tool == "resume_session":
        await manager.pause(session, "approval", push=False)
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_LOOP", asyncio.get_running_loop())
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = mcp_tools._bridge
    def bridge(coro, timeout, event_loop=None):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original(coro, timeout, event_loop)
    monkeypatch.setattr(mcp_tools, "_bridge", bridge)
    results, delivered = [], []
    async def send(*args):
        delivered.append(args)
        return False
    orch._safe_send = send
    def worker():
        try:
            results.append(getattr(mcp_tools, tool)(user_id=OWNER, session_id="chat", path="/tmp/project/a"))
        finally:
            loop.call_soon_threadsafe(finished.set)
    async def caller():
        async with api.project_dispatch_scope(orch, ws, OWNER, "chat", "computer-use-1", tool, {"path": "/tmp/project/a"}):
            await asyncio.to_thread(worker)
    task = asyncio.create_task(caller())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if retirement == "caller_cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif retirement == "takeover":
            session.controller_ws_id = id(object())
            session.authority_generation += 1
        else:
            await manager.end(session, "consent_revoked", cancel_turn=False)
    finally:
        release.set()
    await asyncio.wait_for(finished.wait(), 5)
    if not task.cancelled():
        await task
    assert not delivered
    assert results[0]["_data"]["effect_state"] == "not_started"

@pytest.mark.asyncio
async def test_concurrent_rotation_cannot_install_two_live_sessions_after_teardown(monkeypatch):
    from orchestrator.computer_sessions import ComputerSessionManager

    _, orch, ws, host, _, _ = setup(monkeypatch)
    manager = ComputerSessionManager(orch, orch.computer_hosts)
    orch.computer_sessions = manager
    orch.ui_clients = [ws]
    async def nothing(*args, **kwargs):
        pass
    manager._audit = nothing
    manager._push = nothing
    manager._ensure_sweeper = lambda: None
    original = await manager.start(OWNER, host, ws, "chat", wait_for_ack=False)
    entered, release = asyncio.Event(), asyncio.Event()
    async def audit(session, event, *args, **kwargs):
        if session is original and event == "computer_session.ended":
            entered.set()
            await release.wait()
    manager._audit = audit
    replacing = asyncio.create_task(manager.start(OWNER, host, ws, "other-chat", wait_for_ack=False))
    await asyncio.wait_for(entered.wait(), 5)
    current = await manager.start(OWNER, host, ws, "chat", wait_for_ack=False)
    release.set()
    with pytest.raises(ComputerHostError, match="superseded"):
        await replacing
    assert manager.live_for_owner(OWNER) == [current]
    assert manager.live_for_host(OWNER, host.host_id) is current
    assert current.session_id != original.session_id and current.state == "active"

@pytest.mark.asyncio
async def test_membership_lookup_may_not_extend_queued_call_deadline(monkeypatch):
    from agents.computer_use import mcp_tools
    from orchestrator import computer_hosts
    from orchestrator.computer_sessions import ComputerSessionManager
    import json

    api, orch, ws, host, _, _ = setup(monkeypatch)
    orch.ui_clients = [ws]
    manager = ComputerSessionManager(orch, orch.computer_hosts)
    orch.computer_sessions = manager

    async def nothing(*args, **kwargs):
        pass

    manager._audit = nothing
    manager._push = nothing
    manager._ensure_sweeper = lambda: None
    session = await manager.start(OWNER, host, ws, 'chat', wait_for_ack=False)
    monkeypatch.setattr(mcp_tools, '_ORCH', orch)
    monkeypatch.setattr(mcp_tools, '_LOOP', asyncio.get_running_loop())
    now = [100.0]
    clock = SimpleNamespace(monotonic=lambda: now[0], time=computer_hosts.time.time)
    monkeypatch.setattr(mcp_tools, 'time', clock)
    monkeypatch.setattr(computer_hosts, 'time', clock)
    calls = [0]
    original = api.ProjectService.require_chat

    def lookup(self, owner, chat):
        result = original(self, owner, chat)
        calls[0] += 1
        if calls[0] == 3:
            now[0] += 2.0
        return result

    monkeypatch.setattr(api.ProjectService, 'require_chat', lookup)
    delivered = []

    async def send(socket, raw):
        frame = json.loads(raw)
        delivered.append(frame)
        orch.computer_hosts.handle_response(socket, OWNER, {'request_id': frame['request_id'],
            'ok': True, 'result': {'text': 'late output'}})
        return True

    orch._safe_send = send
    args = {'path': '/tmp/project/a'}
    async with api.project_dispatch_scope(orch, ws, OWNER, 'chat', 'computer-use-1', 'read_file', args):
        result, error = await asyncio.to_thread(mcp_tools._run, host, session, 'read_file', args, timeout=1.0)
    assert calls[0] >= 3
    assert not delivered, (delivered, result, error)

@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_send", "after_reply", "remaining"])
async def test_direct_host_request_keeps_one_deadline_through_membership_and_output(monkeypatch, phase):
    import json
    from orchestrator import computer_hosts
    from orchestrator.computer_sessions import ComputerSessionManager

    api, orch, ws, host, _, _ = setup(monkeypatch)
    manager = ComputerSessionManager(orch, orch.computer_hosts)
    orch.computer_sessions = manager
    orch.ui_clients = [ws]
    async def nothing(*args, **kwargs):
        pass
    manager._audit = nothing
    manager._push = nothing
    manager._ensure_sweeper = lambda: None
    session = await manager.start(OWNER, host, ws, "chat", wait_for_ack=False)
    now = [100.0]
    monkeypatch.setattr(computer_hosts, "time", SimpleNamespace(monotonic=lambda: now[0], time=computer_hosts.time.time))
    calls = [0]
    original = api.ProjectService.require_chat
    def lookup(self, owner, chat):
        result = original(self, owner, chat)
        calls[0] += 1
        if calls[0] == (2 if phase == "after_reply" else 1):
            now[0] += .5 if phase == "remaining" else 2
        return result
    monkeypatch.setattr(api.ProjectService, "require_chat", lookup)
    delivered = []
    async def send(socket, raw):
        frame = json.loads(raw)
        delivered.append(frame)
        orch.computer_hosts.handle_response(socket, OWNER, {"request_id": frame["request_id"], "ok": True,
                                                           "result": {"text": "private"}})
        return True
    orch._safe_send = send
    if phase == "remaining":
        result = await orch.computer_hosts.request(host, session.session_id, "read_file", {"path": "/tmp/project/a"}, 1)
        assert result == {"text": "private"}
        assert delivered[0]["deadline_ms"] == 500
    else:
        with pytest.raises(ComputerHostError) as error:
            await orch.computer_hosts.request(host, session.session_id, "read_file", {"path": "/tmp/project/a"}, 1)
        assert error.value.code == ("timeout" if phase == "before_send" else "outcome_uncertain")
        assert error.value.effect_state == ("not_started" if phase == "before_send" else None)
        assert len(delivered) == (0 if phase == "before_send" else 1)
