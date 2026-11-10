"""Tests for the run_command sandbox and allowed_domains contract: bounded domain validation,
the host capability gate, exact review for escalation and unseen domains, the egress-seeded
per-job proxy policy, tainted violation output and the sandbox audit artifacts.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import random
import uuid
from types import SimpleNamespace

import pytest

from agents.computer_use import mcp_tools
from orchestrator import computer_use_policy as policy
from orchestrator import hitl_confirmation as hc
from orchestrator import remote_confirmation as rc
from orchestrator import taint
from shared.protocol import COMPUTER_HOST_VERBS, ComputerHostDescriptor, ProtocolValidationError
from tests.test_computer_grant_flow_102 import (
    CHAT, OWNER, ROOT, _buttons, _call, _mac, _payload, _texts,
)
from tests.test_computer_use_076 import OTHER, _AgentOrch, _FakeHostSocket, _FakeOrch, _WS, _descriptor
from tests.test_computer_use_tools_102 import _World
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401

DOM = "files.example.com"
NEW_DOM = "telemetry.example.com"
SANDBOX_CHAT = "chat-sandbox"
CTX = {"user_id": OWNER, "session_id": SANDBOX_CHAT}


class _SandboxWorld(_World):
    """The tools-102 world, bound to this module's owner and chat."""

    async def call(self, tool, **kwargs):
        return await asyncio.to_thread(getattr(mcp_tools, tool), **CTX, **kwargs)


class _OwnerHostSocket(_FakeHostSocket):
    """Answers requests for the socket's own owner instead of the imported module constant."""

    def push(self, data):
        self.sent.append(data)
        frame = json.loads(data)
        if frame.get("type") == "computer_request":
            reply = self.answers(frame["verb"], frame["args"])
            loop = asyncio.get_running_loop()
            loop.call_soon(self.orch.computer_hosts.handle_response, self, self.user,
                           {"request_id": frame["request_id"], **reply})


def _runtime_digest() -> str:
    return "".join(random.choices("0123456789abcdef", k=64))


def _runtime_job_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------- domain validation


def test_valid_domains_are_lowercased_wildcards_and_deduplicated():
    assert policy.validate_allowed_domains(
        ["Example.COM", " *.Example.com ", "example.com", "a.b.example.co.uk"]) == \
        ["example.com", "*.example.com", "a.b.example.co.uk"]


@pytest.mark.parametrize("bad", [
    "not a domain", "-leading.example.com", "trailing-.example.com", "under_score.example.com",
    "example..com", ".example.com", "example.com.", "localhost", "example.localhost",
    "127.0.0.1", "10.0.0.5", "192.168.1.4", "::1", "2001:db8::1", "singlelabel", "",
    "   ", "exa mple.com", 7, None, ["example.com"], "x" * 64 + ".example.com",
    "a" * 254 + ".com", "ex\x00ample.com", "exa\nmple.com",
])
def test_malformed_private_or_oversized_domain_entries_are_refused(bad):
    with pytest.raises(ValueError):
        policy.validate_allowed_domains([bad])


def test_a_253_character_domain_still_fits_but_254_does_not():
    fits = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
    assert len(fits) == 253
    assert policy.validate_allowed_domains([fits]) == [fits]
    too_long = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 62])
    assert len(too_long) == 254
    with pytest.raises(ValueError):
        policy.validate_allowed_domains([too_long])


def test_the_domain_list_is_bounded():
    with pytest.raises(ValueError):
        policy.validate_allowed_domains([f"host{i}.example.com" for i in range(65)])
    assert len(policy.validate_allowed_domains(
        [f"host{i}.example.com" for i in range(64)])) == 64


def test_sandbox_mode_and_command_domains_read_the_arguments():
    assert policy.sandbox_mode({}) == "default"
    assert policy.sandbox_mode({"sandbox": "escalated"}) == "escalated"
    assert policy.sandbox_mode({"sandbox": "shadow"}) == "default"
    assert policy.command_domains({"allowed_domains": [DOM]}) == (DOM,)
    assert policy.command_domains({}) == ()
    assert policy.command_domains({"allowed_domains": "example.com"}) == ()


