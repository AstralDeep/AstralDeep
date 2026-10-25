"""Exercises Mac application validation and current-session computer authority.
Host transport and session tests use real registries with synthetic owner sockets.
"""

import asyncio
from types import SimpleNamespace

import pytest

from agents.computer_use import mcp_tools
from orchestrator import computer_use_policy as policy
from orchestrator import remote_confirmation as rc
from orchestrator.computer_hosts import ComputerHostError
from tests.test_computer_use_076 import _descriptor, _FakeOrch, _online_host, _session, _WS, OWNER


@pytest.mark.parametrize("app", [
    "Terminal.app", "com.apple.Terminal", "/System/Applications/Utilities/Terminal.app",
    "iTerm.app", "com.googlecode.iterm2", "/Applications/iTerm.app",
    "WezTerm.app", "com.github.wez.wezterm",
])
def test_mac_shell_aliases_require_consequential_review(app):
    assert policy.is_destructive("open_app", {"app": app})


@pytest.mark.parametrize("app,want", [
    ("Terminal", "com.apple.Terminal"),
    ("Terminal.app", "com.apple.Terminal"),
    ("com.apple.Terminal", "com.apple.Terminal"),
    ("/System/Applications/Utilities/Terminal.app", "/System/Applications/Utilities/Terminal.app"),
    ("/Applications/Google Chrome.app", "/Applications/Google Chrome.app"),
    ("com.google.Chrome", "com.google.Chrome"),
    ("WezTerm.app", "com.github.wez.wezterm"),
    ("Warp", "dev.warp.Warp-Stable"), ("Ghostty.app", "com.mitchellh.ghostty"),
])
def test_mac_open_app_passes_only_validated_bundle_target(monkeypatch, app, want):
    effects = []
    host = SimpleNamespace(name="Mac", platform="macos")
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: (OWNER, "chat", host, SimpleNamespace(session_id="s")))
    monkeypatch.setattr(mcp_tools, "_run", lambda _host, _session, verb, args, _loop:
                        (effects.append((verb, args)) or {"launched": True}, None))
    result = mcp_tools.open_app(app=app)
    assert result["_data"]["launched"] is True
    assert effects == [("open_app", {"app": want, "args": []})]


@pytest.mark.parametrize("app,args", [
    ("Terminal", ["-c", "id"]), ("com.apple.Terminal", ["--command=id"]),
    ("/Applications/../private/Evil.app", []), ("file:///Applications/Terminal.app", []),
    ("/Applications/Terminal.app/Contents/MacOS/Terminal", []),
    ("/Applications/$(id).app", []), ("/Applications/Evil.app\n", []),
])
def test_mac_open_app_denies_unsafe_target_before_effect(monkeypatch, app, args):
    effects = []
    host = SimpleNamespace(name="Mac", platform="macos")
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: (OWNER, "chat", host, SimpleNamespace(session_id="s")))
    monkeypatch.setattr(mcp_tools, "_run", lambda *_args: (effects.append(True) or {}, None))
    result = mcp_tools.open_app(app=app, args=args)
    assert result["_data"]["code"] == "out_of_range"
    assert not effects


async def test_pause_then_resume_does_not_restore_terminal_authority():
    orch = _FakeOrch()
    _host_ws, host = await _online_host(orch)
    session = await _session(orch, host, orch.add(_WS(chat="chat-1")))
    session.grant_terminal(180)
    await orch.computer_sessions.pause(session, "local_input")
    await orch.computer_sessions.resume(session)
    assert not session.terminal_ok
    await orch.computer_sessions.end(session, "user_stop")


async def test_same_controller_chat_change_revokes_existing_terminal_authority():
    orch = _FakeOrch()
    _ws, host = await _online_host(orch)
    controller = orch.add(_WS(chat="chat-1"))
    session = await _session(orch, host, controller)
    session.grant_terminal(180)
    generation = session.authority_generation
    await orch.computer_sessions.start(OWNER, host, controller, "chat-2", wait_for_ack=False)
    assert not session.terminal_ok and session.authority_generation > generation
    await orch.computer_sessions.end(session, "user_stop")


