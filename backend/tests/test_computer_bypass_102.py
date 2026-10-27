"""Exercises the per-host bypass permissions mode end to end: only the host's own signed-in client
socket can switch it, the review gate auto-approves computer-use cards with an audited bypass source
while unavailable privilege, secure-input and Astral authority fail without cards, and every clearing trigger.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator import hitl_confirmation as hc
from orchestrator import remote_confirmation as rc
from orchestrator.orchestrator import Orchestrator
from orchestrator.projection_surfaces import my_computers
from shared.protocol import ComputerHostDescriptor
from tests.test_computer_grant_flow_102 import (
    CHAT, OWNER, ROOT, TEXTEDIT, _buttons, _call, _fresh_pending_cards as _fresh_pending_cards, _mac,
)
from tests.test_computer_use_076 import OTHER, _descriptor, _FakeOrch, _WS
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401

SET = "chrome_computer_bypass_set"
VAULT = {"hwnd": 50, "title": "Vault", "process": "com.1password.1password", "bundle_id": "com.1password.1password",
         "rect": [0, 0, 800, 600], "focused": False}
ADMIN = {"hwnd": 51, "title": "", "process": "com.apple.SecurityAgent", "bundle_id": "com.apple.SecurityAgent",
         "rect": [0, 0, 400, 300], "focused": True}
ASTRAL = {"hwnd": 52, "title": "Astral", "process": "com.personalailabs.astraldeep",
          "bundle_id": "com.personalailabs.astraldeep", "rect": [0, 0, 800, 600], "focused": False}
TERMINAL = {"hwnd": 53, "title": "zsh", "process": "com.apple.Terminal", "bundle_id": "com.apple.Terminal",
            "rect": [0, 0, 800, 600], "focused": False}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(my_computers, "_enabled", lambda: True)


@pytest.fixture
def audits(monkeypatch):
    from audit import recorder as module

    events = []

    class _Recorder:
        async def record(self, event):
            events.append(event)
            return object()

        def record_blocking(self, event):
            events.append(event)

    monkeypatch.setattr(module, "get_recorder", lambda: _Recorder())
    return events


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


def _host_world(owner=OWNER):
    orch = _FakeOrch()
    host_ws = orch.add(_WS(user=owner))
    host, _ = orch.computer_hosts.register(owner, host_ws, ComputerHostDescriptor.from_dict(
        _descriptor(platform="macos", name="Mac")))
    controller = orch.add(_WS(user=owner, chat="chat-1"))
    return orch, host, host_ws, controller


async def _set(orch, websocket, user, payload):
    _key, _params, notice = await my_computers.HANDLERS[SET](orch, websocket, user, ["user"], payload)
    return notice


async def test_only_the_hosts_own_socket_can_switch_bypass(enabled, audits):
    orch, host, host_ws, controller = _host_world()
    foreign_ws = orch.add(_WS(user=OTHER))
    payload = {"host_id": host.host_id, "enabled": True}
    assert "not available" in await _set(orch, controller, OWNER, payload)
    assert "not available" in await _set(orch, foreign_ws, OTHER, payload)
    assert "not available" in await _set(orch, host_ws, OTHER, payload)
    orch.ui_sessions[host_ws]["_invocation_channel"] = "mcp"
    assert "not available" in await _set(orch, host_ws, OWNER, payload)
    orch.ui_sessions[host_ws] = {"sub": OWNER, "machine_class": "scheduled"}
    assert "not available" in await _set(orch, host_ws, OWNER, payload)
    orch.ui_sessions[host_ws] = {"sub": OWNER}
    assert not orch.computer_sessions.bypass_on(OWNER, host.host_id)
    assert not [event for event in audits if event.action_type.startswith("computer_bypass")]
    assert "Bypass on" in await _set(orch, host_ws, OWNER, {**payload, "surface": "my_computers"})
    assert orch.computer_sessions.bypass_on(OWNER, host.host_id)
    assert "Bypass off" in await _set(orch, host_ws, OWNER, {**payload, "enabled": False})
    assert not orch.computer_sessions.bypass_on(OWNER, host.host_id)
    await _settle()
    toggles = [(event.action_type, event.inputs_meta["reason"]) for event in audits
               if event.action_type.startswith("computer_bypass")]
    assert toggles == [("computer_bypass.enabled", "owner"), ("computer_bypass.disabled", "owner")]
    assert all(event.actor_user_id == OWNER for event in audits)


@pytest.mark.parametrize("payload", [{}, {"enabled": True}, {"host_id": "h", "enabled": "yes"},
                                     {"host_id": "h", "enabled": True, "extra": 1}, "nope"])
async def test_malformed_or_mismatched_switches_change_nothing(enabled, payload):
    orch, host, host_ws, _controller = _host_world()
    assert "not available" in await _set(orch, host_ws, OWNER, payload)
    assert "not available" in await _set(orch, host_ws, OWNER, {"host_id": "other", "enabled": True})
    assert not orch.computer_sessions.bypass_on(OWNER, host.host_id)


async def test_the_switch_is_refused_while_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(my_computers, "_enabled", lambda: False)
    orch, host, host_ws, _controller = _host_world()
    notice = await _set(orch, host_ws, OWNER, {"host_id": host.host_id, "enabled": True})
    assert my_computers._DISABLED_MSG in notice and not orch.computer_sessions.bypass_on(OWNER, host.host_id)


@pytest.mark.parametrize("trigger", ["off", "disconnect", "replaced", "withdraw", "restart"])
async def test_every_clearing_trigger_turns_bypass_off(enabled, audits, monkeypatch, trigger):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "computer_use", True)
    orch, host, host_ws, _controller = _host_world()
    for name in ("_register_computer_host", "_teardown_computer_host", "_handle_computer_event"):
        setattr(orch, name, getattr(Orchestrator, name).__get__(orch))
    orch._tunnel_ingress_over_cap = lambda _owner: False
    await _set(orch, host_ws, OWNER, {"host_id": host.host_id, "enabled": True})
    if trigger == "off":
        await _set(orch, host_ws, OWNER, {"host_id": host.host_id, "enabled": False})
    elif trigger == "disconnect":
        await orch._teardown_computer_host(host_ws)
    elif trigger == "replaced":
        await orch._register_computer_host(orch.add(_WS(user=OWNER)), OWNER, ComputerHostDescriptor.from_dict(
            _descriptor(host_id=host.host_id, platform="macos", name="Mac")))
    elif trigger == "withdraw":
        await orch._handle_computer_event(host_ws, SimpleNamespace(payload={"event": "withdraw"}))
    else:
        orch.computer_sessions = type(orch.computer_sessions)(orch, orch.computer_hosts)
    assert not orch.computer_sessions.bypass_on(OWNER, host.host_id)
    await _settle()
    if trigger != "restart":
        reasons = [event.inputs_meta["reason"] for event in audits if event.action_type == "computer_bypass.disabled"]
        assert reasons == [{"off": "owner", "disconnect": "host_offline", "replaced": "host_replaced",
                            "withdraw": "access_disabled"}[trigger]]


async def test_session_frames_and_the_surface_carry_the_bypass_state(enabled, monkeypatch):
    from orchestrator.chrome_events import current_surface_socket

    orch, host, host_ws, controller = _host_world()
    orch.ui_sessions[host_ws]["_client_capabilities"] = ["computer_host_capable"]
    task = asyncio.create_task(orch.computer_sessions.start(OWNER, host, controller, "chat-1"))
    await _settle()
    session = orch.computer_sessions.live_for_host(OWNER, host.host_id)
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "heartbeat", session.session_id, None)
    await task

    def frames():
        return [json.loads(raw) for raw in controller.sent if json.loads(raw).get("type") == "computer_session"]

    assert frames()[-1]["bypass"] is False
    await _set(orch, host_ws, OWNER, {"host_id": host.host_id, "enabled": True})
    assert frames()[-1]["bypass"] is True
    token = current_surface_socket.set(host_ws)
    try:
        cards = await my_computers.components(orch, OWNER, ["user"], {})
        html = await my_computers.render(orch, OWNER, ["user"], {})
    finally:
        current_surface_socket.reset(token)
    local = json.dumps(cards[0])
    assert '"enabled": false' in local and SET in local and host.host_id in local
    assert "bypass on" in json.dumps(cards[1]) and "bypass on" in html and SET in html
    await orch.computer_sessions.end(session, "user_stop")
    assert frames()[-1]["bypass"] is True


async def _bypassed(task_runtime, windows=(TEXTEDIT, VAULT, ADMIN, ASTRAL, TERMINAL), remote=True):
    from orchestrator.computer_use_policy import SCOPES

    session, host = await _mac(task_runtime, windows=windows, remote=remote)
    orch = task_runtime[0]
    orch.tool_permissions.get_tool_scope_map.return_value = dict(SCOPES)
    await orch.computer_sessions.set_bypass(host, True)
    return session, host


def _audited(orch):
    return [call.args[0] for call in orch.audit_recorder.record.await_args_list]


@pytest.mark.parametrize("tool,args", [
    ("type_text", {"text": "hello", "hwnd": 42}), ("click", {"x": 1, "y": 1, "hwnd": 42}),
    ("open_app", {"app": "TextEdit"}), ("set_clipboard", {"text": "x"}),
    ("run_command", {"argv": ["rm", "-rf", "build"], "cwd": ROOT}), ("write_file", {"path": ROOT + "/a", "content": "x"}),
    ("delete_path", {"path": ROOT + "/a"}), ("browser_navigate", {"url": "https://example.org/"}),
    ("interrupt_command", {"job_id": "unknown", "signal": "INT"}),
])
async def test_bypass_auto_approves_a_card_and_audits_its_source(task_runtime, tool, args):
    orch, ws, effects, *_ = task_runtime
    await _bypassed(task_runtime)
    response = await _call(task_runtime, tool, **args)
    assert response.error is None and response.result["_data"]["ok"]
    assert response.result["_data"]["approval_source"] == "bypass"
    assert [name for name, _args in effects] == [tool]
    assert not getattr(orch, "_hitl_pending_calls", {}) and not rc._PENDING_CARDS
    events = _audited(orch)
    bypass = [event for event in events if event.action_type == "hitl.bypass"]
    assert len(bypass) == 1 and bypass[0].inputs_meta["approval_source"] == "bypass"
    assert all(event.inputs_meta.get("approval_source") == "bypass"
               for event in events if event.action_type.startswith("hitl.effect"))


async def test_bypass_also_covers_starting_a_session(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, host = await _bypassed(task_runtime)
    await orch.computer_sessions.end(session, "user_stop", cancel_turn=False)
    await orch.computer_sessions.set_bypass(host, True)
    response = await _call(task_runtime, "start_session")
    assert response.result["_data"]["approval_source"] == "bypass" and [n for n, _a in effects] == ["start_session"]


@pytest.mark.parametrize("tool,args", [
    ("run_command", {"argv": ["sudo", "ls"], "cwd": ROOT}),
    ("run_command", {"command": "sh -c 'doas reboot'", "cwd": ROOT}),
    ("run_command", {"command": "launchctl bootstrap system /Library/LaunchDaemons/x.plist", "cwd": ROOT}),
    ("run_command", {"command": "osascript -e 'do shell script \"id\" with administrator privileges'", "cwd": ROOT}),
    ("type_text", {"text": "hunter2", "hwnd": 50}), ("type_text", {"text": "hunter2", "hwnd": 51}),
    ("press_keys", {"keys": "enter", "hwnd": 51}), ("type_text", {"text": "x"}),
    ("click", {"x": 1, "y": 1, "hwnd": 52}), ("open_app", {"app": "com.personalailabs.astraldeep"}),
])
async def test_bypass_floor_refuses_directly_without_a_card(task_runtime, tool, args):
    orch, ws, effects, *_ = task_runtime
    await _bypassed(task_runtime)
    response = await _call(task_runtime, tool, **args)
    assert response.error and not effects
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})
    assert not [event for event in _audited(orch) if event.action_type == "hitl.bypass"]


async def test_bypass_keeps_delegation_policy_taint_privacy_and_folder_checks(task_runtime, monkeypatch):
    from orchestrator import policy, taint

    orch, ws, effects, *_ = task_runtime
    await _bypassed(task_runtime)
    orch.tool_permissions.is_tool_allowed.return_value = False
    denied = await _call(task_runtime, "type_text", text="a", hwnd=42)
    assert "restricted" in denied.error["message"]
    orch.tool_permissions.is_tool_allowed.return_value = True
    outside = await _call(task_runtime, "run_command", argv=["ls"], cwd="/etc")
    assert outside.error["message"].startswith("cwd_outside_workspace:")
    monkeypatch.setattr(policy, "policy_enabled", lambda: True)
    monkeypatch.setattr(policy, "load_rules", lambda **_kwargs: [])
    monkeypatch.setattr(policy, "evaluate_policy", lambda *_a, **_k: policy.PolicyDecision(
        effect=policy.DENY, reason="blocked by rule"))
    blocked = await _call(task_runtime, "type_text", text="a", hwnd=42)
    assert blocked.error["message"] == "blocked by rule"
    monkeypatch.setattr(policy, "policy_enabled", lambda: False)
    monkeypatch.setattr(taint, "taint_enabled", lambda: True)
    orch._taint_tracker = lambda _chat: SimpleNamespace(effective_trust_of_args=lambda *_a, **_k: taint.UNTRUSTED)
    orch._current_request_text = lambda _chat: ""
    tainted = await _call(task_runtime, "run_command", argv=["wc", "a"], cwd=ROOT)
    assert tainted.error and not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})
    monkeypatch.setattr(taint, "taint_enabled", lambda: False)
    private = await _call(task_runtime, "type_text", text="password: hunter2", hwnd=42)
    assert "Review unavailable" in private.error["message"]
    assert not effects


async def test_bypass_for_another_host_or_owner_does_not_apply(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime)
    other, _ = orch.computer_hosts.register(OWNER, object(), _descriptor(platform="macos", name="Other"))
    await orch.computer_sessions.set_bypass(other, True)
    response = await _call(task_runtime, "type_text", text="a", hwnd=42)
    assert response.result["_data"]["status"] == "confirmation_required" and not effects
    assert hc.bypass_covers(orch, ws, OTHER, CHAT, "computer-use-1", "type_text", {"text": "a", "hwnd": 42}, ()) \
        is False
    assert hc.bypass_covers(orch, ws, OWNER, CHAT, "computer-use-1", "type_text",
                            {"text": "a", "hwnd": 42, "computer": "Not My Mac"}, ()) is False
    assert [b["label"] for b in _buttons(response)][0] == "Approve"


@pytest.mark.parametrize("args,escalates", [
    ({"argv": ["sudo", "ls"]}, True), ({"argv": ["env", "FOO=1", "sudo", "ls"]}, True),
    ({"command": "ls $(sudo cat /etc/sudoers)"}, True), ({"command": "bash -c 'su root'"}, True),
    ({"command": "launchctl bootstrap system x.plist"}, True), ({"argv": ["launchctl", "list"]}, False),
    ({"command": "security authorizationdb write system.privilege.admin allow"}, True),
    ({"command": "security find-generic-password -s x"}, False), ({"command": "dscl . -append /Groups/admin x"}, True),
    ({"command": "dscl . -read /Users/sam"}, False), ({"argv": ["sysadminctl", "-addUser", "x", "-admin"]}, True),
    ({"argv": ["chmod", "u+s", "tool"]}, True), ({"argv": ["chmod", "4755", "tool"]}, True),
    ({"argv": ["chmod", "-R", "644", "dir"]}, False), ({"argv": ["chmod", "u-s", "tool"]}, False),
    ({"command": "echo sudo > notes.txt"}, False), ({"command": "rm -rf build"}, False),
    ({"command": "echo 'unterminated"}, True), ({"command": ""}, True), ({"argv": ["ls"], "command": "ls"}, True),
])
def test_privilege_escalation_is_found_through_wrappers_shells_and_substitutions(args, escalates):
    from orchestrator import computer_use_policy as policy

    assert policy.is_privilege_escalation(args) is escalates


@pytest.mark.parametrize("tool,args,bundle,floor", [
    ("run_command", {"argv": ["defaults", "write", "com.personalailabs.astraldeep", "server", "x"]}, None, True),
    ("run_command", {"command": "open /Applications/Astral.app"}, None, True),
    ("run_command", {"argv": ["ls"]}, None, False),
    ("write_file", {"path": "/Users/sam/Library/Containers/com.personalailabs.astraldeep/x"}, None, True),
    ("delete_path", {"path": ROOT + "/a"}, None, False),
    ("open_app", {"app": "AstralDeep.exe"}, None, True), ("open_app", {"app": "/Applications/Astral Deep.app"}, None, True),
    ("open_app", {"app": "TextEdit"}, None, False),
    ("type_text", {"text": "ls"}, "com.apple.Terminal", True), ("click", {}, "com.apple.Terminal", False),
    ("type_text", {"text": "x"}, "com.agilebits.onepassword7", True), ("click", {}, "com.1password.1password", False),
    ("focus_window", {"title": "Notes"}, None, True), ("focus_window", {"hwnd": 1}, "com.apple.Notes", False),
    ("set_clipboard", {"text": "x"}, None, False), ("browser_click", {"ref": "e1"}, None, False),
])
def test_the_bypass_floor_covers_escalation_secure_input_terminals_and_astral(tool, args, bundle, floor):
    from orchestrator import computer_use_policy as policy

    assert policy.bypass_floor(tool, args, bundle) is floor


async def test_unchanged_switches_record_nothing_and_a_replaced_host_is_never_bypassed(audits, monkeypatch):
    orch, host, host_ws, _controller = _host_world()
    await orch.computer_sessions.set_bypass(host, False)
    await orch.computer_sessions.set_bypass(host, True)
    await orch.computer_sessions.set_bypass(host, True)
    assert [event.action_type for event in audits] == ["computer_bypass.enabled"]
    replacement, _ = orch.computer_hosts.register(OWNER, orch.add(_WS()), ComputerHostDescriptor.from_dict(
        _descriptor(host_id=host.host_id, platform="macos", name="Mac")))
    assert replacement is not host and not orch.computer_sessions.bypass_on(OWNER, host.host_id)
    assert await orch.computer_sessions.clear_bypass(OWNER, host.host_id, "host_replaced") is True
    assert await orch.computer_sessions.clear_bypass(OWNER, host.host_id, "host_replaced") is False


async def test_a_failed_bypass_audit_still_switches(monkeypatch):
    from audit import recorder as module

    class _Broken:
        async def record(self, _event):
            raise RuntimeError("audit down")

    monkeypatch.setattr(module, "get_recorder", lambda: _Broken())
    orch, host, _host_ws, _controller = _host_world()
    await orch.computer_sessions.set_bypass(host, True)
    assert orch.computer_sessions.bypass_on(OWNER, host.host_id)


async def test_a_bypass_action_whose_audit_is_unavailable_does_not_run(task_runtime):
    orch, ws, effects, *_ = task_runtime
    await _bypassed(task_runtime)
    orch.audit_recorder.record.side_effect = RuntimeError("audit down")
    response = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    assert "audit is unavailable" in response.error["message"] and not effects