def test_exact_review_is_required_for_escalation_and_unseen_domains_only():
    escalated = {"sandbox": "escalated"}
    assert policy.sandbox_exact_review_required(escalated) is True
    assert policy.sandbox_exact_review_required(escalated, {DOM}) is True
    seen = {"allowed_domains": [DOM]}
    assert policy.sandbox_exact_review_required(seen) is True
    assert policy.sandbox_exact_review_required(seen, {DOM}) is False
    fresh = {"allowed_domains": [DOM, NEW_DOM]}
    assert policy.sandbox_exact_review_required(fresh, {DOM}) is True
    assert policy.sandbox_exact_review_required({}) is False
    assert policy.sandbox_exact_review_required({"sandbox": "default"}) is False


def test_the_proxy_policy_is_seeded_from_the_server_egress_posture():
    egress = policy.proxy_policy([DOM])
    assert egress["allowed_domains"] == [DOM]
    for cidr in ("10.0.0.0/8", "127.0.0.0/8", "192.168.0.0/16", "169.254.0.0/16"):
        assert cidr in egress["denied_cidrs"]
    requested = policy.proxy_policy(["internal.corp.example"])
    assert set(policy.EGRESS_DENIED_CIDRS) <= set(requested["denied_cidrs"])


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.5.5", "169.254.1.1",
                                  "224.0.0.1", "240.0.0.1", "0.0.0.0", "::1", "fe80::1", "fc00::1", "ff02::1"])
def test_denied_cidrs_keep_the_server_egress_classifications_denied(addr):
    from shared.external_http import _is_private_address

    assert _is_private_address(addr) is True
    address = ipaddress.ip_address(addr)
    assert any(address in ipaddress.ip_network(cidr) for cidr in policy.EGRESS_DENIED_CIDRS)


def test_escalation_lines_name_the_notice_and_the_destinations():
    lines = policy.escalation_lines({"sandbox": "escalated", "allowed_domains": [DOM, NEW_DOM]})
    assert any("OUTSIDE" in line for line in lines)
    assert any(DOM in line and NEW_DOM in line for line in lines)
    assert policy.escalation_lines({}) == []
    assert policy.escalation_lines({"allowed_domains": [DOM]})[-1].startswith("Allowed network destinations:")


def test_the_command_summary_carries_the_sandbox_notice():
    summary = policy._command_summary({"command": "ls", "cwd": ROOT, "sandbox": "escalated",
                                       "allowed_domains": [DOM]}, "Mac")
    assert "OUTSIDE" in summary and DOM in summary
    plain = policy._command_summary({"command": "ls", "cwd": ROOT}, "Mac")
    assert "OUTSIDE" not in plain and DOM not in plain


def test_the_bypass_floor_refuses_sandbox_runs_and_allows_seen_domains():
    escalated = {"command": "ls", "cwd": "/tmp", "sandbox": "escalated"}
    assert policy.bypass_floor("run_command", escalated, None) is True
    seen = {"command": "ls", "cwd": "/tmp", "allowed_domains": [DOM]}
    assert policy.bypass_floor("run_command", seen, None) is True
    assert policy.bypass_floor("run_command", seen, None, approved={DOM}) is False
    fresh = {"command": "ls", "cwd": "/tmp", "allowed_domains": [NEW_DOM]}
    assert policy.bypass_floor("run_command", fresh, None, approved={DOM}) is True


# ---------------------------------------------------------------- descriptor capabilities


def _descriptor_dataclass(caps):
    return ComputerHostDescriptor(
        host_id=str(uuid.uuid4()), name="Mac", platform="macos", client_version="0.5.0",
        screens=({"index": 0, "width": 1280, "height": 720, "scale": 1.0, "primary": True},),
        verbs=tuple(sorted(COMPUTER_HOST_VERBS)), protocol=1, capabilities=caps)


