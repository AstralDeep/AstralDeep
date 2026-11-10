"""Tests for orchestrator/computer_effect_intents.py and its host-request integration: intent
commits around Mac effect requests, typed failure when the intent write fails, reconciliation of
open intents after an interruption, owner acknowledgment with exactly one recovered resume input,
and command-job reconciliation against the host's own retained job list.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from agents.computer_use import mcp_tools
from orchestrator.computer_effect_intents import (
    APPROVAL_KIND,
    INTENT_KIND,
    EffectIntentError,
    EffectIntentLedger,
    argument_digest,
    resume_text,
)
from orchestrator.computer_hosts import ComputerHostError
from shared.protocol import ComputerHostDescriptor
from tests.test_computer_use_076 import OWNER, _FakeOrch, _descriptor, _session, _WS

CHAT = "chat-1"
INCARNATION = str(uuid.uuid4())
FOREIGN_JOB = "11111111-2222-4333-8444-555555555555"


class _Status:
    def __init__(self, value: str):
        self.value = value


class _Row:
    def __init__(self, step_id, owner_id, conversation_id, kind, name, args_truncated, started_at):
        self.step_id = step_id
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.kind = kind
        self.name = name
        self.args_truncated = args_truncated
        self.started_at = started_at
        self.status = _Status("in_progress")
        self.ended_at = None
        self.result_summary = None


class _FakeStepRepository:
    def __init__(self, fail_writes=False):
        self.rows: dict = {}
        self.fail_writes = fail_writes

    def create_step(self, transaction, *, step_id, owner_id, conversation_id, turn_message_id,
                    kind, name, args_truncated, args_was_truncated, started_at):
        if self.fail_writes:
            raise RuntimeError("database unavailable")
        existing = self.rows.get(step_id)
        if existing is not None:
            if existing.owner_id != owner_id:
                raise _Conflict("owner mismatch")
            return existing
        row = _Row(step_id, owner_id, conversation_id, kind, name, args_truncated, started_at)
        self.rows[step_id] = row
        return row

    def list_steps(self, transaction, *, owner_id, conversation_id, limit=1000):
        rows = [row for row in self.rows.values()
                if row.owner_id == owner_id and row.conversation_id == conversation_id]
        return tuple(sorted(rows, key=lambda row: (row.started_at, row.step_id)))[:limit]

    def get_step(self, transaction, *, owner_id, step_id):
        row = self.rows.get(step_id)
        return row if row is not None and row.owner_id == owner_id else None

    def finish_step(self, transaction, *, owner_id, step_id, expected_status, status, ended_at,
                    result_summary=None, result_was_truncated=False, error_message=None):
        if self.fail_writes:
            raise RuntimeError("database unavailable")
        row = self.rows.get(step_id)
        if row is None or row.owner_id != owner_id:
            raise _NotFound("owner-scoped step was not found")
        if row.status.value != expected_status or row.ended_at is not None:
            raise _Conflict("stale fence")
        row.status = _Status(status)
        row.ended_at = ended_at
        row.result_summary = result_summary
        return row


class _Conflict(Exception):
    pass


class _NotFound(Exception):
    pass


class _FakeConversationRepository:
    def __init__(self, chats):
        self._chats = chats

    def list_recent(self, transaction, *, owner_id, limit=20):
        return [SimpleNamespace(conversation_id=chat) for chat in self._chats[:limit]]


class _FakeContext:
    def __init__(self, repository):
        self.repository = repository

    def call(self, operation, **kwargs):
        return operation(None, **kwargs)


class _Recorder:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)
        return event

    def record_blocking(self, event):
        self.events.append(event)
        return event

    def actions(self):
        return [event.action_type for event in self.events]


def _wired_orch(steps, chats=(CHAT,), recorder=None):
    orch = _AnsweringOrch()
    orch.audit_recorder = recorder or _Recorder()
    orch.computer_effect_intents = EffectIntentLedger(
        orch, context=_FakeContext(steps),
        conversations=_FakeContext(_FakeConversationRepository(list(chats))))
    mcp_tools.register_deps(orch)
    return orch


def _ledger(**kwargs):
    steps = _FakeStepRepository(fail_writes=kwargs.pop("fail_writes", False))
    rows = kwargs.pop("rows", ())
    for row in rows:
        steps.rows[row.step_id] = row
    orch = _wired_orch(steps, **kwargs)
    return orch.computer_effect_intents, steps, orch


def _host(**over):
    descriptor = _descriptor(platform="macos", incarnation_id=INCARNATION, **over)
    return SimpleNamespace(host_id=descriptor["host_id"], owner_sub=OWNER, name=descriptor["name"],
                           descriptor=descriptor)


def _effect_row(step_id=None, chat=CHAT, verb="click", owner=OWNER, host_id="host-00000000",
                incarnation=INCARNATION):
    payload = json.dumps({
        "operation_id": step_id or f"creq_{uuid.uuid4().hex}",
        "verb": verb,
        "argument_digest": "a" * 64,
        "host_id": host_id,
        "host_incarnation": incarnation,
        "session_id": "session-1",
        "summary": "clicked things",
    })
    return _Row(step_id or f"creq_{uuid.uuid4().hex}", owner, chat, INTENT_KIND, verb, payload, 1)


async def _settle():
    for _ in range(8):
        await asyncio.sleep(0)


class _Answering(_WS):
    def __init__(self, orch, owner, answers):
        super().__init__(user=owner)
        self._orch = orch
        self._owner = owner
        self._answers = answers

    def push(self, data):
        self.sent.append(data)
        frame = json.loads(data)
        if frame.get("type") == "computer_request":
            reply = self._answers(frame["verb"], frame["args"])
            asyncio.get_running_loop().call_soon(
                self._orch.computer_hosts.handle_response, self, self._owner,
                {"request_id": frame["request_id"], **reply})


class _AnsweringOrch(_FakeOrch):
    async def _safe_send(self, ws, data: str) -> bool:
        if isinstance(ws, _Answering):
            ws.push(data)
            return True
        ws.sent.append(data)
        return True


def _reattach(orch, host, answers):
    orch.ui_clients.remove(host.websocket)
    answering = _Answering(orch, OWNER, answers)
    orch.ui_sessions[answering] = {"sub": OWNER}
    fresh, _superseded = orch.computer_hosts.register(
        OWNER, answering, ComputerHostDescriptor.from_dict(host.descriptor))
    return fresh, answering


async def _mac_session(orch, retained=()):
    descriptor = _descriptor(platform="macos", incarnation_id=INCARNATION, retained_jobs=list(retained))
    host_ws = orch.add(_WS(user=OWNER))
    host, _superseded = orch.computer_hosts.register(OWNER, host_ws,
                                                     ComputerHostDescriptor.from_dict(descriptor))
    controller = orch.add(_WS(chat=CHAT))
    session = await _session(orch, host, controller, chat=CHAT)
    return host, controller, session


def _retained(job_id, state="running", exit_code=None):
    return {"job_id": job_id, "state": state, "exit_code": exit_code, "signal": None,
            "output_bytes": 12}


# --- ledger state machine ---------------------------------------------------------------


async def test_intent_write_failure_refuses_the_request_with_a_typed_error():
    ledger, _steps, _orch = _ledger(fail_writes=True)
    with pytest.raises(EffectIntentError) as failed:
        await ledger.begin_request(owner_id=OWNER, host=_host(), session_id="s-1", chat_id=CHAT,
                                   verb="click", args={"x": 1}, operation_id="creq_" + "1" * 32)
    assert failed.value.code == "effect_intent_failed"
    assert ledger.live_count() == 0


async def test_unknown_outcomes_block_new_effects_until_resolved():
    ledger, _steps, _orch = _ledger()
    intent = await ledger.begin_request(owner_id=OWNER, host=_host(), session_id="s-1", chat_id=CHAT,
                                        verb="click", args={}, operation_id="creq_" + "2" * 32)
    await ledger.outcome_lost(intent, reason="host left")
    with pytest.raises(EffectIntentError) as blocked:
        await ledger.begin_request(owner_id=OWNER, host=_host(), session_id="s-1", chat_id=CHAT,
                                   verb="click", args={}, operation_id="creq_" + "3" * 32)
    assert blocked.value.code == "chat_blocked_for_acknowledgment"
    assert ledger.unknown_for(OWNER, CHAT)


async def test_commit_outcome_finishes_the_row_and_audits_both_sides():
    recorder = _Recorder()
    ledger, steps, _orch = _ledger(recorder=recorder)
    operation = "creq_" + "4" * 32
    await ledger.begin_request(owner_id=OWNER, host=_host(), session_id="s-1", chat_id=CHAT,
                               verb="run_command", args={"argv": ["wc", "-l"]}, operation_id=operation)
    assert steps.rows[operation].status.value == "in_progress"
    assert await ledger.commit_outcome(operation, ok=True) is True
    assert steps.rows[operation].status.value == "completed"
    assert steps.rows[operation].ended_at is not None
    actions = recorder.actions()
    assert actions.count("effect.intent") == 1 and actions.count("effect.committed") == 1
    intent_event = next(event for event in recorder.events if event.action_type == "effect.intent")
    assert intent_event.correlation_id == operation
    digest = intent_event.inputs_meta["argument_digest"]
    assert digest == argument_digest("run_command", {"argv": ["wc", "-l"]})
    assert await ledger.commit_outcome(operation, ok=True) is False


async def test_commit_failure_surfaces_unknown_rather_than_hiding_it():
    recorder = _Recorder()
    ledger, steps, _orch = _ledger(recorder=recorder)
    operation = "creq_" + "5" * 32
    await ledger.begin_request(owner_id=OWNER, host=_host(), session_id="s-1", chat_id=CHAT,
                               verb="click", args={}, operation_id=operation)
    steps.fail_writes = True
    assert await ledger.commit_outcome(operation, ok=True) is False
    assert steps.rows[operation].status.value == "in_progress"
    assert ledger.unknown_for(OWNER, CHAT)
    assert recorder.actions().count("effect.unknown") == 1


async def test_outcome_lost_audits_once_and_stays_unknown():
    recorder = _Recorder()
    ledger, steps, _orch = _ledger(recorder=recorder)
    operation = "creq_" + "6" * 32
    intent = await ledger.begin_request(owner_id=OWNER, host=_host(), session_id="s-1", chat_id=CHAT,
                                        verb="type_text", args={}, operation_id=operation)
    await ledger.outcome_lost(intent, reason="socket lost")
    await ledger.outcome_lost(intent, reason="socket lost")
    assert recorder.actions().count("effect.unknown") == 1
    assert steps.rows[operation].status.value == "in_progress"


async def test_reconcile_chat_marks_orphan_intents_unknown_and_keeps_rows_open():
    recorder = _Recorder()
    orphan = _effect_row()
    ledger, steps, _orch = _ledger(recorder=recorder)
    steps.rows[orphan.step_id] = orphan
    recovered = await ledger.reconcile_chat(OWNER, CHAT)
    assert [item.operation_id for item in recovered] == [orphan.step_id]
    assert ledger.unknown_for(OWNER, CHAT)[0].verb == "click"
    assert steps.rows[orphan.step_id].status.value == "in_progress"
    assert recorder.actions().count("effect.unknown") == 1
    assert await ledger.reconcile_chat(OWNER, CHAT) == ()


async def test_reconcile_chat_skips_live_in_flight_intents():
    ledger, steps, _orch = _ledger()
    operation = "creq_" + "7" * 32
    await ledger.begin_request(owner_id=OWNER, host=_host(), session_id="s-1", chat_id=CHAT,
                               verb="click", args={}, operation_id=operation)
    assert await ledger.reconcile_chat(OWNER, CHAT) == ()
    assert steps.rows[operation].status.value == "in_progress"
    assert ledger.unknown_for(OWNER, CHAT) == ()


async def test_reconcile_owner_scans_the_owners_recent_chats():
    other_chat = "chat-2"
    first, second = _effect_row(chat=CHAT), _effect_row(chat=other_chat)
    ledger, steps, _orch = _ledger(chats=(CHAT, other_chat, "chat-3"))
    steps.rows[first.step_id] = first
    steps.rows[second.step_id] = second
    recovered = await ledger.reconcile_owner(OWNER)
    assert {item.operation_id for item in recovered} == {first.step_id, second.step_id}


async def test_acknowledge_resolves_rows_once_and_gates_one_resume_input():
    recorder = _Recorder()
    host = _host()
    first, second = _effect_row(host_id=host.host_id), _effect_row(host_id=host.host_id)
    ledger, steps, _orch = _ledger(recorder=recorder)
    for row in (first, second):
        steps.rows[row.step_id] = row
    await ledger.reconcile_owner(OWNER)
    resolved = await ledger.resolve_owner_unknowns(OWNER, host.host_id, decision="acknowledge")
    assert sorted(resolved) == [CHAT]
    assert all(item.state.value == "acknowledged" and item.decision == "acknowledge"
               for item in resolved[CHAT])
    for row in (first, second):
        assert steps.rows[row.step_id].status.value == "interrupted"
    assert ledger.unknown_for(OWNER, CHAT) == ()
    assert recorder.actions().count("effect.acknowledged") == 2
    assert ledger.grant_resume_input(OWNER, CHAT, INCARNATION) is True
    assert ledger.grant_resume_input(OWNER, CHAT, INCARNATION) is False
    assert ledger.grant_resume_input(OWNER, CHAT, str(uuid.uuid4())) is True


async def test_discard_cancels_the_rows_instead_of_marking_them_interrupted():
    host = _host()
    orphan = _effect_row(host_id=host.host_id)
    ledger, steps, _orch = _ledger()
    steps.rows[orphan.step_id] = orphan
    await ledger.reconcile_owner(OWNER)
    resolved = await ledger.resolve_owner_unknowns(OWNER, host.host_id, decision="discard")
    assert resolved[CHAT][0].decision == "discard"
    assert steps.rows[orphan.step_id].status.value == "cancelled"


async def test_resolution_is_scoped_to_the_named_host():
    orphan = _effect_row()
    ledger, steps, _orch = _ledger()
    steps.rows[orphan.step_id] = orphan
    await ledger.reconcile_owner(OWNER)
    other = _host()
    other.descriptor["host_id"] = "host-" + "f" * 8
    assert await ledger.resolve_owner_unknowns(OWNER, other.host_id, decision="acknowledge") == {}
    assert ledger.unknown_for(OWNER, CHAT)


async def test_lost_approval_cards_are_reported_once_the_card_is_gone():
    ledger, steps, _orch = _ledger()
    ledger.approval_opened(proposal_id="a" * 32, owner_id=OWNER, chat_id=CHAT,
                           tool_name="run_command", summary="run wc -l")
    marker_id = "apr_" + "a" * 32
    assert steps.rows[marker_id].kind == APPROVAL_KIND
    assert await ledger.reconcile_chat(OWNER, CHAT) == ()
    assert ledger.lost_approvals(OWNER, CHAT) == ()
    restarted, _steps2, _orch2 = _ledger()
    restarted._injected = ledger._injected
    restarted._conversation_context = ledger._conversation_context
    await restarted.reconcile_chat(OWNER, CHAT)
    assert restarted.lost_approvals(OWNER, CHAT) == ("run_command",)
    ledger.approval_resolved("a" * 32, note="declined")
    assert steps.rows[marker_id].status.value == "completed"


def test_resume_text_names_actions_the_decision_and_lost_approvals():
    intents = tuple(SimpleNamespace(verb="run_command") for _ in range(2)) \
        + (SimpleNamespace(verb="write_file"),)
    text = resume_text("MacBook", intents, ("run_command",), "acknowledge")
    assert text.startswith("[Recovered]")
    assert "run_command ×2" in text and "write_file" in text
    assert "acknowledged" in text and "MacBook" in text
    assert "approval card for run_command" in text
    discarded = resume_text("MacBook", intents, (), "discard")
    assert "discarded" in discarded and "approval card" not in discarded


def test_argument_digest_ignores_private_and_order_noise():
    assert argument_digest("click", {"x": 1, "_runtime": object()}) == argument_digest("click", {"x": 1})
    assert argument_digest("click", {"x": 1}) != argument_digest("click", {"x": 2})
    assert argument_digest("click", {"x": 1}) != argument_digest("type_text", {"x": 1})
    assert argument_digest("click", "not-a-mapping") == argument_digest("click", None)


# --- host request path integration ------------------------------------------------------


async def test_effect_request_commits_intent_before_send_and_outcome_after():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, session = await _mac_session(orch)
    seen_in_flight = []

    def _answers(verb, args):
        open_rows = [row for row in steps.rows.values()
                     if row.kind == INTENT_KIND and row.status.value == "in_progress"]
        seen_in_flight.append(bool(open_rows))
        return {"ok": True, "result": {}}

    fresh, answering = _reattach(orch, host, _answers)
    loop = asyncio.get_running_loop()
    result, err = await asyncio.to_thread(
        mcp_tools._run, fresh, session, "open_app", {"app": "TextEdit"}, loop)
    assert err is None and result is not None
    await _settle()
    assert any(frame["verb"] == "open_app" for frame in answering.frames("computer_request"))
    assert seen_in_flight == [True]
    rows = [row for row in steps.rows.values() if row.kind == INTENT_KIND]
    assert len(rows) == 1 and rows[0].status.value == "completed"
    actions = orch.audit_recorder.actions()
    assert "effect.intent" in actions and "effect.committed" in actions


async def test_intent_write_failure_sends_nothing_and_reports_typed_error():
    steps = _FakeStepRepository(fail_writes=True)
    orch = _wired_orch(steps)
    host, _controller, session = await _mac_session(orch)
    with pytest.raises(ComputerHostError) as refused:
        await orch.computer_hosts.request(host, session.session_id, "click", {"x": 1, "hwnd": 7}, 5)
    assert refused.value.code == "effect_intent_failed"
    assert refused.value.effect_state == "not_started"
    assert host.websocket.frames("computer_request") == []
    assert steps.rows == {}


async def test_observe_requests_write_no_intents():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, session = await _mac_session(orch)
    fresh, _answering = _reattach(orch, host, lambda verb, args: {"ok": True, "result": {
        "screen_index": 0, "width": 10, "height": 10, "scale": 1.0, "media_type": "image/jpeg",
        "base64": "aW1hZ2U="}})
    loop = asyncio.get_running_loop()
    result, err = await asyncio.to_thread(mcp_tools._run, fresh, session, "screenshot", {}, loop)
    assert err is None and result
    assert [row for row in steps.rows.values() if row.kind == INTENT_KIND] == []


async def test_timeout_marks_the_intent_unknown():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, session = await _mac_session(orch)
    with pytest.raises(ComputerHostError) as lost:
        await orch.computer_hosts.request(host, session.session_id, "click", {"x": 1, "hwnd": 7}, 0.05)
    assert lost.value.code == "outcome_uncertain"
    assert orch.computer_effect_intents.unknown_for(OWNER, CHAT)


async def test_blocked_chat_refuses_effects_but_allows_observation():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, session = await _mac_session(orch)
    orphan = _effect_row(host_id=host.host_id)
    steps.rows[orphan.step_id] = orphan
    await orch.computer_effect_intents.reconcile_chat(OWNER, CHAT)
    with pytest.raises(ComputerHostError) as blocked:
        await orch.computer_hosts.request(host, session.session_id, "click", {"x": 1, "hwnd": 7}, 5)
    assert blocked.value.code == "chat_blocked_for_acknowledgment"
    assert host.websocket.frames("computer_request") == []
    fresh, _answering = _reattach(orch, host, lambda verb, args: {"ok": True, "result": {
        "screen_index": 0, "width": 10, "height": 10, "scale": 1.0, "media_type": "image/jpeg",
        "base64": "aW1hZ2U="}})
    loop = asyncio.get_running_loop()
    result, err = await asyncio.to_thread(mcp_tools._run, fresh, session, "screenshot", {}, loop)
    assert err is None and result


# --- acknowledgment, resume input, dispatch gate ----------------------------------------


async def test_resolve_outcome_unknowns_appends_exactly_one_resume_input():
    from orchestrator.orchestrator import Orchestrator

    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    orch._resolve_computer_outcome_unknowns = Orchestrator._resolve_computer_outcome_unknowns.__get__(orch)
    host, _controller, _session = await _mac_session(orch)
    orphan = _effect_row(host_id=host.host_id)
    steps.rows[orphan.step_id] = orphan
    await orch.computer_effect_intents.reconcile_owner(OWNER)
    appended = []

    async def _record(stage, *, chat_id, user_id, role, content):
        appended.append((chat_id, role, content))

    orch._append_conversation_message = _record
    await orch._resolve_computer_outcome_unknowns(OWNER, host, "acknowledge", INCARNATION)
    assert len(appended) == 1
    chat_id, role, content = appended[0]
    assert (chat_id, role) == (CHAT, "user")
    assert content.startswith("[Recovered]") and "click" in content
    await orch._resolve_computer_outcome_unknowns(OWNER, host, "acknowledge", INCARNATION)
    assert len(appended) == 1


async def test_dispatch_gate_blocks_unresolved_chats_and_reports_the_actions(monkeypatch):
    from orchestrator.orchestrator import Orchestrator
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "computer_use", True)
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    orch._effect_acknowledgment_gate = Orchestrator._effect_acknowledgment_gate.__get__(orch)
    host = _host()
    orphan = _effect_row(verb="run_command", host_id=host.host_id)
    steps.rows[orphan.step_id] = orphan
    blocked = await orch._effect_acknowledgment_gate(OWNER, CHAT)
    assert blocked is not None and "run_command" in blocked
    assert await orch._effect_acknowledgment_gate(OWNER, "chat-unknown") is None
    await orch.computer_effect_intents.resolve_owner_unknowns(
        OWNER, host.host_id, decision="acknowledge")
    assert await orch._effect_acknowledgment_gate(OWNER, CHAT) is None


async def test_host_registration_reconciles_open_intents_through_the_real_flow(monkeypatch):
    from orchestrator.orchestrator import Orchestrator

    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    orch._register_computer_host = Orchestrator._register_computer_host.__get__(orch)
    orphan = _effect_row()
    steps.rows[orphan.step_id] = orphan
    descriptor = ComputerHostDescriptor.from_dict(_descriptor(platform="macos", incarnation_id=INCARNATION))
    ws = orch.add(_WS(user=OWNER))
    await orch._register_computer_host(ws, OWNER, descriptor)
    assert orch.computer_effect_intents.unknown_for(OWNER, CHAT)
    assert orch.audit_recorder.actions().count("effect.unknown") == 1


async def test_chat_message_dispatch_refuses_when_the_chat_is_blocked(monkeypatch):
    from orchestrator.orchestrator import Orchestrator
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "computer_use", True)
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host = _host()
    orphan = _effect_row(verb="run_command", host_id=host.host_id)
    steps.rows[orphan.step_id] = orphan
    orch._handle_chat_message_impl = Orchestrator._handle_chat_message_impl.__get__(orch)
    orch._effect_acknowledgment_gate = Orchestrator._effect_acknowledgment_gate.__get__(orch)
    rendered = []

    async def send_ui_render(ws, components, target=None, speak=True):
        rendered.append(components)

    orch.send_ui_render = send_ui_render
    ws = orch.add(_WS(chat=CHAT))
    await orch._handle_chat_message_impl(ws, "hello", CHAT, user_id=OWNER)
    assert rendered and "run_command" in json.dumps(rendered[0])
    assert ws.frames("chat_status")[-1]["status"] == "done"


async def test_dispatch_gate_ignores_other_owners(monkeypatch):
    from orchestrator.orchestrator import Orchestrator
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "computer_use", True)
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    orch._effect_acknowledgment_gate = Orchestrator._effect_acknowledgment_gate.__get__(orch)
    foreign = _effect_row(owner="someone-else")
    steps.rows[foreign.step_id] = foreign
    assert await orch._effect_acknowledgment_gate(OWNER, CHAT) is None


# --- command job reconciliation against the host's own list -----------------------------


def _tool_kwargs(host, **over):
    return {"user_id": OWNER, "session_id": CHAT, "computer": host.host_id,
            "_runtime": SimpleNamespace(loop=asyncio.get_running_loop()), **over}


async def test_read_output_reconciles_a_job_from_the_hosts_own_list():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, session = await _mac_session(
        orch, retained=[_retained(FOREIGN_JOB, state="exited", exit_code=3)])
    asked = []

    def _answers(verb, args):
        asked.append((verb, args.get("job_id")))
        assert verb == "read_output" and args["job_id"] == FOREIGN_JOB
        return {"ok": True, "result": {"state": "exited", "exit_code": 3, "chunks": [],
                                       "next_offset": 0}}

    fresh, _answering = _reattach(orch, host, _answers)
    outcome = await asyncio.to_thread(mcp_tools.read_output, **_tool_kwargs(fresh, job_id=FOREIGN_JOB))
    assert outcome["_data"]["reconciled"] is True
    assert outcome["_data"]["state"] == "exited" and outcome["_data"]["exit_code"] == 3
    assert asked == [("read_output", FOREIGN_JOB)]
    assert session.command_jobs.get(FOREIGN_JOB) is None


async def test_read_output_without_a_host_record_stays_unknown():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, _session = await _mac_session(orch, retained=[])
    outcome = await asyncio.to_thread(mcp_tools.read_output, **_tool_kwargs(host, job_id=FOREIGN_JOB))
    assert outcome["_data"]["code"] == "unknown_job"


async def test_interrupt_command_reconciles_from_the_host_list_without_rerunning():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, _session = await _mac_session(orch, retained=[_retained(FOREIGN_JOB)])
    verbs = []

    def _answers(verb, args):
        verbs.append(verb)
        assert verb == "interrupt_command" and args["job_id"] == FOREIGN_JOB
        return {"ok": True, "result": {"state": "interrupted", "exit_code": None}}

    fresh, _answering = _reattach(orch, host, _answers)
    outcome = await asyncio.to_thread(mcp_tools.interrupt_command,
                                      **_tool_kwargs(fresh, job_id=FOREIGN_JOB))
    assert outcome["_data"]["state"] == "interrupted"
    assert outcome["_data"]["reconciled"] is True
    assert verbs == ["interrupt_command"]


async def test_interrupt_command_refuses_jobs_the_host_does_not_report():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, _session = await _mac_session(orch, retained=[])
    outcome = await asyncio.to_thread(mcp_tools.interrupt_command,
                                      **_tool_kwargs(host, job_id=FOREIGN_JOB))
    assert outcome["_data"]["code"] == "unknown_job"


# --- approval markers in the card flow --------------------------------------------------


class _FakeProposalRepository:
    def __init__(self):
        self.rows = {}

    def create(self, transaction, record):
        self.rows[record.proposal_id] = record
        return record

    def get(self, transaction, *, owner_id, proposal_id):
        record = self.rows.get(proposal_id)
        if record is None or record.owner_id != owner_id:
            return None
        return record


class _FakeRuntime:
    from contextlib import contextmanager

    @staticmethod
    @contextmanager
    def transaction():
        yield None


async def test_computer_approval_cards_leave_an_open_marker_and_decisions_close_it():
    from orchestrator import remote_confirmation

    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, _session = await _mac_session(orch)
    orch.runtime_composition = SimpleNamespace(
        plane=SimpleNamespace(runtime=_FakeRuntime(),
                              repositories=SimpleNamespace(
                                  remote_operation_proposals=_FakeProposalRepository())))
    proposal_id, card = remote_confirmation._create_proposal(
        orch, OWNER, CHAT, "computer-use-1", "run_command",
        {"argv": ["wc", "-l"], "cwd": "/Users/sam/Projects/x", "timeout_s": 30,
         "computer": host.host_id}, websocket=host.websocket)
    marker_id = f"apr_{proposal_id}"
    assert marker_id in steps.rows and steps.rows[marker_id].kind == APPROVAL_KIND
    assert orch.computer_effect_intents._approval_markers[marker_id][0] == OWNER
    orch.computer_effect_intents.approval_resolved(proposal_id, note="approved")
    assert steps.rows[marker_id].status.value == "completed"


def test_create_proposal_without_a_chat_writes_no_marker():
    from orchestrator import remote_confirmation

    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    remote_confirmation._open_computer_approval_marker(
        orch, "a" * 32, OWNER, None, "run_command", "summary")
    assert steps.rows == {}


def test_marker_write_failures_never_break_the_card(monkeypatch):
    from orchestrator import remote_confirmation

    steps = _FakeStepRepository()
    orch = _wired_orch(steps)

    def _explode(**kwargs):
        raise RuntimeError("plane down")

    monkeypatch.setattr(orch.computer_effect_intents, "approval_opened", _explode)
    remote_confirmation._open_computer_approval_marker(
        orch, "a" * 32, OWNER, CHAT, "run_command", "summary")
    assert steps.rows == {}


async def test_handle_decision_closes_the_marker_before_validating_the_proposal():
    from orchestrator import remote_confirmation

    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    ledger = orch.computer_effect_intents
    ledger.approval_opened(proposal_id="d" * 32, owner_id=OWNER, chat_id=CHAT,
                           tool_name="run_command", summary="text")
    orch.runtime_composition = SimpleNamespace(
        plane=SimpleNamespace(runtime=_FakeRuntime(),
                              repositories=SimpleNamespace(
                                  remote_operation_proposals=_FakeProposalRepository())))

    async def send_ui_render(ws, components, target=None, speak=True):
        pass

    orch.send_ui_render = send_ui_render
    ws = orch.add(_WS(chat=CHAT))

    def _explode(proposal_id, *, note=""):
        raise RuntimeError("plane down")

    ledger.approval_resolved = _explode
    await remote_confirmation.handle_decision(orch, ws, OWNER,
                                              {"proposal_id": "d" * 32, "decision": "approve"})
    assert "apr_" + "d" * 32 in ledger._approval_markers


# --- restart posture -------------------------------------------------------------------


async def test_effects_without_a_chat_are_refused_and_teardown_skips_the_ledger():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    descriptor = _descriptor(platform="macos", incarnation_id=INCARNATION)
    host_ws = orch.add(_WS(user=OWNER))
    host, _superseded = orch.computer_hosts.register(OWNER, host_ws,
                                                     ComputerHostDescriptor.from_dict(descriptor))
    controller = orch.add(_WS())
    session = await _session(orch, host, controller, chat=None)
    with pytest.raises(ComputerHostError) as refused:
        await orch.computer_hosts.request(host, session.session_id, "click", {"x": 1, "hwnd": 7}, 5)
    assert refused.value.code == "effect_intent_failed"
    assert host_ws.frames("computer_request") == []
    assert await orch.computer_hosts._begin_effect_intent(
        host, "missing-session", "click", {}, "creq_" + "f" * 32) is None


async def test_reconciled_read_validates_stream_and_wait_arguments():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, _session = await _mac_session(orch, retained=[_retained(FOREIGN_JOB)])
    bad_stream = await asyncio.to_thread(
        mcp_tools.read_output, **_tool_kwargs(host, job_id=FOREIGN_JOB, stream="bogus"))
    assert bad_stream["_data"]["code"] == "out_of_range"
    bad_wait = await asyncio.to_thread(
        mcp_tools.read_output, **_tool_kwargs(host, job_id=FOREIGN_JOB, wait_s=99))
    assert bad_wait["_data"]["code"] == "out_of_range"
    bad_signal = await asyncio.to_thread(
        mcp_tools.interrupt_command, **_tool_kwargs(host, job_id=FOREIGN_JOB, signal="BOGUS"))
    assert bad_signal["_data"]["code"] == "out_of_range"


async def test_reconciled_read_ignores_non_matching_retained_records():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, _session = await _mac_session(orch, retained=[_retained(FOREIGN_JOB)])
    outcome = await asyncio.to_thread(
        mcp_tools.read_output, **_tool_kwargs(host, job_id="99999999-9999-4999-8999-999999999999"))
    assert outcome["_data"]["code"] == "unknown_job"


async def test_restart_never_restores_bypass_or_grants_from_the_server_side():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, session = await _mac_session(orch, retained=[_retained(str(uuid.uuid4()))])
    assert orch.computer_sessions.bypass_on(OWNER, host.host_id) is False
    await orch.computer_effect_intents.reconcile_owner(OWNER)
    assert orch.computer_sessions.bypass_on(OWNER, host.host_id) is False
    assert orch.computer_sessions.grants.list(session.session_id) == ()


# --- refusals, degradation, and lazy plane resolution ------------------------------------


async def test_real_orchestrator_wires_the_effect_ledger(orchestrator_factory):
    from orchestrator.computer_effect_intents import EffectIntentLedger

    orch = await asyncio.to_thread(orchestrator_factory)
    assert isinstance(orch.computer_effect_intents, EffectIntentLedger)
    assert orch.computer_effect_intents.live_count() == 0


async def test_requests_still_flow_when_the_ledger_is_absent():
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    del orch.computer_effect_intents
    host, _controller, session = await _mac_session(orch)
    fresh, _answering = _reattach(orch, host, lambda verb, args: {"ok": True, "result": {}})
    result = await orch.computer_hosts.request(fresh, session.session_id, "click", {"x": 1, "hwnd": 7}, 5)
    assert result is not None
    assert [row for row in steps.rows.values() if row.kind == INTENT_KIND] == []


async def test_lost_outcome_marking_failures_do_not_mask_the_host_error(monkeypatch):
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, session = await _mac_session(orch)

    async def _explode(intent, *, reason):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(orch.computer_effect_intents, "outcome_lost", _explode)
    with pytest.raises(ComputerHostError) as lost:
        await orch.computer_hosts.request(host, session.session_id, "click", {"x": 1, "hwnd": 7}, 0.05)
    assert lost.value.code == "outcome_uncertain"


async def test_outcome_commit_failures_do_not_break_the_response(monkeypatch):
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    host, _controller, session = await _mac_session(orch)

    async def _explode(operation_id, *, ok, error_code=""):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(orch.computer_effect_intents, "commit_outcome", _explode)
    fresh, _answering = _reattach(orch, host, lambda verb, args: {"ok": True, "result": {}})
    loop = asyncio.get_running_loop()
    result, err = await asyncio.to_thread(mcp_tools._run, fresh, session, "open_app", {"app": "TextEdit"}, loop)
    assert err is None


async def test_gate_tolerates_a_broken_reconciliation(monkeypatch):
    from orchestrator.orchestrator import Orchestrator
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "computer_use", True)
    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    orch._effect_acknowledgment_gate = Orchestrator._effect_acknowledgment_gate.__get__(orch)

    async def _explode(owner_id, chat_id):
        raise RuntimeError("plane down")

    monkeypatch.setattr(orch.computer_effect_intents, "reconcile_chat", _explode)
    assert await orch._effect_acknowledgment_gate(OWNER, CHAT) is None
    assert await orch._effect_acknowledgment_gate(None, CHAT) is None
    assert await orch._effect_acknowledgment_gate(OWNER, None) is None


async def test_resolution_and_resume_input_failures_are_contained():
    from orchestrator.orchestrator import Orchestrator

    steps = _FakeStepRepository()
    orch = _wired_orch(steps)
    orch._resolve_computer_outcome_unknowns = Orchestrator._resolve_computer_outcome_unknowns.__get__(orch)
    host, _controller, _session = await _mac_session(orch)

    async def _explode(owner_id, host_id, *, decision):
        raise RuntimeError("plane down")

    ledger = orch.computer_effect_intents
    original_resolve = ledger.resolve_owner_unknowns
    ledger.resolve_owner_unknowns = _explode
    await orch._resolve_computer_outcome_unknowns(OWNER, host, "acknowledge", INCARNATION)
    ledger.resolve_owner_unknowns = original_resolve

    orphan = _effect_row(host_id=host.host_id)
    steps.rows[orphan.step_id] = orphan
    await ledger.reconcile_owner(OWNER)

    async def _fail_record(stage, *, chat_id, user_id, role, content):
        raise RuntimeError("history down")

    orch._append_conversation_message = _fail_record
    await orch._resolve_computer_outcome_unknowns(OWNER, host, "acknowledge", INCARNATION)

    second = await ledger.begin_request(owner_id=OWNER, host=host, session_id="s-x", chat_id=CHAT,
                                        verb="click", args={}, operation_id="creq_" + "e" * 32)
    await ledger.outcome_lost(second, reason="socket lost")
    appended = []

    async def _record(stage, *, chat_id, user_id, role, content):
        appended.append((chat_id, role, content))

    orch._append_conversation_message = _record
    await orch._resolve_computer_outcome_unknowns(OWNER, host, "acknowledge", INCARNATION)
    assert appended == []


async def test_begin_request_refusals():
    ledger, _steps, _orch = _ledger()
    with pytest.raises(EffectIntentError) as no_chat:
        await ledger.begin_request(owner_id=OWNER, host=_host(), session_id="s-1", chat_id=None,
                                   verb="click", args={}, operation_id="creq_" + "8" * 32)
    assert no_chat.value.code == "effect_intent_failed"
    host = _host()
    for index in range(64):
        await ledger.begin_request(owner_id=OWNER, host=host, session_id="s-1", chat_id=CHAT,
                                   verb="wait_long", args={}, operation_id=f"creq_{index:032d}")
    with pytest.raises(EffectIntentError) as saturated:
        await ledger.begin_request(owner_id=OWNER, host=host, session_id="s-1", chat_id=CHAT,
                                   verb="click", args={}, operation_id="creq_" + "9" * 32)
    assert saturated.value.code == "effect_intent_failed"
    for index in range(512 - len(ledger._live)):
        await ledger.begin_request(owner_id="other", host=host, session_id="s-2",
                                   chat_id=f"chat-rotating-{index}", verb="wait_long", args={},
                                   operation_id=f"creq_other_{index:028d}")
    with pytest.raises(EffectIntentError) as full:
        await ledger.begin_request(owner_id="other", host=host, session_id="s-2",
                                   chat_id=f"chat-rotating-{511}", verb="wait_long", args={},
                                   operation_id="creq_" + "a" * 32)
    assert full.value.code == "effect_intent_failed"


def test_resume_text_compresses_long_action_lists():
    kinds = [f"verb_{index}" for index in range(14)]
    intents = tuple(SimpleNamespace(verb=verb) for verb in kinds)
    text = resume_text("Mac", intents, (), "acknowledge")
    assert "verb_11" in text and "verb_12" not in text and "more action kinds" in text


async def test_reconcile_skips_rows_it_cannot_interpret():
    ledger, steps, _orch = _ledger()
    garbage = _effect_row()
    garbage.args_truncated = "not-json{{"
    wrong = _effect_row()
    wrong.args_truncated = json.dumps({"verb": 7})
    sequence = _effect_row()
    sequence.args_truncated = json.dumps(["verb"])
    terminal = _effect_row()
    terminal.status = _Status("completed")
    foreign_kind = _effect_row()
    foreign_kind.kind = "tool_call"
    unreported = _Row("apr_bad", OWNER, CHAT, APPROVAL_KIND, "run_command", "also-bad", 1)
    for row in (garbage, wrong, sequence, terminal, foreign_kind, unreported):
        steps.rows[row.step_id] = row
    assert await ledger.reconcile_chat(OWNER, CHAT) == ()
    assert ledger.unknown_for(OWNER, CHAT) == ()
    assert ledger.lost_approvals(OWNER, CHAT) == ("run_command",)


async def test_reconcile_chat_tolerates_a_broken_step_store_and_retries_later():
    ledger, steps, _orch = _ledger()

    def _explode(*args, **kwargs):
        raise RuntimeError("plane unavailable")

    steps.list_steps = _explode
    assert await ledger.reconcile_chat(OWNER, CHAT) == ()
    del steps.list_steps
    orphan = _effect_row()
    steps.rows[orphan.step_id] = orphan
    recovered = await ledger.reconcile_chat(OWNER, CHAT)
    assert [item.operation_id for item in recovered] == [orphan.step_id]


async def test_reconcile_owner_tolerates_a_broken_conversation_store():
    ledger, _steps, _orch = _ledger()

    def _explode(*args, **kwargs):
        raise RuntimeError("plane unavailable")

    ledger._conversations.repository.list_recent = _explode
    assert await ledger.reconcile_owner(OWNER) == ()


async def test_resolution_skips_rows_it_cannot_close_and_other_owners():
    host = _host()
    orphan = _effect_row(host_id=host.host_id)
    foreign = _effect_row(host_id=host.host_id, owner="someone-else")
    ledger, steps, _orch = _ledger()
    steps.rows[orphan.step_id] = orphan
    steps.rows[foreign.step_id] = foreign
    await ledger.reconcile_owner(OWNER)
    steps.fail_writes = True
    resolved = await ledger.resolve_owner_unknowns(OWNER, host.host_id, decision="acknowledge")
    assert resolved == {}
    assert ledger.unknown_for(OWNER, CHAT)
    steps.fail_writes = False
    resolved = await ledger.resolve_owner_unknowns(OWNER, host.host_id, decision="acknowledge")
    assert sorted(resolved) == [CHAT]


def test_resume_gate_refuses_empty_chats():
    ledger, _steps, _orch = _ledger()
    assert ledger.grant_resume_input(OWNER, "", INCARNATION) is False


async def test_approval_marker_failures_are_non_fatal():
    ledger, steps, _orch = _ledger()
    steps.fail_writes = True
    ledger.approval_opened(proposal_id="b" * 32, owner_id=OWNER, chat_id=CHAT,
                           tool_name="run_command", summary="text")
    assert ledger._approval_markers == {}
    ledger.approval_resolved("unknown-proposal")
    steps.fail_writes = False
    ledger.approval_opened(proposal_id="c" * 32, owner_id=OWNER, chat_id=CHAT,
                           tool_name="run_command", summary="text")
    steps.fail_writes = True
    ledger.approval_resolved("c" * 32, note="approved")
    assert ledger._approval_markers == {}


async def test_audit_failures_never_break_the_ledger(monkeypatch):
    ledger, _steps, orch = _ledger()

    class _Broken:
        async def record(self, event):
            raise RuntimeError("audit down")

        def record_blocking(self, event):
            raise RuntimeError("audit down")

    orch.audit_recorder = _Broken()
    intent = await ledger.begin_request(owner_id=OWNER, host=_host(), session_id="s-1", chat_id=CHAT,
                                        verb="click", args={}, operation_id="creq_" + "b" * 32)
    await ledger.outcome_lost(intent, reason="host left")
    assert ledger.unknown_for(OWNER, CHAT)

    orch.audit_recorder = None
    from audit import recorder as audit_module
    monkeypatch.setattr(audit_module, "get_recorder", lambda: None)
    intent = await ledger.begin_request(owner_id=OWNER, host=_host(), session_id="s-1", chat_id="chat-2",
                                        verb="click", args={}, operation_id="creq_" + "c" * 32)
    assert ledger.live_count() == 1


def test_ledger_resolves_the_plane_repositories_lazily_from_the_orchestrator():
    from contextlib import contextmanager

    steps = _FakeStepRepository()
    history = SimpleNamespace(conversations=_FakeConversationRepository([CHAT]))

    class _Runtime:
        @contextmanager
        def transaction(self):
            yield None

    orch = _FakeOrch()
    orch.audit_recorder = _Recorder()
    catalog = SimpleNamespace(chat_steps=steps, history=history)
    orch.runtime_composition = SimpleNamespace(
        plane=SimpleNamespace(runtime=_Runtime(), repositories=catalog))
    ledger = EffectIntentLedger(orch)

    async def _exercise():
        await ledger.begin_request(owner_id=OWNER, host=_host(), session_id="s-1", chat_id=CHAT,
                                   verb="click", args={}, operation_id="creq_" + "d" * 32)
        assert ledger._repository() is not None
        await ledger.reconcile_chat(OWNER, CHAT)
        await ledger.reconcile_owner(OWNER)
        return True

    done = asyncio.new_event_loop().run_until_complete(_exercise())
    assert done is True
    assert ledger._conversation_context is not None
    assert "creq_" + "d" * 32 in steps.rows
