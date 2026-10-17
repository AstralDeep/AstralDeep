"""Verifies isolated terminal profiles against real owner-scoped machine, credential and permission storage.
The SSH socket is substituted here; actual remote PTY acceptance is recorded separately.
"""

import json
import time
from types import SimpleNamespace

import pytest
from astralplane.repositories import RepositoryConflictError

from orchestrator import remote_machines, terminal_ssh
from orchestrator.credential_manager import CredentialManager
from orchestrator.terminal_security import TerminalError, bind_terminal_authority
from orchestrator.terminal_sessions import TerminalSession, TerminalSessionManager
from orchestrator.tool_permissions import ToolPermissionManager
from shared.feature_flags import flags
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
from tests.test_terminal_ssh_runtime import Channel, Client
from tests.helpers.terminal_connection import registered_connection


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setitem(flags._flags, "terminal_shell", True)
    with isolated_plane_runtime("ad_terminal_ssh") as plane:
        credentials = CredentialManager(plane_runtime=plane, plane_repositories=plane.repositories,
                                        data_dir=str(tmp_path / "credentials"))
        permissions = ToolPermissionManager(plane_runtime=plane, plane_repositories=plane.repositories)
        scopes = {"start_terminal": "tools:system", "terminal_write": "tools:system",
                  "terminal_read": "tools:read", "terminal_resize": "tools:write",
                  "terminal_interrupt": "tools:write"}
        permissions.register_tool_scopes("terminal-shell-1", scopes)
        for tool, scope in scopes.items():
            permissions.set_tool_permission("owner", "terminal-shell-1", tool, scope, True)
        machine = remote_machines.create_machine(plane, "owner", "isolated", "terminal.test", 2222,
                                                 "runner", "linux", "plain")
        fingerprint = "SHA256:" + "A" * 43
        remote_machines.record_probe(plane, "owner", machine, "ok", {
            "type": "ssh-ed25519", "fingerprint": fingerprint, "blob_b64": "AAAA",
        })
        credentials.set_machine_credential(machine, "owner", "password", "synthetic-initial")
        profile = tmp_path / "profile.json"
        profile.write_text(json.dumps({"profile": "operator-isolated-ssh-pty/v1", "targets": [{
            "owner_id": "owner", "machine_id": machine, "address": "terminal.test", "port": 2222,
            "username": "runner", "host_key_fingerprint": fingerprint,
        }]}))
        profile.chmod(0o400)
        monkeypatch.setenv("TERMINAL_SSH_PROFILE_FILE", str(profile))
        socket = object()
        orch = SimpleNamespace(plane_repository_source=plane, credential_manager=credentials,
                               tool_permissions=permissions, security_flags={}, ui_sessions={socket: {
                                   "sub": "owner", "exp": time.time() + 3600,
                                   "realm_access": {"roles": ["user"]},
                               }})
        channels = []
        registered_connection(orch, socket)
        clients = []

        def connect(self, target, timeout):
            assert target.machine_id == machine and target.secret == "synthetic-initial"
            channel = Channel()
            client = Client(channel)
            channels.append(channel)
            clients.append(client)
            return client, None

        monkeypatch.setattr(terminal_ssh.ParamikoTransport, "_connect", connect)
        monkeypatch.setattr(TerminalSession, "start_pump", lambda self: None)
        monkeypatch.setattr(TerminalSessionManager, "_ensure_sweeper", lambda self: None)
        manager = TerminalSessionManager(orchestrator=orch)
        orch.terminal_sessions = manager

        def authority(tool):
            return bind_terminal_authority(orch, socket, "owner", tool)

        yield SimpleNamespace(**locals())
        manager.shutdown()


def test_normal_encrypted_owner_inventory_enables_only_approved_runtime(runtime):
    env = runtime
    assert env.credentials.get_machine_credential(env.machine, "foreign") is None
    assert env.manager.capability("foreign")["available"] is False
    info = env.manager.create("owner", authority=env.authority("start_terminal"), runtime_binding=env.manager.runtime_binding("owner"))
    env.manager.write("owner", info["session_id"], "aWQK", authority=env.authority("terminal_write"), runtime_binding=env.manager.runtime_binding("owner"))
    assert env.channels[0].effects[-1] == ("send", b"id\n")
    env.manager._sessions[info["session_id"]].append_output(b"uid=1000(runner)")
    result = env.manager.read("owner", info["session_id"], offset=0, authority=env.authority("terminal_read"))
    assert result["bytes"] == 16
    with pytest.raises(TerminalError, match="terminal_session_not_found"):
        env.manager.read("foreign", info["session_id"], offset=0)


