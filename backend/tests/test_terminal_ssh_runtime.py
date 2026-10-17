"""Exercises explicitly approved SSH terminal profiles and bounded remote session admission.
Only the network boundary is substituted; profile, authority and manager decisions remain real.
"""

import json
from types import SimpleNamespace

from orchestrator.terminal_sessions import TerminalSessionManager
from shared.feature_flags import flags


import base64
import time
from dataclasses import replace

import pytest

from orchestrator import terminal_sessions as sessions
from orchestrator import terminal_ssh as ssh
from orchestrator.remote_transport import MachineTarget
from orchestrator.terminal_security import TerminalError, bind_terminal_authority
from tests.helpers.terminal_connection import registered_connection


def test_isolated_profile_cannot_be_enabled_by_an_owner_machine_alone(monkeypatch):
    monkeypatch.setitem(flags._flags, "terminal_shell", True)
    monkeypatch.delenv("TERMINAL_SSH_PROFILE_FILE", raising=False)
    orch = SimpleNamespace(credential_manager=object())
    manager = TerminalSessionManager(orchestrator=orch)
    assert manager.capability("owner") == {
        "enabled": True, "available": False, "reason": "terminal_isolation_unavailable",
    }
    assert manager._sessions == {}
    assert json.dumps(manager.capability("owner")).find("credential") == -1


class Channel:
    closed = False

    def __init__(self):
        self.effects = []
        self.fail = None

    def settimeout(self, value):
        self.timeout = value

    def get_pty(self, **values):
        self.effects.append(("pty", values))

    def invoke_shell(self):
        self.effects.append(("shell",))
        if self.fail:
            raise self.fail

    def exec_command(self, value):
        self.effects.append(("command", value))

    def send(self, value):
        if self.fail:
            raise self.fail
        self.effects.append(("send", bytes(value)))
        return len(value)

    def resize_pty(self, **values):
        if self.fail:
            raise self.fail
        self.effects.append(("resize", values))

    def close(self):
        self.closed = True


class Client:
    def __init__(self, channel):
        self.channel = channel
        self.closed = False

    def get_transport(self):
        return self

    def open_session(self, *, timeout):
        return self.channel

    def close(self):
        self.closed = True


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setitem(flags._flags, "terminal_shell", True)
    target = MachineTarget("a" * 32, "isolated", "terminal.test", 2222, "runner", "password",
                           secret="synthetic-only-password", host_key_fingerprint="SHA256:" + "A" * 43)
    entry = {"owner_id": "owner", **{key: getattr(target, key) for key in (
        "machine_id", "address", "port", "username", "host_key_fingerprint",
    )}}
    profile = tmp_path / "profile.json"

    def save(document=None):
        profile.chmod(0o600) if profile.exists() else None
        profile.write_text(json.dumps(document or {"profile": "operator-isolated-ssh-pty/v1", "targets": [entry]}))
        profile.chmod(0o400)

    save()
    monkeypatch.setenv("TERMINAL_SSH_PROFILE_FILE", str(profile))
    socket = object()
    claims = {"sub": "owner", "exp": time.time() + 3600, "realm_access": {"roles": ["user"]}}
    permissions = SimpleNamespace(allowed=True)
    orch = SimpleNamespace(plane_repository_source=object(), credential_manager=object(),
                           ui_sessions={socket: claims}, security_flags={},
                           tool_permissions=SimpleNamespace(is_tool_allowed=lambda *_: permissions.allowed))
    registered_connection(orch, socket)
    state = SimpleNamespace(target=target, connects=[], channels=[], clients=[])
    monkeypatch.setattr(ssh.remote_machines, "build_target", lambda source, credentials, owner, machine: state.target)

    def connect(transport, selected, timeout):
        state.connects.append(selected)
        channel = Channel()
        client = Client(channel)
        state.channels.append(channel)
        state.clients.append(client)
        return client, None

    monkeypatch.setattr(ssh.ParamikoTransport, "_connect", connect)
    monkeypatch.setattr(sessions.TerminalSession, "start_pump", lambda self: None)
    original_ensure_sweeper = sessions.TerminalSessionManager._ensure_sweeper
    monkeypatch.setattr(sessions.TerminalSessionManager, "_ensure_sweeper", lambda self: None)
    monkeypatch.setattr(sessions, "audit_machine_event", lambda *args, **kwargs: None)
    manager = sessions.TerminalSessionManager(orchestrator=orch)
    orch.terminal_sessions = manager

    def authority(tool):
        return bind_terminal_authority(orch, socket, "owner", tool)

    def create(**kwargs):
        return manager.create("owner", authority=authority("start_terminal"),
                              runtime_binding=manager.runtime_binding("owner"), **kwargs)

    yield SimpleNamespace(**locals())
    manager.shutdown()


