"""Exercises task grants through the real dispatch gate: the grant decision on authorize_action and
remote_op_decision cards, grant consultation before a card, macOS pointer targeting with the window
cache and screenshot crop, origin grants for the browser pane, and the excluded verbs.
"""

import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import computer_grants as cg
from orchestrator import hitl_confirmation as hc
from orchestrator import remote_confirmation as rc
from tests.helpers.remote_plane_runtime import make_remote_confirmation_plane_source
from tests.test_computer_use_076 import _descriptor, _FakeDB, _FakeOrch
from tests.test_hitl_public_research import _dispatch, runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import _registered_controller, task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401

OWNER, CHAT = "owner-a", "chat-a"
ROOT = "/Users/sam/Projects/x"
TEXTEDIT = {"hwnd": 42, "title": "Untitled", "process": "com.apple.TextEdit", "bundle_id": "com.apple.TextEdit",
            "rect": [0, 0, 800, 600], "focused": True}
NOTES = {"hwnd": 43, "title": "Groceries", "process": "com.apple.Notes", "bundle_id": "com.apple.Notes",
         "rect": [0, 0, 800, 600], "focused": False}
SAFARI = {"hwnd": 44, "title": "Bank", "process": "com.apple.Safari", "bundle_id": "com.apple.Safari",
          "rect": [0, 0, 800, 600], "focused": False}
SCOPES = {"start_session": "tools:write", "interrupt_command": "tools:write", "type_text": "tools:write", "press_keys": "tools:write", "click": "tools:write", "drag": "tools:write",
          "scroll": "tools:write", "open_app": "tools:write", "run_command": "tools:execute",
          "browser_navigate": "tools:write", "browser_click": "tools:write", "browser_scroll": "tools:write",
          "set_clipboard": "tools:write"}


@pytest.fixture(autouse=True)
def _fresh_pending_cards():
    rc._PENDING_CARDS.clear()
    yield
    rc._PENDING_CARDS.clear()


async def _mac(task_runtime, windows=(TEXTEDIT, NOTES, SAFARI), remote=False):
    orch, ws, effects, turns, proposals, outcome = task_runtime
    if not hasattr(orch, "_connection_contexts") or id(ws) not in orch._connection_contexts:
        _registered_controller(orch, ws)
    fake = _FakeOrch()
    host, _ = fake.computer_hosts.register(OWNER, object(), _descriptor(
        platform="macos", name="Mac", workspace_roots=[{"name": "x", "path": ROOT}]))
    fake.ui_sessions[ws] = {"sub": OWNER}
    orch.computer_hosts = fake.computer_hosts
    orch.computer_sessions = fake.computer_sessions
    session = await fake.computer_sessions.start(OWNER, host, ws, CHAT, wait_for_ack=False)
    session.remember_windows([dict(w) for w in windows])
    orch.agents["computer-use-1"] = object()
    orch.tool_permissions.get_tool_scope_map.return_value = SCOPES
    hc.begin_task(orch, ws, OWNER, CHAT)
    if remote:
        source = make_remote_confirmation_plane_source(_FakeDB())
        orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
            runtime=source.plane_runtime, repositories=source.plane_repositories))
        orch._send_or_replace_components = AsyncMock(return_value=[])
    return session, host


async def _call(task_runtime, tool, **args):
    orch, ws, *_ = task_runtime
    return await _dispatch((orch, ws), tool=tool, args=args, agent="computer-use-1")


def _buttons(response):
    return [item for item in response.ui_components[0]["content"] if item["type"] == "button"]


def _payload(response, decision):
    return next(b["payload"] for b in _buttons(response) if b["payload"]["decision"] == decision)


def _texts(response):
    return " ".join(item.get("content", "") for item in response.ui_components[0]["content"] if item["type"] == "text")


def _jpeg(width=1280, height=720):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (20, 20, 20)).save(out, "JPEG")
    return base64.b64encode(out.getvalue()).decode("ascii")


