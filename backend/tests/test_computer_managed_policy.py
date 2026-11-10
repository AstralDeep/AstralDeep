"""Managed-policy ceilings for computer control: the descriptor block is validated at
announce, the stricter of the descriptor and Keycloak role ceilings wins, unavailable role
state fails closed, the dispatch gate refuses forbidden verbs with an audited
remote_op.refused row, permission-caused host failures become permission_missing, and the
diagnostics endpoint reports effective ceilings and their sources.
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from orchestrator import remote_confirmation as rc
from orchestrator.computer_hosts import ComputerHostError
from orchestrator.computer_managed_policy import (
    DESCRIPTOR_SOURCE,
    ROLE_SOURCE_CACHED,
    ROLE_SOURCE_UNAVAILABLE,
    ROLE_SOURCE_VERIFIED,
    PolicyCeilings,
    claim_roles,
    descriptor_ceilings,
    role_ceilings,
)
from orchestrator.computer_sessions import ComputerSessionManager
from orchestrator.orchestrator import Orchestrator, _typed_refusal_meta
from orchestrator.projection_surfaces import my_computers
from shared.protocol import ComputerHostDescriptor, ProtocolValidationError, RegisterUI
from tests.test_computer_use_076 import OWNER, _FakeOrch, _WS, _descriptor
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401

DIGEST = "a" * 64


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(my_computers, "_enabled", lambda: True)


def _policy(**over):
    block = {"digest": DIGEST, "computer_use_allowed": True, "browser_pane_allowed": True,
             "bypass_allowed": True}
    block.update(over)
    return block


def _mac_descriptor(**over):
    return _descriptor(platform="macos", name="Mac", **over)


def _audits(monkeypatch):
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


# --- descriptor contract -------------------------------------------------------------------


def test_descriptor_accepts_and_roundtrips_a_managed_policy_block():
    descriptor = ComputerHostDescriptor.from_dict({**_mac_descriptor(), "managed_policy": _policy()})
    assert descriptor.to_dict()["managed_policy"] == _policy()


@pytest.mark.parametrize("block", [
    {**_policy(), "extra": True},
    {k: v for k, v in _policy().items() if k != "digest"},
    {**_policy(), "digest": "A" * 64},
    {**_policy(), "digest": "a" * 63},
    {**_policy(), "digest": "z" * 64},
    {**_policy(), "bypass_allowed": 0},
    {**_policy(), "computer_use_allowed": "true"},
    {**_policy(), "browser_pane_allowed": None},
    "managed policy",
    ["digest"],
])
def test_descriptor_rejects_malformed_or_oversized_policy_blocks(block):
    with pytest.raises(ProtocolValidationError):
        ComputerHostDescriptor.from_dict({**_mac_descriptor(), "managed_policy": block})


def test_register_ui_drops_a_host_whose_policy_block_is_malformed():
    registration = RegisterUI.from_json(json.dumps({
        "type": "register_ui", "capabilities": [],
        "computer_host": {**_mac_descriptor(), "managed_policy": {**_policy(), "digest": "short"}}}))
    assert registration.computer_host is None
    ok = RegisterUI.from_json(json.dumps({
        "type": "register_ui", "capabilities": [],
        "computer_host": {**_mac_descriptor(), "managed_policy": _policy()}}))
    assert ok.computer_host.to_dict()["managed_policy"]["digest"] == DIGEST


# --- ceilings math -------------------------------------------------------------------------


def test_role_ceilings_map_deny_roles_and_ignore_everything_else():
    assert role_ceilings(["user", "admin"]) == PolicyCeilings()
    assert role_ceilings(["browser-pane-denied"]) == PolicyCeilings(browser_pane=False)
    assert role_ceilings([" computer-use-denied "]) == PolicyCeilings(computer_use=False)
    assert role_ceilings(["computer-bypass-denied", "user"]) == PolicyCeilings(bypass=False)
    assert role_ceilings(["computer-use-denied", "browser-pane-denied", "computer-bypass-denied"]) \
        == PolicyCeilings(False, False, False)


def test_policy_digest_reads_the_block_and_tolerates_garbage():
    from orchestrator.computer_managed_policy import policy_digest

    assert policy_digest({"managed_policy": {"digest": DIGEST}}) == DIGEST
    assert policy_digest({}) is None
    assert policy_digest("not-a-dict") is None
    assert policy_digest({"managed_policy": "flat"}) is None


def test_claim_roles_reads_realm_and_client_roles_and_rejects_unreadable_claims():
    assert claim_roles({"roles": ["user"], "realm_access": {"roles": ["admin"]},
                        "resource_access": {"astral-frontend": {"roles": ["browser-pane-denied"]}}}) \
        == ["user", "admin", "browser-pane-denied"]
    assert claim_roles({"resource_access": {"astral-frontend": "corrupt"}}) == []
    assert claim_roles({"resource_access": {"astral-frontend": {"roles": "corrupt"}}}) == []
    assert claim_roles({"sub": "x"}) == []
    assert claim_roles("not-a-dict") is None
    assert claim_roles(None) is None


def test_refusal_messages_name_the_strict_source():
    from orchestrator.computer_managed_policy import refusal_message

    assert "organization profile" in refusal_message("bypass", "Mac", DESCRIPTOR_SOURCE)
    assert "organization roles" in refusal_message("bypass", "Mac", ROLE_SOURCE_VERIFIED)
    assert "organization roles" in refusal_message("bypass", "Mac", ROLE_SOURCE_CACHED)
    assert "could not be verified" in refusal_message("bypass", "Mac", ROLE_SOURCE_UNAVAILABLE)
    assert "organization roles" in refusal_message("bypass", "Mac", "something-new")


def test_descriptor_ceilings_reads_the_block_and_tolerates_its_absence():
    host = SimpleNamespace(descriptor={"managed_policy": _policy(browser_pane_allowed=False)})
    assert descriptor_ceilings(host) == PolicyCeilings(browser_pane=False)
    assert descriptor_ceilings(SimpleNamespace(descriptor={})) is None
    assert descriptor_ceilings(SimpleNamespace(descriptor=None)) is None


def _world_with_policy(monkeypatch, policy=None, roles=None):
    orch = _FakeOrch()
    host_ws = orch.add(_WS(user=OWNER))
    descriptor = _mac_descriptor() if policy is None else {**_mac_descriptor(), "managed_policy": policy}
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(descriptor))
    controller = orch.add(_WS(user=OWNER, chat="chat-1"))
    if roles is not None:
        orch.ui_sessions[controller] = {**orch.ui_sessions[controller], "roles": roles}
    return orch, host, host_ws, controller


def test_evaluate_combines_descriptor_and_roles_with_the_stricter_winning(monkeypatch):
    orch, host, _ws, controller = _world_with_policy(monkeypatch, policy=_policy(bypass_allowed=False))
    sessions = orch.computer_sessions
    ceilings, sources = sessions.effective_ceilings(host)
    assert ceilings == PolicyCeilings(bypass=False)
    assert sources == {"bypass": DESCRIPTOR_SOURCE}
    orch.ui_sessions[controller]["roles"] = ["browser-pane-denied"]
    ceilings, sources = sessions.effective_ceilings(host)
    assert ceilings == PolicyCeilings(browser_pane=False, bypass=False)
    assert sources == {"browser_pane": ROLE_SOURCE_VERIFIED, "bypass": DESCRIPTOR_SOURCE}
    ceilings, sources = sessions.effective_ceilings(host, controller)
    assert ceilings == PolicyCeilings(browser_pane=False, bypass=False)


def test_unverified_role_state_fails_closed_and_a_verified_record_is_kept(monkeypatch):
    orch, host, _ws, controller = _world_with_policy(monkeypatch)
    empty = ComputerSessionManager(orch, orch.computer_hosts)
    ceilings, sources = empty.effective_ceilings(host, ui_sessions={})
    assert ceilings == PolicyCeilings(False, False, False)
    assert set(sources.values()) == {ROLE_SOURCE_UNAVAILABLE}
    strayed_sessions = ComputerSessionManager(SimpleNamespace(ui_sessions={}), orch.computer_hosts)
    deny_claims = {controller: {"sub": OWNER, "roles": ["browser-pane-denied"]}}
    ceilings, sources = strayed_sessions.effective_ceilings(host, ui_sessions=deny_claims)
    assert ceilings == PolicyCeilings(browser_pane=False)
    ceilings, sources = strayed_sessions.effective_ceilings(host)
    assert ceilings == PolicyCeilings(browser_pane=False)
    assert sources == {"browser_pane": ROLE_SOURCE_CACHED}


def test_role_ceilings_aggregate_across_all_owner_sockets(monkeypatch):
    orch, host, _ws, controller = _world_with_policy(monkeypatch)
    other = orch.add(_WS(user=OWNER))
    orch.ui_sessions[other] = {"sub": OWNER, "roles": ["computer-bypass-denied"]}
    ceilings, sources = orch.computer_sessions.effective_ceilings(host)
    assert ceilings == PolicyCeilings(bypass=False)
    assert sources == {"bypass": ROLE_SOURCE_VERIFIED}


# --- enforcement: session start and the bypass switch ---------------------------------------


async def test_session_start_refuses_a_computer_use_ceiling_from_the_descriptor(monkeypatch):
    _audits(monkeypatch)
    orch, host, host_ws, controller = _world_with_policy(
        monkeypatch, policy=_policy(computer_use_allowed=False))
    with pytest.raises(ComputerHostError) as excinfo:
        await orch.computer_sessions.start(OWNER, host, controller, "chat-1", wait_for_ack=False)
    assert excinfo.value.code == "managed_policy"
    assert "organization profile" in excinfo.value.message
    assert orch.computer_sessions.live_for_owner(OWNER) == []


async def test_session_start_refuses_a_role_ceiling_without_a_descriptor(monkeypatch):
    _audits(monkeypatch)
    orch, host, host_ws, controller = _world_with_policy(monkeypatch, roles=["computer-use-denied"])
    with pytest.raises(ComputerHostError) as excinfo:
        await orch.computer_sessions.start(OWNER, host, controller, "chat-1", wait_for_ack=False)
    assert excinfo.value.code == "managed_policy"
    assert "organization roles" in excinfo.value.message


async def test_unverified_role_state_refuses_the_session_start(monkeypatch):
    _audits(monkeypatch)
    orch, host, host_ws, controller = _world_with_policy(monkeypatch)
    empty = ComputerSessionManager(SimpleNamespace(ui_sessions={}), orch.computer_hosts)
    with pytest.raises(ComputerHostError) as excinfo:
        await empty.start(OWNER, host, controller, "chat-1", wait_for_ack=False)
    assert excinfo.value.code == "managed_policy"
    assert "could not be verified" in excinfo.value.message


async def test_bypass_switch_refuses_a_descriptor_ceiling_and_audits_the_refusal(monkeypatch):
    audits = _audits(monkeypatch)
    orch, host, host_ws, controller = _world_with_policy(monkeypatch, policy=_policy(bypass_allowed=False))
    with pytest.raises(ComputerHostError) as excinfo:
        await orch.computer_sessions.set_bypass(host, True)
    assert excinfo.value.code == "managed_policy" and "bypass" in excinfo.value.message
    assert not orch.computer_sessions.bypass_on(OWNER, host.host_id)
    refused = [event for event in audits if event.action_type == "remote_op.refused"]
    assert len(refused) == 1
    assert refused[0].inputs_meta["reason"] == "managed_policy"
    assert refused[0].inputs_meta["verb"] == "bypass_enable"
    assert refused[0].outcome == "failure"
    await orch.computer_sessions.set_bypass(host, False)
    assert [event.action_type for event in audits if event.action_type == "computer_bypass.disabled"] == []


async def test_bypass_switch_still_disables_and_enables_without_ceilings(monkeypatch):
    _audits(monkeypatch)
    orch, host, host_ws, controller = _world_with_policy(monkeypatch)
    await orch.computer_sessions.set_bypass(host, True)
    assert orch.computer_sessions.bypass_on(OWNER, host.host_id)
    await orch.computer_sessions.set_bypass(host, False)
    assert not orch.computer_sessions.bypass_on(OWNER, host.host_id)


async def test_the_bypass_refusal_is_tolerant_of_audit_failures(monkeypatch):
    from audit import recorder as module

    monkeypatch.setattr(module, "get_recorder", lambda: None)
    orch, host, _ws, _controller = _world_with_policy(
        monkeypatch, policy=_policy(bypass_allowed=False))
    with pytest.raises(ComputerHostError):
        await orch.computer_sessions.set_bypass(host, True)

    class _Broken:
        def record_blocking(self, _event):
            raise RuntimeError("audit down")

    monkeypatch.setattr(module, "get_recorder", lambda: _Broken())
    with pytest.raises(ComputerHostError):
        await orch.computer_sessions.set_bypass(host, True)


async def test_role_ceiling_refuses_enabling_bypass(monkeypatch):
    _audits(monkeypatch)
    orch, host, host_ws, controller = _world_with_policy(monkeypatch, roles=["computer-bypass-denied"])
    with pytest.raises(ComputerHostError) as excinfo:
        await orch.computer_sessions.set_bypass(host, True)
    assert excinfo.value.code == "managed_policy"
    assert "organization roles" in excinfo.value.message


async def test_the_surface_shows_why_bypass_was_refused(enabled, monkeypatch):
    _audits(monkeypatch)
    orch, host, host_ws, _controller = _world_with_policy(
        monkeypatch, policy=_policy(bypass_allowed=False))
    _key, _params, notice = await my_computers.HANDLERS["chrome_computer_bypass_set"](
        orch, host_ws, OWNER, ["user"], {"host_id": host.host_id, "enabled": True})
    assert "organization profile" in notice


# --- announce audit artifacts ---------------------------------------------------------------


async def test_digest_and_refusal_audits_survive_a_missing_or_broken_recorder(monkeypatch):
    from audit import recorder as module

    monkeypatch.setattr(module, "get_recorder", lambda: None)
    orch, _host, _host_ws, _controller = _world_with_policy(monkeypatch)
    from orchestrator.orchestrator import _audit_computer_announce_refused, _audit_computer_policy_digest

    descriptor = ComputerHostDescriptor.from_dict(
        {**_mac_descriptor(), "managed_policy": _policy(browser_pane_allowed=False)})
    for name in ("_register_computer_host",):
        setattr(orch, name, getattr(Orchestrator, name).__get__(orch))
    orch._tunnel_ingress_over_cap = lambda _owner: False
    await orch._register_computer_host(orch.add(_WS(user=OWNER)), OWNER, descriptor)
    _audit_computer_announce_refused(OWNER, {"host": {"host_id": str(uuid4())}}, "bad digest")

    class _Broken:
        def record_blocking(self, _event):
            raise RuntimeError("audit down")

    monkeypatch.setattr(module, "get_recorder", lambda: _Broken())
    _audit_computer_policy_digest(SimpleNamespace(owner_sub=OWNER, host_id="h", name="Mac"),
                                  {"managed_policy": _policy()})
    _audit_computer_announce_refused(OWNER, {"host": None}, "bad digest")


async def test_a_policy_digest_is_recorded_on_announce(monkeypatch):
    audits = _audits(monkeypatch)
    orch, _host, _host_ws, _controller = _world_with_policy(
        monkeypatch, policy=_policy(browser_pane_allowed=False))
    for name in ("_register_computer_host",):
        setattr(orch, name, getattr(Orchestrator, name).__get__(orch))
    orch._tunnel_ingress_over_cap = lambda _owner: False
    descriptor = ComputerHostDescriptor.from_dict(
        {**_mac_descriptor(), "managed_policy": _policy(browser_pane_allowed=False)})
    await orch._register_computer_host(orch.add(_WS(user=OWNER)), OWNER, descriptor)
    announced = [event for event in audits if event.action_type == "computer_host.announced"]
    assert len(announced) == 1
    assert announced[0].inputs_meta["policy_digest"] == DIGEST
    assert announced[0].inputs_meta["browser_pane_allowed"] is False


async def test_a_malformed_announce_is_refused_and_audited(monkeypatch):
    from shared.feature_flags import flags

    audits = _audits(monkeypatch)
    monkeypatch.setitem(flags._flags, "computer_use", True)
    orch, _host, _host_ws, _controller = _world_with_policy(monkeypatch)
    for name in ("_handle_computer_event", "_register_computer_host"):
        setattr(orch, name, getattr(Orchestrator, name).__get__(orch))
    orch._tunnel_ingress_over_cap = lambda _owner: False
    await orch._handle_computer_event(orch.add(_WS(user=OWNER)), SimpleNamespace(payload={
        "event": "announce", "host": {**_mac_descriptor(), "managed_policy": _policy()},
    }))
    assert len(audits) == 1
    assert audits[0].action_type == "computer_host.announced"
    audits.clear()
    await orch._handle_computer_event(orch.add(_WS(user=OWNER)), SimpleNamespace(payload={
        "event": "announce", "host": {**_mac_descriptor(), "managed_policy": {**_policy(), "digest": "nope"}},
    }))
    refused = [event for event in audits if event.action_type == "computer_host.announce_refused"]
    assert len(refused) == 1 and refused[0].inputs_meta["reason"] == "invalid_descriptor"
    assert "digest" in refused[0].inputs_meta["detail"]


# --- dispatch gate --------------------------------------------------------------------------


class _FakeDB:
    rows = {}

    def upsert(self, *a, **k):
        return None


def _gate_world(policy=None, roles=None):
    from tests.test_computer_use_076 import _gate_orch, _Hashable

    orch = _gate_orch(_FakeDB())
    ws = _Hashable()
    orch.ui_sessions[ws] = {"sub": OWNER}
    if roles is not None:
        orch.ui_sessions[ws]["roles"] = roles
    fake = _FakeOrch()
    descriptor = _mac_descriptor() if policy is None else {**_mac_descriptor(), "managed_policy": policy}
    host, _ = fake.computer_hosts.register(OWNER, object(), ComputerHostDescriptor.from_dict(descriptor))
    orch.computer_hosts = fake.computer_hosts
    orch.computer_sessions = fake.computer_sessions
    return orch, ws, host


def _recording(monkeypatch):
    events = []

    class _Recorder:
        async def record(self, event):
            events.append(event)
            return object()

        def record_blocking(self, event):
            events.append(event)

    monkeypatch.setattr("audit.recorder.get_recorder", lambda: _Recorder())
    return events


@pytest.mark.parametrize("verb,args", [
    ("browser_snapshot", {}), ("browser_navigate", {"url": "https://example.org/"}),
    ("browser_batch", {"actions": [{"tool": "browser_snapshot", "args": {}}]}),
])
async def test_a_browser_pane_ceiling_refuses_browser_verbs_with_an_audited_refusal(monkeypatch, verb, args):
    audits = _recording(monkeypatch)
    orch, ws, host = _gate_world(policy=_policy(browser_pane_allowed=False))
    out = rc.evaluate(orch, ws, "computer-use-1", verb, {**args, "computer": "Mac"}, "chat", OWNER)
    assert out is not None and "organization profile" in out[0] and "browser pane" in out[0]
    assert out[1][0]["type"] == "alert"
    rows = [event for event in audits if event.action_type == "remote_op.refused"]
    assert len(rows) == 1 and rows[0].inputs_meta["reason"] == "managed_policy"
    assert rows[0].inputs_meta["verb"] == verb and rows[0].outcome == "failure"


async def test_a_computer_use_ceiling_refuses_host_verbs_but_keeps_inventory_and_de_escalation(monkeypatch):
    _recording(monkeypatch)
    orch, ws, host = _gate_world(policy=_policy(computer_use_allowed=False))
    for verb, args in (("screenshot", {}), ("click", {"x": 1, "y": 1})):
        out = rc.evaluate(orch, ws, "computer-use-1", verb, {**args, "computer": "Mac"}, "chat", OWNER)
        assert out is not None and "computer use" in out[0]
    assert rc.evaluate(orch, ws, "computer-use-1", "list_computers", {}, "chat", OWNER) is None
    assert rc.evaluate(orch, ws, "computer-use-1", "end_session", {"computer": "Mac"}, "chat", OWNER) is None


async def test_a_role_ceiling_refuses_without_a_descriptor_block(monkeypatch):
    _recording(monkeypatch)
    orch, ws, host = _gate_world(roles=["browser-pane-denied"])
    out = rc.evaluate(orch, ws, "computer-use-1", "browser_snapshot", {"computer": "Mac"}, "chat", OWNER)
    assert out is not None and "organization roles" in out[0]
    orch.ui_sessions = {}
    cached = rc.evaluate(orch, ws, "computer-use-1", "browser_snapshot", {"computer": "Mac"}, "chat", OWNER)
    assert cached is not None and "organization roles" in cached[0]


async def test_unverified_role_state_refuses_host_verbs(monkeypatch):
    _recording(monkeypatch)
    orch, ws, host = _gate_world()
    orch.ui_sessions = {}
    out = rc.evaluate(orch, ws, "computer-use-1", "screenshot", {"computer": "Mac"}, "chat", OWNER)
    assert out is not None and "could not be verified" in out[0]


async def test_verbs_stay_allowed_when_the_ceiling_permits_them(monkeypatch):
    _recording(monkeypatch)
    orch, ws, host = _gate_world(policy=_policy())
    assert rc.evaluate(orch, ws, "computer-use-1", "screenshot", {"computer": "Mac"}, "chat", OWNER) is None
    assert rc.evaluate(orch, ws, "computer-use-1", "browser_snapshot", {"computer": "Mac"}, "chat", OWNER) is None
    assert rc.evaluate(orch, ws, "remote-compute-1", "list_machines", {}, "chat", OWNER) is None


async def test_a_stale_bypass_switch_cannot_authorize_while_the_ceiling_forbids_bypass(monkeypatch):
    _recording(monkeypatch)
    orch, ws, host = _gate_world(policy=_policy(bypass_allowed=False))
    sessions = orch.computer_sessions
    sessions._bypass[(OWNER, host.host_id)] = host
    out = rc.evaluate(orch, ws, "computer-use-1", "set_clipboard",
                      {"text": "x", "computer": "Mac"}, "chat", OWNER)
    assert out is not None and "bypass" in out[0]
    from orchestrator import hitl_confirmation as hc

    decision, reason = hc.consent_decision(orch, ws, OWNER, "chat", "computer-use-1", "set_clipboard",
                                           {"text": "x", "computer": "Mac"}, ())
    assert decision == "refuse" and "bypass" in reason


async def test_stale_bypass_still_stands_when_the_ceiling_permits(monkeypatch):
    _recording(monkeypatch)
    orch, ws, host = _gate_world(policy=_policy())
    sessions = orch.computer_sessions
    sessions._bypass[(OWNER, host.host_id)] = host
    from orchestrator import hitl_confirmation as hc

    decision, _reason = hc.consent_decision(orch, ws, OWNER, "chat", "computer-use-1", "set_clipboard",
                                            {"text": "x", "computer": "Mac"}, ())
    assert decision == "standing"


# --- typed host failures --------------------------------------------------------------------


def test_a_permission_cause_becomes_a_typed_permission_missing_error():
    from agents.computer_use.mcp_tools import _host_error

    out = _host_error(ComputerHostError("permission_denied", "Allow Screen Recording in System Settings."))
    assert out["_data"]["code"] == "permission_missing"
    assert "System Settings" in out["_data"]["message"]
    assert "resume computer access" in out["_data"]["next_action"]
    accessibility = _host_error(ComputerHostError("permission_denied", "Allow Accessibility access in System Settings."))
    assert accessibility["_data"]["code"] == "permission_missing"
    keyboard = _host_error(ComputerHostError("permission_denied", "Accessibility access or keyboard input is unavailable."))
    assert keyboard["_data"]["code"] == "permission_missing"


def test_non_permission_denials_keep_their_host_code():
    from agents.computer_use.mcp_tools import _host_error

    out = _host_error(ComputerHostError("permission_denied", "The workspace selection was superseded."))
    assert out["_data"]["code"] == "permission_denied"
    managed = _host_error(ComputerHostError("managed_policy", "Mac's organization profile forbids bypass permissions; "
                                                            "ask the organization administrator to change the managed policy."))
    assert managed["_data"]["code"] == "managed_policy"
    assert "Do not retry" in managed["_data"]["next_action"]


def test_typed_refusal_meta_extracts_the_refusal_code():
    assert _typed_refusal_meta({"_data": {"code": "permission_missing", "message": "m"}}) \
        == {"refusal_code": "permission_missing"}
    assert _typed_refusal_meta({"_data": {"code": "", "message": "m"}}) == {}
    assert _typed_refusal_meta({"_data": {"ok": True}}) == {}
    assert _typed_refusal_meta("not-a-dict") == {}


async def test_the_standard_tool_audit_row_records_the_typed_refusal(task_runtime, monkeypatch):
    from shared.protocol import MCPResponse

    events = []

    class _Recorder:
        async def record(self, event):
            events.append(event)
            return object()

        def record_blocking(self, event):
            events.append(event)

    monkeypatch.setattr("audit.hooks.get_recorder", lambda: _Recorder())
    orch, ws, effects, turns, proposals, outcome = task_runtime
    from orchestrator.computer_use_policy import SCOPES

    orch.agents["computer-use-1"] = object()
    orch.tool_permissions.get_tool_scope_map.return_value = dict(SCOPES)
    outcome[0] = MCPResponse(result={"_data": {"code": "permission_missing", "message": "m", "next_action": ""}})
    response = await _dispatch_call(task_runtime, "list_computers")
    assert response.error is None
    ends = [event for event in events if event.action_type == "tool.list_computers.end"]
    assert ends and ends[-1].outputs_meta.get("refusal_code") == "permission_missing"
    rest = await orch.execute_authorized_tool(
        claims={"sub": "owner-a"}, user_id="owner-a", agent_id="computer-use-1",
        tool_name="list_computers", arguments={}, channel="rest", chat_id="chat-a")
    assert rest.error is None
    ends = [event for event in events if event.action_type == "tool.list_computers.end"]
    assert len(ends) >= 2 and ends[-1].outputs_meta.get("refusal_code") == "permission_missing"


async def _dispatch_call(task_runtime, tool, **args):
    from tests.test_hitl_public_research import _dispatch

    orch, ws = task_runtime[:2]
    return await _dispatch((orch, ws), tool=tool, args=args, agent="computer-use-1")


# --- diagnostics endpoint -------------------------------------------------------------------


def _policy_app(orch):
    from orchestrator.computer_policy_api import computer_policy_router
    from orchestrator.auth import verify_user

    app = FastAPI()
    app.include_router(computer_policy_router)
    app.state.orchestrator = orch
    return app, verify_user


def _diagnostics_world(monkeypatch, *, policy=None, roles=None):
    orch, host, host_ws, controller = _world_with_policy(monkeypatch, policy=policy, roles=roles)
    return orch, host


def test_diagnostics_reports_effective_ceilings_and_sources(monkeypatch):
    orch, host = _diagnostics_world(
        monkeypatch, policy=_policy(browser_pane_allowed=False))
    app, verify_user = _policy_app(orch)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/computer/managed-policy").status_code == 401
    app.dependency_overrides[verify_user] = lambda: {"sub": OWNER}
    response = client.get("/api/computer/managed-policy")
    assert response.status_code == 200
    body = response.json()
    assert body["computer_use_enabled"] in (True, False)
    assert body["roles"] == {"source": ROLE_SOURCE_VERIFIED, "denied": []}
    assert len(body["hosts"]) == 1
    row = body["hosts"][0]
    assert row["name"] == "Mac" and row["platform"] == "macos"
    assert row["policy_digest"] == DIGEST
    assert row["ceilings"] == {"computer_use": True, "browser_pane": False, "bypass": True}
    assert row["sources"] == {"browser_pane": DESCRIPTOR_SOURCE}


def test_diagnostics_reports_role_and_unavailable_sources(monkeypatch):
    orch, host = _diagnostics_world(monkeypatch, roles=["computer-bypass-denied"])
    app, verify_user = _policy_app(orch)
    app.dependency_overrides[verify_user] = lambda: {"sub": OWNER}
    body = TestClient(app).get("/api/computer/managed-policy").json()
    assert body["roles"]["denied"] == ["bypass"]
    row = body["hosts"][0]
    assert row["policy_digest"] is None
    assert row["ceilings"]["bypass"] is False and row["sources"] == {"bypass": ROLE_SOURCE_VERIFIED}
    strayed = SimpleNamespace(_orch=SimpleNamespace(ui_sessions={}), _role_ceilings={})
    strayed.effective_ceilings = lambda host, websocket=None, ui_sessions=None: \
        ComputerSessionManager.effective_ceilings(strayed, host, websocket, ui_sessions)
    orch.computer_sessions = strayed
    body = TestClient(app).get("/api/computer/managed-policy").json()
    assert body["roles"] == {"source": ROLE_SOURCE_UNAVAILABLE, "denied": ["computer_use", "browser_pane", "bypass"]}
    assert body["hosts"][0]["sources"] == {"computer_use": ROLE_SOURCE_UNAVAILABLE,
                                           "browser_pane": ROLE_SOURCE_UNAVAILABLE,
                                           "bypass": ROLE_SOURCE_UNAVAILABLE}


def test_diagnostics_refuses_when_remote_control_is_absent():
    from orchestrator.computer_policy_api import computer_policy_router, _owner_id
    from orchestrator.auth import verify_user

    app = FastAPI()
    app.include_router(computer_policy_router)
    app.state.orchestrator = SimpleNamespace()
    app.dependency_overrides[verify_user] = lambda: {"sub": None}
    assert TestClient(app, raise_server_exceptions=False).get("/api/computer/managed-policy").status_code == 401
    app.dependency_overrides[verify_user] = lambda: {"sub": OWNER}
    assert TestClient(app, raise_server_exceptions=False).get("/api/computer/managed-policy").status_code == 503
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_owner_id({"sub": None}))
    assert excinfo.value.status_code == 401