async def test_manual_pause_after_local_takeover_cannot_retain_resume_eligibility():
    orch = _FakeOrch()
    ws = orch.add(_WS(chat="chat-1"))
    host, _ = orch.computer_hosts.register(OWNER, ws, _descriptor(platform="macos"))
    session = await _session(orch, host, ws)
    authority = rc.capture_computer_approval(orch, ws, OWNER, "chat-1", {})
    await orch.computer_sessions.pause(session, "local_input")
    await orch.computer_sessions.pause(session, "manual_pause")
    assert not rc.computer_approval_current(orch, ws, authority, allow_local_pause=True)
    assert session.state == "paused" and session.pause_reason == "manual_pause"
    await orch.computer_sessions.end(session, "user_stop")


async def test_host_reregistration_ends_every_displaced_session():
    from orchestrator.orchestrator import Orchestrator

    orch = _FakeOrch()
    first_socket, first = await _online_host(orch)
    second_socket = orch.add(_WS())
    second, _ = orch.computer_hosts.register(OWNER, second_socket, _descriptor(host_id="second-host"))
    first_session = await _session(orch, first, orch.add(_WS(chat="chat-1")))
    second_session = await _session(orch, second, orch.add(_WS(chat="chat-2")))
    first_session.grant_terminal(180)
    second_session.grant_terminal(180)
    await Orchestrator._register_computer_host(orch, second_socket, OWNER, _descriptor(host_id=first.host_id))
    assert first_session.state == second_session.state == "ended"
    assert not first_session.terminal_ok and not second_session.terminal_ok
    assert orch.computer_hosts.host_for_socket(first_socket) is None


@pytest.mark.parametrize("current", [True, False])
async def test_window_review_uses_bounded_current_session_labels_and_table(monkeypatch, current):
    orch = _FakeOrch()
    ws = orch.add(_WS(chat="chat-1"))
    host, _ = orch.computer_hosts.register(OWNER, ws, _descriptor(platform="macos"))
    session = await _session(orch, host, ws)
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: (OWNER, "chat-1", host, session))

    def windows(*_args):
        if not current:
            orch.computer_hosts.register(OWNER, object(), _descriptor(platform="macos", host_id=host.host_id))
        return {"windows": [{"hwnd": 42, "title": "Synthetic notes" + "x" * 300,
            "process": "com.apple.Notes", "bundle_id": "com.apple.Notes"}]}, None

    monkeypatch.setattr(mcp_tools, "_run", windows)
    result = mcp_tools.list_windows()
    table = result["_ui_components"][0]
    assert table["type"] == "table" and table["headers"] == ["Window", "App", "ID"]
    assert table["rows"][0][1:] == ["com.apple.Notes", "42"]
    if current:
        authority = rc.capture_computer_approval(orch, ws, OWNER, "chat-1", {})
        label = rc.computer_resume_target(ws, authority, "type_text", {"hwnd": 42})
        assert "com.apple.Notes" in label and "Synthetic notes" in label and len(label) <= 450
    else:
        assert not session.window_labels
    await orch.computer_sessions.end(session, "user_stop")