def test_approved_runtime_opens_only_pinned_owner_target_and_roundtrips_bytes(isolated):
    env = isolated
    assert env.manager.capability("owner") == {"enabled": True, "available": True, "reason": None}
    info = env.create(cols=100, rows=30)
    ident = info["session_id"]
    assert env.state.connects == [env.target]
    assert env.state.channels[0].effects == [("pty", {"term": "xterm", "width": 100, "height": 30}), ("shell",)]
    binary = b"\x00\xff\x1bA"
    env.manager.write("owner", ident, base64.b64encode(binary).decode(), authority=env.authority("terminal_write"), runtime_binding=env.manager.runtime_binding("owner"))
    assert env.state.channels[0].effects[-1] == ("send", binary)
    session = env.manager._sessions[ident]
    session.append_output(binary)
    output = env.manager.read("owner", ident, offset=0, authority=env.authority("terminal_read"))
    assert base64.b64decode(output["data_b64"]) == binary
    assert output["next_offset"] == 4
    env.manager.resize("owner", ident, cols=90, rows=25, authority=env.authority("terminal_resize"))
    env.manager.interrupt("owner", ident, authority=env.authority("terminal_interrupt"))
    assert env.state.channels[0].effects[-2:] == [("resize", {"width": 90, "height": 25}), ("send", b"\x03")]
    assert env.manager.close("owner", ident)["closed"]
    assert env.state.clients[0].closed and env.state.channels[0].closed
    assert env.manager.list_sessions("owner") == []


def test_explicit_command_uses_only_remote_pty_exec(isolated):
    isolated.create(command="printf 'remote only'")
    assert isolated.state.channels[0].effects[-1] == ("command", "printf 'remote only'")


def test_proposal_machine_identity_requires_same_approved_target_and_live_session(isolated):
    env = isolated
    binding = env.manager.runtime_binding("owner")
    assert env.manager.runtime_machine_id("owner", runtime_binding=binding) == env.target.machine_id
    with pytest.raises(TerminalError, match="terminal_target_changed"):
        env.manager.runtime_machine_id("owner", runtime_binding="0" * 64)
    ident = env.create()["session_id"]
    assert env.manager.runtime_machine_id("owner", runtime_binding=binding, session_id=ident) == env.target.machine_id
    with pytest.raises(TerminalError, match="terminal_session_not_found"):
        env.manager.runtime_machine_id("owner", runtime_binding=binding, session_id="missing")
    env.state.target = replace(env.target, secret="rotated-synthetic")
    with pytest.raises(TerminalError, match="terminal_target_changed"):
        env.manager.runtime_machine_id("owner", runtime_binding=binding, session_id=ident)
    assert env.state.connects == [env.target]