def test_descriptor_capabilities_round_trip():
    descriptor = ComputerHostDescriptor.from_dict(
        _descriptor(platform="macos", capabilities=["command_sandbox"]))
    assert descriptor.capabilities == ("command_sandbox",)
    data = descriptor.to_dict()
    assert data["capabilities"] == ["command_sandbox"]
    assert ComputerHostDescriptor.from_dict(data).capabilities == ("command_sandbox",)
    assert ComputerHostDescriptor.from_dict(_descriptor(platform="macos")).capabilities == ()
    assert "capabilities" not in ComputerHostDescriptor.from_dict(
        _descriptor(platform="macos")).to_dict()


@pytest.mark.parametrize("caps", [["unknown_thing"], ["command_sandbox", "command_sandbox"],
                                  ["command_sandbox", "also_unknown"], ["command_sandbox", 5]])
def test_descriptor_refuses_unknown_duplicate_or_non_string_capabilities(caps):
    with pytest.raises(ProtocolValidationError):
        ComputerHostDescriptor.from_dict(_descriptor(platform="macos", capabilities=caps))


def test_descriptor_refuses_a_non_sequence_capability_field():
    descriptor = _descriptor_dataclass(7)
    with pytest.raises(ProtocolValidationError):
        descriptor.validate()


def test_descriptor_caps_the_capability_count():
    with pytest.raises(ProtocolValidationError):
        _descriptor_dataclass(tuple(f"cap{i}" for i in range(9))).validate()


def test_the_host_registry_exposes_the_sandbox_capability():
    orch = _FakeOrch()
    capable_ws = orch.add(_WS(user=OWNER))
    capable, _ = orch.computer_hosts.register(OWNER, capable_ws, ComputerHostDescriptor.from_dict(
        _descriptor(platform="macos", capabilities=["command_sandbox"])))
    plain, _ = orch.computer_hosts.register(OWNER, orch.add(_WS(user=OWNER)),
                                            _descriptor(platform="macos", name="Plain"))
    assert capable.sandbox_capability is True
    assert plain.sandbox_capability is False


# ---------------------------------------------------------------- tool argument gate

PLAIN = SimpleNamespace(name="Mac", project_mode=False, sandbox_capability=False,
                        workspace_roots=[{"name": "x", "path": ROOT}])
CAPABLE = SimpleNamespace(name="Mac", project_mode=False, sandbox_capability=True,
                          workspace_roots=[{"name": "x", "path": ROOT}])


def _args(kwargs, host):
    return mcp_tools._command_args({"argv": ["wc", "-l", "notes.md"], "cwd": ROOT, **kwargs}, host)


def test_a_host_without_the_capability_gets_a_clear_refusal_for_either_argument():
    with pytest.raises(mcp_tools._Refusal) as exc:
        _args({"allowed_domains": [DOM]}, PLAIN)
    assert exc.value.code == "sandbox_unsupported"
    with pytest.raises(mcp_tools._Refusal) as exc:
        _args({"sandbox": "escalated"}, PLAIN)
    assert exc.value.code == "sandbox_unsupported"
    assert "does not advertise the command sandbox" in exc.value.args[0]
    # plain runs and sandbox=default stay available on the same host
    assert _args({"sandbox": "default"}, PLAIN)["argv"] == ["wc", "-l", "notes.md"]


@pytest.mark.parametrize("bad", [["not a domain"], ["10.0.0.5"], ["localhost"], [""], ["x" * 64 + ".example.com"],
                                 "example.com", [7]])
def test_malformed_domain_entries_are_refused_before_any_card(bad):
    with pytest.raises(mcp_tools._Refusal) as exc:
        _args({"allowed_domains": bad}, CAPABLE)
    assert exc.value.code == "out_of_range"