async def test_superseded_host_cannot_receive_or_complete_old_requests():
    orch = _FakeOrch()
    old_ws, old = await _online_host(orch)
    pending = asyncio.create_task(orch.computer_hosts.request(old, "session", "click", {"x": 1, "y": 1}, 10))
    await asyncio.sleep(0)
    replacement_ws = orch.add(_WS())
    replacement, _ = orch.computer_hosts.register(OWNER, replacement_ws, _descriptor(host_id=old.host_id))
    with pytest.raises(ComputerHostError) as uncertain:
        await asyncio.wait_for(pending, 1)
    assert uncertain.value.code == "outcome_uncertain"
    with pytest.raises(ComputerHostError) as stale:
        await orch.computer_hosts.request(old, "session", "click", {"x": 2, "y": 2}, 10)
    assert stale.value.code == "host_offline"
    assert not replacement_ws.sent
    request = asyncio.create_task(orch.computer_hosts.request(replacement, "session", "list_windows", {}, 10))
    await asyncio.sleep(0)
    request_id = replacement_ws.frames("computer_request")[-1]["request_id"]
    assert not orch.computer_hosts.handle_response(old_ws, OWNER, {"request_id": request_id, "ok": True})
    assert orch.computer_hosts.handle_response(replacement_ws, OWNER, {"request_id": request_id, "ok": True, "result": {}})
    assert await request == {}


@pytest.mark.parametrize("tool,args,want", [
    ("write_file", {"path": "/test/file", "content": "safe"}, False),
    ("confirm_action", {"summary": "Buy an item"}, False),
    ("confirm_action", {"summary": "Run the reviewed command", "terminal_app": "notepad"}, False),
    ("confirm_action", {"summary": "Run the reviewed command", "terminal_app": "com.apple.Terminal"}, True),
    ("open_app", {"app": "com.apple.Terminal"}, True),
    ("open_app", {"app": "Google Chrome"}, False),
])
async def test_terminal_grant_requires_exact_current_terminal_action(tool, args, want):
    orch = _FakeOrch()
    _host_ws, host = await _online_host(orch)
    controller = orch.add(_WS(chat="chat-1"))
    session = await _session(orch, host, controller)
    authority = rc.capture_computer_approval(orch, controller, OWNER, "chat-1", {})
    row = SimpleNamespace(proposal_id="p", owner_id=OWNER, machine_id=host.host_id,
                          conversation_id="chat-1", tool_name=tool, arguments=args)
    rc._computer_use_on_approved(orch, row, controller, authority)
    assert session.terminal_ok is want
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("change", ["pause", "chat", "owner", "controller", "host", "session"])
async def test_stale_terminal_approval_cannot_grant_new_authority(change):
    orch = _FakeOrch()
    _host_ws, host = await _online_host(orch)
    controller = orch.add(_WS(chat="chat-1"))
    session = await _session(orch, host, controller)
    authority = rc.capture_computer_approval(orch, controller, OWNER, "chat-1", {})
    row = SimpleNamespace(proposal_id="p", owner_id=OWNER, machine_id=host.host_id,
                          conversation_id="chat-1", tool_name="open_app", arguments={"app": "powershell"})
    if change == "pause":
        await orch.computer_sessions.pause(session, "local_input")
        await orch.computer_sessions.resume(session)
    elif change == "chat":
        orch._ws_active_chat[id(controller)] = "other-chat"
    elif change == "owner":
        orch.ui_sessions[controller]["sub"] = "other-owner"
    elif change == "controller":
        session.controller_ws_id = id(object())
    elif change == "host":
        orch.computer_hosts.register(OWNER, orch.add(_WS()), _descriptor(host_id=host.host_id))
    else:
        await orch.computer_sessions.end(session, "user_stop")
        session = await _session(orch, host, controller)
    rc._computer_use_on_approved(orch, row, controller, authority)
    assert not session.terminal_ok
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("verb,want", [("click", "outcome_uncertain"), ("list_windows", "host_offline")])
async def test_host_disconnect_distinguishes_uncertain_delivered_effect(verb, want):
    orch = _FakeOrch()
    ws, host = await _online_host(orch)
    pending = asyncio.create_task(orch.computer_hosts.request(host, "session", verb, {}, 10))
    await asyncio.sleep(0)
    assert ws.frames("computer_request")
    orch.computer_hosts.on_socket_closed(ws)
    with pytest.raises(ComputerHostError) as refused:
        await pending
    assert refused.value.code == want