@pytest.mark.parametrize("field,value", [
    ("machine_id", "b" * 32), ("address", "foreign.test"), ("port", 22),
    ("username", "root"), ("host_key_fingerprint", None), ("secret", ""),
    ("cred_type", "other"), ("passphrase", 1),
])
def test_changed_or_unusable_current_target_never_opens_a_channel(isolated, field, value):
    isolated.state.target = replace(isolated.target, **{field: value})
    with pytest.raises(TerminalError, match="terminal_target_unavailable"):
        isolated.create()
    assert isolated.state.connects == [] and isolated.manager._pending == {}


@pytest.mark.parametrize("command", ["", " ", "x" * 513, "x\x00y", 1])
def test_invalid_command_never_opens_a_channel(isolated, command):
    with pytest.raises(TerminalError, match="terminal_command_invalid"):
        isolated.create(command=command)
    assert isolated.state.connects == []


@pytest.mark.parametrize("data", ["", "@@@", "YQ", 1, "YQ==\n", base64.b64encode(b"x" * 16385).decode()])
def test_invalid_input_does_not_reach_remote_channel(isolated, data):
    ident = isolated.create()["session_id"]
    with pytest.raises(TerminalError, match="terminal_input_invalid"):
        isolated.manager.write("owner", ident, data, authority=isolated.authority("terminal_write"), runtime_binding=isolated.manager.runtime_binding("owner"))
    assert len(isolated.state.channels[0].effects) == 2


@pytest.mark.parametrize("change,code", [("credential", "terminal_target_changed"),
                                          ("profile", "terminal_target_changed"),
                                          ("permission", "terminal_permission_denied"),
                                          ("disconnect", "terminal_owner_required")])
def test_revocation_before_input_closes_session_without_sending(isolated, change, code):
    env = isolated
    ident = env.create()["session_id"]
    authority = env.authority("terminal_write")
    if change == "credential":
        env.state.target = replace(env.target, secret="rotated-synthetic")
    elif change == "profile":
        env.profile.chmod(0o600)
        env.profile.write_text(env.profile.read_text() + " ")
        env.profile.chmod(0o400)
    elif change == "permission":
        env.permissions.allowed = False
    else:
        env.orch.ui_sessions.clear()
    with pytest.raises(TerminalError, match=code):
        env.manager.write("owner", ident, "eAo=", authority=authority, runtime_binding=env.manager.runtime_binding("owner"))
    assert env.state.channels[0].closed and env.state.clients[0].closed
    assert len(env.state.channels[0].effects) == 2


def test_auth_loss_during_connect_closes_client_before_pty(isolated, monkeypatch):
    original = ssh.ParamikoTransport._connect

    def connect(*args):
        result = original(*args)
        isolated.orch.ui_sessions.clear()
        return result

    monkeypatch.setattr(ssh.ParamikoTransport, "_connect", connect)
    with pytest.raises(TerminalError, match="terminal_owner_required"):
        isolated.create()
    assert isolated.state.channels[0].effects == []
    assert isolated.state.clients[0].closed
    assert isolated.manager._sessions == {} and isolated.manager._pending == {}


def test_partial_spawn_failure_is_sanitized_and_releases_capacity(isolated, monkeypatch):
    original = ssh.ParamikoTransport._connect

    def connect(*args):
        client, policy = original(*args)
        client.channel.fail = RuntimeError("private remote detail")
        return client, policy

    monkeypatch.setattr(ssh.ParamikoTransport, "_connect", connect)
    with pytest.raises(TerminalError, match="^terminal_spawn_failed$") as refused:
        isolated.create()
    assert "private" not in str(refused.value)
    assert isolated.state.channels[0].closed and isolated.state.clients[0].closed
    assert isolated.manager._pending == {} and isolated.manager._sessions == {}