def test_valid_sandbox_arguments_reach_the_host_normalized():
    args = _args({"allowed_domains": [DOM, "Other.Example.com", DOM], "sandbox": "escalated"}, CAPABLE)
    assert args["allowed_domains"] == [DOM, "other.example.com"]
    assert args["sandbox"] == "escalated"
    plain = _args({"sandbox": "default"}, CAPABLE)
    assert "sandbox" not in plain and "allowed_domains" not in plain
    empty = _args({"allowed_domains": []}, CAPABLE)
    assert "allowed_domains" not in empty


def test_an_unknown_sandbox_mode_is_refused():
    with pytest.raises(mcp_tools._Refusal) as exc:
        _args({"sandbox": "shadow"}, CAPABLE)
    assert exc.value.code == "out_of_range"


def test_the_tool_schema_declares_both_arguments():
    schema = mcp_tools.TOOL_REGISTRY["run_command"]["input_schema"]["properties"]
    assert schema["sandbox"]["enum"] == ["default", "escalated"]
    assert schema["allowed_domains"]["maxItems"] == 64


# ---------------------------------------------------------------- run_command end to end


@pytest.fixture
async def sandbox_world(monkeypatch):
    replies = {}
    sent_args = []

    def answers(verb, args):
        sent_args.append((verb, args))
        reply = replies.get(verb)
        if callable(reply):
            return reply(args)
        return reply if reply is not None else {"ok": True, "result": {}}

    from orchestrator import terminal_host_review

    orch = _AgentOrch()
    mcp_tools.register_deps(orch)
    monkeypatch.setattr(terminal_host_review, "computer_use_review_current",
                        lambda current: current is orch)
    host_ws = _OwnerHostSocket(orch, answers)
    host_ws.user = OWNER
    orch.add(host_ws)
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(_descriptor(
        platform="macos", name="Mac", workspace_roots=[{"name": "x", "path": ROOT}],
        capabilities=["command_sandbox"])))
    orch.add(_WS(user=OWNER, chat=SANDBOX_CHAT))
    task = asyncio.create_task(asyncio.to_thread(mcp_tools.start_session, **CTX))
    for _ in range(400):
        await asyncio.sleep(0.005)
        live = orch.computer_sessions.live_for_host(OWNER, host.host_id)
        if live is not None:
            await orch.computer_sessions.on_host_event(OWNER, host.host_id, "heartbeat", live.session_id, None)
            break
    await task
    session = orch.computer_sessions.live_for_host(OWNER, host.host_id)
    world = _SandboxWorld(orch, host, host_ws, session, replies)
    world.sent_args = sent_args
    yield world
    await orch.computer_sessions.end_all("user_stop")


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_the_per_job_proxy_policy_reaches_the_host_before_the_command(sandbox_world):
    world = sandbox_world
    world.replies["run_command"] = {"ok": True, "result": {"exit_code": 0, "stdout": "ok", "stderr": "",
                                                           "cwd": ROOT, "started_at": "t"}}
    result = await world.call("run_command", argv=["curl", "-fsSL", f"https://{DOM}/data"], cwd=ROOT,
                              allowed_domains=[DOM])
    assert result["_data"]["exit_code"] == 0
    verb, args = world.sent_args[-1]
    assert verb == "run_command"
    assert args["allowed_domains"] == [DOM]
    assert args["proxy"]["allowed_domains"] == [DOM]
    assert "10.0.0.0/8" in args["proxy"]["denied_cidrs"] and "127.0.0.0/8" in args["proxy"]["denied_cidrs"]
    assert DOM in world.session.approved_domains
    # a plain run carries no proxy policy
    await world.call("run_command", argv=["wc", "-l", "notes.md"], cwd=ROOT)
    verb, args = world.sent_args[-1]
    assert "proxy" not in args and "allowed_domains" not in args