async def test_input_card_offers_a_window_grant_that_covers_later_input(task_runtime):
    orch, ws, effects, _turns, _proposals, _outcome = task_runtime
    session, host = await _mac(task_runtime)
    first = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    labels = [b["label"] for b in _buttons(first)]
    assert labels == ["Approve", 'Allow for this task in window "Untitled"', "Decline"]
    assert "TextEdit" in _texts(first) and "Untitled" in _texts(first)
    await hc.handle_decision(orch, ws, OWNER, _payload(first, "grant_task"))
    await asyncio.sleep(0)
    assert len(effects) == 1
    grant, = orch.computer_sessions.grants.list(session.session_id)
    assert dict(grant.scope) == {"kind": "window", "bundle_id": "com.apple.TextEdit", "hwnd": 42}
    assert (grant.owner_id, grant.host_id, grant.chat_id) == (OWNER, host.host_id, CHAT)
    second = await _call(task_runtime, "press_keys", keys="enter", hwnd=42)
    assert second.error is None and second.result["_data"]["ok"] and len(effects) == 2
    assert not orch._hitl_pending_calls
    actions = [call.args[0].action_type for call in orch.audit_recorder.record.await_args_list]
    assert "hitl.granted" in actions and actions.count("hitl.effect_started") == 2
    other = await _call(task_runtime, "type_text", text="hello", hwnd=43)
    assert other.result["_data"]["status"] == "confirmation_required" and len(effects) == 2