@pytest.mark.parametrize("verb,want", [("click", "outcome_uncertain"), ("list_windows", "host_offline")])
@pytest.mark.parametrize("failure", [False, RuntimeError])
async def test_failed_delivery_attempt_does_not_claim_a_mutation_never_happened(monkeypatch, verb, want, failure):
    orch = _FakeOrch()
    _ws, host = await _online_host(orch)

    async def send(*_args):
        if failure is RuntimeError:
            raise RuntimeError("synthetic send interruption")
        return False

    monkeypatch.setattr(orch, "_safe_send", send)
    with pytest.raises(ComputerHostError) as refused:
        await orch.computer_hosts.request(host, "session", verb, {}, 1)
    assert refused.value.code == want and not orch.computer_hosts._pending


async def test_queued_input_cannot_survive_pause_and_resume(monkeypatch):
    orch = _FakeOrch()
    _ws, host = await _online_host(orch)
    session = await _session(orch, host, orch.add(_WS(chat="chat-1")))
    effects = []
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_bridge = mcp_tools._bridge

    def bridge(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        return original_bridge(*args, **kwargs)

    async def effect(*_args):
        effects.append(True)
        return {}

    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_bridge", bridge)
    monkeypatch.setattr(orch.computer_hosts, "request", effect)
    await session.lock.acquire()
    pending = asyncio.create_task(asyncio.to_thread(mcp_tools._run, host, session, "click", {"x": 1, "y": 2}, loop))
    await entered.wait()
    await orch.computer_sessions.pause(session, "local_input")
    await orch.computer_sessions.resume(session)
    session.lock.release()
    _result, refusal = await pending
    assert refusal["_data"]["code"] == "no_session" and not effects
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("elapsed", [2, 11])
async def test_queue_uses_original_deadline_without_a_late_physical_effect(monkeypatch, elapsed):
    orch = _FakeOrch()
    _ws, host = await _online_host(orch)
    session = await _session(orch, host, orch.add(_WS(chat="chat-1")))
    physical, clock = [], [100.0]
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_bridge = mcp_tools._bridge

    def bridge(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        return original_bridge(*args, **kwargs)

    async def request(_host, _session, _verb, _args, timeout):
        physical.append(timeout)
        return {}

    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_bridge", bridge)
    monkeypatch.setattr(mcp_tools, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(orch.computer_hosts, "request", request)
    await session.lock.acquire()
    pending = asyncio.create_task(asyncio.to_thread(mcp_tools._run, host, session, "click", {"x": 1, "y": 2}, loop))
    await entered.wait()
    clock[0] += elapsed
    session.lock.release()
    _result, refusal = await pending
    if elapsed < 10:
        assert refusal is None and physical == [10 - elapsed]
    else:
        assert not physical and refusal["_data"]["code"] == "timeout"
    await orch.computer_sessions.end(session, "user_stop")


def test_bridge_timeout_cancels_its_submitted_coroutine(monkeypatch):
    from unittest.mock import Mock

    submitted = Mock()
    submitted.result.side_effect = TimeoutError
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda *_args: submitted)

    async def waiting():
        return None

    coroutine = waiting()
    try:
        with pytest.raises(TimeoutError):
            mcp_tools._bridge(coroutine, 3, loop=object())
    finally:
        coroutine.close()
    submitted.cancel.assert_called_once_with()
    submitted.result.assert_called_once_with(timeout=3)


@pytest.mark.parametrize("verb,want", [("click", "outcome_uncertain"), ("list_windows", "timeout")])
def test_outer_bridge_timeout_reports_effect_uncertainty(monkeypatch, verb, want):
    monkeypatch.setattr(mcp_tools, "_ORCH", _FakeOrch())

    def timeout(coro, *_args):
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(mcp_tools, "_bridge", timeout)
    result, error = mcp_tools._run(object(), SimpleNamespace(authority_generation=0), verb, {})
    assert result is None and error["_data"]["code"] == want


@pytest.mark.parametrize("verb,args", [("type_text", {"text": "safe"}), ("press_keys", {"keys": "enter"}),
    ("click", {"x": 1, "y": 2}), ("drag", {"x1": 1, "y1": 2, "x2": 3, "y2": 4})])
def test_mac_input_forwards_exact_reviewed_window(monkeypatch, verb, args):
    effects = []
    host = SimpleNamespace(name="Mac", platform="macos")
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: (OWNER, "chat", host,
        SimpleNamespace(session_id="s", terminal_ok=True)))
    monkeypatch.setattr(mcp_tools, "_run", lambda _host, _session, name, payload, _loop:
        (effects.append((name, payload)) or {}, None))
    getattr(mcp_tools, verb)(hwnd=42, **args)
    assert effects and effects[0][1]["hwnd"] == 42


@pytest.mark.parametrize("hwnd", [True, 0, -1, 1.5, "42", 2**53 + 1])
def test_mac_input_invalid_target_never_reaches_executor(monkeypatch, hwnd):
    effects = []
    host = SimpleNamespace(name="Mac", platform="macos")
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: (OWNER, "chat", host,
        SimpleNamespace(session_id="s", terminal_ok=True)))
    monkeypatch.setattr(mcp_tools, "_run", lambda *_args: (effects.append(True) or {}, None))
    result = mcp_tools.type_text(text="safe", hwnd=hwnd)
    assert result["_data"]["code"] == "out_of_range" and not effects


