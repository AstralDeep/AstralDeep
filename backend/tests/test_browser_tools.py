"""Tests for backend/agents/computer_use/browser_tools.py: the ten browser_* tools, their
registry entries, host error surfacing, the consequential-control handshake, and the origin
helpers the grant gate uses.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agents.computer_use import browser_tools, mcp_tools
from orchestrator import computer_use_policy as policy
from orchestrator import datamarking
from orchestrator.computer_grants import GrantStore
from orchestrator.orchestrator import Orchestrator
from shared.protocol import MCPRequest
from tests.test_computer_use_076 import (
    OWNER,
    _AgentOrch,
    _FakeHostSocket,
    _WS,
    _descriptor,
    _session,
)
from shared.protocol import ComputerHostDescriptor

CHAT = "chat-1"
PAGE = "https://example.org/form"
ORIGIN = "https://example.org"
BROWSER_TOOLS = sorted(policy.BROWSER_VERBS)
CTX = {"user_id": OWNER, "session_id": CHAT}


@pytest.fixture(autouse=True)
def _clean_pages():
    browser_tools._PAGES.clear()
    yield
    browser_tools._PAGES.clear()


class World:
    def __init__(self, orch, host, host_ws, session, replies):
        self.orch, self.host, self.host_ws, self.session, self.replies = orch, host, host_ws, session, replies

    async def call(self, tool, **kwargs):
        return await asyncio.to_thread(getattr(browser_tools, tool), **CTX, **kwargs)

    def requests(self, verb=None):
        frames = self.host_ws.frames("computer_request")
        return [f for f in frames if verb is None or f["verb"] == verb]


def _snapshot_reply(**over):
    result = {"snapshot_id": "s9", "url": PAGE, "title": "Form", "frames": [{"id": "f0", "url": PAGE}],
              "lines": ['[e1] heading "Order"', '[e2] textbox "Name" value=""', '[e3] button "Submit"'],
              "truncated": False, "pending_dialog": None}
    result.update(over)
    return {"ok": True, "result": result}


def _error(code, message="refused"):
    return {"ok": False, "error": {"code": code, "message": message}}


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
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(_descriptor()))
    controller = orch.add(_WS(chat=CHAT))
    session = await _session(orch, host, controller, CHAT)
    return World(orch, host, host_ws, session, replies)


_BIDI = "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"


def test_canonical_browser_url_lowercases_the_scheme_and_host_and_drops_default_ports():
    canonical = policy.canonical_browser_url
    assert canonical("HTTPS://Example.ORG:443/Path/Up?Q=1#Frag") == "https://example.org/Path/Up?Q=1#Frag"
    assert canonical("HTTP://example.org:80") == "http://example.org"
    assert canonical("http://example.org:443/") == "http://example.org:443/"
    assert canonical("https://example.org:80/") == "https://example.org:80/"
    assert canonical("https://example.org:8443/x") == "https://example.org:8443/x"
    assert canonical("https://example.org:/x") == "https://example.org/x"
    assert canonical("https://sub-1.my_host.example./x") == "https://sub-1.my_host.example./x"
    assert canonical("https://good.example/a@evil.example") == "https://good.example/a@evil.example"
    assert canonical("https://good.example?x=@evil.example") == "https://good.example?x=@evil.example"
    assert canonical("https://good.example#@evil.example") == "https://good.example#@evil.example"
    assert canonical("https://xn--e1awd7f.example/") == "https://xn--e1awd7f.example/"


def test_canonical_browser_url_preserves_ipv6_literals():
    canonical = policy.canonical_browser_url
    assert canonical("http://[::1]:8080/x") == "http://[::1]:8080/x"
    assert canonical("HTTPS://[2001:DB8::1]:443/") == "https://[2001:db8::1]/"
    assert canonical("http://[::1]") == "http://[::1]"
    for bad in ("http://[::1", "http://[::1%25en0]/", "http://[::1%en0]/", "http://[zzzz::1]/", "http://[::1]:99999/"):
        assert canonical(bad) is None


@pytest.mark.parametrize("raw", [
    "https://good.example\\@evil.example/",
    "https://good.example\\.evil.example/",
    "https:\\\\good.example/",
    "https://good.example:80@evil.example/",
    "https://user@evil.example/",
    "https://user:pw@example.org/",
    "https://@example.org/",
    "https://good.example/\tx", "https://good.example\t/", "https://good\n.example/", "https://good.example/\r\nx",
    " https://example.org/", "https://example.org/ ", "https://exa mple.org/", "https://example.org/a b",
    "https://example.org/\x00", "https://example.org/\x7f", "https://example.org/\x1b[0m",
    "https://example.org/\u202e/gpj.exe", "https://example.org/\u200eevil", "https://ex\u2066ample.org/",
    "https://example.org/\u2069", "https://example.org/\u200f", "https://example.org/" + _BIDI,
    "https://exаmple.org/", "https://münchen.example/", "https://example.org/é",
    "https://exa%6dple.org/", "https://example.org%2eevil.example/", "https://exa$ple.org/", "https://.example/",
    "https://example.org:0/", "https://example.org:99999/", "https://example.org:http/",
    "https:good.example", "https:/good.example", "https:///good.example", "//good.example", "good.example",
    "file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi", "ftp://example.org/", "about:blank",
    "ws://example.org/", "https://", "", "x" * 2100, "https://example.org/" + "a" * 2100,
    None, 7, ["https://example.org/"], b"https://example.org/",
])
def test_canonical_browser_url_rejects_every_parser_differential(raw):
    assert policy.canonical_browser_url(raw) is None
    assert browser_tools.origin_of(raw) is None


def test_the_card_origin_label_comes_from_the_canonical_url_and_is_empty_when_rejected():
    assert policy._origin_label("HTTPS://Example.org:443/x") == "https://example.org"
    assert policy._origin_label("http://[::1]:8080/") == "http://[::1]:8080"
    for raw in ("https://good.example\\@evil.example/", "https://u:p@example.org/", "https://exаmple.org/", None):
        assert policy._origin_label(raw) == ""


def test_origin_of_derives_from_the_canonical_url_only():
    for raw in ("https://Example.org:443/a?b#c", "https://example.org"):
        assert browser_tools.origin_of(raw) == "https://example.org"
    assert browser_tools.origin_of("http://Example.org:80/") == "http://example.org"
    assert browser_tools.origin_of("https://example.org\\@evil.example/") is None


def test_origin_of_normalizes_http_origins_and_refuses_everything_else():
    assert browser_tools.origin_of("https://Example.ORG/a/b?q=1#f") == "https://example.org"
    assert browser_tools.origin_of("HTTP://example.org:80/x") == "http://example.org"
    assert browser_tools.origin_of("https://example.org:443") == "https://example.org"
    assert browser_tools.origin_of("https://example.org:8443/x") == "https://example.org:8443"
    assert browser_tools.origin_of("http://[::1]:8080/x") == "http://[::1]:8080"
    for refused in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi", "ftp://example.org",
                    "about:blank", "example.org", "https://", "//example.org", "https://user:pw@example.org/",
                    "https://example.org:99999/", "https://exa mple.org/", "https://example.org/\nx",
                    "https://example.org/" + "a" * 2100, "http://[::1/", "", None, 7, ["https://example.org"]):
        assert browser_tools.origin_of(refused) is None


@pytest.mark.parametrize("verb", sorted(policy.BROWSER_VERBS & {
    "browser_navigate", "browser_click", "browser_fill", "browser_press", "browser_scroll", "browser_dialog"}))
def test_origin_of_agrees_with_the_origin_grant_store(verb):
    store = GrantStore()
    url = "HTTPS://Example.org:443/path?q=1"
    grant = store.create(OWNER, "host", "sess", CHAT, "task", {"kind": "origin", "origin": browser_tools.origin_of(url)},
                         ("browser",), now=1000.0)
    args = {"url": url} if verb == "browser_navigate" else {}
    target = browser_tools.browser_target(verb, args, url)
    found = store.match("sess", verb, target, now=1001.0, owner_id=OWNER, host_id="host", chat_id=CHAT)
    assert found == grant
    other = browser_tools.browser_target(verb, {"url": "https://evil.example/"} if verb == "browser_navigate" else {},
                                         "https://evil.example/")
    assert store.match("sess", verb, other, now=1001.0, owner_id=OWNER, host_id="host", chat_id=CHAT) is None


def test_browser_target_uses_the_url_for_navigation_and_the_page_for_everything_else():
    assert browser_tools.browser_target("browser_navigate", {"url": "https://a.example/x"}, PAGE) == {
        "origin": "https://a.example", "destructive": False}
    assert browser_tools.browser_target("browser_navigate", {"action": "reload"}, PAGE) == {
        "origin": ORIGIN, "destructive": False}
    assert browser_tools.browser_target("browser_click", {"ref": "e3", "snapshot_id": "s9"}, PAGE) == {
        "origin": ORIGIN, "destructive": False}
    assert browser_tools.browser_target("browser_scroll", {}, None) == {"destructive": False}
    assert browser_tools.browser_target("browser_dialog", {"action": "accept"}, "about:blank") == {"destructive": False}


def test_an_invalid_navigation_url_never_borrows_the_current_page_origin():
    for bad in ("file:///etc/hosts", "javascript:alert(1)", "https://user:pw@example.org/", 7, "",
                "https://example.org\\@evil.example/", "https://example.org/\u202e"):
        assert browser_tools.browser_target("browser_navigate", {"url": bad}, PAGE) == {"destructive": False}


def test_a_confirm_element_makes_click_and_fill_targets_destructive_so_no_grant_covers_them():
    line = '[e3] button "Pay now"'
    for verb in ("browser_click", "browser_fill"):
        args = {"ref": "e3", "snapshot_id": "s9", "confirm_element": line}
        assert browser_tools.is_consequential(verb, args) is True
        assert browser_tools.browser_target(verb, args, PAGE) == {"origin": ORIGIN, "destructive": True}
    store = GrantStore()
    store.create(OWNER, "host", "sess", CHAT, "task", {"kind": "origin", "origin": ORIGIN}, ("browser",), now=1.0)
    target = browser_tools.browser_target("browser_click", {"confirm_element": line}, PAGE)
    assert store.match("sess", "browser_click", target, now=2.0, owner_id=OWNER, host_id="host", chat_id=CHAT) is None
    for args in ({}, {"confirm_element": ""}, {"confirm_element": "   "}, {"confirm_element": 7}):
        assert browser_tools.is_consequential("browser_click", args) is False
    assert browser_tools.is_consequential("browser_press", {"confirm_element": line}) is False
    assert browser_tools.is_consequential("browser_navigate", {"confirm_element": line}) is False


def test_page_url_follows_the_snapshot_a_click_names_and_never_guesses_for_unknown_ones():
    assert browser_tools.page_url("sess") is None
    browser_tools._note_page("sess", PAGE, "s9")
    browser_tools._note_page("sess", "https://other.example/next", "s10")
    assert browser_tools.page_url("sess") == "https://other.example/next"
    assert browser_tools.page_url("sess", {"snapshot_id": "s9"}) == PAGE
    assert browser_tools.page_url("sess", {"snapshot_id": "s404"}) is None
    browser_tools._note_page("sess", "about:blank")
    assert browser_tools.page_url("sess") == "https://other.example/next"
    browser_tools.forget("sess")
    assert browser_tools.page_url("sess") is None
    browser_tools.forget("sess")


def test_page_state_is_bounded():
    for i in range(browser_tools._MAX_PAGES + 10):
        browser_tools._note_page(f"sess-{i}", PAGE, "s1")
    assert len(browser_tools._PAGES) == browser_tools._MAX_PAGES
    assert browser_tools.page_url("sess-0") is None
    for i in range(browser_tools._MAX_SNAPSHOTS + 5):
        browser_tools._note_page("one", f"https://example.org/{i}", f"s{i}")
    assert len(browser_tools._PAGES["one"].snapshots) == browser_tools._MAX_SNAPSHOTS
    for i in range(browser_tools._MAX_HELD + 5):
        browser_tools._hold("one", "s1", [f"e{i}"], f'[e{i}] button "Pay"')
    assert len(browser_tools._PAGES["one"].held) == browser_tools._MAX_HELD


def test_register_adds_the_ten_tools_with_policy_scopes_tiers_and_timeouts():
    registry = {}
    browser_tools.register(registry)
    assert sorted(registry) == BROWSER_TOOLS and len(registry) == 10
    for name, entry in registry.items():
        assert entry["scope"] == policy.SCOPES[name]
        assert entry["tier"] == policy.TIERS[name]
        assert entry["timeout"] == policy.TIMEOUTS[name]
        assert entry["retryable"] is False
        assert entry["function"] is getattr(browser_tools, name)
        schema = entry["input_schema"]
        assert schema["type"] == "object" and "computer" in schema["properties"]
        assert set(schema["required"]) <= set(schema["properties"])
        if name in policy.DESTRUCTIVE_CLASSIFICATION:
            assert entry["destructive"] is policy.DESTRUCTIVE_CLASSIFICATION[name]
        else:
            assert "destructive" not in entry
    assert registry["browser_navigate"]["destructive"] == "always"
    assert registry["browser_snapshot"]["scope"] == "tools:read"
    assert registry["browser_click"]["scope"] == "tools:write"
    assert registry["browser_click"]["input_schema"]["required"] == ["ref", "snapshot_id"]
    assert registry["browser_fill"]["input_schema"]["required"] == ["fields", "snapshot_id"]
    assert registry["browser_wait"]["input_schema"]["required"] == ["for"]
    assert registry["browser_press"]["input_schema"]["required"] == ["keys"]
    assert registry["browser_dialog"]["input_schema"]["required"] == ["action"]


def test_descriptions_state_the_pane_contract():
    registry = {}
    browser_tools.register(registry)
    snapshot = registry["browser_snapshot"]["description"]
    assert "browser pane" in snapshot and "e12" in snapshot and "snapshot_id" in snapshot
    assert "pixel" in snapshot and "Astral's own windows" in snapshot
    assert "user's own browser" in snapshot and "desktop path" in snapshot
    assert "untrusted" in snapshot.lower()
    assert "snapshot_id" in registry["browser_click"]["description"]
    assert "snapshot_id" in registry["browser_fill"]["description"]
    assert "password" in registry["browser_fill"]["description"].lower()
    assert "confirm_element" in registry["browser_click"]["description"]
    assert registry["browser_click"]["input_schema"]["properties"]["confirm_element"]["type"] == "string"
    assert registry["browser_fill"]["input_schema"]["properties"]["confirm_element"]["type"] == "string"


def test_the_agent_registry_carries_the_browser_tools_with_strict_policy_equality():
    assert set(policy.BROWSER_VERBS) <= set(mcp_tools.TOOL_REGISTRY)
    for name in BROWSER_TOOLS:
        assert mcp_tools.TOOL_REGISTRY[name]["scope"] == policy.SCOPES[name]
    assert set(mcp_tools.TOOL_REGISTRY) == policy.ALL_VERBS


def test_summary_for_names_the_canonical_origin():
    card = policy.summary_for("browser_navigate", {"url": "https://Example.org:443/login?next=/a"}, "RYZENROLL")
    assert "https://example.org/login" in card and "RYZENROLL" in card and "browser pane" in card
    assert "next=" not in card and "?…" in card and ":443" not in card
    custom = policy.summary_for("browser_navigate", {"url": "http://localhost:8080/app"}, "Mac")
    assert "http://localhost:8080" in custom


@pytest.mark.parametrize("raw", [
    "https://user:hunter2@example.org/", "https://good.example\\@evil.example/", "https://good.example:80@evil.example/",
    "https://good.example/\tx@evil.example", "https://good\n.example/", "https://example.org/\u202e/gpj.exe",
    "https://exаmple.org/", "javascript:alert(1)", "file:///etc/passwd",
])
def test_summary_for_a_rejected_address_says_it_will_be_refused_and_shows_no_host(raw):
    card = policy.summary_for("browser_navigate", {"url": raw}, "Mac")
    assert "refused" in card and "nothing will load" in card
    for leaked in ("good", "evil", "example", "hunter2", "gpj", "localhost", "etc/passwd", "alert"):
        assert leaked not in card


def test_summary_for_navigation_actions_and_refused_addresses():
    for action in ("back", "forward", "reload"):
        assert action in policy.summary_for("browser_navigate", {"action": action}, "Mac").lower()
    assert "Navigate" in policy.summary_for("browser_navigate", {"action": ["x"]}, "Mac")
    bad = policy.summary_for("browser_navigate", {"url": "javascript:alert(1)\n\x00"}, "Mac")
    assert "refused" in bad and "\n" not in bad and "\x00" not in bad
    assert "browser pane on your computer" in policy.summary_for("browser_navigate", {}, "")
    assert "your computer" in policy.summary_for("browser_navigate", {"url": ORIGIN}, "")


def test_summary_for_the_consequential_browser_card():
    click = policy.summary_for("browser_click", {"ref": "e3", "confirm_element": '[e3] button "Pay now"'}, "Mac")
    assert '[e3] button "Pay now"' in click and "Mac" in click and "can't undo" in click
    fill = policy.summary_for("browser_fill", {"confirm_element": '[e2] textbox "Total"'}, "Mac")
    assert '[e2] textbox "Total"' in fill and "Mac" in fill
    assert policy.summary_for("browser_click", {"ref": "e3"}, "Mac") == "browser_click on Mac"
    assert policy.summary_for("browser_click", {"confirm_element": 7}, "Mac") == "browser_click on Mac"
    long = policy.summary_for("browser_click", {"confirm_element": "x" * 900}, "Mac")
    assert len(long) < 500
    spoofed = policy.summary_for("browser_click", {"confirm_element": f'[e3] button "{_BIDI}Pay\x07 now"\n'}, "Mac")
    assert '[e3] button "Pay now"' in spoofed
    assert not any(ch in spoofed for ch in _BIDI) and "\x07" not in spoofed and "\n" not in spoofed


async def test_snapshot_forwards_the_bounded_request_and_returns_untrusted_lines_without_a_digest(world):
    world.replies["browser_snapshot"] = _snapshot_reply()
    out = await world.call("browser_snapshot")
    assert world.requests("browser_snapshot")[-1]["args"] == {"mode": "interactive", "max_chars": 60000}
    data = out["_data"]
    assert data["snapshot_id"] == "s9" and data["url"] == PAGE and data["title"] == "Form"
    assert data["lines"][2] == '[e3] button "Submit"' and data["truncated"] is False
    assert data["frames"] == [{"id": "f0", "url": PAGE}] and data["pending_dialog"] is None
    assert data["computer"] == "RYZENROLL" and data["trust"] == "untrusted page content"
    assert "_model_digest" not in out and "_model_digest" not in data and out["_ui_components"] == []
    assert browser_tools.page_url(world.session.session_id, {"snapshot_id": "s9"}) == PAGE

    res = SimpleNamespace(error=None, result=out)
    assert Orchestrator._result_has_model_digest(res) is False
    content = Orchestrator._tool_result_to_llm_content(res, "browser_snapshot")
    marked = datamarking.spotlight(content, "SENTINEL")
    assert marked.startswith("<<UNTRUSTED SENTINEL>>") and '[e3] button \\"Submit\\"' in marked


async def test_snapshot_modes_scope_and_limits(world):
    world.replies["browser_snapshot"] = _snapshot_reply()
    await world.call("browser_snapshot", mode="all", scope_ref="e12", max_chars=2000)
    assert world.requests("browser_snapshot")[-1]["args"] == {"mode": "all", "scope_ref": "e12", "max_chars": 2000}
    before = len(world.requests())
    for bad in ({"mode": "tree"}, {"scope_ref": "e1; drop"}, {"scope_ref": 3}, {"max_chars": 999},
                {"max_chars": 60001}, {"max_chars": "x"}):
        assert (await world.call("browser_snapshot", **bad))["_data"]["code"] == "out_of_range"
    assert len(world.requests()) == before


async def test_snapshot_lines_are_sanitized_and_bounded_to_sixty_thousand_characters(world):
    long_line = "[e1] text " + "x" * 5000
    many = [f'[e{i}] link "{"y" * 1500}"' for i in range(2, 80)]
    world.replies["browser_snapshot"] = _snapshot_reply(
        lines=["[e0] \x00heading\x07 ok", long_line, 7, None, *many], title="T" * 900, url=PAGE + "?" + "q" * 4000,
        frames=[{"id": f"f{i}", "url": PAGE} for i in range(50)] + ["bad"],
        pending_dialog={"type": "confirm", "message": "Sure?" * 300, "default_text": "d" * 900})
    data = (await world.call("browser_snapshot"))["_data"]
    assert data["lines"][0] == "[e0] heading ok"
    assert len(data["lines"][1]) <= browser_tools._LINE_CHARS + 1 and data["lines"][1].endswith("…")
    assert data["truncated"] is True
    assert sum(len(line) + 1 for line in data["lines"]) <= browser_tools.MAX_PAGE_CHARS
    assert len(data["title"]) <= browser_tools._TITLE_CHARS + 1
    assert data["url"] is None or len(data["url"]) <= browser_tools._URL_CHARS
    assert len(data["frames"]) == browser_tools._MAX_FRAMES
    dialog = data["pending_dialog"]
    assert dialog["type"] == "confirm" and len(dialog["message"]) <= 501 and len(dialog["default_text"]) <= 201
    assert len(json.dumps(data)) < 70000


async def test_snapshot_keeps_the_host_truncation_flag_and_refuses_a_malformed_snapshot(world):
    world.replies["browser_snapshot"] = _snapshot_reply(truncated=True, lines=[])
    assert (await world.call("browser_snapshot"))["_data"]["truncated"] is True
    for bad in (_snapshot_reply(snapshot_id="s9; drop"), _snapshot_reply(snapshot_id=None),
                {"ok": True, "result": {"snapshot_id": "s1", "lines": "not a list"}}):
        world.replies["browser_snapshot"] = bad
        out = await world.call("browser_snapshot")
        assert out["_data"]["code"] == "failed"


async def test_get_text_is_bounded_to_sixty_thousand_characters(world):
    world.replies["browser_get_text"] = {"ok": True, "result": {"text": "a\x00b" + "z" * 70000, "url": PAGE,
                                                                  "title": "Form", "truncated": False}}
    out = await world.call("browser_get_text", ref="e5")
    assert world.requests("browser_get_text")[-1]["args"] == {"ref": "e5", "max_chars": 60000}
    data = out["_data"]
    assert data["text"].startswith("ab") and len(data["text"]) == browser_tools.MAX_PAGE_CHARS + 1
    assert data["truncated"] is True and data["url"] == PAGE and "_model_digest" not in out
    world.replies["browser_get_text"] = {"ok": True, "result": {"text": "short"}}
    short = (await world.call("browser_get_text", max_chars=1500))["_data"]
    assert short["text"] == "short" and short["truncated"] is False and short["url"] is None
    assert world.requests("browser_get_text")[-1]["args"] == {"max_chars": 1500}
    world.replies["browser_get_text"] = {"ok": True, "result": {"text": "x" * 20, "truncated": True}}
    assert (await world.call("browser_get_text"))["_data"]["truncated"] is True
    assert (await world.call("browser_get_text", ref="bad ref"))["_data"]["code"] == "out_of_range"
    assert (await world.call("browser_get_text", max_chars=5))["_data"]["code"] == "out_of_range"
    world.replies["browser_get_text"] = {"ok": True, "result": {"text": 5}}
    assert (await world.call("browser_get_text"))["_data"]["text"] == ""


async def test_get_text_of_a_secret_field_is_refused_with_guidance(world):
    world.replies["browser_get_text"] = _error("secret_field", "e4 is a password field")
    out = await world.call("browser_get_text", ref="e4")
    data = out["_data"]
    assert data["status"] == "refused" and data["code"] == "secret_field"
    assert "never reads or fills" in data["next_action"] and "themselves" in data["next_action"]
    assert out["_ui_components"][0]["type"] == "alert" and out["_ui_components"][0]["variant"] == "warning"


async def test_screenshot_attaches_the_image_captioned_as_untrusted_page_content(world):
    world.replies["browser_screenshot"] = {"ok": True, "result": {
        "width": 1280, "height": 720, "media_type": "image/jpeg", "base64": "/9j/AAAA", "url": PAGE, "title": "Form"}}
    out = await world.call("browser_screenshot")
    assert world.requests("browser_screenshot")[-1]["args"] == {"max_width": 1280}
    assert out["_images"] == [{"media_type": "image/jpeg", "base64": "/9j/AAAA", "caption": "UNTRUSTED page content"}]
    image = out["_ui_components"][0]
    assert image["type"] == "image" and image["id"] == f"au_cubrowser_{world.session.session_id}"
    assert image["url"] == "data:image/jpeg;base64,/9j/AAAA" and "UNTRUSTED page content" in image["alt"]
    assert "base64" not in json.dumps(out["_data"]) and out["_data"]["width"] == 1280
    assert world.session.last_screenshot is None
    await world.call("browser_screenshot", max_width=800, ref="e7")
    assert world.requests("browser_screenshot")[-1]["args"] == {"max_width": 800, "ref": "e7"}


async def test_screenshot_validation_failures_and_non_visual_sessions(world):
    for bad in ({"max_width": 100}, {"max_width": 5000}, {"ref": "no good"}):
        assert (await world.call("browser_screenshot", **bad))["_data"]["code"] == "out_of_range"
    for bad in ({"ok": True, "result": {"base64": ""}},
                {"ok": True, "result": {"base64": "AAAA", "media_type": "image/svg+xml"}},
                {"ok": True, "result": {"base64": "A" * (browser_tools._MAX_IMAGE_B64 + 1)}}):
        world.replies["browser_screenshot"] = bad
        assert (await world.call("browser_screenshot"))["_data"]["code"] == "failed"
    world.session.images_supported = False
    world.replies["browser_screenshot"] = {"ok": True, "result": {"base64": "/9j/AAAA", "width": "x"}}
    out = await world.call("browser_screenshot")
    assert out["_images"] == [] and "cannot see images" in out["_data"]["note"] and out["_data"]["width"] == 0


async def test_wait_validates_and_forwards_each_condition(world):
    world.replies["browser_wait"] = {"ok": True, "result": {"matched": True, "url": PAGE, "title": "Form"}}
    out = await world.call("browser_wait", **{"for": "load"})
    assert world.requests("browser_wait")[-1]["args"] == {"for": "load", "seconds": 10.0}
    assert out["_data"]["matched"] is True and out["_data"]["url"] == PAGE
    await world.call("browser_wait", **{"for": "text", "text": "Saved", "seconds": 3})
    assert world.requests("browser_wait")[-1]["args"] == {"for": "text", "text": "Saved", "seconds": 3.0}
    await world.call("browser_wait", **{"for": "ref", "ref": "e9", "seconds": 0.5})
    assert world.requests("browser_wait")[-1]["args"] == {"for": "ref", "ref": "e9", "seconds": 0.5}
    before = len(world.requests())
    for bad in ({"for": "sleep"}, {"for": "text"}, {"for": "text", "text": ""}, {"for": "text", "text": "x" * 300},
                {"for": "ref"}, {"for": "ref", "ref": "no good"}, {"for": "load", "seconds": 0},
                {"for": "load", "seconds": 11}, {"for": "load", "seconds": "soon"}, {}):
        assert (await world.call("browser_wait", **bad))["_data"]["code"] == "out_of_range"
    assert len(world.requests()) == before
    world.replies["browser_wait"] = {"ok": True, "result": {"matched": False}}
    assert (await world.call("browser_wait", **{"for": "load"}))["_data"]["matched"] is False


async def test_navigate_to_a_url_and_by_action_updates_the_known_page(world):
    world.replies["browser_navigate"] = {"ok": True, "result": {"url": PAGE, "title": "Form"}}
    out = await world.call("browser_navigate", url=PAGE)
    assert world.requests("browser_navigate")[-1]["args"] == {"url": PAGE}
    assert out["_data"]["url"] == PAGE and out["_data"]["title"] == "Form"
    assert browser_tools.page_url(world.session.session_id) == PAGE
    world.replies["browser_navigate"] = {"ok": True, "result": {"url": "https://example.org/next"}}
    await world.call("browser_navigate", action="forward")
    assert world.requests("browser_navigate")[-1]["args"] == {"action": "forward"}
    assert browser_tools.page_url(world.session.session_id) == "https://example.org/next"
    before = len(world.requests())
    for bad in ({}, {"action": "sideways"}, {"url": PAGE, "action": "back"}, {"action": 3}):
        assert (await world.call("browser_navigate", **bad))["_data"]["code"] == "out_of_range"
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "https://u:p@example.org/", 7, "",
                "https://good.example\\@evil.example/", "https://good.example/\tx", "https://good.example/\u202e",
                "https://exаmple.org/", " https://example.org/"):
        refused = await world.call("browser_navigate", url=bad)
        assert refused["_data"]["code"] == "url_rejected" and refused["_ui_components"][0]["variant"] == "error"
        assert (await world.call("browser_navigate", url=bad, action="back"))["_data"]["code"] == "out_of_range"
    assert len(world.requests()) == before


async def test_navigate_forwards_the_canonical_url_never_the_raw_one(world):
    world.replies["browser_navigate"] = {"ok": True, "result": {"url": "https://example.org/Path"}}
    out = await world.call("browser_navigate", url="HTTPS://Example.ORG:443/Path?Q=1")
    assert world.requests("browser_navigate")[-1]["args"] == {"url": "https://example.org/Path?Q=1"}
    assert out["_data"]["navigated"] == "https://example.org/Path?Q=1"
    await world.call("browser_navigate", url="http://[::1]:80/x")
    assert world.requests("browser_navigate")[-1]["args"] == {"url": "http://[::1]/x"}


async def test_navigate_surfaces_a_refused_origin_and_a_pending_dialog(world):
    world.replies["browser_navigate"] = _error("origin_not_approved", "https://evil.example is not approved")
    out = await world.call("browser_navigate", url="https://evil.example/")
    assert out["_data"]["status"] == "refused" and out["_data"]["code"] == "origin_not_approved"
    assert "browser_navigate" in out["_data"]["next_action"] and "asked" in out["_data"]["next_action"]
    assert "evil.example" in out["_data"]["message"]
    world.replies["browser_navigate"] = _error("pending_dialog", "an alert is open: Leave site?")
    out = await world.call("browser_navigate", action="reload")
    assert out["_data"]["code"] == "pending_dialog" and "browser_dialog" in out["_data"]["next_action"]


async def test_click_forwards_the_snapshot_bound_request_and_validates_it(world):
    world.replies["browser_click"] = {"ok": True, "result": {"url": PAGE, "title": "Form"}}
    out = await world.call("browser_click", ref="e3", snapshot_id="s9")
    assert world.requests("browser_click")[-1]["args"] == {
        "ref": "e3", "button": "left", "count": 1, "snapshot_id": "s9"}
    assert out["_data"]["clicked"] == "e3" and "browser_snapshot" in out["_data"]["note"]
    await world.call("browser_click", ref="e3", snapshot_id="s9", button="right", count=2)
    assert world.requests("browser_click")[-1]["args"]["button"] == "right"
    before = len(world.requests())
    for bad in ({"snapshot_id": "s9"}, {"ref": "e3"}, {"ref": "e3", "snapshot_id": ""},
                {"ref": "e 3", "snapshot_id": "s9"}, {"ref": "e3", "snapshot_id": "s9", "button": "middle"},
                {"ref": "e3", "snapshot_id": "s9", "count": 3}, {"ref": 3, "snapshot_id": "s9"}):
        assert (await world.call("browser_click", **bad))["_data"]["code"] == "out_of_range"
    assert len(world.requests()) == before


async def test_a_stale_reference_tells_the_model_to_take_a_new_snapshot(world):
    world.replies["browser_click"] = _error("stale_ref", "e3 no longer resolves")
    out = await world.call("browser_click", ref="e3", snapshot_id="s9")
    data = out["_data"]
    assert data["status"] == "refused" and data["code"] == "stale_ref" and data["computer"] == "RYZENROLL"
    assert "browser_snapshot" in data["next_action"] and "snapshot_id" in data["next_action"]
    assert out["_ui_components"] == []
    res = SimpleNamespace(error=None, result=out)
    assert "browser_snapshot" in Orchestrator._tool_result_to_llm_content(res, "browser_click")


async def test_actionable_refusals_survive_the_mcp_server_as_results_not_errors(world):
    from agents.computer_use.mcp_server import MCPServer

    world.replies["browser_click"] = _error("stale_ref", "gone")
    server = MCPServer()
    response = await asyncio.to_thread(server.process_request, MCPRequest(
        request_id="r1", method="tools/call", params={"name": "browser_click", "arguments": {
            **CTX, "ref": "e3", "snapshot_id": "s9"}}))
    assert response.error is None and response.result["_data"]["code"] == "stale_ref"
    names = {tool["name"] for tool in server.get_tool_list()}
    assert set(BROWSER_TOOLS) <= names


async def test_other_host_failures_keep_the_ordinary_error_shape(world):
    world.replies["browser_click"] = _error("timeout", "slow page")
    out = await world.call("browser_click", ref="e3", snapshot_id="s9")
    assert out["_data"]["code"] == "timeout" and out["_ui_components"][0]["variant"] == "error"
    await world.orch.computer_sessions.on_host_event(OWNER, world.host.host_id, "paused", world.session.session_id,
                                                     "local_input")
    paused = await world.call("browser_snapshot")
    assert paused["_data"]["code"] == "local_resume_required" and paused["_ui_components"][0]["variant"] == "error"
    unattended = await asyncio.to_thread(browser_tools.browser_snapshot, session_id=CHAT)
    assert unattended["_data"]["code"] == "unattended_refused"


async def test_a_host_without_the_browser_verbs_is_reported_as_the_pane_being_off(world):
    world.host.descriptor["verbs"] = ["screenshot"]
    out = await world.call("browser_snapshot")
    assert out["_data"]["status"] == "refused" and out["_data"]["code"] == "unsupported"
    assert "browser pane" in out["_data"]["next_action"] and "Computer controls" in out["_data"]["next_action"]
    assert out["_ui_components"][0]["variant"] == "warning"


async def test_fill_validates_fields_and_forwards_the_snapshot_bound_request(world):
    world.replies["browser_fill"] = {"ok": True, "result": {"url": PAGE}}
    out = await world.call("browser_fill", snapshot_id="s9", fields=[
        {"ref": "e2", "value": "Ada"}, {"ref": "e4", "value": "x@example.org", "extra": "dropped"}])
    assert world.requests("browser_fill")[-1]["args"] == {
        "fields": [{"ref": "e2", "value": "Ada"}, {"ref": "e4", "value": "x@example.org"}], "snapshot_id": "s9"}
    assert out["_data"]["filled"] == ["e2", "e4"]
    before = len(world.requests())
    too_many = [{"ref": f"e{i}", "value": "v"} for i in range(21)]
    for bad in ({"snapshot_id": "s9", "fields": []}, {"snapshot_id": "s9", "fields": "e2=Ada"},
                {"snapshot_id": "s9", "fields": too_many}, {"fields": [{"ref": "e2", "value": "v"}]},
                {"snapshot_id": "s9", "fields": [{"ref": "e2"}]}, {"snapshot_id": "s9", "fields": ["e2"]},
                {"snapshot_id": "s9", "fields": [{"ref": "bad ref", "value": "v"}]},
                {"snapshot_id": "s9", "fields": [{"ref": "e2", "value": "v" * 4001}]},
                {"snapshot_id": "s9", "fields": [{"ref": "e2", "value": 5}]}):
        assert (await world.call("browser_fill", **bad))["_data"]["code"] == "out_of_range"
    assert len(world.requests()) == before


async def test_filling_a_secret_field_is_refused_with_guidance(world):
    world.replies["browser_fill"] = _error("secret_field", "e4 is a password field")
    out = await world.call("browser_fill", snapshot_id="s9", fields=[{"ref": "e4", "value": "hunter2"}])
    assert out["_data"]["code"] == "secret_field" and "themselves" in out["_data"]["next_action"]
    assert "hunter2" not in json.dumps(out)


async def test_press_scroll_and_dialog_validate_and_forward(world):
    world.replies["browser_press"] = {"ok": True, "result": {}}
    out = await world.call("browser_press", keys="Enter")
    assert world.requests("browser_press")[-1]["args"] == {"keys": "Enter"} and out["_data"]["keys"] == "Enter"
    await world.call("browser_press", keys="cmd+a", ref="e2")
    assert world.requests("browser_press")[-1]["args"] == {"keys": "cmd+a", "ref": "e2"}
    for bad in ({}, {"keys": ""}, {"keys": "rm -rf /;"}, {"keys": "x" * 80}, {"keys": "Enter", "ref": "no good"}):
        assert (await world.call("browser_press", **bad))["_data"]["code"] == "out_of_range"

    world.replies["browser_scroll"] = {"ok": True, "result": {}}
    await world.call("browser_scroll")
    assert world.requests("browser_scroll")[-1]["args"] == {"dy": 600}
    out = await world.call("browser_scroll", dy=-250, ref="e8")
    assert world.requests("browser_scroll")[-1]["args"] == {"dy": -250, "ref": "e8"} and out["_data"]["dy"] == -250
    for bad in ({"dy": 0}, {"dy": 20001}, {"dy": "far"}, {"ref": "no good"}):
        assert (await world.call("browser_scroll", **bad))["_data"]["code"] == "out_of_range"

    world.replies["browser_dialog"] = {"ok": True, "result": {}}
    out = await world.call("browser_dialog", action="accept", text="yes")
    assert world.requests("browser_dialog")[-1]["args"] == {"action": "accept", "text": "yes"}
    assert out["_data"]["action"] == "accept"
    await world.call("browser_dialog", action="dismiss")
    assert world.requests("browser_dialog")[-1]["args"] == {"action": "dismiss"}
    for bad in ({}, {"action": "ignore"}, {"action": "accept", "text": "x" * 1001}, {"action": "accept", "text": 4}):
        assert (await world.call("browser_dialog", **bad))["_data"]["code"] == "out_of_range"


async def test_dialog_pending_error_on_a_click_points_at_browser_dialog(world):
    world.replies["browser_press"] = _error("pending_dialog", "confirm: Delete everything?")
    out = await world.call("browser_press", keys="Enter")
    assert out["_data"]["code"] == "pending_dialog" and "accept or dismiss" in out["_data"]["next_action"]


async def test_a_consequential_control_is_recorded_and_returned_for_exact_review(world):
    line = '[e3] button "Pay now"'
    world.replies["browser_snapshot"] = _snapshot_reply()
    await world.call("browser_snapshot")
    world.replies["browser_click"] = _error("confirmation_required", f"consequential control\n{line}\nextra")
    out = await world.call("browser_click", ref="e3", snapshot_id="s9")
    data = out["_data"]
    assert data["status"] == "refused" and data["code"] == "confirmation_required" and data["element"] == line
    assert "confirm_element" in data["next_action"] and line in data["next_action"]
    page = browser_tools._PAGES[world.session.session_id]
    assert page.held[("s9", "e3")] == line


async def test_a_consequential_control_without_a_parseable_line_is_still_actionable(world):
    world.replies["browser_click"] = _error("confirmation_required", "\x07\x07")
    out = await world.call("browser_click", ref="e3", snapshot_id="s9")
    assert out["_data"]["code"] == "confirmation_required" and out["_data"]["element"] == ""
    assert "browser_snapshot" in out["_data"]["next_action"]
    page = browser_tools._PAGES.get(world.session.session_id)
    assert page is None or not page.held


async def test_a_consequential_message_without_an_element_token_is_held_for_the_requested_reference(world):
    world.replies["browser_click"] = _error("confirmation_required", "submit control in a payment form\x07")
    out = await world.call("browser_click", ref="e3", snapshot_id="s9")
    assert out["_data"]["element"] == "submit control in a payment form"
    assert browser_tools._PAGES[world.session.session_id].held[("s9", "e3")] == "submit control in a payment form"
    assert "submit control in a payment form" in out["_data"]["next_action"]


async def test_a_consequential_fill_records_the_field_the_host_named(world):
    world.replies["browser_fill"] = _error("confirmation_required", '[e4] textbox "Amount"')
    await world.call("browser_fill", snapshot_id="s9", fields=[
        {"ref": "e2", "value": "a"}, {"ref": "e4", "value": "5"}])
    held = browser_tools._PAGES[world.session.session_id].held
    assert list(held) == [("s9", "e4")]
    world.replies["browser_fill"] = _error("confirmation_required", "needs review")
    out = await world.call("browser_fill", snapshot_id="s10", fields=[{"ref": "e2", "value": "a"}])
    assert out["_data"]["element"] == "needs review" and held[("s10", "e2")] == "needs review"


def _approve(monkeypatch, approved=True):
    from orchestrator import hitl_confirmation

    calls = []

    def approved_call(orch, owner, chat, agent, tool):
        calls.append((owner, chat, agent, tool))
        return approved

    monkeypatch.setattr(hitl_confirmation, "approved_call", approved_call)
    return calls


async def _hold_consequential(world, ref="e3"):
    line = f'[{ref}] button "Pay now"'
    world.replies["browser_snapshot"] = _snapshot_reply()
    await world.call("browser_snapshot")
    world.replies["browser_click"] = _error("confirmation_required", line)
    await world.call("browser_click", ref=ref, snapshot_id="s9")
    return line


async def test_an_approved_exact_review_issues_a_one_shot_permit_bound_to_ref_origin_and_snapshot(world, monkeypatch):
    line = await _hold_consequential(world)
    calls = _approve(monkeypatch)
    world.replies["browser_click"] = {"ok": True, "result": {"url": PAGE}}
    out = await world.call("browser_click", ref="e3", snapshot_id="s9", confirm_element=line)
    args = world.requests("browser_click")[-1]["args"]
    assert args["permit"] == {"ref": "e3", "origin": ORIGIN, "snapshot_id": "s9"}
    assert args["ref"] == "e3" and "confirm_element" not in args
    assert calls == [(OWNER, CHAT, "computer-use-1", "browser_click")]
    assert out["_data"]["clicked"] == "e3"
    assert not browser_tools._PAGES[world.session.session_id].held
    again = await world.call("browser_click", ref="e3", snapshot_id="s9", confirm_element=line)
    assert again["_data"]["code"] == "confirm_element_mismatch"


async def test_without_an_approved_review_the_retry_carries_no_permit(world, monkeypatch):
    line = await _hold_consequential(world)
    _approve(monkeypatch, approved=False)
    world.replies["browser_click"] = _error("confirmation_required", line)
    out = await world.call("browser_click", ref="e3", snapshot_id="s9", confirm_element=line)
    assert "permit" not in world.requests("browser_click")[-1]["args"]
    assert out["_data"]["code"] == "confirmation_required"
    assert browser_tools._PAGES[world.session.session_id].held[("s9", "e3")] == line


async def test_a_confirm_element_the_host_never_reported_is_refused_before_any_request(world, monkeypatch):
    await _hold_consequential(world)
    _approve(monkeypatch)
    before = len(world.requests("browser_click"))
    for claimed in ('[e3] button "Next"', '[e3] button "Pay now" and more', "unrelated"):
        out = await world.call("browser_click", ref="e3", snapshot_id="s9", confirm_element=claimed)
        assert out["_data"]["code"] == "confirm_element_mismatch" and "browser_snapshot" in out["_data"]["next_action"]
    out = await world.call("browser_click", ref="e3", snapshot_id="s404", confirm_element='[e3] button "Pay now"')
    assert out["_data"]["code"] == "confirm_element_mismatch"
    assert len(world.requests("browser_click")) == before


async def test_a_permit_needs_a_known_page_origin(world, monkeypatch):
    line = '[e3] button "Pay now"'
    browser_tools._hold(world.session.session_id, "s9", ["e3"], line)
    _approve(monkeypatch)
    out = await world.call("browser_click", ref="e3", snapshot_id="s9", confirm_element=line)
    assert out["_data"]["code"] == "confirm_element_mismatch"
    assert len(world.requests("browser_click")) == 0


async def test_an_approved_consequential_fill_carries_a_permit_for_the_matching_fields(world, monkeypatch):
    world.replies["browser_snapshot"] = _snapshot_reply()
    await world.call("browser_snapshot")
    line = '[e4] textbox "Amount"'
    browser_tools._hold(world.session.session_id, "s9", ["e4"], line)
    _approve(monkeypatch)
    world.replies["browser_fill"] = {"ok": True, "result": {}}
    fields = [{"ref": "e2", "value": "a"}, {"ref": "e4", "value": "5"}]
    await world.call("browser_fill", snapshot_id="s9", fields=fields, confirm_element=line)
    args = world.requests("browser_fill")[-1]["args"]
    assert args["permit"] == {"refs": ["e4"], "origin": ORIGIN, "snapshot_id": "s9"}
    assert args["fields"] == fields


async def test_a_fill_with_a_line_the_host_never_reported_is_refused_before_any_request(world, monkeypatch):
    _approve(monkeypatch)
    out = await world.call("browser_fill", snapshot_id="s9", fields=[{"ref": "e2", "value": "a"}],
                           confirm_element='[e2] textbox "Total"')
    assert out["_data"]["code"] == "confirm_element_mismatch" and world.requests("browser_fill") == []


async def test_model_supplied_permits_never_reach_the_host(world):
    world.replies["browser_click"] = {"ok": True, "result": {}}
    await world.call("browser_click", ref="e3", snapshot_id="s9", permit={"ref": "e3"})
    assert "permit" not in world.requests("browser_click")[-1]["args"]
    world.replies["browser_fill"] = {"ok": True, "result": {}}
    await world.call("browser_fill", snapshot_id="s9", fields=[{"ref": "e2", "value": "a"}], permit=True)
    assert "permit" not in world.requests("browser_fill")[-1]["args"]


async def test_state_is_per_session_so_another_sessions_hold_is_never_used(world, monkeypatch):
    line = await _hold_consequential(world)
    _approve(monkeypatch)
    browser_tools.forget(world.session.session_id)
    out = await world.call("browser_click", ref="e3", snapshot_id="s9", confirm_element=line)
    assert out["_data"]["code"] == "confirm_element_mismatch"


async def test_tools_run_when_the_orchestrator_has_no_registry():
    mcp_tools.register_deps(SimpleNamespace())
    out = await asyncio.to_thread(browser_tools.browser_snapshot, **CTX)
    assert out["_data"]["code"] == "computer_unavailable"


_VALID_CALLS = {
    "browser_snapshot": {}, "browser_get_text": {}, "browser_screenshot": {}, "browser_wait": {"for": "load"},
    "browser_navigate": {"url": PAGE}, "browser_click": {"ref": "e3", "snapshot_id": "s9"},
    "browser_fill": {"fields": [{"ref": "e2", "value": "a"}], "snapshot_id": "s9"},
    "browser_press": {"keys": "Enter"}, "browser_scroll": {}, "browser_dialog": {"action": "accept"},
}


def test_the_valid_call_table_covers_every_browser_tool():
    assert sorted(_VALID_CALLS) == BROWSER_TOOLS


@pytest.mark.parametrize("tool", BROWSER_TOOLS)
async def test_every_tool_refuses_without_a_signed_in_user(tool):
    out = await asyncio.to_thread(getattr(browser_tools, tool), session_id=CHAT, **_VALID_CALLS[tool])
    assert out["_data"]["code"] == "unattended_refused"


@pytest.mark.parametrize("tool", BROWSER_TOOLS)
async def test_every_tool_surfaces_a_host_refusal_instead_of_a_result(world, tool):
    for verb in BROWSER_TOOLS:
        world.replies[verb] = _error("stale_ref", "gone")
    out = await world.call(tool, **_VALID_CALLS[tool])
    assert out["_data"]["code"] == "stale_ref" and out["_data"]["status"] == "refused"
    assert len(world.requests(tool)) == 1


@pytest.mark.parametrize("tool", BROWSER_TOOLS)
async def test_every_tool_needs_a_live_session(world, tool):
    await world.orch.computer_sessions.end(world.session, "user_stop")
    out = await world.call(tool, **_VALID_CALLS[tool])
    assert out["_data"]["code"] == "no_session" and world.requests(tool) == []
