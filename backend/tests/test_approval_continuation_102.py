"""Exercises the turn that resumes after an approval decision in hitl_confirmation.py and
remote_confirmation.py: guidance is captured from the deciding human, bound for the resumed turn
through the real handle_chat_message, closed afterwards, and never outlives a stale session.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from orchestrator import hitl_confirmation as hc
from orchestrator import remote_confirmation as rc
from orchestrator.human_request_authority import _socket_policy
from orchestrator.orchestrator import Orchestrator
from orchestrator.turn_guidance_authority import _GuidanceOrigin, current_turn_guidance
from persistent_agents.models import AssignmentError
from shared.feature_flags import flags
from tests.test_computer_grant_flow_102 import (
    OWNER, ROOT, _buttons, _call, _fresh_pending_cards as _fresh_pending_cards, _mac, _payload,
)
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401


def _origin(owner=OWNER):
    return _GuidanceOrigin(SimpleNamespace(assert_host_current=lambda _orch: None), owner, "token", "{}", None,
                           datetime.now(timezone.utc) + timedelta(hours=1), _socket_policy())


@pytest.fixture
def guided(task_runtime, monkeypatch):
    orch, ws, effects, turns, proposals, outcome = task_runtime
    monkeypatch.setitem(flags._flags, "user_skills", True)
    origins, turns_seen, follow_ups = [], [], []

    async def guidance(_orch, _websocket):
        origin = _origin()
        origins.append(origin)
        return origin, True

    async def turn(websocket, message, chat_id, display_message=None, **kwargs):
        binding = current_turn_guidance(expected_orchestrator=orch, websocket=websocket, chat_id=chat_id)
        turns_seen.append((message, binding.origin))
        follow_ups.append(await _call(task_runtime, "press_keys", keys="enter", hwnd=42))

    monkeypatch.setattr(hc, "continuation_guidance", guidance)
    orch.handle_chat_message = Orchestrator.handle_chat_message.__get__(orch)
    orch._handle_chat_message_with_guidance = turn
    return SimpleNamespace(orch=orch, ws=ws, effects=effects, origins=origins, turns=turns_seen,
                           follow_ups=follow_ups)


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)
    resumes = [task for task in asyncio.all_tasks()
               if task.get_name().startswith(("hitl-continue-", "remote-op-continue-"))]
    if resumes:
        await asyncio.wait_for(asyncio.gather(*resumes), timeout=120)


def _statuses(orch, ws):
    sent = []

    async def send(target, data):
        frame = json.loads(data)
        if target is ws and frame.get("type") == "chat_status":
            sent.append(frame["status"])
        return True

    orch._safe_send = send
    return sent


@pytest.mark.parametrize("decision", ["approve", "grant_task"])
async def test_an_approved_effect_and_its_continuation_announce_the_turn(task_runtime, guided, decision):
    statuses = _statuses(guided.orch, guided.ws)
    await _mac(task_runtime)
    card = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    await hc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, decision))
    await _settle()
    assert statuses == ["executing", "thinking", "done"] and len(guided.turns) == 1


async def test_a_command_approval_announces_the_effect_and_its_continuation(task_runtime, guided):
    statuses = _statuses(guided.orch, guided.ws)
    await _mac(task_runtime, remote=True)
    card = await _call(task_runtime, "run_command", argv=["wc", "notes.md"], cwd=ROOT)
    await rc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, "approve"))
    await _settle()
    assert statuses == ["executing", "thinking", "done"] and len(guided.turns) == 1


@pytest.mark.parametrize("change", ["stale", "no_guidance", "decline"])
async def test_an_approval_without_a_continuation_still_ends_the_turn(task_runtime, guided, monkeypatch, change):
    async def unavailable(_orch, _websocket):
        return None, False

    statuses = _statuses(guided.orch, guided.ws)
    if change == "no_guidance":
        monkeypatch.setattr(hc, "continuation_guidance", unavailable)
    await _mac(task_runtime)
    card = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    await hc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, "decline" if change == "decline" else
                                                                           "approve"))
    if change == "stale":
        guided.orch._ws_active_chat[id(guided.ws)] = "chat-b"
    await _settle()
    assert not guided.turns
    assert statuses == ([] if change == "decline" else ["executing", "done"])


async def test_a_newer_turn_keeps_its_own_state_when_a_stale_continuation_is_refused(task_runtime, guided):
    statuses = _statuses(guided.orch, guided.ws)
    await _mac(task_runtime)
    card = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    await hc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, "approve"))
    hc.begin_task(guided.orch, guided.ws, OWNER, "chat-a")
    await _settle()
    assert not guided.turns and statuses == ["executing"]


@pytest.mark.parametrize("path", ["hitl", "command"])
async def test_cancel_during_the_approved_effect_interrupts_it_without_a_continuation(task_runtime, guided, path):
    orch, ws = guided.orch, guided.ws
    statuses = _statuses(orch, ws)
    started, interrupted = asyncio.Event(), []

    async def blocked(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.append(True)
            raise

    session, _host = await _mac(task_runtime, remote=path == "command")
    card = (await _call(task_runtime, "run_command", argv=["wc", "notes.md"], cwd=ROOT) if path == "command"
            else await _call(task_runtime, "type_text", text="hello", hwnd=42))
    orch._execute_with_retry = blocked
    decide = rc.handle_decision if path == "command" else hc.handle_decision
    decision = asyncio.create_task(decide(orch, ws, OWNER, _payload(card, "approve")))
    await asyncio.wait_for(started.wait(), 5)
    await orch._cancel_turn(ws, OWNER, "chat-a")
    await asyncio.wait_for(decision, 5)
    await _settle()
    assert interrupted and not decision.cancelled() and not guided.turns
    assert statuses[0] == "executing" and statuses[-1] == "done" and "thinking" not in statuses
    finished = json.dumps([call.args for call in orch.workspace.aupsert.await_args_list])
    assert "Cancelled" in finished and session.state == "active"
    assert not getattr(orch, "_turn_tool_tasks", {}).get((id(ws), "chat-a"))


async def test_cancel_before_the_continuation_starts_prevents_it(task_runtime, guided, monkeypatch):
    statuses = _statuses(guided.orch, guided.ws)
    await _mac(task_runtime)
    card = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    original = hc.continue_after_approval

    def deferred(*args, **kwargs):
        task = original(*args, **kwargs)
        guided.orch.cancelled_sessions[id(guided.ws)] = True
        guided.orch._hitl_task_origins.pop((OWNER, "chat-a"), None)
        return task

    monkeypatch.setattr(hc, "continue_after_approval", deferred)
    await hc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, "approve"))
    await _settle()
    assert len(guided.effects) == 1 and not guided.turns and statuses == ["executing", "done"]


@pytest.mark.parametrize("decision", ["approve", "grant_task"])
async def test_approval_resumes_under_the_deciders_guidance_and_dispatches_the_next_tool(
        task_runtime, guided, decision):
    await _mac(task_runtime)
    card = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    await hc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, decision))
    await _settle()
    assert len(guided.effects) == (2 if decision == "grant_task" else 1)
    (message, origin), = guided.turns
    assert message == hc.CONTINUATION_TEXT and origin is guided.origins[0] and origin.owner_id == OWNER
    follow_up, = guided.follow_ups
    if decision == "grant_task":
        assert follow_up.result["_data"]["ok"]
    else:
        assert follow_up.result["_data"]["status"] == "confirmation_required"
    assert origin.closed


async def test_decline_never_captures_guidance_or_resumes(task_runtime, guided):
    await _mac(task_runtime)
    card = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    await hc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, "decline"))
    await _settle()
    assert not guided.effects and not guided.turns and not guided.origins


async def test_a_stale_session_blocks_the_resume_and_closes_the_guidance(task_runtime, guided):
    await _mac(task_runtime)
    card = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    await hc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, "approve"))
    guided.orch._ws_active_chat[id(guided.ws)] = "chat-b"
    await _settle()
    assert len(guided.effects) == 1 and not guided.turns and guided.origins[0].closed


async def test_unavailable_guidance_runs_the_approved_action_without_a_resume(task_runtime, guided, monkeypatch):
    async def unavailable(_orch, _websocket):
        return None, False

    monkeypatch.setattr(hc, "continuation_guidance", unavailable)
    await _mac(task_runtime)
    card = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    await hc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, "approve"))
    await _settle()
    assert len(guided.effects) == 1 and not guided.turns


async def test_refused_decision_closes_the_captured_guidance(task_runtime, guided):
    await _mac(task_runtime)
    card = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    payload = _payload(card, "approve")
    guided.orch.history.get_chat.return_value = None
    await hc.handle_decision(guided.orch, guided.ws, OWNER, payload)
    await _settle()
    assert not guided.effects and not guided.turns and guided.origins[0].closed


async def test_expired_guidance_refuses_the_resume_without_failing_the_decision(task_runtime, guided, monkeypatch):
    async def expired(_orch, _websocket):
        origin = _origin()
        origin.close()
        return origin, True

    monkeypatch.setattr(hc, "continuation_guidance", expired)
    await _mac(task_runtime)
    card = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    await hc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, "approve"))
    await _settle()
    assert len(guided.effects) == 1 and not guided.turns


@pytest.mark.parametrize("decision", ["approve", "grant_task"])
async def test_single_card_command_approval_resumes_under_guidance(task_runtime, guided, decision):
    await _mac(task_runtime, remote=True)
    card = await _call(task_runtime, "run_command", argv=["wc", "notes.md"], cwd=ROOT)
    await rc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, decision))
    await _settle()
    assert len(guided.effects) == 1
    (message, origin), = guided.turns
    assert message == hc.CONTINUATION_TEXT and origin.closed


async def test_command_card_decline_and_pending_review_do_not_resume(task_runtime, guided):
    await _mac(task_runtime, remote=True)
    card = await _call(task_runtime, "run_command", argv=["wc", "notes.md"], cwd=ROOT)
    await rc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, "decline"))
    terminal = await _call(task_runtime, "open_app", app="Terminal")
    await rc.handle_decision(guided.orch, guided.ws, OWNER, _payload(terminal, "approve"))
    await _settle()
    assert not guided.turns and all(origin.closed for origin in guided.origins)
    assert [b["label"] for b in _buttons(terminal)] == ["Approve", "Decline"]


async def test_command_resume_stops_when_the_computer_session_changes(task_runtime, guided):
    session, _host = await _mac(task_runtime, remote=True)
    card = await _call(task_runtime, "run_command", argv=["wc", "notes.md"], cwd=ROOT)
    await rc.handle_decision(guided.orch, guided.ws, OWNER, _payload(card, "approve"))
    await guided.orch.computer_sessions.pause(session, "local_input")
    await _settle()
    assert len(guided.effects) == 1 and not guided.turns and guided.origins[0].closed


async def test_continuation_guidance_is_skipped_when_skills_and_evidence_are_off(monkeypatch):
    from orchestrator import evidence_context

    monkeypatch.setitem(flags._flags, "user_skills", False)
    monkeypatch.setattr(evidence_context, "needs_authority", lambda _orch: False)
    assert await hc.continuation_guidance(object(), object()) == (None, True)


async def test_continuation_guidance_reads_the_deciding_human_and_retires_the_read(monkeypatch):
    from orchestrator import human_request_authority as hra
    from orchestrator import turn_guidance_authority as tga
    from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT

    monkeypatch.setitem(flags._flags, "user_skills", True)
    origin, retired, seen = _origin(), [], []
    caller = object()

    async def read(*, expected_orchestrator, websocket, operation_context):
        seen.append(operation_context)
        return caller

    async def capture(value, *, expected_orchestrator):
        assert value is caller
        return origin

    monkeypatch.setattr(hra, "current_socket_human_read", read)
    monkeypatch.setattr(hra, "retire_socket_human_read", retired.append)
    monkeypatch.setattr(tga, "capture_turn_guidance_from_human", capture)
    context = {"human_request": SimpleNamespace(purpose="skill_lookup")}
    token = _CONNECTION_OPERATION_CONTEXT.set(context)
    try:
        assert await hc.continuation_guidance(object(), object()) == (origin, True)
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    assert retired == [caller] and seen == [context]

    async def foreground():
        return caller

    token = _CONNECTION_OPERATION_CONTEXT.set({"human_request": SimpleNamespace(purpose="remote_effect",
                                                                                authenticate=foreground)})
    try:
        assert await hc.continuation_guidance(object(), object()) == (origin, True)
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    assert retired == [caller]

    async def refused(**_kwargs):
        raise AssignmentError("human_skill_lookup_unavailable", 503)

    monkeypatch.setattr(hra, "current_socket_human_read", refused)
    assert await hc.continuation_guidance(object(), object()) == (None, False)


def test_decision_frames_can_carry_a_guidance_read(monkeypatch):
    from orchestrator import human_request_authority as hra

    built = []

    class Request:
        def __init__(self, boundary, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(hra, "_HumanSocketRequest", Request)
    for action, method in (("authorize_action", "WS_READ"), ("remote_op_decision", "WS_READ"),
                           ("chat_message", "WS_READ"), ("chrome_open", None)):
        request = hra.capture_human_socket_request(object(), websocket=object(), context=object(),
            message={"type": "ui_event", "action": action, "payload": {}}, purpose="skill_lookup")
        assert (request is not None) is (method is not None)
    assert [kwargs["method"] for kwargs in built] == ["WS_READ"] * 3
    assert all(kwargs["purpose"] == "skill_lookup" for kwargs in built)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("cache_loss", [False, True])
async def test_mode_withdrawal_or_authority_cache_loss_never_replays_hydrated_review(
        task_runtime, guided, monkeypatch, remote, cache_loss):
    from orchestrator.approval_receipts import withdraw_computer_reviews
    session, host = await _mac(task_runtime, remote=remote)
    card = (await _call(task_runtime, "run_command", argv=["wc", "notes.md"], cwd=ROOT) if remote else
            await _call(task_runtime, "type_text", text="hello", hwnd=42))
    payload = _payload(card, "approve")
    assert card.ui_components[0]["computer_host_id"] == host.host_id
    async def presentation(*args, **kwargs):
        pass
    monkeypatch.setattr(hc, "_finish_card", presentation)
    monkeypatch.setattr(rc, "_replace_card", presentation)
    if cache_loss:
        guided.orch._hitl_pending_calls = {}
        guided.orch._computer_approval_authorities = {}
    else:
        await withdraw_computer_reviews(guided.orch, OWNER, host.host_id)
    await guided.orch.computer_sessions.set_bypass(host, True)
    await guided.orch.computer_sessions.set_bypass(host, False)
    handler = rc.handle_decision if remote else hc.handle_decision
    await handler(guided.orch, guided.ws, OWNER, payload)
    await handler(guided.orch, guided.ws, OWNER, payload)
    await _settle()
    assert not guided.effects and not guided.turns
    await guided.orch.computer_sessions.end(session, "user_stop")


async def test_withdrawn_review_has_no_suspended_turn_or_automatic_continuation(task_runtime, monkeypatch):
    from orchestrator.approval_receipts import withdraw_computer_reviews
    orch, ws, effects, turns, *_ = task_runtime
    session, host = await _mac(task_runtime)
    cards = []
    async def chat(websocket, message, chat_id, display, **kwargs):
        turns.append(message)
        cards.append(await _call(task_runtime, "type_text", text="hello", hwnd=42))
    orch.handle_chat_message = chat
    origin = asyncio.create_task(orch._serialized_chat(ws, "origin", "chat-a", "origin", user_id=OWNER))
    await origin
    assert origin.done() and not origin.cancelled()
    assert not orch._chat_locks[(id(ws), "chat-a")].locked()
    assert not orch._workspace_locks["chat-a"].locked()
    assert orch._hitl_pending_calls and not effects
    unrelated = asyncio.Event()
    other = asyncio.create_task(unrelated.wait())
    async def presentation(*args, **kwargs):
        pass
    monkeypatch.setattr(hc, "_finish_card", presentation)
    await withdraw_computer_reviews(orch, OWNER, host.host_id)
    assert not orch._hitl_pending_calls and not other.done()
    await hc.handle_decision(orch, ws, OWNER, _payload(cards[0], "approve"))
    await _settle()
    assert turns == ["origin"] and not effects and not other.done()
    unrelated.set()
    await other
    await orch.computer_sessions.end(session, "user_stop")