@pytest.mark.parametrize("args", [{"terminal_app": "com.apple.Terminal"},
    {"terminal_app": "Google Chrome", "hwnd": 42}, {"terminal_app": "com.apple.Terminal", "hwnd": 42}])
def test_terminal_specific_input_cannot_claim_consent_from_arguments(monkeypatch, args):
    effects = []
    host = SimpleNamespace(name="Mac", platform="macos")
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: (OWNER, "chat", host,
        SimpleNamespace(session_id="s", terminal_ok=True)))
    monkeypatch.setattr(mcp_tools, "_run", lambda *_args: (effects.append(True) or {}, None))
    result = mcp_tools.type_text(text="safe", **args)
    assert result["_data"].get("code") in {"out_of_range", "confirmation_required"} and not effects


@pytest.mark.parametrize("app,want", [("Terminal", "approved"), ("Google Chrome", "out_of_range")])
def test_explicit_confirmation_validates_terminal_identity(monkeypatch, app, want):
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: (OWNER, "chat", SimpleNamespace(platform="macos"),
        SimpleNamespace(session_id="s")))
    result = mcp_tools.confirm_action(summary="Run the reviewed command", terminal_app=app)
    assert result["_data"].get(want) is True if want == "approved" else result["_data"]["code"] == want


@pytest.mark.parametrize("tool,args,want", [
    ("confirm_action", {"terminal_app": "Terminal", "summary": "Run a command"}, "com.apple.Terminal"),
    ("write_file", {"path": "/selected/output.txt"}, "/selected/output.txt"),
    ("delete_path", {"path": "relative.txt"}, None),
    ("open_app", {"app": "/Applications/../Terminal.app"}, None),
    ("set_clipboard", {"text": "safe"}, None),
])
async def test_combined_resume_requires_a_valid_declared_target(tool, args, want):
    orch = _FakeOrch()
    ws = orch.add(_WS(chat="chat-1"))
    host, _ = orch.computer_hosts.register(OWNER, ws, _descriptor(platform="macos"))
    session = await _session(orch, host, ws)
    authority = rc.capture_computer_approval(orch, ws, OWNER, "chat-1", {})
    assert rc.computer_resume_target(ws, authority, tool, args) == want
    if want is None:
        await orch.computer_sessions.pause(session, "local_input")
        assert await rc.resume_computer_approval(orch, ws, authority, tool, args) is None
        assert session.state == "paused"
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("change", ["owner", "chat", "machine", "summary", "args"])
async def test_terminal_grant_rejects_changed_approval_record(change):
    orch = _FakeOrch()
    ws = orch.add(_WS(chat="chat-1"))
    host, _ = orch.computer_hosts.register(OWNER, ws, _descriptor(platform="macos"))
    session = await _session(orch, host, ws)
    authority = rc.capture_computer_approval(orch, ws, OWNER, "chat-1", {})
    row = SimpleNamespace(owner_id=OWNER, conversation_id="chat-1", machine_id=host.host_id,
        tool_name="open_app", arguments={"app": "Terminal"})
    if change == "owner":
        row.owner_id = "other"
    elif change == "chat":
        row.conversation_id = "other"
    elif change == "machine":
        row.machine_id = "other"
    elif change == "summary":
        row.tool_name = "confirm_action"
        row.arguments = {"terminal_app": "Terminal", "summary": ""}
    else:
        row.arguments["args"] = ["-c", "id"]
    assert not rc._computer_use_on_approved(orch, row, ws, authority)
    assert not session.terminal_ok
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("app", ["cmd", "python", "psql", "powershell", "bash", "/Applications/Terminal.app"])
async def test_mac_terminal_confirmation_has_closed_native_identities(monkeypatch, app):
    orch = _FakeOrch()
    ws = orch.add(_WS(chat="chat-1"))
    host, _ = orch.computer_hosts.register(OWNER, ws, _descriptor(platform="macos"))
    session = await _session(orch, host, ws)
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: (OWNER, "chat-1", host, session))
    result = mcp_tools.confirm_action(summary="A reviewed command", terminal_app=app)
    assert result["_data"]["code"] == "out_of_range"
    authority = rc.capture_computer_approval(orch, ws, OWNER, "chat-1", {})
    row = SimpleNamespace(owner_id=OWNER, conversation_id="chat-1", machine_id=host.host_id,
        tool_name="confirm_action", arguments={"terminal_app": app, "summary": "A reviewed command"})
    assert not rc._computer_use_on_approved(orch, row, ws, authority) and not session.terminal_ok
    await orch.computer_sessions.end(session, "user_stop")