async def test_grant_survives_turns_and_is_revoked_with_the_session_pause(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, _host = await _mac(task_runtime)
    first = await _call(task_runtime, "type_text", text="a", hwnd=42)
    await hc.handle_decision(orch, ws, OWNER, _payload(first, "grant_task"))
    hc.begin_task(orch, ws, OWNER, CHAT)
    assert (await _call(task_runtime, "type_text", text="b", hwnd=42)).result["_data"]["ok"]
    await orch.computer_sessions.pause(session, "local_input")
    await orch.computer_sessions.resume(session, local=True)
    again = await _call(task_runtime, "type_text", text="c", hwnd=42)
    assert again.result["_data"]["status"] == "confirmation_required" and len(effects) == 2


async def test_grant_is_bound_to_its_chat(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime)
    orch.computer_sessions.grants.create(OWNER, host.host_id, session.session_id, "chat-b", "t",
                                         {"kind": "window", "bundle_id": "com.apple.TextEdit", "hwnd": 42},
                                         ("input",), now=__import__("time").time())
    response = await _call(task_runtime, "type_text", text="a", hwnd=42)
    assert response.result["_data"]["status"] == "confirmation_required" and not effects


async def test_open_app_card_offers_an_application_grant_covering_its_windows(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, _host = await _mac(task_runtime)
    first = await _call(task_runtime, "open_app", app="com.apple.TextEdit")
    assert [b["label"] for b in _buttons(first)][1] == "Allow for this task in TextEdit"
    await hc.handle_decision(orch, ws, OWNER, _payload(first, "grant_task"))
    grant, = orch.computer_sessions.grants.list(session.session_id)
    assert dict(grant.scope) == {"kind": "app", "bundle_id": "com.apple.TextEdit"}
    assert (await _call(task_runtime, "type_text", text="x", hwnd=42)).result["_data"]["ok"]
    named = await _call(task_runtime, "open_app", app="TextEdit")
    assert "grant_task" not in json.dumps(named.ui_components) and len(effects) == 2


@pytest.mark.parametrize("tool,args", [
    ("type_text", {"text": "x", "hwnd": 44}), ("type_text", {"text": "x"}), ("press_keys", {"keys": "enter"}),
    ("set_clipboard", {"text": "x"}), ("open_app", {"app": "com.apple.Safari"}), ("open_app", {"app": "TextEdit"}),
    ("open_app", {"app": "/Applications/TextEdit.app"}),
])
async def test_never_grantable_or_untargeted_actions_offer_no_grant(task_runtime, tool, args):
    await _mac(task_runtime)
    response = await _call(task_runtime, tool, **args)
    assert [b["label"] for b in _buttons(response)] == ["Approve", "Decline"]


async def test_terminal_input_and_tainted_sinks_stay_on_exact_review(task_runtime, monkeypatch):
    from orchestrator import taint

    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime, windows=(dict(TEXTEDIT, bundle_id="com.apple.Terminal",
                                                           process="com.apple.Terminal"),))
    terminal = await _call(task_runtime, "type_text", text="ls", hwnd=42, terminal_app="Terminal")
    assert [b["label"] for b in _buttons(terminal)] == ["Approve", "Decline"]
    session.remember_windows([TEXTEDIT])
    store = orch.computer_sessions.grants
    store.create(OWNER, host.host_id, session.session_id, CHAT, "t", {"kind": "command"}, ("command",),
                 {"argv0": "wc", "root": ROOT}, now=__import__("time").time())
    monkeypatch.setattr(taint, "taint_enabled", lambda: True)
    orch._taint_tracker = lambda _chat: SimpleNamespace(effective_trust_of_args=lambda *_a, **_k: taint.UNTRUSTED)
    orch._current_request_text = lambda _chat: ""
    assert hc.grant_covers(orch, ws, OWNER, CHAT, "run_command",
                           {"argv": ["wc", "notes.md"], "cwd": ROOT}) is True
    assert await hc.granted_dispatch(orch, ws, OWNER, CHAT, "computer-use-1", "run_command",
                                     {"argv": ["wc", "notes.md"], "cwd": ROOT}, ("source_bound",)) is None
    assert not effects


@pytest.mark.parametrize("tool,args", [("click", {"x": 10, "y": 10}), ("click", {"x": 10, "y": 10, "hwnd": 99}),
                                       ("drag", {"x1": 1, "y1": 1, "x2": 5, "y2": 5}), ("scroll", {"x": 1, "y": 1})])
async def test_mac_pointer_verbs_without_a_listed_window_are_refused_before_a_card(task_runtime, tool, args):
    orch, *_ = task_runtime
    await _mac(task_runtime)
    response = await _call(task_runtime, tool, **args)
    assert response.error["message"] == "Call list_windows and pass hwnd for the target window."
    assert not getattr(orch, "_hitl_pending_calls", {})


async def test_pointer_card_carries_application_window_and_a_marked_crop(task_runtime):
    session, _host = await _mac(task_runtime)
    session.remember_screenshot(_jpeg(), "image/jpeg", 1280, 720, 0)
    response = await _call(task_runtime, "click", x=1200, y=700, hwnd=42)
    images = [item for item in response.ui_components[0]["content"] if item["type"] == "image"]
    assert len(images) == 1 and images[0]["url"].startswith("data:image/jpeg;base64,")
    assert "Untrusted" in images[0]["alt"]
    from PIL import Image

    crop = Image.open(io.BytesIO(base64.b64decode(images[0]["url"].split(",", 1)[1])))
    assert crop.size == (512, 512)
    assert "TextEdit" in _texts(response) and "Untitled" in _texts(response)
    drag = await _call(task_runtime, "drag", x1=10, y1=10, x2=40, y2=40, hwnd=42)
    assert [i for i in drag.ui_components[0]["content"] if i["type"] == "image"]


async def test_pointer_card_without_pillow_or_screenshot_has_no_image(task_runtime, monkeypatch):
    import sys

    session, _host = await _mac(task_runtime)
    plain = await _call(task_runtime, "click", x=10, y=10, hwnd=42)
    assert not [i for i in plain.ui_components[0]["content"] if i["type"] == "image"]
    session.remember_screenshot(_jpeg(), "image/jpeg", 1280, 720, 0)
    monkeypatch.setitem(sys.modules, "PIL", None)
    blocked = await _call(task_runtime, "click", x=11, y=11, hwnd=42)
    assert not [i for i in blocked.ui_components[0]["content"] if i["type"] == "image"]
    assert "TextEdit" in _texts(blocked)


def test_crop_refuses_off_screen_targets_corrupt_or_oversized_images():
    from orchestrator.computer_sessions import ComputerSession

    session = ComputerSession("s", OWNER, "h", "Mac", 1, "d", "Mac", CHAT)
    assert session.pointer_crop([(1, 1)]) is None
    session.remember_screenshot(_jpeg(64, 64), "image/jpeg", 64, 64, 0)
    assert session.pointer_crop([(500, 500)]) is None and session.pointer_crop([]) is None
    small = session.pointer_crop([(10, 10)])
    from PIL import Image

    assert Image.open(io.BytesIO(base64.b64decode(small))).size == (64, 64)
    session.remember_screenshot(base64.b64encode(b"not an image").decode(), "image/jpeg", 1, 1, 0)
    assert session.pointer_crop([(0, 0)]) is None
    session.remember_screenshot("@@@", "image/jpeg", 1, 1, 0)
    assert session.screenshot_cache is None
    session.remember_screenshot("A" * (5 * 1024 * 1024), "image/jpeg", 1, 1, 0)
    assert session.screenshot_cache is None


async def test_decision_for_a_card_without_an_offer_is_refused(task_runtime):
    orch, ws, effects, *_ = task_runtime
    await _mac(task_runtime)
    response = await _call(task_runtime, "type_text", text="x")
    payload = dict(_payload(response, "approve"), decision="grant_task")
    await hc.handle_decision(orch, ws, OWNER, payload)
    assert not effects and not orch.computer_sessions.grants.list(next(iter(
        orch.computer_sessions._sessions)))


async def test_grant_creation_failure_runs_nothing(task_runtime, monkeypatch):
    orch, ws, effects, *_ = task_runtime
    await _mac(task_runtime)
    response = await _call(task_runtime, "type_text", text="x", hwnd=42)

    def refuse(*_args, **_kwargs):
        raise ValueError("too many task grants for this session")

    monkeypatch.setattr(orch.computer_sessions.grants, "create", refuse)
    await hc.handle_decision(orch, ws, OWNER, _payload(response, "grant_task"))
    assert not effects
    assert "exact review" in str(orch.send_ui_render.await_args_list)


async def test_run_command_card_grants_the_program_in_its_folder(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime, remote=True)
    first = await _call(task_runtime, "run_command", argv=["wc", "-l", "notes.md"], cwd=ROOT + "/notes")
    assert first.result["_data"]["status"] == "confirmation_required"
    labels = [b["label"] for b in _buttons(first)]
    assert labels == ["Approve", "Allow wc in x for this task", "Decline"]
    summary = _texts(first)
    assert "Mac" in summary and '["wc", "-l", "notes.md"]' in summary and ROOT + "/notes" in summary
    await rc.handle_decision(orch, ws, OWNER, _payload(first, "grant_task"))
    assert len(effects) == 1 and not orch._hitl_pending_calls
    grant, = orch.computer_sessions.grants.list(session.session_id)
    assert dict(grant.command) == {"argv0": "wc", "root": ROOT} and dict(grant.scope) == {"kind": "command"}
    again = await _call(task_runtime, "run_command", argv=["wc", "-l", "other.md"], cwd=ROOT)
    assert again.error is None and again.result["_data"]["ok"] and len(effects) == 2
    shell = await _call(task_runtime, "run_command", command="wc -l other.md", cwd=ROOT)
    assert shell.result["_data"]["status"] == "confirmation_required" and len(effects) == 2
    for args in ({"command": "wc -l a | sh", "cwd": ROOT}, {"argv": ["cat", "a"], "cwd": ROOT},
                 {"argv": ["wc", "a"], "cwd": "/tmp"}, {"argv": ["wc", "a"], "cwd": ROOT, "env": {"PATH": "/x"}},
                 {"argv": ["wc", "a"], "cwd": ROOT, "env": {"LANG": "C"}}):
        refused = await _call(task_runtime, "run_command", **args)
        assert refused.result["_data"]["status"] == "confirmation_required"
    assert len(effects) == 2
    with_env = await _call(task_runtime, "run_command", argv=["wc", "b"], cwd=ROOT, env={"LANG": "C"})
    assert "Environment: LANG=C" in _texts(with_env) and len(_buttons(with_env)) == 2


async def test_plain_approval_of_a_command_is_one_card_one_execution_and_no_grant(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, _host = await _mac(task_runtime, remote=True)
    args = {"argv": ["wc", "notes.md"], "cwd": ROOT}
    first = await _call(task_runtime, "run_command", **args)
    payload = _payload(first, "approve")
    await rc.handle_decision(orch, ws, OWNER, payload)
    assert len(effects) == 1 and effects[0][1]["argv"] == ["wc", "notes.md"]
    assert not getattr(orch, "_hitl_pending_calls", {})
    assert orch.computer_sessions.grants.list(session.session_id) == ()
    actions = [call.args[0].action_type for call in orch.audit_recorder.record.await_args_list]
    assert "hitl.remote_reviewed" in actions and actions.count("hitl.effect_started") == 1
    renders = [c for c in orch.send_ui_render.await_args_list
               if any(isinstance(item, dict) and item.get("type") == "card" for item in c.args[1])]
    assert renders == []
    await rc.handle_decision(orch, ws, OWNER, payload)
    assert len(effects) == 1
    row = SimpleNamespace(proposal_id=payload["proposal_id"], owner_id=OWNER, conversation_id=CHAT,
                          agent_id="computer-use-1", tool_name="run_command", args_fingerprint=rc._fingerprint(args))
    risks = ("source_bound", "privileged", "tools:execute")
    with rc.source_review_scope(orch, row):
        replay = await hc.evaluate(orch, ws, OWNER, CHAT, "computer-use-1", "run_command", dict(args), risks)
        changed = await hc.evaluate(orch, ws, OWNER, CHAT, "computer-use-1", "run_command",
                                    {"argv": ["wc", "other.md"], "cwd": ROOT}, risks)
    assert replay.result["_data"]["status"] == "confirmation_required"
    assert changed.result["_data"]["status"] == "confirmation_required"
    assert len(effects) == 1


async def test_denying_the_command_card_runs_nothing_and_raises_no_second_card(task_runtime):
    orch, ws, effects, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    first = await _call(task_runtime, "run_command", argv=["wc", "notes.md"], cwd=ROOT)
    await rc.handle_decision(orch, ws, OWNER, _payload(first, "decline"))
    assert not effects and not getattr(orch, "_hitl_pending_calls", {})


async def test_remote_review_needs_the_same_session_and_a_consumed_proposal(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime, remote=True)
    args = {"argv": ["wc", "notes.md"], "cwd": ROOT}
    first = await _call(task_runtime, "run_command", **args)
    proposal = _payload(first, "approve")["proposal_id"]
    row = SimpleNamespace(proposal_id=proposal, owner_id=OWNER, conversation_id=CHAT, agent_id="computer-use-1",
                          tool_name="run_command", args_fingerprint=rc._fingerprint(args))
    risks = ("source_bound", "privileged", "tools:execute")
    with rc.source_review_scope(orch, row):
        pending = await hc.evaluate(orch, ws, OWNER, CHAT, "computer-use-1", "run_command", dict(args), risks)
    assert pending.result["_data"]["status"] == "confirmation_required" and not effects


async def test_destructive_command_card_offers_no_grant(task_runtime):
    await _mac(task_runtime, remote=True)
    response = await _call(task_runtime, "run_command", command="rm -rf build", cwd=ROOT)
    assert [b["label"] for b in _buttons(response)] == ["Approve", "Decline"]


async def test_remote_grant_decision_is_refused_without_an_offer_or_for_other_agents(task_runtime):
    orch, ws, effects, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    response = await _call(task_runtime, "run_command", command="rm -rf build", cwd=ROOT)
    payload = dict(_payload(response, "approve"), decision="grant_task")
    await rc.handle_decision(orch, ws, OWNER, payload)
    assert not effects
    assert "cannot" in str(orch.send_ui_render.await_args_list).lower()


async def test_browser_navigation_approval_creates_an_origin_grant(task_runtime, monkeypatch):
    from agents.computer_use import browser_tools

    orch, ws, effects, *_ = task_runtime
    session, _host = await _mac(task_runtime, remote=True)
    first = await _call(task_runtime, "browser_navigate", url="https://example.org/a")
    assert [b["label"] for b in _buttons(first)] == ["Approve", "Decline"]
    await rc.handle_decision(orch, ws, OWNER, _payload(first, "approve"))
    assert len(effects) == 1 and not orch._hitl_pending_calls
    grant, = orch.computer_sessions.grants.list(session.session_id)
    assert dict(grant.scope) == {"kind": "origin", "origin": "https://example.org"}
    assert (await _call(task_runtime, "browser_navigate", url="https://example.org/b")).result["_data"]["ok"]
    browser_tools._note_page(session.session_id, "https://example.org/b", "s1")
    assert (await _call(task_runtime, "browser_scroll", dy=300)).result["_data"]["ok"]
    consequential = await _call(task_runtime, "browser_click", ref="e3", snapshot_id="s1",
                                confirm_element='[e3] button "Pay"')
    assert consequential.result["_data"]["status"] == "confirmation_required" and len(effects) == 3
    other = await _call(task_runtime, "browser_navigate", url="https://evil.example/")
    assert other.result["_data"]["status"] == "confirmation_required"


async def test_browser_navigation_to_a_refused_url_never_gets_a_card(task_runtime):
    orch, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    response = await _call(task_runtime, "browser_navigate", url="javascript:alert(1)")
    assert "url_rejected" in json.dumps(response.result)
    assert not [c for c in response.ui_components if c.get("type") == "card"]


def test_task_origin_carries_an_audit_task_id(task_runtime):
    orch, ws, *_ = task_runtime
    hc.begin_task(orch, ws, OWNER, CHAT)
    origin = orch._hitl_task_origins[(OWNER, CHAT)]
    assert origin[0] is ws and len(origin[1].task_id) == 32


def test_grant_events_use_the_store_audit_names():
    assert (cg.EVENT_CREATED, cg.EVENT_USED) == ("computer_grant.created", "computer_grant.used")


async def test_interrupt_of_a_tracked_job_is_a_control_action_without_a_card(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, _host = await _mac(task_runtime)
    session.command_jobs["job-1"] = {"started_at": "2026-10-26T00:00:00Z"}
    response = await _call(task_runtime, "interrupt_command", job_id="job-1", signal="INT")
    assert response.error is None and response.result["_data"]["ok"] and len(effects) == 1
    actions = [call.args[0].action_type for call in orch.audit_recorder.record.await_args_list]
    assert "hitl.control" in actions and "hitl.effect_started" in actions
    unknown = await _call(task_runtime, "interrupt_command", job_id="job-9", signal="INT")
    assert unknown.result["_data"]["status"] == "confirmation_required" and len(effects) == 1


def _all_text(response):
    return json.dumps(response.ui_components)


@pytest.mark.parametrize("named", [None, "ryzenroll mobile", "RyzenRoll Mobile"])
async def test_input_and_session_cards_name_the_registered_host_only(task_runtime, named):
    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime)
    host.descriptor["name"] = "RyzenRoll Mobile"
    extra = {"computer": named} if named else {}
    card = await _call(task_runtime, "type_text", text="hello", hwnd=42, **extra)
    assert "On RyzenRoll Mobile" in _texts(card)
    if named and named != "RyzenRoll Mobile":
        assert named not in _all_text(card)
    await orch.computer_sessions.end(session, "user_stop")
    start = await _call(task_runtime, "start_session", **extra)
    assert "On RyzenRoll Mobile" in _texts(start)


@pytest.mark.parametrize("tool,args", [("type_text", {"text": "x", "hwnd": 42}), ("start_session", {}),
                                       ("run_command", {"argv": ["wc", "a"], "cwd": ROOT})])
async def test_an_unresolvable_computer_name_raises_no_card(task_runtime, tool, args):
    orch, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    response = await _call(task_runtime, tool, computer="Not My Mac", **args)
    text = json.dumps(response.error or response.result)
    assert "computer_unavailable" in text and "Not My Mac" in text
    assert not [c for c in (response.ui_components or []) if c.get("type") == "card"]
    assert not getattr(orch, "_hitl_pending_calls", {})


@pytest.mark.parametrize("named", [None, "ryzenroll mobile"])
async def test_command_card_names_the_registered_host_only(task_runtime, named):
    orch, *_ = task_runtime
    _session, host = await _mac(task_runtime, remote=True)
    host.descriptor["name"] = "RyzenRoll Mobile"
    extra = {"computer": named} if named else {}
    card = await _call(task_runtime, "run_command", argv=["wc", "notes.md"], cwd=ROOT, **extra)
    assert card.ui_components[0]["title"] == "Confirm an action on RyzenRoll Mobile"
    assert "Run a command on RyzenRoll Mobile" in _texts(card)
    if named:
        assert named not in _all_text(card)


@pytest.mark.parametrize("args,grantable", [
    ({"command": "wc -l notes.md"}, False), ({"argv": ["nice", "wc", "-l", "notes.md"]}, False),
    ({"argv": ["awk", "1", "notes.md"]}, False), ({"argv": ["wc", "-l", "notes.md"]}, True),
])
async def test_only_plain_argv_command_cards_offer_a_grant(task_runtime, args, grantable):
    await _mac(task_runtime, remote=True)
    card = await _call(task_runtime, "run_command", cwd=ROOT, **args)
    labels = [b["label"] for b in _buttons(card)]
    assert labels == (["Approve", "Allow wc in x for this task", "Decline"] if grantable else ["Approve", "Decline"])