async def test_violation_summaries_return_as_untrusted_bounded_output(sandbox_world, monkeypatch):
    world = sandbox_world
    recorded = []
    monkeypatch.setattr(taint, "taint_enabled", lambda: True)
    tracker = SimpleNamespace(record_output=lambda output, source, inp: recorded.append((output, source, inp)))
    world.orch._taint_tracker = lambda _chat: tracker
    long_target = "https://" + NEW_DOM + "/" + "y" * 500
    violations = [{"source": "socket", "operation": "connect", "target": long_target,
                   "detail": "egress denied", "blocked": True}] * 25
    world.replies["run_command"] = {"ok": True, "result": {
        "exit_code": 0, "stdout": "partial", "stderr": "", "cwd": ROOT, "started_at": "t",
        "sandbox": {"mode": "default", "profile_digest": _runtime_digest(), "violations": violations}}}
    result = await world.call("run_command", argv=["curl", f"https://{DOM}"], cwd=ROOT,
                              allowed_domains=[DOM])
    rows = result["_data"]["sandbox_violations"]
    assert len(rows) == 20
    assert rows[0]["source"] == "socket" and rows[0]["operation"] == "connect"
    assert rows[0]["blocked"] is True
    assert len(rows[0]["target"]) <= 201 and NEW_DOM + "/" + "y" * 400 not in rows[0]["target"]
    assert recorded and recorded[0][1] == taint.UNTRUSTED and recorded[0][2] == taint.TRUSTED
    # the same summary is attached to background jobs
    world.replies["run_command"] = {"ok": True, "result": {"job_id": str(uuid.uuid4()),
        "started_at": "t", "sandbox": {"mode": "default", "violations": [{"detail": "denied", "blocked": True}]}}}
    background = await world.call("run_command", argv=["curl", f"https://{DOM}"], cwd=ROOT,
                                  allowed_domains=[DOM], background=True)
    assert background["_data"]["sandbox_violations"][0]["detail"] == "denied"


async def test_the_sandbox_result_audit_carries_the_host_profile_digest(sandbox_world, monkeypatch):
    from audit import recorder as module

    events = []

    class _Recorder:
        async def record(self, event):
            events.append(event)
            return object()

        def record_blocking(self, event):
            events.append(event)

    monkeypatch.setattr(module, "get_recorder", lambda: _Recorder())
    world = sandbox_world
    digest = _runtime_digest()
    world.replies["run_command"] = {"ok": True, "result": {"exit_code": 0, "stdout": "", "stderr": "",
        "cwd": ROOT, "started_at": "t",
        "sandbox": {"mode": "default", "profile_digest": digest, "violations": [{}]}}}
    await world.call("run_command", argv=["curl", f"https://{DOM}"], cwd=ROOT, allowed_domains=[DOM])
    await _settle()
    recorded = [event for event in events if event.action_type == "command.sandbox_result"]
    assert len(recorded) == 1
    meta = recorded[0].inputs_meta
    assert meta["profile_digest"] == digest and meta["mode"] == "default" and meta["violation_count"] == 1
    # a run without sandbox metadata records nothing
    world.replies["run_command"] = {"ok": True, "result": {"exit_code": 0, "stdout": "", "stderr": "",
                                                           "cwd": ROOT, "started_at": "t"}}
    await world.call("run_command", argv=["wc", "-l", "notes.md"], cwd=ROOT)
    await _settle()
    assert len([event for event in events if event.action_type == "command.sandbox_result"]) == 1


# ---------------------------------------------------------------- session audit events


async def _violated_world(monkeypatch):
    from audit import recorder as module

    events = []

    class _Recorder:
        async def record(self, event):
            events.append(event)
            return object()

        def record_blocking(self, event):
            events.append(event)

    monkeypatch.setattr(module, "get_recorder", lambda: _Recorder())
    orch = _FakeOrch()
    host_ws = orch.add(_WS(user=OWNER))
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(
        _descriptor(platform="macos", capabilities=["command_sandbox"])))
    controller = orch.add(_WS(user=OWNER, chat="chat-1"))
    task = asyncio.create_task(orch.computer_sessions.start(OWNER, host, controller, "chat-1"))
    await asyncio.sleep(0)
    live = orch.computer_sessions.live_for_host(OWNER, host.host_id)
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "heartbeat", live.session_id, None)
    session = await task
    return orch, host, session, events