async def test_host_socket_owner_replacement_refuses_before_frame_delivery():
    orch = _FakeOrch()
    ws, host = await _online_host(orch)
    orch.ui_sessions[ws]["sub"] = "other-owner"
    with pytest.raises(ComputerHostError) as refused:
        await orch.computer_hosts.request(host, "session", "click", {"x": 1, "y": 1}, 1)
    assert refused.value.code == "host_offline" and not ws.sent


async def test_host_registration_under_new_owner_settles_old_effect_as_uncertain():
    orch = _FakeOrch()
    ws, host = await _online_host(orch)
    pending = asyncio.create_task(orch.computer_hosts.request(host, "session", "click", {"x": 1, "y": 1}, 1))
    await asyncio.sleep(0)
    response_future = next(iter(orch.computer_hosts._pending.values()))[0]
    orch.ui_sessions[ws]["sub"] = "other-owner"
    orch.computer_hosts.register("other-owner", ws, _descriptor(host_id=host.host_id))
    assert response_future.done()
    with pytest.raises(ComputerHostError) as refused:
        await pending
    assert refused.value.code == "outcome_uncertain"


async def test_owner_rebinding_ends_the_previous_owners_session_and_terminal_grant():
    from orchestrator.orchestrator import Orchestrator

    orch = _FakeOrch()
    ws, host = await _online_host(orch)
    session = await _session(orch, host, orch.add(_WS(chat="chat-1")))
    session.grant_terminal(180)
    orch.ui_sessions[ws]["sub"] = "other-owner"
    await Orchestrator._register_computer_host(orch, ws, "other-owner", _descriptor(host_id=host.host_id))
    assert session.state == "ended" and not session.terminal_ok