@pytest.mark.parametrize("transition", ["credential_deleted", "credential_rotated", "permission", "machine_deleted", "host_key"])
def test_durable_revocation_closes_before_any_further_terminal_input(runtime, transition):
    env = runtime
    ident = env.manager.create("owner", authority=env.authority("start_terminal"), runtime_binding=env.manager.runtime_binding("owner"))["session_id"]
    authority = env.authority("terminal_write")
    approved_binding = env.manager.runtime_binding("owner")
    if transition == "credential_deleted":
        env.credentials.delete_machine_credential(env.machine, "owner")
    elif transition == "credential_rotated":
        env.credentials.set_machine_credential(env.machine, "owner", "password", "synthetic-rotated")
    elif transition == "permission":
        env.permissions.set_tool_permission("owner", "terminal-shell-1", "terminal_write", "tools:system", False)
    elif transition == "machine_deleted":
        remote_machines.delete_machine(env.plane, "owner", env.machine)
    else:
        remote_machines.retrust_host_key(env.plane, "owner", env.machine)
    with pytest.raises(TerminalError):
        env.manager.write("owner", ident, "aWQK", authority=authority, runtime_binding=approved_binding)
    assert env.channels[0].closed and env.clients[0].closed
    assert env.channels[0].effects == [("pty", {"term": "xterm", "width": 80, "height": 24}), ("shell",)]


def test_foreign_credential_update_cannot_rebind_current_owner_terminal(runtime):
    env = runtime
    with pytest.raises(RepositoryConflictError):
        env.credentials.set_machine_credential(env.machine, "foreign", "password", "foreign-synthetic")
    ident = env.manager.create("owner", authority=env.authority("start_terminal"), runtime_binding=env.manager.runtime_binding("owner"))["session_id"]
    env.credentials.delete_machine_credential(env.machine, "foreign")
    env.manager.write("owner", ident, "eAo=", authority=env.authority("terminal_write"), runtime_binding=env.manager.runtime_binding("owner"))
    assert env.channels[0].effects[-1] == ("send", b"x\n")


def _execution(env, label):
    from dataclasses import replace
    from datetime import UTC, datetime
    from tests.test_work_recovery_postgres import _node, _request
    from orchestrator.work_admission import AdmissionClass, OperationOwner, OwnerScope

    coordinator = getattr(env.orch, "work_admission", None)
    if coordinator is None:
        coordinator = _node(env.plane, lambda: datetime.now(UTC))
        env.orch.work_admission = coordinator
    request = replace(_request(label), owner=OperationOwner(OwnerScope.USER, "owner", None))
    coordinator.submit(request)
    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None
    return coordinator, claim


def test_completed_creation_work_does_not_invalidate_a_fresh_terminal_action(runtime):
    from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
    from orchestrator.work_admission import OperationState

    env = runtime
    coordinator, created = _execution(env, "terminal-create")
    token = _CONNECTION_OPERATION_CONTEXT.set({"execution_fence": created.fence})
    try:
        info = env.manager.create("owner", authority=env.authority("start_terminal"), runtime_binding=env.manager.runtime_binding("owner"))
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    coordinator.terminalize(created.fence, state=OperationState.COMPLETED, terminal_code=None, safe_summary=None, retry_after_ms=None)
    _, write = _execution(env, "terminal-input")
    token = _CONNECTION_OPERATION_CONTEXT.set({"execution_fence": write.fence})
    try:
        env.manager.write("owner", info["session_id"], "eAo=", authority=env.authority("terminal_write"), runtime_binding=env.manager.runtime_binding("owner"))
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    env.manager.sweep()
    assert env.channels[0].effects[-1] == ("send", b"x\n")
    assert not env.channels[0].closed


@pytest.mark.parametrize("transition", ["cancelled", "reselected", "terminalized", "admission_replaced", "runtime_replaced"])
def test_changed_execution_fence_during_ssh_connect_closes_before_any_pty(runtime, monkeypatch, transition):
    from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
    from orchestrator.work_admission import OperationOwner, OperationState, OwnerScope

    env = runtime
    coordinator, claim = _execution(env, "terminal-fenced")
    original = terminal_ssh.ParamikoTransport._connect

    def connect(*args):
        result = original(*args)
        if transition == "cancelled":
            coordinator.cancel(owner=OperationOwner(OwnerScope.USER, "owner", None), operation_id=claim.operation.operation_id,
                               terminal_code="cancelled_by_user")
        elif transition == "reselected":
            coordinator.reselect_execution(claim.fence)
        elif transition == "admission_replaced":
            env.orch.work_admission = object()
        elif transition == "runtime_replaced":
            env.orch.plane_repository_source = object()
        else:
            coordinator.terminalize(claim.fence, state=OperationState.COMPLETED, terminal_code=None, safe_summary=None, retry_after_ms=None)
        return result

    monkeypatch.setattr(terminal_ssh.ParamikoTransport, "_connect", connect)
    token = _CONNECTION_OPERATION_CONTEXT.set({"execution_fence": claim.fence})
    try:
        authority = env.authority("start_terminal")
        with pytest.raises(TerminalError, match="terminal_execution_authority_lost"):
            env.manager.create("owner", authority=authority, runtime_binding=env.manager.runtime_binding("owner"))
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    assert env.clients[0].closed and env.channels[0].effects == []
    assert env.manager._pending == {} and env.manager._sessions == {}


def test_production_admission_cannot_execute_without_a_current_work_fence(runtime):
    env = runtime
    _execution(env, "missing-context")
    with pytest.raises(TerminalError, match="terminal_execution_authority_required"):
        env.authority("start_terminal")
    assert env.channels == []
