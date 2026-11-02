"""Exercises the browser-pane parity tools in agents/computer_use/browser_tools.py (find, hover, zoom, console,
network, evaluate, resize, tabs and the browser_batch composite) against a fake host, the batch's binding to its
reviewed authority, and their review, grant and bypass behavior through the real dispatch gate.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from agents.computer_use import browser_tools
from orchestrator import computer_use_policy as policy
from orchestrator import hitl_confirmation as hc
from orchestrator import remote_confirmation as rc
from tests.test_browser_tools import (
    CHAT, ORIGIN, OWNER, PAGE, _clean_pages as _clean_pages, _error, _snapshot_reply, world as world,
)
from tests.test_computer_grant_flow_102 import (
    OWNER as GATE_OWNER, _buttons, _call, _fresh_pending_cards as _fresh_pending_cards, _mac,
    _payload,
)
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401

IMAGE = {"base64": "/9j/PAGE", "media_type": "image/jpeg", "width": 640, "height": 400}


def _ok(**result):
    return {"ok": True, "result": result}


async def test_find_returns_matching_lines_bound_to_a_snapshot_on_the_current_page(world):
    world.replies["browser_find"] = _ok(snapshot_id="s12", url=PAGE, title="Form", matches=[
        {"ref": "e3", "line": '[e3] button "Submit"\x07'}, {"ref": "x9", "line": "bad"}, "junk", {"line": "no ref"}])
    out = await world.call("browser_find", query="submit", max=3)
    assert world.requests("browser_find")[-1]["args"] == {"query": "submit", "max": 3}
    data = out["_data"]
    assert data["matches"] == [{"ref": "e3", "line": '[e3] button "Submit"'}] and data["count"] == 1
    assert data["snapshot_id"] == "s12" and data["trust"] == "untrusted page content"
    assert browser_tools.page_url(world.session.session_id, {"snapshot_id": "s12"}) == PAGE
    world.replies["browser_find"] = _ok(snapshot_id="s13", matches=[])
    bare = await world.call("browser_find", query="submit")
    assert bare["_data"]["url"] == PAGE and world.requests("browser_find")[-1]["args"]["max"] == 10
    assert browser_tools.page_url(world.session.session_id, {"snapshot_id": "s13"}) == PAGE
    before = len(world.requests())
    for bad in ({"query": ""}, {"query": "x" * 201}, {"query": "a", "max": 21}, {"query": None}):
        assert (await world.call("browser_find", **bad))["_data"]["code"] == "out_of_range"
    assert len(world.requests()) == before
    for bad in (_ok(snapshot_id="bad id", matches=[]), _ok(snapshot_id="s1", matches="x")):
        world.replies["browser_find"] = bad
        assert (await world.call("browser_find", query="a"))["_data"]["code"] == "failed"


async def test_hover_is_snapshot_bound_and_stale_references_point_at_a_new_snapshot(world):
    world.replies["browser_hover"] = _ok(url=PAGE, title="Form")
    out = await world.call("browser_hover", ref="e3", snapshot_id="s9")
    assert world.requests("browser_hover")[-1]["args"] == {"ref": "e3", "snapshot_id": "s9"}
    assert out["_data"]["hovered"] == "e3" and out["_data"]["url"] == PAGE
    world.replies["browser_hover"] = _error("stale_ref", "gone")
    stale = await world.call("browser_hover", ref="e3", snapshot_id="s9")
    assert stale["_data"]["code"] == "stale_ref" and "browser_snapshot" in stale["_data"]["next_action"]
    for bad in ({"ref": "e3"}, {"ref": "e 3", "snapshot_id": "s9"}, {"snapshot_id": "s9"}):
        assert (await world.call("browser_hover", **bad))["_data"]["code"] == "out_of_range"


async def test_zoom_returns_an_untrusted_region_image(world):
    world.replies["browser_zoom"] = _ok(**IMAGE, url=PAGE)
    out = await world.call("browser_zoom", x0=10, y0=20, x1=110, y1=70)
    assert world.requests("browser_zoom")[-1]["args"] == {"x0": 10, "y0": 20, "x1": 110, "y1": 70, "scale": 2}
    assert out["_data"]["region"] == {"x0": 10, "y0": 20, "x1": 110, "y1": 70}
    assert out["_images"][0]["caption"] == browser_tools.IMAGE_CAPTION
    assert out["_ui_components"][0]["id"].startswith("au_cubrowserzoom_")
    await world.call("browser_zoom", x0=0, y0=0, x1=5, y1=5, scale=4)
    assert world.requests("browser_zoom")[-1]["args"]["scale"] == 4
    before = len(world.requests())
    for bad in ({"x0": 10, "y0": 0, "x1": 10, "y1": 5}, {"x0": 0, "y0": 9, "x1": 5, "y1": 2},
                {"x0": 0, "y0": 0, "x1": 5, "y1": 5, "scale": 0}, {"x0": -1, "y0": 0, "x1": 5, "y1": 5}, {}):
        assert (await world.call("browser_zoom", **bad))["_data"]["code"] == "out_of_range"
    assert len(world.requests()) == before
    world.replies["browser_zoom"] = _ok(base64="", media_type="image/jpeg")
    assert (await world.call("browser_zoom", x0=0, y0=0, x1=5, y1=5))["_data"]["code"] == "failed"


async def test_console_entries_are_sanitized_bounded_and_filtered_by_the_host(world):
    entries = [{"level": "error", "message": "boom\x07" + "m" * 3000, "source": PAGE + "?token=abc&x=1", "line": 3,
                "time": "2026-11-01T00:00:00Z"}, {"level": "log", "message": "ok", "line": float("nan")}, "junk"]
    world.replies["browser_console"] = _ok(entries=entries, url=PAGE)
    out = await world.call("browser_console", level="error", limit=5, pattern="boom")
    assert world.requests("browser_console")[-1]["args"] == {"level": "error", "limit": 5, "pattern": "boom"}
    data = out["_data"]
    assert data["count"] == 2 and data["entries"][0]["message"].startswith("boom") and data["truncated"] is False
    assert len(data["entries"][0]["message"]) <= browser_tools._LINE_CHARS + 1
    assert data["entries"][0]["source"] == PAGE + "?token=<redacted>&x=1" and data["entries"][1]["line"] is None
    many = [{"level": "log", "message": "y" * 1900} for _ in range(150)]
    world.replies["browser_console"] = _ok(entries=many)
    bounded = await world.call("browser_console", limit=200)
    assert bounded["_data"]["truncated"] is True and len(json.dumps(bounded["_data"])) < 70_000
    assert world.requests("browser_console")[-1]["args"] == {"level": "all", "limit": 200}
    for bad in ({"level": "warn"}, {"limit": 0}, {"limit": 201}, {"pattern": ""}, {"pattern": "x" * 201}):
        assert (await world.call("browser_console", **bad))["_data"]["code"] == "out_of_range"


async def test_network_requests_redact_credentials_and_keep_numbers(world):
    world.replies["browser_network"] = _ok(url=PAGE, truncated=True, requests=[
        {"url": "https://api.example.org/v1?access_token=s3cr3t&page=2&Session_Id=9", "type": "fetch",
         "method": "GET", "status": 200, "duration_ms": 12.5, "transfer_size": 300},
        {"url": "https://example.org/", "status": True, "duration_ms": float("inf")}])
    out = await world.call("browser_network", limit=10, url_pattern="api")
    assert world.requests("browser_network")[-1]["args"] == {"limit": 10, "url_pattern": "api"}
    first, second = out["_data"]["requests"]
    assert first["url"] == "https://api.example.org/v1?access_token=<redacted>&page=2&Session_Id=<redacted>"
    assert (first["status"], first["duration_ms"], first["transfer_size"]) == (200, 12.5, 300)
    assert second["status"] is None and second["duration_ms"] is None and out["_data"]["truncated"] is True
    assert "s3cr3t" not in json.dumps(out)
    for bad in ({"limit": 0}, {"limit": 101}, {"url_pattern": 5}):
        assert (await world.call("browser_network", **bad))["_data"]["code"] == "out_of_range"


async def test_evaluate_forwards_the_script_and_returns_cleaned_untrusted_json(world):
    deep = current = {}
    for _ in range(30):
        current["n"] = {}
        current = current["n"]
    world.replies["browser_evaluate"] = _ok(url=PAGE, type="object", value={
        "title": "Form\x07", "n": float("nan"), "items": [1, True, None, "x"], "deep": deep})
    out = await world.call("browser_evaluate", script="({title: document.title})")
    assert world.requests("browser_evaluate")[-1]["args"] == {"script": "({title: document.title})"}
    data = out["_data"]
    assert data["value"]["title"] == "Form" and data["value"]["n"] is None
    assert data["value"]["items"] == [1, True, None, "x"] and data["trust"] == "untrusted page content"
    assert data["value_type"] == "object" and data["truncated"] is False and "page data" in data["note"]
    world.replies["browser_evaluate"] = _ok(value="z" * 60_000)
    big = (await world.call("browser_evaluate", script="1"))["_data"]
    assert big["truncated"] is True and isinstance(big["value"], str) and len(big["value"]) == 50_000
    world.replies["browser_evaluate"] = _ok(value=["a"] * 20_000)
    listed = (await world.call("browser_evaluate", script="1"))["_data"]
    assert listed["truncated"] is True and isinstance(listed["value"], str)
    world.replies["browser_evaluate"] = _ok()
    assert (await world.call("browser_evaluate", script="1"))["_data"]["value"] is None
    for bad in ({"script": ""}, {"script": "x" * 8001}, {"script": 4}, {}):
        assert (await world.call("browser_evaluate", **bad))["_data"]["code"] == "out_of_range"


def test_cleaning_stops_at_the_depth_bound_and_stringifies_unknown_values():
    deep = current = {}
    for _ in range(30):
        current["n"] = {}
        current = current["n"]
    cleaned = browser_tools._clean(deep)
    depth = 0
    while isinstance(cleaned, dict):
        cleaned, depth = cleaned.get("n"), depth + 1
    assert depth == browser_tools._MAX_JSON_DEPTH and cleaned is None
    assert browser_tools._clean(float("inf")) is None and browser_tools._clean(b"x") == "b'x'"


async def test_resize_takes_a_preset_or_a_size_and_a_color_scheme(world):
    world.replies["browser_resize"] = _ok(width=390, height=844, url=PAGE)
    out = await world.call("browser_resize", preset="mobile", color_scheme="dark")
    assert world.requests("browser_resize")[-1]["args"] == {"preset": "mobile", "color_scheme": "dark"}
    assert (out["_data"]["width"], out["_data"]["height"], out["_data"]["preset"]) == (390, 844, "mobile")
    await world.call("browser_resize", width=1024, height=768)
    assert world.requests("browser_resize")[-1]["args"] == {"width": 1024, "height": 768}
    await world.call("browser_resize", color_scheme="light")
    assert world.requests("browser_resize")[-1]["args"] == {"color_scheme": "light"}
    before = len(world.requests())
    for bad in ({}, {"preset": "watch"}, {"preset": "mobile", "width": 400, "height": 400}, {"width": 400},
                {"width": 100, "height": 400}, {"width": 400, "height": 5000}, {"color_scheme": "sepia"}):
        assert (await world.call("browser_resize", **bad))["_data"]["code"] == "out_of_range"
    assert len(world.requests()) == before


async def test_tabs_track_the_active_tab_and_a_switch_retires_held_references(world):
    world.replies["browser_snapshot"] = _snapshot_reply()
    await world.call("browser_snapshot")
    browser_tools._hold(world.session.session_id, "s9", ["e3"], '[e3] button "Pay"')
    world.replies["browser_tabs"] = _ok(active_tab_id="t1", tabs=[
        {"tab_id": "t1", "url": PAGE, "title": "Form", "active": True}, {"tab_id": "t 2", "url": PAGE}, "junk"])
    listed = await world.call("browser_tabs", action="list")
    assert listed["_data"]["tabs"] == [{"tab_id": "t1", "url": PAGE, "title": "Form", "active": True}]
    assert listed["_data"]["active_tab_id"] == "t1" and "note" not in listed["_data"]
    page = browser_tools._PAGES[world.session.session_id]
    assert page.tab == "t1" and page.held and page.snapshots
    world.replies["browser_tabs"] = _ok(tabs=[{"tab_id": "t1", "url": PAGE},
                                              {"tab_id": "t2", "url": "https://other.example/", "active": True}])
    created = await world.call("browser_tabs", action="create", url="HTTPS://Other.Example:443/")
    assert world.requests("browser_tabs")[-1]["args"] == {"action": "create", "url": "https://other.example/"}
    assert created["_data"]["active_tab_id"] == "t2" and created["_data"]["url"] == "https://other.example/"
    assert "browser_snapshot" in created["_data"]["note"]
    assert not page.held and not page.snapshots and page.tab == "t2"
    world.replies["browser_tabs"] = _ok(active_tab_id="t2", tabs=[{"tab_id": "t2", "url": "about:blank"}])
    await world.call("browser_tabs", action="close", tab_id="t1")
    assert world.requests("browser_tabs")[-1]["args"] == {"action": "close", "tab_id": "t1"}
    assert browser_tools.page_url(world.session.session_id) == "https://other.example/"
    world.replies["browser_tabs"] = _ok(active_tab_id="t3", tabs=[{"tab_id": "t3", "url": "about:blank"}])
    await world.call("browser_tabs", action="select", tab_id="t3")
    assert browser_tools.page_url(world.session.session_id) is None
    before = len(world.requests())
    for bad, code in (({"action": "open"}, "out_of_range"), ({"action": "select"}, "out_of_range"),
                      ({"action": "close", "tab_id": "t 1"}, "out_of_range"),
                      ({"action": "list", "url": PAGE}, "out_of_range"),
                      ({"action": "create", "url": "javascript:alert(1)"}, "url_rejected")):
        assert (await world.call("browser_tabs", **bad))["_data"]["code"] == code
    assert len(world.requests()) == before


async def test_snapshot_forwards_a_subtree_reference_and_depth(world):
    world.replies["browser_snapshot"] = _snapshot_reply()
    await world.call("browser_snapshot", mode="all", ref="e4", depth=3)
    assert world.requests("browser_snapshot")[-1]["args"] == {"mode": "all", "max_chars": 60000, "depth": 3,
                                                              "ref": "e4"}
    before = len(world.requests())
    for bad in ({"ref": "e4", "scope_ref": "e5"}, {"depth": 0}, {"depth": 31}, {"ref": "bad ref"}):
        assert (await world.call("browser_snapshot", **bad))["_data"]["code"] == "out_of_range"
    assert len(world.requests()) == before


def test_targets_mark_scripts_new_sites_and_reviewed_batches_destructive():
    assert browser_tools.browser_target("browser_tabs", {"action": "create", "url": "https://a.example/x"}, PAGE) == {
        "origin": "https://a.example", "destructive": False}
    assert browser_tools.browser_target("browser_tabs", {"action": "list"}, PAGE) == {"origin": ORIGIN,
                                                                                      "destructive": False}
    assert browser_tools.browser_target("browser_tabs", {"action": "create", "url": "javascript:x"}, PAGE) == {
        "destructive": False}
    assert browser_tools.browser_target("browser_evaluate", {"script": "1"}, PAGE)["destructive"] is True
    quiet = [{"tool": "browser_snapshot"}, {"tool": "browser_click", "args": {"ref": "e1", "snapshot_id": "s1"}}]
    assert browser_tools.browser_target("browser_batch", {"actions": quiet}, PAGE) == {"origin": ORIGIN,
                                                                                       "destructive": False}
    for loud in ([{"tool": "browser_navigate", "args": {"action": "reload"}}], [{"tool": "browser_evaluate"}],
                 [{"tool": "browser_click", "args": {"confirm_element": "[e1] button \"Pay\""}}],
                 [{"tool": "browser_tabs", "args": {"action": "create", "url": PAGE}}], [{"tool": "click"}],
                 "nope", [], [{"tool": "browser_snapshot", "extra": 1}], [{"tool": 5}], [{"tool": "browser_find",
                                                                                          "args": []}]):
        assert browser_tools.browser_target("browser_batch", {"actions": loud}, PAGE)["destructive"] is True, loud


def test_summaries_name_the_script_size_and_the_new_tab_site():
    assert "13-character script" in policy.summary_for("browser_evaluate", {"script": "document.body"}, "Mac")
    assert "0-character" in policy.summary_for("browser_evaluate", {}, "Mac")
    card = policy.summary_for("browser_tabs", {"action": "create", "url": "https://Example.org/a?x=1"}, "Mac")
    assert "https://example.org/a" in card and "x=1" not in card and "Mac" in card
    assert "refused" in policy.summary_for("browser_tabs", {"action": "create", "url": "file:///x"}, "Mac")
    assert policy.summary_for("browser_tabs", {"action": "select"}, "") == (
        "Select tabs in the Astral browser pane on your computer")
    assert policy.summary_for("browser_tabs", {"action": ["x"]}, "Mac").startswith("Manage tabs")


async def test_a_batch_runs_each_step_through_its_own_tool_in_order(world):
    world.replies["browser_snapshot"] = _snapshot_reply()
    world.replies["browser_fill"] = _ok(url=PAGE)
    world.replies["browser_click"] = _ok(url=PAGE)
    world.replies["browser_screenshot"] = _ok(**IMAGE, url=PAGE)
    actions = [{"tool": "browser_snapshot"}, {"tool": "browser_fill", "args": {
        "snapshot_id": "s9", "fields": [{"ref": "e2", "value": "Sam"}]}},
        {"tool": "browser_click", "args": {"ref": "e3", "snapshot_id": "s9"}},
        *[{"tool": "browser_screenshot"} for _ in range(4)]]
    out = await world.call("browser_batch", actions=actions)
    assert [f["verb"] for f in world.requests()] == ["browser_snapshot", "browser_fill", "browser_click",
                                                     *["browser_screenshot"] * 4]
    data = out["_data"]
    assert data["steps"] == data["completed"] == 7 and "stopped_at" not in data
    assert data["results"][1] == {"index": 1, "tool": "browser_fill", "ok": True,
                                  "result": data["results"][1]["result"]}
    assert data["results"][2]["result"]["clicked"] == "e3"
    assert len(out["_images"]) == 3 and len(out["_ui_components"]) == 3
    assert all(item["type"] == "image" for item in out["_ui_components"])


async def test_a_batch_stops_at_the_first_failure_or_review_and_sends_nothing_after_it(world):
    world.replies["browser_snapshot"] = _snapshot_reply()
    world.replies["browser_click"] = _error("confirmation_required", '[e3] button "Pay now"')
    steps = [{"tool": "browser_snapshot"}, {"tool": "browser_click", "args": {"ref": "e3", "snapshot_id": "s9"}},
             {"tool": "browser_scroll"}]
    out = await world.call("browser_batch", actions=steps)
    data = out["_data"]
    assert data["stopped_at"] == 1 and data["completed"] == 1 and len(data["results"]) == 2
    assert data["results"][1]["result"]["code"] == "confirmation_required"
    assert "confirm_element" in data["results"][1]["result"]["next_action"] and "Step 1" in data["next_action"]
    assert world.requests("browser_scroll") == []
    world.replies["browser_scroll"] = _error("timeout", "slow")
    failed = await world.call("browser_batch", actions=[{"tool": "browser_scroll"}, {"tool": "browser_snapshot"}])
    assert failed["_data"]["stopped_at"] == 0 and failed["_data"]["results"][0]["result"]["code"] == "timeout"
    assert failed["_ui_components"] == [] and len(world.requests("browser_snapshot")) == 1
    bad_arg = await world.call("browser_batch", actions=[{"tool": "browser_scroll", "args": {"dy": 0}}])
    assert bad_arg["_data"]["results"][0]["result"]["code"] == "out_of_range"


async def test_a_batch_stops_when_the_page_moves_to_a_site_it_did_not_open(world):
    world.replies["browser_snapshot"] = _snapshot_reply()
    await world.call("browser_snapshot")
    world.replies["browser_click"] = _ok(url="https://elsewhere.example/landing")
    out = await world.call("browser_batch", actions=[
        {"tool": "browser_click", "args": {"ref": "e3", "snapshot_id": "s9"}}, {"tool": "browser_scroll"}])
    data = out["_data"]
    assert data["stopped_at"] == 1 and data["results"][1]["result"]["code"] == "origin_changed"
    assert world.requests("browser_scroll") == []
    world.replies["browser_navigate"] = _ok(url="https://next.example/")
    world.replies["browser_scroll"] = _ok(url="https://next.example/")
    opened = await world.call("browser_batch", actions=[
        {"tool": "browser_navigate", "args": {"url": "https://next.example/"}}, {"tool": "browser_scroll"}])
    assert opened["_data"]["completed"] == 2


@pytest.mark.parametrize("actions", [
    [], [{"tool": "browser_scroll"}] * 21, "x", [{"tool": "click"}], [{"tool": "browser_batch"}],
    [{"tool": "browser_scroll", "args": {"user_id": "someone"}}], [{"tool": "browser_scroll", "args": {"_runtime": 1}}],
    [{"tool": "browser_click", "args": {"ref": "e3", "snapshot_id": "s9", "confirm_element": "x"}}],
    [{"tool": "browser_click", "args": {"permit": {"ref": "e3"}}}], [["browser_scroll"]],
    [{"tool": "browser_scroll", "args": "dy"}], [{"tool": "browser_scroll", "extra": True}],
])
async def test_a_batch_validates_every_step_before_running_any(world, actions):
    out = await world.call("browser_batch", actions=actions)
    assert out["_data"]["code"] == "out_of_range" and world.requests() == []


async def test_a_batch_needs_a_signed_in_user_and_a_live_session(world):
    assert (await asyncio.to_thread(browser_tools.browser_batch, session_id=CHAT,
                                    actions=[{"tool": "browser_scroll"}]))["_data"]["code"] == "unattended_refused"
    await world.orch.computer_sessions.end(world.session, "user_stop")
    assert (await world.call("browser_batch", actions=[{"tool": "browser_scroll"}]))["_data"]["code"] == "no_session"


async def test_batch_steps_carry_the_batch_approval_and_nothing_else_does(world, monkeypatch):
    call = hc.PendingCall("r1", OWNER, CHAT, "computer-use-1", "browser_batch", "{}", (), time.monotonic() + 60,
                          computer_authority=SimpleNamespace(session=world.session, websocket=None,
                                                             generation=world.session.authority_generation))
    approval = hc.Approval(world.orch, call, consumed=True, effect_started=True)
    monkeypatch.setattr(hc, "_current_session", lambda *_args, **_kwargs: True)
    world.replies["browser_snapshot"] = _snapshot_reply()
    world.replies["browser_scroll"] = _ok(url=PAGE)
    token = hc._APPROVAL.set(approval)
    try:
        out = await world.call("browser_batch", actions=[{"tool": "browser_snapshot"}, {"tool": "browser_scroll"}])
        direct = await world.call("browser_scroll")
    finally:
        hc._APPROVAL.reset(token)
    assert out["_data"]["completed"] == 2 and len(world.requests("browser_scroll")) == 1
    assert direct["_data"]["code"] == "confirmation_required"
    step = browser_tools._BatchStep(approval, world.session, "browser_scroll")
    marker = browser_tools._BATCH.set(step)
    try:
        assert browser_tools.batch_effect_matches(approval, world.session, "browser_scroll")
        assert not browser_tools.batch_effect_matches(approval, world.session, "browser_click")
        assert not browser_tools.batch_effect_matches(object(), world.session, "browser_scroll")
        assert not browser_tools.batch_effect_matches(approval, object(), "browser_scroll")
        step.active = False
        assert not browser_tools.batch_effect_matches(approval, world.session, "browser_scroll")
    finally:
        browser_tools._BATCH.reset(marker)
    assert not browser_tools.batch_effect_matches(approval, world.session, "browser_scroll")


def test_registry_describes_the_parity_tools_and_registers_the_batch_as_a_composite():
    from agents.computer_use import mcp_tools

    reg = mcp_tools.TOOL_REGISTRY
    batch = reg["browser_batch"]
    assert (batch["tier"], batch["scope"], batch["timeout"]) == ("input", "tools:write", 120.0)
    assert "browser_batch" in policy.COMPOSITE_VERBS and "browser_batch" not in policy.HOST_VERBS
    assert batch["input_schema"]["required"] == ["actions"] and "20" in batch["description"]
    assert reg["browser_evaluate"]["destructive"] == "always" and reg["browser_tabs"]["destructive"] == "always"
    assert reg["browser_evaluate"]["tier"] == "consequential"
    assert "mode all" in reg["browser_snapshot"]["description"] and "read_page" in reg["browser_snapshot"]["description"]
    assert "ref" in reg["browser_snapshot"]["input_schema"]["properties"]
    for name in ("browser_find", "browser_zoom", "browser_console", "browser_network"):
        assert reg[name]["tier"] == "observe"
    for name in ("browser_hover", "browser_resize", "browser_tabs"):
        assert reg[name]["tier"] == "input"


async def _browser_mac(task_runtime, **kwargs):
    from orchestrator.computer_use_policy import SCOPES

    session, host = await _mac(task_runtime, **kwargs)
    task_runtime[0].tool_permissions.get_tool_scope_map.return_value = dict(SCOPES)
    return session, host


def _texts(response):
    return json.dumps(response.ui_components[0])


async def test_evaluate_needs_one_exact_review_and_runs_once_after_approval(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, _host = await _browser_mac(task_runtime, remote=True)
    browser_tools._note_page(session.session_id, PAGE, "s1")
    first = await _call(task_runtime, "browser_evaluate", script="document.title")
    assert first.result["_data"]["status"] == "confirmation_required" and not effects
    card = _texts(first)
    assert "document.title" in card and "14-character script" in card
    assert [b["label"] for b in _buttons(first)] == ["Approve", "Decline"]
    await rc.handle_decision(orch, ws, GATE_OWNER, _payload(first, "approve"))
    assert [name for name, _args in effects] == ["browser_evaluate"] and not orch._hitl_pending_calls


async def test_bypass_runs_evaluate_and_batches_with_an_audited_bypass_source(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, host = await _browser_mac(task_runtime, remote=True)
    await orch.computer_sessions.set_bypass(host, True)
    evaluated = await _call(task_runtime, "browser_evaluate", script="1")
    batched = await _call(task_runtime, "browser_batch", actions=[
        {"tool": "browser_navigate", "args": {"url": "https://example.org/"}}, {"tool": "browser_snapshot"}])
    assert evaluated.result["_data"]["approval_source"] == batched.result["_data"]["approval_source"] == "bypass"
    assert [name for name, _args in effects] == ["browser_evaluate", "browser_batch"]
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


async def test_a_new_tab_at_a_site_uses_the_navigation_origin_approval(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, _host = await _browser_mac(task_runtime, remote=True)
    first = await _call(task_runtime, "browser_tabs", action="create", url="https://example.org/a")
    assert first.result["_data"]["status"] == "confirmation_required" and "https://example.org/a" in _texts(first)
    await rc.handle_decision(orch, ws, GATE_OWNER, _payload(first, "approve"))
    assert len(effects) == 1 and not orch._hitl_pending_calls
    grant, = orch.computer_sessions.grants.list(session.session_id)
    assert dict(grant.scope) == {"kind": "origin", "origin": "https://example.org"}
    browser_tools._note_page(session.session_id, "https://example.org/a", "s1")
    assert (await _call(task_runtime, "browser_tabs", action="list")).result["_data"]["ok"]
    assert (await _call(task_runtime, "browser_hover", ref="e1", snapshot_id="s1")).result["_data"]["ok"]
    quiet = await _call(task_runtime, "browser_batch", actions=[
        {"tool": "browser_snapshot"}, {"tool": "browser_click", "args": {"ref": "e1", "snapshot_id": "s1"}}])
    assert quiet.result["_data"]["ok"] and len(effects) == 4
    loud = await _call(task_runtime, "browser_batch", actions=[
        {"tool": "browser_navigate", "args": {"url": "https://example.org/b"}}])
    assert loud.result["_data"]["status"] == "confirmation_required" and len(effects) == 4
    assert [b["label"] for b in _buttons(loud)] == ["Approve", "Decline"]


async def test_a_batch_keeps_its_result_bounded_and_says_which_outputs_were_omitted(world):
    world.replies["browser_get_text"] = _ok(text="t" * 59_000, url=PAGE, title="Form")
    out = await world.call("browser_batch", actions=[{"tool": "browser_get_text"} for _ in range(4)])
    data = out["_data"]
    assert data["completed"] == 4 and len(json.dumps(data)) < 200_000
    assert "omitted" not in data["results"][0]["result"] and "omitted" in data["results"][3]["result"]
    assert data["results"][3]["result"]["url"] == PAGE and data["results"][3]["ok"] is True


async def test_a_new_tab_at_a_refused_address_never_gets_a_card(task_runtime):
    orch, *_ = task_runtime
    await _browser_mac(task_runtime, remote=True)
    response = await _call(task_runtime, "browser_tabs", action="create", url="javascript:alert(1)")
    assert response.error and "url_rejected" in response.error["message"]
    assert not [c for c in (response.ui_components or []) if c.get("type") == "card"]


async def test_a_batch_never_starts_a_step_that_could_outlast_its_own_deadline(world, monkeypatch):
    monkeypatch.setattr(browser_tools, "_BATCH_MARGIN_S", policy.TIMEOUTS["browser_batch"] - 8.0)
    world.replies["browser_scroll"] = _ok(url=PAGE)
    out = await world.call("browser_batch", actions=[{"tool": "browser_scroll"}, {"tool": "browser_wait",
                                                                                "args": {"for": "load"}}])
    data = out["_data"]
    assert data["completed"] == 1 and data["stopped_at"] == 1
    assert data["results"][1]["result"]["code"] == "batch_deadline" and world.requests("browser_wait") == []