def test_session_capacity_reserves_before_blocking_connect(isolated, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    monkeypatch.setattr(sessions, "SESSIONS_PER_OWNER", 1)
    entered = threading.Event()
    release = threading.Event()
    original = ssh.ParamikoTransport._connect

    def connect(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(ssh.ParamikoTransport, "_connect", connect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(isolated.create)
        assert entered.wait(5)
        try:
            with pytest.raises(TerminalError, match="terminal_session_limit_reached"):
                isolated.create()
        finally:
            release.set()
        assert first.result()["session_id"]
    assert len(isolated.state.connects) == 1 and isolated.manager._pending == {}


def test_shutdown_during_connect_cannot_publish_or_restart_sweeper(isolated, monkeypatch):
    original = ssh.ParamikoTransport._connect

    def connect(*args):
        result = original(*args)
        isolated.manager.shutdown()
        return result

    monkeypatch.setattr(ssh.ParamikoTransport, "_connect", connect)
    with pytest.raises(TerminalError, match="terminal_target_changed"):
        isolated.create()
    assert isolated.state.clients[0].closed and isolated.manager._sessions == {}
    assert isolated.manager._pending == {} and isolated.manager._sweeper_stop.is_set()


def test_connection_cleanup_closes_only_its_terminal(isolated):
    ident = isolated.create()["session_id"]
    assert isolated.manager.close_for_connection(object()) == 0
    assert isolated.manager.close_for_connection(isolated.socket) == 1
    assert isolated.manager._sessions[ident].close_reason == "disconnected"
    assert isolated.state.clients[0].closed


@pytest.mark.parametrize("change", ["missing", "writable", "world_readable", "symlink", "directory", "oversize", "malformed", "duplicate", "unknown", "foreign"])
def test_profile_failures_cannot_open_a_network_connection(isolated, monkeypatch, change):
    profile = isolated.profile
    if change == "missing":
        profile.unlink()
    elif change == "writable":
        profile.chmod(0o600)
    elif change == "world_readable":
        profile.chmod(0o444)
    elif change == "symlink":
        other = profile.with_suffix(".link")
        other.symlink_to(profile)
        monkeypatch.setenv("TERMINAL_SSH_PROFILE_FILE", str(other))
    elif change == "directory":
        monkeypatch.setenv("TERMINAL_SSH_PROFILE_FILE", str(profile.parent))
    elif change in {"oversize", "malformed", "duplicate"}:
        profile.chmod(0o600)
        profile.write_text({"oversize": " " * 65537, "malformed": "[", "duplicate": '{"profile":1,"profile":2}'}[change])
        profile.chmod(0o400)
    else:
        document = {"profile": "operator-isolated-ssh-pty/v1", "targets": [dict(isolated.entry)]}
        if change == "unknown":
            document["targets"][0]["isolation"] = True
        else:
            document["targets"][0]["owner_id"] = "foreign"
        isolated.save(document)
    with pytest.raises(TerminalError, match="terminal_(profile_invalid|isolation_unavailable)"):
        isolated.create()
    assert isolated.state.connects == []


@pytest.mark.parametrize("field,value", [("port", True), ("port", 0), ("port", 65536), ("username", "root;id"),
                                        ("address", "\nremote"), ("machine_id", "bad"),
                                        ("host_key_fingerprint", "SHA256:bad"), ("owner_id", "x" * 513)])
def test_malformed_profile_bindings_fail_closed(isolated, field, value):
    entry = dict(isolated.entry, **{field: value})
    isolated.save({"profile": "operator-isolated-ssh-pty/v1", "targets": [entry]})
    assert isolated.manager.capability("owner")["reason"] == "terminal_profile_invalid"
    assert isolated.state.connects == []


def test_profile_removal_before_input_closes_the_existing_channel(isolated):
    ident = isolated.create()["session_id"]
    authority = isolated.authority("terminal_write")
    isolated.profile.unlink()
    with pytest.raises(TerminalError, match="terminal_profile_invalid"):
        isolated.manager.write("owner", ident, "eAo=", authority=authority, runtime_binding="old-binding")
    assert isolated.state.clients[0].closed and isolated.state.channels[0].closed


def test_shutdown_after_publication_cannot_return_a_working_session(isolated, monkeypatch):
    monkeypatch.setattr(sessions.TerminalSession, "start_pump", lambda _: isolated.manager.shutdown())
    with pytest.raises(TerminalError, match="terminal_target_changed"):
        isolated.create()
    assert isolated.state.clients[0].closed
    assert isolated.manager._sweeper_stop.is_set()


def test_failed_spawn_does_not_leave_a_published_session(isolated, monkeypatch):
    def fail(_):
        raise RuntimeError("thread admission failed")

    monkeypatch.setattr(sessions.TerminalSession, "start_pump", fail)
    with pytest.raises(TerminalError, match="terminal_spawn_failed"):
        isolated.create()
    assert isolated.state.clients[0].closed
    assert isolated.manager._sessions == {} and isolated.manager._pending == {}


def test_actual_terminal_agent_create_and_binary_input_use_current_bound_authority(isolated, monkeypatch):
    from agents.terminal_shell import mcp_tools

    env = isolated
    monkeypatch.setattr(mcp_tools, "_ORCH", env.orch)

    def invoke(tool, **arguments):
        return mcp_tools.TOOL_REGISTRY[tool]["function"](
            user_id="owner", _terminal_authority=env.authority(tool),
            runtime_binding=env.manager.runtime_binding("owner"), **arguments)

    created = invoke("start_terminal", cols=92, rows=28)
    ident = created["_data"]["session"]["session_id"]
    written = invoke("terminal_write", session_id=ident, data="echo safe\n")
    assert written["_data"]["session"]["session_id"] == ident
    assert env.state.channels[0].effects[-1] == ("send", b"echo safe\n")
    invoke("terminal_write", session_id=ident, data_b64="AP8=")
    assert env.state.channels[0].effects[-1] == ("send", b"\x00\xff")
    assert invoke("terminal_write", session_id=ident, data="x", data_b64="eA==")["_data"]["code"] == "terminal_input_invalid"
    assert invoke("terminal_write", session_id=ident, data=1)["_data"]["code"] == "terminal_input_invalid"
    assert invoke("terminal_write", session_id=None, data="x")["_data"]["code"] == "terminal_session_invalid"
    invoke("terminal_resize", session_id=ident, cols=110, rows=40)
    invoke("terminal_interrupt", session_id=ident)
    assert env.state.channels[0].effects[-2:] == [("resize", {"width": 110, "height": 40}), ("send", b"\x03")]
    env.state.channels[0].fail = OSError("private transport secret")
    failure = invoke("terminal_write", session_id=ident, data="x")
    assert failure["_data"]["code"] == "terminal_write_failed"
    assert "private transport secret" not in json.dumps(failure)
    assert env.state.clients[0].closed


@pytest.mark.parametrize("effect", ["resize", "interrupt"])
def test_remote_control_failure_closes_transport_without_exposing_error(isolated, effect):
    ident = isolated.create()["session_id"]
    isolated.state.channels[0].fail = OSError("private remote error")
    arguments = {"cols": 90, "rows": 30} if effect == "resize" else {}
    with pytest.raises(TerminalError, match="^terminal_transport_lost$"):
        getattr(isolated.manager, effect)("owner", ident, authority=isolated.authority("terminal_" + effect), **arguments)
    assert isolated.state.clients[0].closed


def test_output_wait_revalidates_revoked_permission_before_returning_bytes(isolated, monkeypatch):
    ident = isolated.create()["session_id"]
    authority = isolated.authority("terminal_read")

    def readable(session, timeout):
        session.append_output(b"private output")
        isolated.permissions.allowed = False

    monkeypatch.setattr(isolated.manager, "_wait_readable", readable)
    with pytest.raises(TerminalError, match="terminal_permission_denied"):
        isolated.manager.read("owner", ident, offset=0, wait_seconds=1, authority=authority)
    assert isolated.state.clients[0].closed


@pytest.mark.parametrize("result", [[None, b"remote output", b""], [OSError("private peer detail")]])
def test_remote_pump_preserves_actual_bytes_and_closes_on_eof_or_transport_failure(isolated, result):
    ident = isolated.create()["session_id"]
    session = isolated.manager._sessions[ident]
    values = iter(result)

    def receive(size):
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    session.remote.receive = receive
    session._pump_loop()
    assert session.closed and isolated.state.clients[0].closed
    assert session.read(0)[0] == (b"remote output" if len(result) == 3 else b"")


def test_partial_input_rechecks_authority_before_each_chunk(isolated):
    ident = isolated.create()["session_id"]
    channel = isolated.state.channels[0]

    def partial(value):
        channel.effects.append(("send", bytes(value[:1])))
        isolated.permissions.allowed = False
        return 1

    channel.send = partial
    with pytest.raises(TerminalError, match="terminal_permission_denied"):
        isolated.manager.write("owner", ident, "YWJj", authority=isolated.authority("terminal_write"), runtime_binding=isolated.manager.runtime_binding("owner"))
    assert channel.effects[-1] == ("send", b"a")
    assert isolated.state.clients[0].closed


def test_partial_input_cannot_extend_total_transport_deadline(isolated, monkeypatch):
    ident = isolated.create()["session_id"]
    channel = isolated.state.channels[0]
    channel.send = lambda value: 1
    ticks = iter([0, 0, 6])
    monkeypatch.setattr(ssh.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TerminalError, match="terminal_write_failed"):
        isolated.manager.write("owner", ident, "YWJj", authority=isolated.authority("terminal_write"), runtime_binding=isolated.manager.runtime_binding("owner"))
    assert isolated.state.clients[0].closed


def test_zero_byte_send_is_a_failure_not_reported_as_written(isolated):
    ident = isolated.create()["session_id"]
    isolated.state.channels[0].send = lambda value: 0
    with pytest.raises(TerminalError, match="terminal_write_failed"):
        isolated.manager.write("owner", ident, "eA==", authority=isolated.authority("terminal_write"), runtime_binding=isolated.manager.runtime_binding("owner"))
    assert isolated.state.clients[0].closed


def test_remote_receive_handles_ready_output_eof_and_wait(isolated, monkeypatch):
    channel = Channel()
    channel.recv_ready = lambda: True
    channel.recv = lambda size: b"bytes"[:size]
    channel.exit_status_ready = lambda: False
    remote = ssh.SSHPTY(Client(channel), channel)
    assert remote.receive(3) == b"byt"
    channel.recv_ready = lambda: False
    observed = []
    monkeypatch.setattr(ssh.select, "select", lambda *args: observed.append(args))
    assert remote.receive(3) is None and observed
    channel.closed = True
    assert remote.receive(3) == b""


def test_periodic_sweep_closes_revoked_runtime_sessions(isolated):
    ident = isolated.create()["session_id"]
    isolated.state.target = replace(isolated.target, secret="rotated")
    isolated.manager.sweep()
    assert isolated.manager._sessions[ident].closed and isolated.state.clients[0].closed


def test_profile_change_between_precheck_and_reservation_cannot_retarget_an_approval(isolated, monkeypatch):
    env = isolated
    approved = env.manager.runtime_binding("owner")
    original = env.manager.require_execution
    calls = 0

    def check(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 2:
            env.profile.chmod(0o600)
            env.profile.write_text(env.profile.read_text() + " ")
            env.profile.chmod(0o400)
        return result

    monkeypatch.setattr(env.manager, "require_execution", check)
    with pytest.raises(TerminalError, match="terminal_target_changed"):
        env.manager.create("owner", authority=env.authority("start_terminal"), runtime_binding=approved)
    assert env.state.connects == [] and env.manager._pending == {}


def test_reconnected_current_owner_can_resume_closed_output_without_reopening_runtime(isolated):
    env = isolated
    ident = env.create()["session_id"]
    env.manager._sessions[ident].append_output(b"retained result")
    env.manager.close_for_connection(env.socket)
    claims = env.orch.ui_sessions.pop(env.socket)
    replacement = object()
    env.orch.ui_sessions[replacement] = claims
    authority = bind_terminal_authority(env.orch, replacement, "owner", "terminal_read")
    result = env.manager.read("owner", ident, offset=0, authority=authority)
    assert base64.b64decode(result["data_b64"]) == b"retained result"
    assert result["eof"] and result["session"] is None
    assert len(env.state.connects) == 1 and env.state.clients[0].closed


def test_one_remote_account_cannot_be_approved_for_two_owners(isolated):
    entry = dict(isolated.entry, owner_id="foreign", address="same-runtime-alias.test")
    isolated.save({"profile": "operator-isolated-ssh-pty/v1", "targets": [isolated.entry, entry]})
    with pytest.raises(TerminalError, match="terminal_profile_invalid"):
        isolated.create()
    assert isolated.state.connects == []


def test_closed_output_retention_cannot_grow_without_bound(isolated, monkeypatch):
    monkeypatch.setattr(sessions, "RETAINED_SESSION_LIMIT", 2, raising=False)
    first = isolated.create()["session_id"]
    isolated.manager.close("owner", first)
    second = isolated.create()["session_id"]
    isolated.manager.close("owner", second)
    third = isolated.create()["session_id"]
    assert set(isolated.manager._sessions) == {second, third}
    with pytest.raises(TerminalError, match="terminal_session_not_found"):
        isolated.manager.read("owner", first, offset=0)


def test_human_target_preview_is_exact_binding_and_contains_no_credential_identity(isolated):
    env = isolated
    digest = env.manager.runtime_binding("owner")
    preview = env.manager.runtime_preview("owner", runtime_binding=digest)
    assert preview == ("terminal.test:2222 as runner; machine " + "a" * 32 + "; host key SHA256:" + "A" * 43)
    assert env.target.secret not in preview and env.manager.require_execution("owner")[0].credential_digest not in preview
    with pytest.raises(TerminalError, match="terminal_target_changed"):
        env.manager.runtime_preview("owner", runtime_binding="0" * 64)
    assert env.state.connects == []


def test_original_request_check_cannot_expire_during_blocking_ssh_open(isolated, monkeypatch):
    env = isolated
    alive = {"current": True}

    def current():
        if not alive["current"]:
            raise TerminalError("terminal_authority_expired", 401)

    authority = replace(env.authority("start_terminal"), _request_check=current)
    original = ssh.ParamikoTransport._connect

    def connect(*args):
        result = original(*args)
        alive["current"] = False
        return result

    monkeypatch.setattr(ssh.ParamikoTransport, "_connect", connect)
    with pytest.raises(TerminalError, match="terminal_authority_expired"):
        env.manager.create("owner", authority=authority, runtime_binding=env.manager.runtime_binding("owner"))
    assert env.state.clients[0].closed and env.state.channels[0].effects == []


def test_session_lifetime_does_not_reuse_completed_request_callback(isolated):
    env = isolated
    alive = {"current": True}

    def current():
        if not alive["current"]:
            raise TerminalError("terminal_authority_expired", 401)

    authority = replace(env.authority("start_terminal"), _request_check=current)
    info = env.manager.create("owner", authority=authority, runtime_binding=env.manager.runtime_binding("owner"))
    alive["current"] = False
    env.manager.sweep()
    assert not env.state.clients[0].closed
    env.manager.write("owner", info["session_id"], "eAo=", authority=env.authority("terminal_write"),
                       runtime_binding=env.manager.runtime_binding("owner"))
    assert env.state.channels[0].effects[-1] == ("send", b"x\n")


def test_original_request_is_rechecked_after_permission_storage(isolated):
    alive = {"current": True}

    def current():
        if not alive["current"]:
            raise TerminalError("terminal_authority_expired", 401)

    authority = replace(isolated.authority("start_terminal"), _request_check=current)

    def permissions(*args):
        alive["current"] = False
        return True

    isolated.orch.tool_permissions.is_tool_allowed = permissions
    with pytest.raises(TerminalError, match="terminal_authority_expired"):
        authority.assert_current("start_terminal")
    assert isolated.state.connects == []


def test_target_preview_for_input_cannot_hide_a_changed_session_binding(isolated):
    env = isolated
    ident = env.create()["session_id"]
    digest = env.manager.runtime_binding("owner", session_id=ident)
    assert "terminal.test:2222" in env.manager.runtime_preview("owner", runtime_binding=digest, session_id=ident)
    env.state.target = replace(env.target, secret="rotated")
    with pytest.raises(TerminalError, match="terminal_target_changed"):
        env.manager.runtime_preview("owner", runtime_binding=digest, session_id=ident)


def test_shutdown_between_partial_input_chunks_prevents_any_later_send(isolated):
    ident = isolated.create()["session_id"]
    channel = isolated.state.channels[0]

    def partial(value):
        channel.effects.append(("send", bytes(value[:1])))
        isolated.manager.shutdown()
        return 1

    channel.send = partial
    with pytest.raises(TerminalError, match="terminal_session_closed"):
        isolated.manager.write("owner", ident, "YWJj", authority=isolated.authority("terminal_write"),
                               runtime_binding=isolated.manager.runtime_binding("owner"))
    assert [effect for effect in channel.effects if effect[0] == "send"] == [("send", b"a")]


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["handle_ui_connection", "handle_ui_connection_fastapi"])
async def test_real_connection_teardown_closes_its_remote_channel(isolated, entrypoint):
    from orchestrator.orchestrator import Orchestrator
    from starlette.websockets import WebSocketDisconnect

    env = isolated

    class Socket:
        async def accept(self):
            return None

        async def receive_text(self):
            raise WebSocketDisconnect()

        async def recv(self):
            raise StopAsyncIteration()

    socket = Socket()
    env.orch.ui_sessions[socket] = env.orch.ui_sessions.pop(env.socket)
    registered_connection(env.orch, socket)
    authority = bind_terminal_authority(env.orch, socket, "owner", "start_terminal")
    ident = env.manager.create("owner", authority=authority, runtime_binding=env.manager.runtime_binding("owner"))["session_id"]

    async def serve(transport, receive):
        await receive()

    async def noop(*args):
        return None

    env.orch._serve_ui_frames = serve
    env.orch._cleanup_streams = lambda *_: None
    env.orch._disconnect_personal_agent_host = noop
    env.orch._teardown_owner_tunnels = noop
    env.orch._teardown_computer_host = noop
    env.orch._clear_voice_control_binding = lambda *_: None
    env.orch._drop_chat_locks = lambda *_: None
    env.orch.ui_clients = []
    env.orch._registered_events = {}
    env.orch._ws_active_chat = {}
    env.orch._ws_timeline_mode = {}
    env.orch._ws_welcome = {}
    env.orch.stream_manager = None
    env.orch.rote = SimpleNamespace(cleanup=lambda *_: None)
    await getattr(Orchestrator, entrypoint)(env.orch, socket)
    assert env.state.clients[0].closed and env.state.channels[0].closed
    assert env.manager._sessions[ident].close_reason == "disconnected"
    assert socket not in env.orch.ui_sessions


def test_one_background_sweeper_is_reused_and_settles_on_shutdown(isolated, monkeypatch):
    monkeypatch.setattr(sessions.TerminalSessionManager, "_ensure_sweeper", isolated.original_ensure_sweeper)
    isolated.create()
    worker = isolated.manager._sweeper
    isolated.create()
    assert isolated.manager._sweeper is worker and worker.is_alive()
    isolated.manager.shutdown()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert all(client.closed for client in isolated.state.clients)