async def test_host_violation_events_are_audited_tied_to_the_job(monkeypatch):
    orch, host, session, events = await _violated_world(monkeypatch)
    job_id = _runtime_job_id()
    digest = _runtime_digest()
    await orch.computer_sessions.on_sandbox_violation(OWNER, host.host_id, session.session_id, {
        "session_id": session.session_id, "job_id": job_id, "source": "socket", "operation": "connect",
        "target": "https://unapproved.example", "detail": "egress denied", "blocked": True,
        "sandbox": {"mode": "default", "profile_digest": digest}})
    recorded = [event for event in events if event.action_type == "command.sandbox_violation"]
    assert len(recorded) == 1
    meta = recorded[0].inputs_meta
    assert meta["job_id"] == job_id
    assert meta["violation"]["target"] == "https://unapproved.example" and meta["violation"]["blocked"] is True
    assert meta["profile_digest"] == digest and meta["mode"] == "default"
    # a different owner, host or session is ignored
    for owner, host_id, session_id in ((OTHER, host.host_id, session.session_id),
                                       (OWNER, "other-host", session.session_id),
                                       (OWNER, host.host_id, "other-session")):
        await orch.computer_sessions.on_sandbox_violation(owner, host_id, session_id,
                                                          {"session_id": session_id, "blocked": True})
    assert len([event for event in events if event.action_type == "command.sandbox_violation"]) == 1


async def test_violation_events_without_a_sandbox_block_still_record_bounded(monkeypatch):
    orch, host, session, events = await _violated_world(monkeypatch)
    await orch.computer_sessions.on_sandbox_violation(OWNER, host.host_id, session.session_id,
                                                      {"session_id": session.session_id})
    recorded = [event for event in events if event.action_type == "command.sandbox_violation"]
    assert len(recorded) == 1
    assert recorded[0].inputs_meta["violation"] == "bounded"


async def test_note_sandbox_result_schedules_the_audit_from_async_and_sync(monkeypatch):
    orch, host, session, events = await _violated_world(monkeypatch)
    digest = _runtime_digest()
    orch.computer_sessions.note_sandbox_result(session, "job-1",
                                               {"mode": "default", "profile_digest": digest, "violations": []})
    await _settle()
    recorded = [event for event in events if event.action_type == "command.sandbox_result"]
    assert len(recorded) == 1
    assert recorded[0].inputs_meta["profile_digest"] == digest
    # from a worker thread the orchestrator loop is used instead
    orch.computer_sessions.note_sandbox_result(session, "job-2", {"profile_digest": digest},
                                               loop=asyncio.get_running_loop())
    await _settle()
    recorded = [event for event in events if event.action_type == "command.sandbox_result"]
    assert len(recorded) == 2 and recorded[1].inputs_meta["job_id"] == "job-2"
    # nothing is recorded for an empty sandbox block
    orch.computer_sessions.note_sandbox_result(session, "job-3", {}, loop=asyncio.get_running_loop())
    await _settle()
    assert len([event for event in events if event.action_type == "command.sandbox_result"]) == 2


def test_sandbox_audit_metadata_is_carried_on_the_remote_op_events():
    sandbox = rc._sandbox_meta("run_command", {"sandbox": "escalated", "allowed_domains": [DOM]})
    assert sandbox == {"mode": "escalated", "allowed_domains": [DOM]}
    assert rc._sandbox_meta("run_command", {"command": "ls"}) is None
    assert rc._sandbox_meta("read_output", {"sandbox": "escalated"}) is None
    event = rc._audit_event(OWNER, "remote_op.proposed", "proposed run_command", verb="run_command",
                            sandbox=sandbox)
    assert event.inputs_meta["sandbox"] == {"mode": "escalated", "allowed_domains": [DOM]}
    plain = rc._audit_event(OWNER, "remote_op.proposed", "proposed run_command", verb="run_command")
    assert "sandbox" not in plain.inputs_meta


