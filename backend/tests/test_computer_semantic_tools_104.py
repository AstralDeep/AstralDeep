"""Exercises the Mac semantic-control tools in agents/computer_use/mcp_tools.py (list_apps, ax_tree, ax_find,
ax_action, menu, zoom and element-addressed pointer actions) against a fake host, their policy and session
caches, workspace exclusion reporting, and bypass restoration through the real registration and review gate.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agents.computer_use import mcp_tools
from orchestrator import computer_use_policy as policy
from orchestrator import hitl_confirmation as hc
from orchestrator import remote_confirmation as rc
from orchestrator.computer_hosts import ComputerHostError
from orchestrator.computer_sessions import MAX_AX_HELD, MAX_AX_SNAPSHOTS, ComputerSession
from orchestrator.orchestrator import Orchestrator
from orchestrator.projection_surfaces import my_computers
from shared.protocol import ComputerHostDescriptor
from tests.test_computer_grant_flow_102 import (
    CHAT as GATE_CHAT, OWNER as GATE_OWNER, ROOT as GATE_ROOT, TEXTEDIT, _buttons, _call,
    _fresh_pending_cards as _fresh_pending_cards, _mac, _payload,
)
from tests.test_computer_use_076 import OWNER, _AgentOrch, _descriptor, _FakeHostSocket, _WS
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import _registered_controller, task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401

CHAT = "chat-1"
CTX = {"user_id": OWNER, "session_id": CHAT}
ROOT = "/Users/sam/Projects/x"
EXCLUDED = ["private/", ".env*", "*.pem"]
SHOT = {"screen_index": 0, "width": 1280, "height": 720, "scale": 0.5, "media_type": "image/jpeg",
        "base64": "/9j/AAAA"}
TREE = {"snapshot_id": "x3", "app": {"pid": 501, "name": "Mail", "bundle_id": "com.apple.mail"},
        "window": {"hwnd": 42, "title": "New Message"}, "truncated": False,
        "lines": ['[a1] AXWindow "New Message" (0,0,800,600) focused', '[a2] AXTextField "To" value="" (10,10,300,20)',
                  '[a7] AXButton "Send" (700,10,60,24) enabled', '[a8] AXButton "Save Draft" (620,10,70,24)']}
TERMINAL_TREE = {**TREE, "snapshot_id": "x9", "app": {"pid": 77, "name": "Terminal", "bundle_id": "com.apple.Terminal"},
                 "window": {"hwnd": 53, "title": "zsh"}}


class World:
    def __init__(self, orch, host, host_ws, session, replies):
        self.orch, self.host, self.host_ws, self.session, self.replies = orch, host, host_ws, session, replies

    async def call(self, tool, **kwargs):
        return await asyncio.to_thread(getattr(mcp_tools, tool), **CTX, **kwargs)

    def requests(self, verb=None):
        return [f for f in self.host_ws.frames("computer_request") if verb is None or f["verb"] == verb]


@pytest.fixture
async def world():
    replies = {}

    def answers(verb, args):
        reply = replies.get(verb)
        if callable(reply):
            return reply(args)
        return reply if reply is not None else {"ok": True, "result": {}}

    orch = _AgentOrch()
    mcp_tools.register_deps(orch)
    host_ws = orch.add(_FakeHostSocket(orch, answers))
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(_descriptor(
        platform="macos", name="Mac", workspace_roots=[{"name": "x", "path": ROOT}], excluded=EXCLUDED)))
    orch.add(_WS(chat=CHAT))
    task = asyncio.create_task(asyncio.to_thread(mcp_tools.start_session, **CTX))
    for _ in range(400):
        await asyncio.sleep(0.005)
        live = orch.computer_sessions.live_for_host(OWNER, host.host_id)
        if live is not None:
            await orch.computer_sessions.on_host_event(OWNER, host.host_id, "heartbeat", live.session_id, None)
            break
    started = await task
    session = orch.computer_sessions.live_for_host(OWNER, host.host_id)
    world = World(orch, host, host_ws, session, replies)
    world.started = started
    yield world
    await orch.computer_sessions.end_all("user_stop")


def _ok(**result):
    return {"ok": True, "result": result}


def _error(code, message="refused", **extra):
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


def _approve(monkeypatch, approved=True):
    calls = []

    def approved_call(orch, owner, chat, agent, tool):
        calls.append(tool)
        return approved

    monkeypatch.setattr(hc, "approved_call", approved_call)
    return calls


async def _snapshot(world, tree=TREE):
    world.replies["ax_tree"] = _ok(**tree)
    return await world.call("ax_tree")


def test_consequential_labels_follow_the_host_word_list_and_desktop_commands():
    for words in ("Buy now", "PAY", "Purchase", "Place order", "Checkout", "Delete account", "Remove", "Send",
                  "Transfer funds", "Confirm payment", "Pay-now", "(order)", "SendMessage", "Quit TextEdit",
                  "Empty Trash…", "Erase", "Force Quit", "Shut Down…", "Log Out Sam…", "Revert to Saved"):
        assert policy.consequential_label(words), words
    for words in ("Payment history", "Repay", "Ordering guide", "Sender", "Confirm", "Removed items", "Next", "Save",
                  "Quitter", "", None, 7):
        assert not policy.consequential_label(words), words


def test_consequential_input_covers_review_lines_values_and_menu_leaves():
    assert policy.is_consequential_input("ax_action", {"confirm_element": '[a7] AXButton "Send"'})
    assert policy.is_consequential_input("ax_action", {"action": "pick", "value": "Delete All"})
    assert policy.is_consequential_input("ax_action", {"action": "set_value", "value": "send it"})
    assert not policy.is_consequential_input("ax_action", {"action": "focus", "value": "Send"})
    assert not policy.is_consequential_input("ax_action", {"action": "press"})
    assert not policy.is_consequential_input("ax_action", {"confirm_element": 7, "action": "press"})
    assert policy.is_consequential_input("menu", {"path": ["File", "Quit"]})
    assert not policy.is_consequential_input("menu", {"path": ["File", "Quit"], "list": True})
    assert not policy.is_consequential_input("menu", {"path": ["File", "Save"]})
    assert not policy.is_consequential_input("menu", {"path": "File"})
    assert not policy.is_consequential_input("click", {"confirm_element": "x"})
    assert policy.menu_path(["File"] * 7) is None and policy.menu_path(["", "x"]) is None
    assert policy.menu_path([1]) is None and policy.menu_path(["x" * 101]) is None
    assert policy.menu_path(["File", "Save As…"]) == ["File", "Save As…"]


def test_binding_fills_the_snapshot_window_and_refuses_unsafe_or_mismatched_targets():
    target = {"hwnd": 42, "pid": 501, "bundle_id": "com.apple.mail", "app": "Mail", "title": "New"}
    args = {"ref": "a7", "snapshot_id": "x3"}
    assert policy.bind_ax_target("click", args, target) is None and args["hwnd"] == 42
    assert policy.bind_ax_target("click", {"x": 1, "y": 2}, None) is None
    assert policy.bind_ax_target("menu", {"path": ["File"]}, None) is None
    assert policy.bind_ax_target("menu", {"pid": 501, "path": ["File"]}, None) is None
    assert policy.bind_ax_target("ax_action", {"ref": "a7"}, None).code == "stale_ref"
    mismatch = policy.bind_ax_target("ax_action", {"ref": "a7", "hwnd": 43}, target)
    assert mismatch.code == "out_of_range" and "omit hwnd" in str(mismatch)
    terminal = {**target, "bundle_id": "com.apple.Terminal"}
    assert policy.bind_ax_target("ax_action", {"ref": "a7"}, terminal).code == "terminal_target"
    assert policy.bind_ax_target("menu", {"pid": 77, "path": ["Shell"]}, terminal).code == "terminal_target"
    assert policy.bind_ax_target("click", {"ref": "a7"}, terminal) is None
    astral = {**target, "bundle_id": "com.personalailabs.astraldeep"}
    assert policy.bind_ax_target("click", {"ref": "a7"}, astral).code == "out_of_range"
    windowless = {"ref": "a7"}
    assert policy.bind_ax_target("ax_action", windowless, {**target, "hwnd": None}) is None and "hwnd" not in windowless


@pytest.mark.parametrize("tool,args,bundle,floor", [
    ("ax_action", {"action": "press"}, "com.apple.mail", False),
    ("ax_action", {"action": "press"}, None, False),
    ("ax_action", {"action": "set_value", "value": "x"}, "com.apple.mail", False),
    ("ax_action", {"action": "set_value", "value": "x"}, None, True),
    ("ax_action", {"action": "set_value", "value": "x"}, "com.1password.1password", True),
    ("ax_action", {"action": "press"}, "com.personalailabs.astraldeep", True),
    ("ax_action", {"action": "press"}, "com.apple.Terminal", True),
    ("menu", {"path": ["File"]}, None, False),
    ("menu", {"path": ["File"]}, "com.googlecode.iterm2", True),
    ("click", {"ref": "a7"}, "com.apple.mail", False),
])
def test_the_bypass_floor_covers_accessibility_targets(tool, args, bundle, floor):
    assert policy.bypass_floor(tool, args, bundle) is floor


def test_session_caches_are_bounded_bound_to_windows_and_forgotten():
    session = ComputerSession("s", OWNER, "h", "Mac", 1, "d", "Mac", CHAT)
    for index in range(MAX_AX_SNAPSHOTS + 3):
        session.remember_ax_snapshot(f"x{index}", {"hwnd": 40 + index, "pid": 9, "bundle_id": "com.apple.mail",
                                                   "app": "Mail", "title": f"W{index}"}, {})
    assert len(session.ax_snapshots) == MAX_AX_SNAPSHOTS and "x0" not in session.ax_snapshots
    assert session.window_cache[50]["bundle_id"] == "com.apple.mail" and session.window_labels[50].endswith("W10")
    assert session.ax_target("menu", {"pid": 9})["hwnd"] == 40 + MAX_AX_SNAPSHOTS + 2
    session.remember_apps([{"pid": 12, "bundle_id": "com.apple.Notes", "name": "Notes"}])
    assert session.ax_target("menu", {"pid": 12}) == {"pid": 12, "hwnd": None, "bundle_id": "com.apple.Notes",
                                                       "app": "Notes", "title": ""}
    assert session.ax_target("menu", {"pid": "12"}) is None and session.ax_target("ax_action", {}) is None
    session.note_window(50, "com.apple.mail", "Renamed")
    assert session.window_cache[50]["title"] == "Renamed"
    session.note_window(50, "com.apple.Notes", "")
    assert session.window_cache[50]["bundle_id"] == "com.apple.Notes"
    session.note_window(0, "com.apple.mail", "x")
    session.note_window(60, "", "x")
    assert 0 not in session.window_cache and 60 not in session.window_cache
    for index in range(MAX_AX_HELD + 4):
        session.hold_ax(("x1", f"a{index}"), "line")
    assert len(session.ax_held) == MAX_AX_HELD
    session.forget_observations()
    assert not session.ax_snapshots and not session.ax_held and not session.app_cache and not session.window_cache


def test_host_errors_carry_a_bounded_single_line_element_line():
    exc = ComputerHostError("confirmation_required", "x", element_line='[a7] AXButton "Send"\n\x07' + "y" * 400)
    assert exc.element_line.startswith('[a7] AXButton "Send" ') and len(exc.element_line) == 200
    assert ComputerHostError("x", "y", element_line=" \n").element_line is None
    assert ComputerHostError("x", "y", element_line=7).element_line is None


def test_registry_entries_steer_semantic_control_and_stay_scanner_clean():
    reg = mcp_tools.TOOL_REGISTRY
    for name in ("list_apps", "ax_tree", "ax_find", "zoom"):
        assert reg[name]["tier"] == "observe" and reg[name]["scope"] == "tools:read"
    for name in ("ax_action", "menu"):
        assert reg[name]["tier"] == "input" and reg[name]["scope"] == "tools:write"
        assert "confirm_element" in reg[name]["input_schema"]["properties"]
        assert "destructive" not in reg[name]
    assert "[a12]" in reg["ax_tree"]["description"] and "expire" in reg["ax_tree"]["description"]
    assert "ax_find" in reg["list_apps"]["description"] and "ax_find" in reg["list_windows"]["description"]
    assert reg["ax_find"]["input_schema"]["required"] == ["query"]
    assert reg["ax_action"]["input_schema"]["required"] == ["ref", "snapshot_id", "action"]
    assert reg["ax_action"]["input_schema"]["properties"]["action"]["enum"] == list(policy.AX_ACTIONS)
    assert reg["menu"]["input_schema"]["required"] == ["path"] and "run_command" in reg["ax_action"]["description"]
    for name in ("click", "double_click", "right_click", "move", "drag"):
        text = reg[name]["description"]
        assert "ax_find" in text and "menu" in text and "ref" in reg[name]["input_schema"]["properties"]
        assert reg[name]["input_schema"]["required"] == []
    assert "ref2" in reg["drag"]["input_schema"]["properties"]
    for name in ("read_file", "list_dir", "write_file", "delete_path", "run_command", "list_computers"):
        assert "excluded" in reg[name]["description"]


async def test_list_apps_returns_sanitized_apps_and_remembers_them(world):
    world.replies["list_apps"] = _ok(apps=[
        {"pid": 501, "name": "Mail\x07", "bundle_id": "com.apple.mail", "active": True, "hidden": False,
         "window_count": 2},
        {"pid": 0, "name": "bad"}, "junk", {"pid": 77, "name": "Terminal", "bundle_id": "com.apple.Terminal"}])
    out = await world.call("list_apps")
    assert world.requests("list_apps")[-1]["args"] == {}
    assert out["_data"]["apps"] == [
        {"pid": 501, "name": "Mail", "bundle_id": "com.apple.mail", "active": True, "hidden": False, "window_count": 2},
        {"pid": 77, "name": "Terminal", "bundle_id": "com.apple.Terminal", "active": False, "hidden": False,
         "window_count": 0}]
    assert out["_ui_components"][0]["type"] == "table"
    assert world.session.app_cache[77] == {"bundle_id": "com.apple.Terminal", "name": "Terminal"}
    world.replies["list_apps"] = _error("timeout", "slow")
    assert (await world.call("list_apps"))["_data"]["code"] == "timeout"


async def test_ax_tree_forwards_bounded_arguments_and_caches_the_snapshot_window(world):
    out = await _snapshot(world)
    assert world.requests("ax_tree")[-1]["args"] == {"depth": 6, "max_nodes": 300, "mode": "interactive"}
    data = out["_data"]
    assert (data["snapshot_id"], data["app"], data["bundle_id"], data["pid"], data["hwnd"], data["window_title"]) == (
        "x3", "Mail", "com.apple.mail", 501, 42, "New Message")
    assert data["lines"] == TREE["lines"] and data["truncated"] is False
    assert data["trust"] == "untrusted screen content" and "snapshot_id" in data["note"]
    assert out["_ui_components"] == []
    cached = world.session.ax_snapshots["x3"]
    assert cached["consequential"] == {"a7": '[a7] AXButton "Send"'}
    assert world.session.window_cache[42]["bundle_id"] == "com.apple.mail"
    assert world.session.app_cache[501]["name"] == "Mail"
    await world.call("ax_tree", hwnd=42, pid=501, depth=2, max_nodes=50, mode="all")
    assert world.requests("ax_tree")[-1]["args"] == {"hwnd": 42, "pid": 501, "depth": 2, "max_nodes": 50,
                                                     "mode": "all"}
    before = len(world.requests())
    for bad in ({"depth": 9}, {"max_nodes": 0}, {"mode": "tree"}, {"hwnd": 0}, {"pid": "x"}, {"hwnd": 2**31}):
        assert (await world.call("ax_tree", **bad))["_data"]["code"] == "out_of_range"
    assert len(world.requests()) == before


async def test_ax_tree_bounds_its_lines_and_refuses_unreadable_snapshots(world):
    lines = ["[a1] AXStaticText " + "z" * 900, 5, *[f'[a{i}] AXButton "B{i}"' + "q" * 580 for i in range(2, 200)]]
    world.replies["ax_tree"] = _ok(**{**TREE, "lines": lines, "app": "Mail", "hwnd": 42, "bundle_id": "com.apple.mail"})
    data = (await world.call("ax_tree"))["_data"]
    assert data["truncated"] is True and sum(len(line) + 1 for line in data["lines"]) <= 60_000
    assert data["lines"][0].endswith("…") and data["app"] == "Mail" and data["hwnd"] == 42
    for bad in ({**TREE, "snapshot_id": "s3"}, {**TREE, "lines": "x"}, {**TREE, "snapshot_id": None}):
        world.replies["ax_tree"] = _ok(**bad)
        assert (await world.call("ax_tree"))["_data"]["code"] == "failed"
    world.replies["ax_tree"] = _error("timeout", "slow")
    assert (await world.call("ax_tree"))["_data"]["code"] == "timeout"


async def test_ax_find_returns_sanitized_matches_and_marks_consequential_controls(world):
    world.replies["ax_find"] = _ok(snapshot_id="x4", hwnd=42, pid=501, bundle_id="com.apple.mail", app="Mail", matches=[
        {"ref": "a7", "role": "AXButton", "title": "Send\x07", "value": "", "frame": {"x": 700, "y": 10, "w": 60, "h": 24},
         "enabled": True, "focused": False, "actions": ["AXPress", 4]},
        {"ref": "a9", "role": "AXMenuItem", "title": "Archive", "value": "Delete later", "frame": {"x": "1"}},
        {"ref": "e3", "role": "AXButton", "title": "bad ref"}, "junk"])
    out = await world.call("ax_find", query="send", role="AXButton", max=5)
    assert world.requests("ax_find")[-1]["args"] == {"query": "send", "role": "AXButton", "max": 5}
    data = out["_data"]
    assert data["count"] == 2 and data["snapshot_id"] == "x4" and data["hwnd"] == 42
    assert data["matches"][0] == {"ref": "a7", "role": "AXButton", "title": "Send", "value": "",
                                  "frame": {"x": 700, "y": 10, "w": 60, "h": 24}, "enabled": True, "focused": False,
                                  "actions": ["AXPress"]}
    assert data["matches"][1]["frame"] == {} and data["matches"][1]["enabled"] is True
    assert world.session.ax_snapshots["x4"]["consequential"] == {
        "a7": '[a7] AXButton "Send"', "a9": '[a9] AXMenuItem "Archive"'}
    before = len(world.requests())
    for bad in ({"query": ""}, {"query": "x" * 201}, {"query": 7}, {"query": "a", "role": "x" * 41},
                {"query": "a", "max": 51}, {"query": "a", "role": ""}, {}):
        assert (await world.call("ax_find", **bad))["_data"]["code"] == "out_of_range"
    assert len(world.requests()) == before
    world.replies["ax_find"] = _ok(snapshot_id="x5", matches="nope")
    assert (await world.call("ax_find", query="a"))["_data"]["code"] == "failed"
    world.replies["ax_find"] = _error("host_offline", "gone")
    assert (await world.call("ax_find", query="a"))["_data"]["code"] == "host_offline"


async def test_ax_action_presses_by_reference_without_sending_the_window(world):
    await _snapshot(world)
    world.replies["ax_action"] = _ok(element="Save Draft", app="Mail", screenshot=SHOT)
    out = await world.call("ax_action", ref="a8", snapshot_id="x3", action="press")
    assert world.requests("ax_action")[-1]["args"] == {"ref": "a8", "snapshot_id": "x3", "action": "press"}
    assert out["_data"]["element"] == "Save Draft" and out["_data"]["app"] == "Mail"
    assert out["_images"][0]["caption"] == "Screen of Mac after ax_action"
    await world.call("ax_action", ref="a2", snapshot_id="x3", action="set_value", value="sam@example.org")
    assert world.requests("ax_action")[-1]["args"]["value"] == "sam@example.org"


@pytest.mark.parametrize("args", [
    {"ref": "e7", "snapshot_id": "x3", "action": "press"}, {"ref": "a7", "snapshot_id": "s3", "action": "press"},
    {"ref": "a7", "snapshot_id": "x3", "action": "explode"}, {"ref": "a7", "snapshot_id": "x3", "action": "pick"},
    {"ref": "a7", "snapshot_id": "x3", "action": "set_value", "value": "x" * 4001},
    {"ref": "a7", "snapshot_id": "x3", "action": "press", "value": 7}, {"snapshot_id": "x3", "action": "press"},
])
async def test_ax_action_validates_its_arguments_before_any_request(world, args):
    await _snapshot(world)
    out = await world.call("ax_action", **args)
    assert out["_data"]["code"] == "out_of_range" and world.requests("ax_action") == []


async def test_ax_action_refuses_unknown_snapshots_terminals_and_window_mismatches(world):
    stale = await world.call("ax_action", ref="a7", snapshot_id="x404", action="press")
    assert stale["_data"]["status"] == "refused" and stale["_data"]["code"] == "stale_ref"
    assert "ax_find" in stale["_data"]["next_action"]
    await _snapshot(world, TERMINAL_TREE)
    terminal = await world.call("ax_action", ref="a2", snapshot_id="x9", action="press")
    assert terminal["_data"]["code"] == "terminal_target" and "run_command" in terminal["_data"]["next_action"]
    await _snapshot(world)
    mismatch = await world.call("ax_action", ref="a8", snapshot_id="x3", action="press", hwnd=43)
    assert mismatch["_data"]["code"] == "out_of_range" and world.requests("ax_action") == []


async def test_a_known_consequential_control_is_held_for_exact_review_before_any_request(world, monkeypatch):
    await _snapshot(world)
    first = await world.call("ax_action", ref="a7", snapshot_id="x3", action="press")
    data = first["_data"]
    line = '[a7] AXButton "Send"'
    assert data["status"] == "refused" and data["code"] == "confirmation_required" and data["element"] == line
    assert line in data["next_action"] and world.requests("ax_action") == []
    assert world.session.ax_held[("x3", "a7")] == line
    mismatch = await world.call("ax_action", ref="a7", snapshot_id="x3", action="press",
                                confirm_element='[a7] AXButton "Save"')
    assert mismatch["_data"]["code"] == "confirm_element_mismatch" and world.requests("ax_action") == []
    _approve(monkeypatch, approved=False)
    again = await world.call("ax_action", ref="a7", snapshot_id="x3", action="press", confirm_element=line)
    assert again["_data"]["code"] == "confirmation_required" and world.requests("ax_action") == []
    calls = _approve(monkeypatch)
    world.replies["ax_action"] = _ok(element="Send", app="Mail")
    done = await world.call("ax_action", ref="a7", snapshot_id="x3", action="press", confirm_element=line)
    assert calls == ["ax_action"] and done["_data"]["element"] == "Send"
    assert world.requests("ax_action")[-1]["args"] == {
        "ref": "a7", "snapshot_id": "x3", "action": "press", "permit": {"ref": "a7", "snapshot_id": "x3", "hwnd": 42}}
    assert ("x3", "a7") not in world.session.ax_held
    replay = await world.call("ax_action", ref="a7", snapshot_id="x3", action="press", confirm_element=line)
    assert replay["_data"]["code"] == "confirm_element_mismatch"


async def test_a_consequential_value_or_known_field_needs_review_for_pick_and_set_value(world):
    await _snapshot(world)
    pick = await world.call("ax_action", ref="a2", snapshot_id="x3", action="pick", value="Delete All")
    assert pick["_data"]["element"] == '[a2] pick "Delete All"' and world.requests("ax_action") == []
    known = await world.call("ax_action", ref="a7", snapshot_id="x3", action="set_value", value="x")
    assert known["_data"]["element"] == '[a7] AXButton "Send" set_value "x"'
    changed = await world.call("ax_action", ref="a2", snapshot_id="x3", action="pick", value="Delete Everything",
                               confirm_element='[a2] pick "Delete All"')
    assert changed["_data"]["code"] == "confirm_element_mismatch"
    focus = await world.call("ax_action", ref="a7", snapshot_id="x3", action="focus")
    assert "code" not in focus["_data"] and len(world.requests("ax_action")) == 1


async def test_a_host_reported_review_is_held_and_an_approved_retry_carries_the_permit(world, monkeypatch):
    await _snapshot(world)
    line = '[a8] AXButton "Save Draft" — Mail'
    world.replies["ax_action"] = _error("confirmation_required", "needs review", element_line=line)
    out = await world.call("ax_action", ref="a8", snapshot_id="x3", action="press")
    assert out["_data"]["code"] == "confirmation_required" and out["_data"]["element"] == line
    _approve(monkeypatch, approved=False)
    world.replies["ax_action"] = _error("confirmation_required", f"still needs review\n{line}")
    unapproved = await world.call("ax_action", ref="a8", snapshot_id="x3", action="press", confirm_element=line)
    assert "permit" not in world.requests("ax_action")[-1]["args"] and unapproved["_data"]["element"] == line
    _approve(monkeypatch)
    world.replies["ax_action"] = _ok()
    await world.call("ax_action", ref="a8", snapshot_id="x3", action="press", confirm_element=line)
    assert world.requests("ax_action")[-1]["args"]["permit"] == {"ref": "a8", "snapshot_id": "x3", "hwnd": 42}
    world.replies["ax_action"] = _error("confirmation_required", "\x07")
    blank = await world.call("ax_action", ref="a8", snapshot_id="x3", action="press")
    assert blank["_data"]["element"] == "" and "ax_find" in blank["_data"]["next_action"]


@pytest.mark.parametrize("code,hint", [("terminal_target", "run_command"), ("stale_ref", "ax_find"),
                                       ("excluded_path", "excluded")])
async def test_actionable_host_refusals_become_guidance(world, code, hint):
    await _snapshot(world)
    world.replies["ax_action"] = _error(code, "no")
    out = await world.call("ax_action", ref="a8", snapshot_id="x3", action="press")
    assert out["_data"]["status"] == "refused" and out["_data"]["code"] == code and hint in out["_data"]["next_action"]
    world.replies["ax_action"] = _error("failed", "broke")
    assert (await world.call("ax_action", ref="a8", snapshot_id="x3", action="press"))["_ui_components"][0][
        "variant"] == "error"


async def test_menu_invokes_and_lists_and_reviews_consequential_leaves(world, monkeypatch):
    await _snapshot(world)
    world.replies["menu"] = _ok(screenshot=SHOT)
    out = await world.call("menu", pid=501, path=["File", "Save"])
    assert world.requests("menu")[-1]["args"] == {"pid": 501, "path": ["File", "Save"]}
    assert out["_data"] == {"computer": "Mac", "path": ["File", "Save"], "invoked": True,
                            "screenshot": {"width": 1280, "height": 720, "scale": 0.5, "screen_index": 0}}
    world.replies["menu"] = _ok(items=[{"title": "Save", "enabled": True, "checked": False, "shortcut": "⌘S",
                                        "has_children": False}, {"title": "Export", "has_children": True}, "junk"])
    listed = await world.call("menu", path=["File"], list=True)
    assert world.requests("menu")[-1]["args"] == {"path": ["File"], "list": True}
    assert listed["_data"]["items"][1] == {"title": "Export", "enabled": True, "checked": False, "shortcut": "",
                                           "has_children": True}
    before = len(world.requests("menu"))
    quit_ = await world.call("menu", pid=501, path=["Mail", "Quit Mail"])
    line = "menu Mail: Mail > Quit Mail"
    assert quit_["_data"]["code"] == "confirmation_required" and quit_["_data"]["element"] == line
    assert len(world.requests("menu")) == before
    _approve(monkeypatch)
    world.replies["menu"] = _ok()
    await world.call("menu", pid=501, path=["Mail", "Quit Mail"], confirm_element=line)
    assert world.requests("menu")[-1]["args"] == {"pid": 501, "path": ["Mail", "Quit Mail"],
                                                  "permit": {"path": ["Mail", "Quit Mail"], "pid": 501}}
    frontmost = await world.call("menu", path=["Finder", "Empty Trash…"])
    assert frontmost["_data"]["element"] == "menu frontmost app: Finder > Empty Trash…"
    world.replies["menu"] = _error("confirmation_required", "needs review", element_line="Edit > Delete")
    hosted = await world.call("menu", path=["Edit", "Remove Formatting"], list=False)
    assert hosted["_data"]["code"] == "confirmation_required"
    await world.call("menu", path=["Edit", "Copy"])
    assert world.session.ax_held[("menu", "0:Edit\x1fCopy")] == "Edit > Delete"


@pytest.mark.parametrize("args", [{"path": []}, {"path": ["a"] * 7}, {"path": [1]}, {"path": "File"},
                                  {"path": ["File"], "list": "yes"}, {"path": ["File"], "pid": 0}, {}])
async def test_menu_validates_its_arguments(world, args):
    out = await world.call("menu", **args)
    assert out["_data"]["code"] == "out_of_range" and world.requests("menu") == []


async def test_menu_on_a_terminal_or_astral_app_is_refused_before_any_request(world):
    world.session.remember_apps([{"pid": 77, "bundle_id": "com.apple.Terminal", "name": "Terminal"},
                                 {"pid": 88, "bundle_id": "com.personalailabs.astraldeep", "name": "Astral"}])
    assert (await world.call("menu", pid=77, path=["Shell", "New Window"]))["_data"]["code"] == "terminal_target"
    assert (await world.call("menu", pid=88, path=["Astral", "Settings…"]))["_data"]["code"] == "out_of_range"
    assert world.requests("menu") == []


async def test_zoom_returns_a_region_image_without_touching_the_session_geometry(world):
    world.session.last_screenshot = {"width": 1280, "height": 720, "scale": 0.5, "screen_index": 0, "at": 1.0}
    world.replies["zoom"] = _ok(base64="/9j/ZOOM", media_type="image/png", width=400, height=200, scale=2.0,
                                region={"x": 10, "y": 20, "w": 200, "h": 100})
    out = await world.call("zoom", x=10, y=20, w=200, h=100, hwnd=42)
    assert world.requests("zoom")[-1]["args"] == {"x": 10, "y": 20, "w": 200, "h": 100, "scale": 2, "hwnd": 42}
    assert out["_data"]["region"] == {"x": 10, "y": 20, "w": 200, "h": 100} and out["_data"]["width"] == 400
    assert out["_images"][0]["base64"] == "/9j/ZOOM" and out["_ui_components"][0]["id"].startswith("au_cuzoom_")
    assert world.session.last_screenshot["at"] == 1.0 and world.session.screenshot_cache is None
    world.replies["zoom"] = _ok(base64="/9j/ZOOM", media_type="image/jpeg", width=400, height=200, region="bad")
    fallback = await world.call("zoom", x=1, y=2, w=3, h=4, scale=4)
    assert fallback["_data"]["region"] == {"x": 1, "y": 2, "w": 3, "h": 4}
    world.session.images_supported = False
    blind = await world.call("zoom", x=1, y=2, w=3, h=4)
    assert blind["_images"] == [] and "ax_find" in blind["_data"]["note"]
    world.replies["zoom"] = _ok(base64="", media_type="image/jpeg")
    assert (await world.call("zoom", x=1, y=2, w=3, h=4))["_data"]["code"] == "failed"
    world.replies["zoom"] = _error("timeout")
    assert (await world.call("zoom", x=1, y=2, w=3, h=4))["_data"]["code"] == "timeout"
    before = len(world.requests("zoom"))
    for bad in ({"x": 1, "y": 2, "w": 0, "h": 4}, {"x": 1, "y": 2, "w": 3, "h": 4, "scale": 5},
                {"x": -1, "y": 2, "w": 3, "h": 4}, {"y": 2, "w": 3, "h": 4}, {"x": 1, "y": 2, "w": 3, "h": 4, "hwnd": 0}):
        assert (await world.call("zoom", **bad))["_data"]["code"] == "out_of_range"
    assert len(world.requests("zoom")) == before


async def test_pointer_actions_accept_an_element_reference_and_take_the_window_from_the_snapshot(world):
    await _snapshot(world)
    world.replies["click"] = _ok(screenshot=SHOT)
    out = await world.call("click", ref="a8", snapshot_id="x3")
    assert world.requests("click")[-1]["args"] == {"ref": "a8", "snapshot_id": "x3", "button": "left", "count": 1,
                                                   "hwnd": 42}
    assert out["_data"]["ref"] == "a8" and out["_images"]
    for verb in ("double_click", "right_click", "move"):
        await world.call(verb, ref="a2", snapshot_id="x3")
        assert world.requests(verb)[-1]["args"] == {"ref": "a2", "snapshot_id": "x3", "hwnd": 42}
    send = await world.call("click", ref="a7", snapshot_id="x3")
    assert send["_data"]["code"] == "confirmation_required" and "ax_action" in send["_data"]["next_action"]
    assert len(world.requests("click")) == 1
    await world.call("right_click", ref="a7", snapshot_id="x3")
    assert world.requests("right_click")[-1]["args"]["ref"] == "a7"
    stale = await world.call("click", ref="a8", snapshot_id="x77")
    assert stale["_data"]["code"] == "stale_ref"


@pytest.mark.parametrize("args", [
    {"ref": "a8", "snapshot_id": "x3", "x": 1, "y": 2}, {}, {"x": 1, "y": 2, "snapshot_id": "x3", "hwnd": 42},
    {"ref": "a8"}, {"ref": "e8", "snapshot_id": "x3"}, {"x": 1},
])
async def test_pointer_addressing_takes_exactly_one_form(world, args):
    await _snapshot(world)
    out = await world.call("click", **args)
    assert out["_data"]["code"] == "out_of_range" and world.requests("click") == []


async def test_drag_accepts_reference_ends_and_mixed_ends(world):
    await _snapshot(world)
    await world.call("drag", ref="a2", snapshot_id="x3", ref2="a8")
    assert world.requests("drag")[-1]["args"] == {"ref": "a2", "snapshot_id": "x3", "ref2": "a8", "hwnd": 42}
    await world.call("drag", ref="a2", snapshot_id="x3", x2=300, y2=400)
    assert world.requests("drag")[-1]["args"] == {"ref": "a2", "snapshot_id": "x3", "x2": 300, "y2": 400, "hwnd": 42}
    await world.call("drag", x1=1, y1=2, x2=3, y2=4, hwnd=42)
    assert world.requests("drag")[-1]["args"] == {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "hwnd": 42}
    before = len(world.requests("drag"))
    for bad in ({"x1": 1, "y1": 2, "ref2": "a8", "hwnd": 42}, {"ref": "a2", "snapshot_id": "x3", "ref2": "a8", "x2": 3},
                {"ref": "a2", "snapshot_id": "x3", "ref2": "bad"}, {"ref": "a2", "snapshot_id": "x3"}):
        assert (await world.call("drag", **bad))["_data"]["code"] == "out_of_range"
    assert len(world.requests("drag")) == before
    assert (await world.call("drag", ref="a2", snapshot_id="x404", ref2="a8"))["_data"]["code"] == "stale_ref"


async def test_coordinate_pointer_actions_keep_their_window_requirement(world):
    out = await world.call("click", x=10, y=20)
    assert out["_data"]["code"] == "out_of_range" and "list_windows" in out["_data"]["message"]
    world.session.remember_windows([{"hwnd": 42, "title": "Untitled", "bundle_id": "com.apple.TextEdit"}])
    world.replies["scroll"] = _ok()
    await world.call("scroll", x=5, y=6, hwnd=42)
    assert world.requests("scroll")[-1]["args"] == {"x": 5, "y": 6, "dx": 0, "dy": -3, "hwnd": 42}


async def test_excluded_patterns_reach_the_agent_and_excluded_paths_explain_themselves(world):
    assert world.started["_data"]["excluded"] == EXCLUDED
    listed = await world.call("list_computers")
    assert listed["_data"]["computers"][0]["excluded"] == EXCLUDED
    assert world.host.public()["excluded"] == EXCLUDED
    world.replies["read_file"] = _error("excluded_path", "private/notes.md is excluded")
    out = await world.call("read_file", path=ROOT + "/private/notes.md")
    assert out["_data"]["code"] == "excluded_path" and "excluded" in out["_data"]["next_action"]
    plain = ComputerHostDescriptor.from_dict(_descriptor(platform="macos"))
    host, _ = world.orch.computer_hosts.register("someone", object(), plain)
    assert host.excluded == [] and host.public()["excluded"] == []


def _effect(effect):
    tool, args = effect
    return tool, {key: value for key, value in args.items() if not key.startswith("_")}


async def _gate_mac(task_runtime, **kwargs):
    from orchestrator.computer_use_policy import SCOPES

    session, host = await _mac(task_runtime, **kwargs)
    task_runtime[0].tool_permissions.get_tool_scope_map.return_value = dict(SCOPES)
    session.remember_ax_snapshot("x3", {"hwnd": 42, "pid": 501, "bundle_id": "com.apple.TextEdit",
                                        "app": "TextEdit", "title": "Untitled"},
                                 {"a7": '[a7] AXButton "Delete"'})
    return session, host


async def test_a_reference_click_gets_its_window_at_the_gate_and_offers_window_grants(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, _host = await _gate_mac(task_runtime)
    first = await _call(task_runtime, "click", ref="a8", snapshot_id="x3")
    assert [b["label"] for b in _buttons(first)] == ["Approve", 'Allow for this task in window "Untitled"',
                                                    "Allow for this task in TextEdit", "Decline"]
    await hc.handle_decision(orch, ws, GATE_OWNER, _payload(first, "grant_task"))
    assert _effect(effects[-1]) == ("click", {"ref": "a8", "snapshot_id": "x3", "hwnd": 42})
    again = await _call(task_runtime, "ax_action", ref="a8", snapshot_id="x3", action="press")
    assert again.result["_data"]["ok"] and effects[-1][1]["hwnd"] == 42


async def test_an_application_grant_covers_actions_and_menus_but_never_a_consequential_step(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, _host = await _gate_mac(task_runtime)
    first = await _call(task_runtime, "ax_action", ref="a8", snapshot_id="x3", action="press")
    await hc.handle_decision(orch, ws, GATE_OWNER, next(b["payload"] for b in _buttons(first)
                                                         if b["payload"].get("grant_scope") == "app"))
    assert len(effects) == 1
    assert (await _call(task_runtime, "menu", pid=501, path=["Format", "Bold"])).result["_data"]["ok"]
    assert _effect(effects[-1]) == ("menu", {"pid": 501, "path": ["Format", "Bold"], "hwnd": 42})
    reviewed = await _call(task_runtime, "ax_action", ref="a7", snapshot_id="x3", action="press",
                           confirm_element='[a7] AXButton "Delete"')
    assert reviewed.result["_data"]["status"] == "confirmation_required" and len(effects) == 2
    assert [b["label"] for b in _buttons(reviewed)] == ["Approve", "Decline"]
    assert "confirm_element" in json.dumps(reviewed.ui_components[0])
    quit_ = await _call(task_runtime, "menu", pid=501, path=["TextEdit", "Quit TextEdit"])
    assert quit_.result["_data"]["status"] == "confirmation_required" and len(effects) == 2


async def test_the_gate_refuses_stale_terminal_and_astral_targets_without_a_card(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, _host = await _gate_mac(task_runtime)
    session.remember_ax_snapshot("x9", {"hwnd": 53, "pid": 77, "bundle_id": "com.apple.Terminal", "app": "Terminal",
                                        "title": "zsh"}, {})
    session.remember_ax_snapshot("x8", {"hwnd": 52, "pid": 88, "bundle_id": "com.personalailabs.astraldeep",
                                        "app": "Astral", "title": "Astral"}, {})
    for args, code in (({"ref": "a1", "snapshot_id": "x404", "action": "press"}, "stale_ref"),
                       ({"ref": "a1", "snapshot_id": "x9", "action": "press"}, "terminal_target"),
                       ({"ref": "a1", "snapshot_id": "x8", "action": "press"}, "out_of_range")):
        response = await _call(task_runtime, "ax_action", **args)
        assert response.error and response.error["message"].startswith(code)
    assert not effects and not getattr(orch, "_hitl_pending_calls", {}) and not rc._PENDING_CARDS


async def test_bypass_auto_approves_accessibility_actions_and_keeps_the_floor(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, host = await _gate_mac(task_runtime, remote=True)
    await orch.computer_sessions.set_bypass(host, True)
    response = await _call(task_runtime, "ax_action", ref="a7", snapshot_id="x3", action="press",
                           confirm_element='[a7] AXButton "Delete"')
    assert response.result["_data"]["approval_source"] == "bypass" and effects[-1][0] == "ax_action"
    session.remember_ax_snapshot("x5", {"hwnd": 50, "pid": 60, "bundle_id": "com.1password.1password",
                                        "app": "1Password", "title": "Vault"}, {})
    secure = await _call(task_runtime, "ax_action", ref="a1", snapshot_id="x5", action="set_value", value="hunter2")
    assert secure.error and len(effects) == 1


async def test_a_bypass_frame_right_after_registration_covers_later_cards(task_runtime, monkeypatch):
    orch, ws, effects, *_ = task_runtime
    monkeypatch.setattr(my_computers, "_enabled", lambda: True)
    _registered_controller(orch, ws)
    from tests.test_computer_use_076 import _FakeOrch

    fake = _FakeOrch()
    orch.computer_hosts, orch.computer_sessions = fake.computer_hosts, fake.computer_sessions
    fake.computer_hosts._orch = fake.computer_sessions._orch = orch
    orch.ui_clients = [ws]
    sent = []

    async def safe_send(_socket, raw):
        sent.append(raw)
        return True

    orch._safe_send = safe_send
    orch._get_user_id = lambda socket: GATE_OWNER if socket is ws else None
    orch.agents["computer-use-1"] = object()
    from orchestrator.computer_use_policy import SCOPES

    orch.tool_permissions.get_tool_scope_map.return_value = dict(SCOPES)
    descriptor = ComputerHostDescriptor.from_dict(_descriptor(platform="macos", name="Mac",
                                                              workspace_roots=[{"name": "x", "path": GATE_ROOT}]))
    await Orchestrator._register_computer_host(orch, ws, GATE_OWNER, descriptor)
    host = orch.computer_hosts.host_for_socket(ws)
    payload = {"host_id": host.host_id, "enabled": True}
    _key, _params, notice = await my_computers.HANDLERS["chrome_computer_bypass_set"](orch, ws, GATE_OWNER, ["user"],
                                                                                       payload)
    assert "Bypass on" in json.dumps(notice) and orch.computer_sessions.bypass_on(GATE_OWNER, host.host_id)
    session = await orch.computer_sessions.start(GATE_OWNER, host, ws, GATE_CHAT, wait_for_ack=False)
    session.remember_windows([dict(TEXTEDIT)])
    hc.begin_task(orch, ws, GATE_OWNER, GATE_CHAT)
    response = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    assert response.result["_data"]["approval_source"] == "bypass" and [name for name, _a in effects] == ["type_text"]

    await Orchestrator._register_computer_host(orch, ws, GATE_OWNER, descriptor)
    again = orch.computer_hosts.host_for_socket(ws)
    assert again is not host and orch.computer_sessions.bypass_on(GATE_OWNER, host.host_id)
    frames = [json.loads(raw) for raw in sent if json.loads(raw).get("type") == "computer_session"]
    assert frames[-1]["state"] == "ended" and frames[-1]["bypass"] is True

    orch.computer_sessions = type(orch.computer_sessions)(orch, orch.computer_hosts)
    assert not orch.computer_sessions.bypass_on(GATE_OWNER, host.host_id)
    await Orchestrator._register_computer_host(orch, ws, GATE_OWNER, descriptor)
    restarted = orch.computer_hosts.host_for_socket(ws)
    await my_computers.HANDLERS["chrome_computer_bypass_set"](orch, ws, GATE_OWNER, ["user"], payload)
    assert orch.computer_sessions.bypass_on(GATE_OWNER, restarted.host_id)
    session = await orch.computer_sessions.start(GATE_OWNER, restarted, ws, GATE_CHAT, wait_for_ack=False)
    session.remember_windows([dict(TEXTEDIT)])
    hc.begin_task(orch, ws, GATE_OWNER, GATE_CHAT)
    restored = await _call(task_runtime, "press_keys", keys="enter", hwnd=42)
    assert restored.result["_data"]["approval_source"] == "bypass" and len(effects) == 2

    other = _WS(user=GATE_OWNER)
    orch.ui_sessions[other] = {"sub": GATE_OWNER}
    replaced, _ = orch.computer_hosts.register(GATE_OWNER, other, descriptor)
    assert await orch.computer_sessions.clear_bypass(GATE_OWNER, host.host_id, "host_replaced", replaced) is True
    assert not orch.computer_sessions.bypass_on(GATE_OWNER, host.host_id)


_VALID = {"list_apps": {}, "ax_tree": {}, "ax_find": {"query": "Send"},
          "ax_action": {"ref": "a8", "snapshot_id": "x3", "action": "press"}, "menu": {"path": ["File"]},
          "zoom": {"x": 0, "y": 0, "w": 10, "h": 10}}


@pytest.mark.parametrize("tool", sorted(_VALID))
async def test_every_semantic_tool_needs_a_signed_in_user_and_a_live_session(world, tool):
    unattended = await asyncio.to_thread(getattr(mcp_tools, tool), session_id=CHAT, **_VALID[tool])
    assert unattended["_data"]["code"] == "unattended_refused"
    await world.orch.computer_sessions.end(world.session, "user_stop")
    out = await world.call(tool, **_VALID[tool])
    assert out["_data"]["code"] == "no_session" and world.requests(tool) == []


async def test_a_host_that_acts_on_a_consequential_element_without_a_permit_is_logged(world, caplog):
    await _snapshot(world)
    world.replies["ax_action"] = _ok(element="Delete Mailbox", app="Mail")
    with caplog.at_level("WARNING", logger="ComputerUseTools"):
        await world.call("ax_action", ref="a8", snapshot_id="x3", action="press")
        world.replies["ax_action"] = _ok(element="Save", bundle_id="com.apple.Terminal")
        await world.call("ax_action", ref="a8", snapshot_id="x3", action="press")
    assert sum("without a review permit" in record.message for record in caplog.records) == 2
