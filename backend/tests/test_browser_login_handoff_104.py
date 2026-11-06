"""Exercises the owner login handoff verbs: browser_request_login waits for the owner's hand-back
and reports only the resulting origin and title, browser_fill_login takes only field references
and an origin with per-action review even under bypass, denial happens before dispatch, and the
audit rows carry origin and item-label hashes but never fill values.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from types import SimpleNamespace

import pytest

from agents.computer_use import browser_tools, mcp_tools
from orchestrator import computer_use_policy as policy
from orchestrator import hitl_confirmation as hc
from orchestrator import remote_confirmation as rc
from shared.protocol import COMPUTER_HOST_VERBS, ComputerHostDescriptor
from tests.test_browser_tools import CHAT, ORIGIN, PAGE, _error, _snapshot_reply
from tests.test_computer_grant_flow_102 import (
    CHAT as GATE_CHAT, OWNER as GATE_OWNER, SCOPES, _buttons, _call, _mac, _payload, _texts,
)
from tests.test_computer_bypass_102 import _host_world, _set
from tests.test_computer_use_076 import (
    OWNER, _AgentOrch, _descriptor, _FakeHostSocket, _session, _WS,
)
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401

FILL_ARGS = {"origin": ORIGIN, "refs": ["e2", "e3"], "snapshot_id": "s9"}
FILL_RISKS = ("source_bound", "mutation_or_disclosure", "tools:write")
LOGIN_TOOLS = ("browser_request_login", "browser_fill_login")


class _LateHostSocket(_FakeHostSocket):
    def __init__(self, orch, answers, silent=()):
        super().__init__(orch, answers)
        self.silent = set(silent)

    def push(self, data):
        frame = json.loads(data)
        if frame.get("verb") in self.silent:
            return
        super().push(data)


class _Recorder:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)
        return object()

    def record_blocking(self, event):
        self.events.append(event)

    def by(self, action_type):
        return [event for event in self.events if event.action_type == action_type]


@pytest.fixture
async def login_world():
    replies = {}

    def answers(verb, args):
        reply = replies.get(verb)
        if callable(reply):
            return reply(args)
        return reply if reply is not None else {"ok": True, "result": {}}

    orch = _AgentOrch()
    mcp_tools.register_deps(orch)
    host_ws = _LateHostSocket(orch, answers)
    orch.ui_clients.append(host_ws)
    orch.ui_sessions[host_ws] = {"sub": OWNER}
    from tests.test_computer_use_076 import _registered_context
    orch._connection_contexts[id(host_ws)] = _registered_context(host_ws)
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(_descriptor()))
    controller = orch.add(_WS(chat=CHAT))
    session = await _session(orch, host, controller, CHAT)
    world = SimpleNamespace(orch=orch, host=host, host_ws=host_ws, session=session, replies=replies,
                            controller=controller)

    async def call(tool, **kwargs):
        return await asyncio.to_thread(getattr(browser_tools, tool),
                                       user_id=OWNER, session_id=CHAT, **kwargs)

    world.call = call

    def requests(verb=None):
        frames = host_ws.frames("computer_request")
        return [f for f in frames if verb is None or f["verb"] == verb]

    world.requests = requests
    return world


@pytest.fixture
def audits(monkeypatch):
    from audit import recorder as module

    rec = _Recorder()
    monkeypatch.setattr(module, "get_recorder", lambda: rec)
    return rec


async def _snapshot_page(world, **over):
    world.replies["browser_snapshot"] = _snapshot_reply(**over)
    out = await world.call("browser_snapshot")
    assert out["_data"]["url"] == PAGE
    return out


def _approved(world, tool, arguments):
    from orchestrator.remote_confirmation import capture_computer_approval

    authority = capture_computer_approval(world.orch, world.controller, OWNER, CHAT,
                                          {"computer": world.host.name})
    assert authority is not None
    witness = hc.cancellation_witness(world.orch, world.controller, OWNER, CHAT, create=True)
    call = hc.PendingCall(str(uuid.uuid4()), OWNER, CHAT, "computer-use-1", tool,
                          json.dumps(arguments), FILL_RISKS, time.monotonic() + 900,
                          computer_authority=authority, websocket=world.controller,
                          cancellation=witness)
    token = hc._APPROVAL.set(hc.Approval(world.orch, call, consumed=True, effect_started=True))
    return token


def test_login_verbs_join_the_closed_protocol_policy_and_registry_sets():
    for name, tier, scope, timeout in (("browser_request_login", "input", "tools:write", 630.0),
                                       ("browser_fill_login", "consequential", "tools:write", 15.0)):
        assert name in COMPUTER_HOST_VERBS and name in policy.BROWSER_VERBS
        assert policy.TIERS[name] == tier and policy.SCOPES[name] == scope
        assert policy.TIMEOUTS[name] == timeout
        entry = mcp_tools.TOOL_REGISTRY[name]
        assert entry["tier"] == tier and entry["scope"] == scope and entry["timeout"] == timeout
        assert entry["retryable"] is False
    assert policy.HOST_VERBS == COMPUTER_HOST_VERBS
    assert "browser_request_login" in policy.WAIT_ON_OWNER_VERBS
    assert policy.LOCAL_CONFIRM_VERBS == frozenset({"browser_fill_login"})


def test_fill_login_preflight_refuses_values_extra_arguments_and_bad_shapes():
    good = dict(FILL_ARGS)
    assert policy.fill_login_preflight(good) == ""
    assert "never a value" in policy.fill_login_preflight(None)
    assert "never a value" in policy.fill_login_preflight(["origin"])
    for bad in ({**good, "value": "hunter2"}, {**good, "fields": [{"ref": "e2", "value": "x"}]},
                {**good, "password": "hunter2"}, {**good, "refs": ["e2"], "confirm_element": "x"},
                {**good, "origin": "https://example.org/path"},
                {**good, "origin": "ftp://example.org"}, {**good, "origin": ""},
                {**good, "refs": []}, {**good, "refs": ["e2", "e2"]}, {**good, "refs": ["a12"]},
                {**good, "refs": "e2"}, {**good, "snapshot_id": "x9"}, {**good, "snapshot_id": 9},
                {"origin": ORIGIN, "refs": ["e2"]}):
        assert policy.fill_login_preflight(bad) != ""
    assert "value" in policy.fill_login_preflight({**good, "value": "hunter2"})
    assert "hunter2" not in policy.fill_login_preflight({**good, "password": "hunter2"})


async def test_request_login_waits_and_reports_only_the_origin_and_title(login_world, audits):
    world = login_world
    await _snapshot_page(world)
    world.replies["browser_request_login"] = {"ok": True, "result": {
        "url": "https://example.org/accounts/signin?next=/private", "title": "Sign in — Example"}}
    out = await world.call("browser_request_login", reason="The project dashboard needs a sign-in")
    frame = world.requests("browser_request_login")[-1]
    assert frame["args"] == {"reason": "The project dashboard needs a sign-in", "origin": ORIGIN}
    data = out["_data"]
    assert data["status"] == "handed_back" and data["origin"] == ORIGIN
    assert data["title"] == "Sign in — Example" and data["computer"] == world.host.name
    assert "signin" not in json.dumps(data) and "next=" not in json.dumps(data)
    assert browser_tools.page_url(world.session.session_id) == "https://example.org/accounts/signin?next=/private"
    started, ended = audits.by("browser.login_handoff.started"), audits.by("browser.login_handoff.ended")
    assert len(started) == 1 and len(ended) == 1
    assert started[0].inputs_meta["origin"] == ORIGIN and ended[0].inputs_meta["origin"] == ORIGIN
    assert started[0].correlation_id == ended[0].correlation_id
    assert started[0].event_class == "agent_tool_call" and ended[0].outcome == "success"


async def test_request_login_without_a_known_page_omits_the_origin_hint(login_world, audits):
    world = login_world
    world.replies["browser_request_login"] = {"ok": True, "result": {"url": PAGE, "title": "Form"}}
    out = await world.call("browser_request_login", reason="Sign-in required")
    assert world.requests("browser_request_login")[-1]["args"] == {"reason": "Sign-in required"}
    assert out["_data"]["origin"] == ORIGIN and out["_data"]["title"] == "Form"
    assert audits.by("browser.login_handoff.started")[0].inputs_meta.get("origin") is None


async def test_cancelled_handoff_resolves_the_turn_with_a_clear_no_retry_status(login_world, audits):
    world = login_world
    await _snapshot_page(world)
    world.replies["browser_request_login"] = _error("login_cancelled", "The owner cancelled the handoff")
    out = await world.call("browser_request_login", reason="Sign-in required")
    data = out["_data"]
    assert data["status"] == "refused" and data["code"] == "login_cancelled"
    assert "cancel" in data["message"].lower()
    assert "do not" in data["next_action"].lower() and "retry" in data["next_action"].lower()
    assert len(world.requests("browser_request_login")) == 1
    ended = audits.by("browser.login_handoff.ended")
    assert len(ended) == 1 and ended[0].outcome == "failure"


async def test_timed_out_handoff_resolves_with_login_timeout_and_no_retry(login_world, audits, monkeypatch):
    world = login_world
    world.host_ws.silent = {"browser_request_login"}
    await _snapshot_page(world)
    monkeypatch.setitem(policy.TIMEOUTS, "browser_request_login", 0.2)
    out = await world.call("browser_request_login", reason="Sign-in required")
    data = out["_data"]
    assert data["status"] == "refused" and data["code"] == "login_timeout"
    assert "did not hand the login back" in data["message"]
    assert "do not" in data["next_action"].lower() and "retry" in data["next_action"].lower()
    ended = audits.by("browser.login_handoff.ended")
    assert len(ended) == 1 and ended[0].outcome == "failure"
    assert audits.by("credential_fill.requested") == []


async def test_request_login_refuses_bad_reasons_before_dispatch(login_world):
    world = login_world
    before = len(world.requests())
    for bad in ({}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 201}, {"reason": None},
                {"reason": "a\x00b"}):
        out = await world.call("browser_request_login", **bad)
        assert out["_data"]["code"] == "out_of_range"
    assert len(world.requests()) == before
    token = browser_tools._BATCH.set(object())
    try:
        out = await world.call("browser_request_login", reason="Sign in")
    finally:
        browser_tools._BATCH.reset(token)
    assert out["_data"]["code"] == "out_of_range" and "browser_batch" in out["_data"]["message"]
    assert len(world.requests()) == before


async def test_fill_login_refuses_values_extra_arguments_and_mismatched_origins_before_dispatch(
        login_world, audits):
    world = login_world
    await _snapshot_page(world)
    before = len(world.requests())
    for bad, code in (({**FILL_ARGS, "value": "hunter2"}, "out_of_range"),
                      ({**FILL_ARGS, "fields": [{"ref": "e2", "value": "x"}]}, "out_of_range"),
                      ({**FILL_ARGS, "password": "hunter2"}, "out_of_range"),
                      ({**FILL_ARGS, "origin": "https://example.org/other/path"}, "out_of_range"),
                      ({**FILL_ARGS, "snapshot_id": "s8"}, "stale_ref")):
        out = await world.call("browser_fill_login", **bad)
        assert out["_data"]["code"] == code, bad
        assert "hunter2" not in json.dumps(out)
    out = await world.call("browser_fill_login", **{**FILL_ARGS, "origin": "https://other.org"})
    assert out["_data"]["code"] == "origin_mismatch"
    assert len(world.requests()) == before
    refused = audits.by("credential_fill.refused")
    assert len(refused) >= 1
    mismatches = [event for event in refused if "origin_mismatch" in event.description]
    assert mismatches and all(event.inputs_meta["origin"] in (ORIGIN, "https://other.org")
                              for event in mismatches)
    assert all("hunter2" not in json.dumps(event.inputs_meta) for event in refused)


async def test_fill_login_dispatches_only_references_origin_and_permit_after_approval(login_world, audits):
    world = login_world
    await _snapshot_page(world)
    world.replies["browser_fill_login"] = {"ok": True, "result": {
        "filled": [{"ref": "e2", "label": "Email"}, {"ref": "e3", "label": "Remember me"}],
        "url": PAGE, "title": "Sign in"}}
    arguments = {**FILL_ARGS, "user_id": OWNER, "session_id": CHAT}
    token = _approved(world, "browser_fill_login", arguments)
    try:
        out = await world.call("browser_fill_login", **FILL_ARGS)
    finally:
        hc._APPROVAL.reset(token)
    frame = world.requests("browser_fill_login")[-1]
    assert frame["args"] == {"refs": ["e2", "e3"], "snapshot_id": "s9", "origin": ORIGIN,
                             "permit": {"origin": ORIGIN, "snapshot_id": "s9"}}
    assert "value" not in json.dumps(frame["args"])
    data = out["_data"]
    assert data["status"] == "filled" and data["origin"] == ORIGIN
    assert data["filled_fields"] == [{"ref": "e2", "label": "Email"}, {"ref": "e3", "label": "Remember me"}]
    requested, approved = audits.by("credential_fill.requested"), audits.by("credential_fill.approved")
    assert len(requested) == 1 and len(approved) == 1
    assert requested[0].inputs_meta["origin"] == ORIGIN
    assert approved[0].inputs_meta["origin"] == ORIGIN
    assert approved[0].inputs_meta["item_labels_sha256"] == [
        hashlib.sha256("Email".encode("utf-8")).hexdigest(),
        hashlib.sha256("Remember me".encode("utf-8")).hexdigest()]
    assert "Email" not in json.dumps(approved[0].inputs_meta)
    assert requested[0].correlation_id == approved[0].correlation_id


async def test_fill_login_without_a_server_permit_relies_on_the_hosts_local_confirmation(login_world, audits):
    world = login_world
    await _snapshot_page(world)
    world.replies["browser_fill_login"] = {"ok": True, "result": {
        "filled": [{"ref": "e2", "label": "Email"}], "url": PAGE}}
    out = await world.call("browser_fill_login", **FILL_ARGS)
    frame = world.requests("browser_fill_login")[-1]
    assert frame["args"] == {"refs": ["e2", "e3"], "snapshot_id": "s9", "origin": ORIGIN}
    assert "permit" not in frame["args"]
    assert out["_data"]["status"] == "filled"
    assert len(audits.by("credential_fill.requested")) == 1
    approved = audits.by("credential_fill.approved")
    assert len(approved) == 1 and approved[0].inputs_meta["origin"] == ORIGIN
    assert approved[0].inputs_meta["item_labels_sha256"] == [hashlib.sha256(b"Email").hexdigest()]


async def test_fill_login_forwards_host_refusals_and_audits_them(login_world, audits):
    world = login_world
    await _snapshot_page(world)
    world.replies["browser_fill_login"] = _error("stale_ref", "gone")
    out = await world.call("browser_fill_login", **FILL_ARGS)
    assert out["_data"]["code"] == "stale_ref"
    refused = audits.by("credential_fill.refused")
    assert len(refused) == 1 and refused[0].inputs_meta["origin"] == ORIGIN
    assert audits.by("credential_fill.approved") == []


async def test_login_handoff_and_fill_never_run_inside_browser_batch(login_world):
    world = login_world
    await _snapshot_page(world)
    out = await world.call("browser_batch", actions=[
        {"tool": "browser_fill_login", "args": dict(FILL_ARGS)}])
    results = out["_data"]["results"]
    assert results[0]["ok"] is False and results[0]["result"]["code"] == "out_of_range"
    assert "browser_batch" in results[0]["result"]["message"]
    out = await world.call("browser_batch", actions=[
        {"tool": "browser_request_login", "args": {"reason": "Sign in"}}])
    results = out["_data"]["results"]
    assert results[0]["ok"] is False and results[0]["result"]["code"] == "batch_deadline"
    assert world.requests("browser_fill_login") == [] and world.requests("browser_request_login") == []


async def test_malformed_host_replies_fail_both_login_tools_without_dispatch_side_effects(login_world, audits):
    world = login_world
    await _snapshot_page(world)
    world.replies["browser_request_login"] = {"ok": True}
    out = await world.call("browser_request_login", reason="Sign in")
    assert out["_data"]["code"] == "failed"
    assert "origin" in out["_data"]["message"]
    ended = audits.by("browser.login_handoff.ended")
    assert len(ended) == 1 and ended[0].outcome == "failure" and "malformed_reply" in ended[0].description
    token = _approved(world, "browser_fill_login", {**FILL_ARGS, "user_id": OWNER, "session_id": CHAT})
    try:
        world.replies["browser_fill_login"] = {"ok": True}
        out = await world.call("browser_fill_login", **FILL_ARGS)
    finally:
        hc._APPROVAL.reset(token)
    assert out["_data"]["status"] == "filled" and out["_data"]["filled_fields"] == []
    assert audits.by("credential_fill.refused") == []
    approved = audits.by("credential_fill.approved")
    assert len(approved) == 1 and "item_labels_sha256" not in approved[0].inputs_meta


async def test_fill_login_cancel_and_timeout_surface_the_no_retry_status(login_world, audits):
    world = login_world
    await _snapshot_page(world)
    world.replies["browser_fill_login"] = _error("login_cancelled", "The owner cancelled the fill")
    out = await world.call("browser_fill_login", **FILL_ARGS)
    assert out["_data"]["code"] == "login_cancelled"
    assert "do not" in out["_data"]["next_action"].lower()
    assert audits.by("credential_fill.refused") == [] and audits.by("credential_fill.approved") == []


def test_login_verb_review_summaries_name_the_owner_actions():
    request = policy.summary_for("browser_request_login", {"reason": "Dashboard access"}, "Your Mac")
    assert "sign in" in request.lower() and "Dashboard access" in request and "Your Mac" in request
    assert "origin and title" in request
    fill = policy.summary_for("browser_fill_login", {"origin": ORIGIN, "refs": ["e2", "e3"]}, "Your Mac")
    assert "Fill 2 login field(s)" in fill and ORIGIN in fill and "never see or supply" in fill


async def test_verbs_are_refused_for_hosts_that_do_not_advertise_them(login_world, audits):
    world = login_world
    await _snapshot_page(world)
    orch = world.orch
    quiet = _LateHostSocket(orch, lambda verb, args: (_snapshot_reply() if verb == "browser_snapshot"
                                                      else {"ok": True, "result": {"url": PAGE, "title": "T"}}))
    orch.ui_clients.append(quiet)
    orch.ui_sessions[quiet] = {"sub": OWNER}
    from tests.test_computer_use_076 import _registered_context
    orch._connection_contexts[id(quiet)] = _registered_context(quiet)
    limited = {key: value for key, value in _descriptor().items() if key != "verbs"}
    limited["host_id"] = str(uuid.uuid4())
    limited["name"] = "OLDMAC"
    limited["verbs"] = [verb for verb in _descriptor()["verbs"] if verb not in LOGIN_TOOLS]
    host, _ = orch.computer_hosts.register(OWNER, quiet, ComputerHostDescriptor.from_dict(limited))
    await _session(orch, host, orch.add(_WS(chat="chat-2")), "chat-2")
    world.replies["browser_snapshot"] = _snapshot_reply()
    await asyncio.to_thread(browser_tools.browser_snapshot, user_id=OWNER, session_id="chat-2",
                            computer=host.name)
    kwargs = {"computer": host.name}
    out = await world.call("browser_request_login", reason="Sign in", **kwargs)
    assert out["_data"]["code"] == "unsupported" and "does not support" in out["_data"]["message"]
    out = await world.call("browser_fill_login", **{**FILL_ARGS, **kwargs})
    assert out["_data"]["code"] == "unsupported"
    assert world.requests("browser_request_login") == [] and world.requests("browser_fill_login") == []
    started, ended = audits.by("browser.login_handoff.started"), audits.by("browser.login_handoff.ended")
    assert len(started) == 1 and len(ended) == 1
    assert ended[0].outcome == "failure" and "unsupported" in ended[0].description


async def _bypassed_world(monkeypatch):
    from orchestrator.projection_surfaces import my_computers
    monkeypatch.setattr(my_computers, "_enabled", lambda: True)
    orch, host, host_ws, controller = _host_world()
    notice = await _set(orch, host_ws, GATE_OWNER, {"host_id": host.host_id, "enabled": True,
                                                    "surface": "my_computers"})
    assert "Bypass on" in notice
    assert orch.computer_sessions.bypass_on(GATE_OWNER, host.host_id) is True
    return orch, controller


async def test_bypass_never_covers_the_fill_but_covers_the_handoff_request(monkeypatch):
    orch, controller = await _bypassed_world(monkeypatch)
    risks = FILL_RISKS
    fill = {"origin": ORIGIN, "refs": ["e2"], "snapshot_id": "s9", "computer": "Mac"}
    assert hc.consent_decision(orch, controller, GATE_OWNER, GATE_CHAT, "computer-use-1",
                               "browser_fill_login", fill, risks) == ("review", None)
    assert hc.bypass_covers(orch, controller, GATE_OWNER, GATE_CHAT, "computer-use-1",
                            "browser_fill_login", fill, risks) is False
    request = {"reason": "Sign in to continue", "computer": "Mac"}
    assert hc.consent_decision(orch, controller, GATE_OWNER, GATE_CHAT, "computer-use-1",
                               "browser_request_login", request, risks) == ("standing", None)


async def test_without_bypass_both_login_verbs_need_review():
    orch, _host, _host_ws, controller = _host_world()
    risks = FILL_RISKS
    for tool, args in (("browser_fill_login", {"origin": ORIGIN, "refs": ["e2"], "snapshot_id": "s9",
                                               "computer": "Mac"}),
                       ("browser_request_login", {"reason": "Sign in", "computer": "Mac"})):
        assert hc.consent_decision(orch, controller, GATE_OWNER, GATE_CHAT, "computer-use-1",
                                   tool, args, risks) == ("review", None)


async def test_the_gate_refuses_malformed_fill_arguments_before_any_card(task_runtime):
    orch, ws, effects, _turns, _proposals, _outcome = task_runtime
    await _mac(task_runtime, remote=True)
    orch.tool_permissions.get_tool_scope_map.return_value = {
        **SCOPES, "browser_request_login": "tools:write", "browser_fill_login": "tools:write"}
    response = await _call(task_runtime, "browser_fill_login",
                           **{**FILL_ARGS, "password": "hunter2"})
    assert response.error is None
    assert "takes only origin, refs and snapshot_id" in response.result["_data"]["message"]
    assert "hunter2" not in json.dumps(response.result)
    assert not [c for c in (response.ui_components or []) if c.get("type") == "card"]
    assert not getattr(orch, "_hitl_pending_calls", {})
    assert rc._PENDING_CARDS == {}
    assert effects == []


async def test_the_gate_shows_a_review_card_for_a_well_formed_fill(task_runtime):
    orch, ws, effects, _turns, _proposals, _outcome = task_runtime
    await _mac(task_runtime, remote=True)
    orch.tool_permissions.get_tool_scope_map.return_value = {
        **SCOPES, "browser_request_login": "tools:write", "browser_fill_login": "tools:write"}
    response = await _call(task_runtime, "browser_fill_login", **FILL_ARGS)
    assert response.error is None and response.result["_data"]["status"] == "confirmation_required"
    labels = [button["label"] for button in _buttons(response)]
    assert labels == ["Approve", "Decline"]
    assert "Fill 2 login field(s)" in _texts(response) and effects == []
    await rc.handle_decision(orch, ws, GATE_OWNER, _payload(response, "decline"))
    await asyncio.sleep(0)
    assert effects == []