def test_session_approved_domains_are_bounded_and_normalized():
    from orchestrator.computer_sessions import ComputerSession

    session = ComputerSession(session_id="s", owner_sub=OWNER, host_id="h", host_name="Mac",
                              controller_ws_id=1, controller_device_id="d", controller_label="l", chat_id=CHAT)
    session.note_approved_domains([DOM, "EXAMPLE.com", "", 7, None])
    assert session.approved_domains == {DOM, "example.com"}
    session.note_approved_domains([f"host{i}.example.com" for i in range(70)])
    assert len(session.approved_domains) == 64


# ---------------------------------------------------------------- exact review through the gate


async def test_escalated_runs_and_new_domains_are_never_covered_by_bypass(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime)
    await orch.computer_sessions.set_bypass(host, True)
    escalated = {"argv": ["curl", f"https://{DOM}"], "cwd": ROOT, "sandbox": "escalated"}
    assert hc.bypass_covers(orch, ws, OWNER, CHAT, "computer-use-1", "run_command", escalated, ()) is False
    unseen = {"argv": ["curl", f"https://{NEW_DOM}"], "cwd": ROOT, "allowed_domains": [NEW_DOM]}
    assert hc.bypass_covers(orch, ws, OWNER, CHAT, "computer-use-1", "run_command", unseen, ()) is False
    assert hc.consent_decision(orch, ws, OWNER, CHAT, "computer-use-1", "run_command", escalated, ()) == \
        ("review", None)
    # once the domains are seen in this session, standing consent applies again
    session.note_approved_domains([NEW_DOM])
    assert hc.bypass_covers(orch, ws, OWNER, CHAT, "computer-use-1", "run_command", unseen, ()) is True
    assert not effects


async def test_an_escalated_run_still_raises_the_exact_review_card(task_runtime):
    orch, ws, effects, *_ = task_runtime
    _session_obj, host = await _mac(task_runtime, remote=True)
    await orch.computer_sessions.set_bypass(host, True)
    response = await _call(task_runtime, "run_command", argv=["curl", f"https://{DOM}"], cwd=ROOT,
                           sandbox="escalated")
    assert response.result["_data"]["status"] == "confirmation_required" and not effects
    assert [b["label"] for b in _buttons(response)] == ["Approve", "Decline"]
    texts = _texts(response)
    assert "OUTSIDE the operating-system sandbox" in texts and DOM in texts
    await rc.handle_decision(orch, ws, OWNER, _payload(response, "decline"))


async def test_a_task_grant_does_not_cover_a_sandbox_run_and_the_card_offers_no_grant(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime, remote=True)
    store = orch.computer_sessions.grants
    store.create(OWNER, host.host_id, session.session_id, CHAT, "t", {"kind": "command"}, ("command",),
                 {"argv0": "curl", "root": ROOT}, now=__import__("time").time())
    domain_args = {"argv": ["curl", f"https://{DOM}"], "cwd": ROOT, "allowed_domains": [DOM]}
    assert await hc.granted_dispatch(orch, ws, OWNER, CHAT, "computer-use-1", "run_command",
                                     domain_args, ()) is None
    escalated = {"argv": ["curl", f"https://{DOM}"], "cwd": ROOT, "sandbox": "escalated"}
    assert await hc.granted_dispatch(orch, ws, OWNER, CHAT, "computer-use-1", "run_command",
                                     escalated, ()) is None
    response = await _call(task_runtime, "run_command", **domain_args)
    assert response.result["_data"]["status"] == "confirmation_required"
    assert [b["label"] for b in _buttons(response)] == ["Approve", "Decline"]
    assert DOM in _texts(response) and "Allowed network destinations" in _texts(response)
    assert not effects


async def test_seen_domains_keep_the_task_grant_working(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime, remote=True)
    store = orch.computer_sessions.grants
    store.create(OWNER, host.host_id, session.session_id, CHAT, "t", {"kind": "command"}, ("command",),
                 {"argv0": "curl", "root": ROOT}, now=__import__("time").time())
    session.note_approved_domains([DOM])
    seen = {"argv": ["curl", f"https://{DOM}"], "cwd": ROOT, "allowed_domains": [DOM]}
    response = await _call(task_runtime, "run_command", **seen)
    # a seen domain stays covered by the task grant, so it runs without a card
    assert response.error is None and response.result["_data"]["ok"]
    assert len(effects) == 1


def test_violation_summaries_cope_with_raw_and_non_dict_rows():
    rows = mcp_tools._sandbox_violation_summary({"detail": "solo", "blocked": False})
    assert rows == [{"source": "", "operation": "", "target": "", "detail": "solo", "blocked": False}]
    mixed = mcp_tools._sandbox_violation_summary(["raw text", 7, {"source": "socket", "detail": "d"}])
    assert mixed[0] == {"detail": "raw text"} and mixed[1] == {"detail": "7"}
    assert mixed[2]["source"] == "socket" and mixed[2]["blocked"] is False


def test_violation_taint_marking_survives_a_broken_tracker(monkeypatch):
    monkeypatch.setattr(taint, "taint_enabled", lambda: True)
    world = SimpleNamespace(_taint_tracker=lambda _chat: SimpleNamespace(
        record_output=lambda *_a: (_ for _ in ()).throw(RuntimeError("tracker down"))))
    monkeypatch.setattr(mcp_tools, "_ORCH", world)
    mcp_tools._taint_sandbox_violations("chat-1", [{"detail": "d"}])  # must not raise
    mcp_tools._taint_sandbox_violations(None, [{"detail": "d"}])  # no chat: no-op


async def test_violation_events_ignore_non_dict_payloads(monkeypatch):
    orch, host, session, events = await _violated_world(monkeypatch)
    await orch.computer_sessions.on_sandbox_violation(OWNER, host.host_id, session.session_id, "not-a-dict")
    assert not [event for event in events if event.action_type == "command.sandbox_violation"]


async def test_note_sandbox_result_is_silent_outside_a_loop_and_for_dead_loops(monkeypatch):
    orch, host, session, events = await _violated_world(monkeypatch)
    result: list = []

    def worker():
        # no running loop on this thread: the note is dropped without raising
        result.append(orch.computer_sessions.note_sandbox_result(session, "job-x", {"mode": "default"}))

    await asyncio.to_thread(worker)
    await _settle()
    assert result == [None]
    assert not [event for event in events if event.action_type == "command.sandbox_result"]
    # a closed loop makes run_coroutine_threadsafe raise: still swallowed
    closed = asyncio.new_event_loop()
    closed.close()
    orch.computer_sessions.note_sandbox_result(session, "job-y", {"mode": "default"}, loop=closed)
    await _settle()
    assert not [event for event in events if event.action_type == "command.sandbox_result"]


async def test_the_host_event_stream_records_sandbox_violations(monkeypatch):
    orch, host, session, events = await _violated_world(monkeypatch)
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "computer_use", True)
    orch._tunnel_ingress_over_cap = lambda _owner: False
    job_id = _runtime_job_id()
    payload = {"event": "command.sandbox_violation", "host_id": host.host_id,
               "session_id": session.session_id, "job_id": job_id, "blocked": True}
    from orchestrator.orchestrator import Orchestrator

    await Orchestrator._handle_computer_event(orch, host.websocket, SimpleNamespace(payload=payload))
    assert [event.action_type for event in events if event.action_type == "command.sandbox_violation"]
