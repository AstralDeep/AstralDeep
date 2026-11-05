"""Tests for the conversation-branch WS handlers: exact refusal frames, owner-denial
audits, summary generation with its PHI gate and failure recovery, fork isolation from
task grants, bypass state, approvals, pending cards and goal plans, and WS routing.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock

from orchestrator import conversation_branches as cb
from orchestrator.history import BRANCH_SUMMARY_PREFIX


class _AuditRecorder:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)
        return object()


class _StubOrch:
    class _CredentialSource:
        USER = "user"
        SYSTEM = "system"

    def __init__(self, history):
        self.history = history
        self.sent = []
        self.broadcasts = []
        self.serialized = []
        self.hydrations = []
        self.audit_recorder = SimpleNamespace(record=AsyncMock())
        self._record_llm_call = AsyncMock()
        self._llm_audit_principals = lambda _ws: ("owner", "owner")
        self._CredentialSource = _StubOrch._CredentialSource

    async def _safe_send(self, websocket, text):
        self.sent.append(json.loads(text))

    async def _broadcast_user_history(self, user_id=None):
        self.broadcasts.append(user_id)

    async def _serialized_chat(self, websocket, message, chat_id, display_message,
                               **kwargs):
        self.serialized.append((message, chat_id, kwargs))

    def _conversation_authority(self, context, websocket):
        return None

    async def _emit_hydration_snapshot(self, websocket, **kwargs):
        self.hydrations.append(kwargs)


@pytest.fixture
def audit_recorder(monkeypatch):
    recorder = _AuditRecorder()
    import audit.hooks as hooks

    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    monkeypatch.setattr(hooks, "get_recorder", lambda: recorder)
    return recorder


@pytest.fixture
def owner():
    return f"handler-{uuid.uuid4().hex[:12]}"


def _branched(owner_id, turns=(("user", "hello"), ("assistant", "hi"))):
    from tests.test_conversation_branches import branch_history

    hm = branch_history()
    chat_id = str(uuid.uuid4())
    hm.create_chat(chat_id, user_id=owner_id)
    for role, text in turns:
        hm.add_message(chat_id, role, text, user_id=owner_id)
    view = hm.branch_active_view(chat_id, owner_id)
    assert view is not None and view["path"]
    return hm, chat_id, view


def _payload(chat_id, turn_id, **extra):
    payload = {"chat_id": chat_id, "turn_id": str(turn_id)}
    payload.update(extra)
    return payload


def test_fork_action_announces_and_auds(owner, audit_recorder):
    hm, chat_id, view = _branched(owner)
    orch = _StubOrch(hm)
    asyncio.run(cb.handle_branch_action(
        orch, object(), owner, "chat_fork", _payload(chat_id, view["path"][-1].message_id),
        submission_id="sub-1",
    ))

    created = [frame for frame in orch.sent if frame["type"] == "chat_created"]
    assert len(created) == 1
    new_chat_id = created[0]["payload"]["chat_id"]
    assert new_chat_id != chat_id
    assert created[0]["payload"]["source_chat_id"] == chat_id
    event = audit_recorder.events[-1]
    assert event.action_type == "chat.forked"
    assert event.inputs_meta["source_message_id"] == str(view["path"][-1].message_id)
    assert event.inputs_meta["forked_chat_id"] == new_chat_id
    assert orch.broadcasts == [owner]
    forked = hm.get_chat(new_chat_id, user_id=owner)
    assert [message["content"] for message in forked["messages"]] == ["hello", "hi"]


def test_regenerate_action_dispatches_the_persisted_parent_turn(owner, audit_recorder):
    hm, chat_id, view = _branched(
        owner, turns=(("user", "What is the launch window?"), ("assistant", "At dusk."))
    )
    reply_id = view["path"][-1].message_id
    orch = _StubOrch(hm)
    asyncio.run(cb.handle_branch_action(
        orch, object(), owner, "chat_regenerate", _payload(chat_id, reply_id),
    ))

    assert len(orch.serialized) == 1
    message, dispatched_chat, kwargs = orch.serialized[0]
    assert dispatched_chat == chat_id
    assert message == "What is the launch window?"
    assert kwargs["persisted_turn"]["message_id"] == reply_id - 1
    event = audit_recorder.events[-1]
    assert event.action_type == "chat.regenerated"
    assert event.inputs_meta["regenerated_message_id"] == str(reply_id)


def test_branch_select_switches_and_auds(owner, audit_recorder):
    hm, chat_id, view = _branched(owner)
    target_id = view["path"][0].message_id
    orch = _StubOrch(hm)
    asyncio.run(cb.handle_branch_action(
        orch, object(), owner, "chat_branch_select",
        _payload(chat_id, view["path"][-1].message_id, target_turn_id=str(target_id)),
    ))

    head = hm._history.call(
        hm._history.repository.conversations.branch_head,
        owner_id=owner, conversation_id=chat_id,
    )
    assert head.head_message_id == target_id
    event = audit_recorder.events[-1]
    assert event.action_type == "chat.branch_switched"
    assert event.inputs_meta["target_message_id"] == str(target_id)


def test_foreign_chat_is_refused_and_audited(owner, audit_recorder):
    hm, chat_id, view = _branched(owner)
    orch = _StubOrch(hm)
    asyncio.run(cb.handle_branch_action(
        orch, object(), "someone-else", "chat_fork",
        _payload(chat_id, view["path"][-1].message_id), submission_id="sub-2",
    ))

    refusal = orch.sent[-1]
    assert refusal["type"] == "error" and refusal["accepted"] is False
    assert refusal["submission_id"] == "sub-2"
    event = audit_recorder.events[-1]
    assert event.outcome == "failure" and event.outcome_detail == "owner_denied"
    assert not orch.serialized


def test_unsupported_plane_refuses_with_the_client_message(owner, audit_recorder):
    from tests.helpers.voice_plane_runtime import history_manager, isolated_voice_plane_runtime

    with isolated_voice_plane_runtime("branch_handlers_gate") as runtime:
        hm = history_manager(runtime)
        chat_id = hm.create_chat(user_id=owner)
        hm.add_message(chat_id, "user", "hello", user_id=owner)
        orch = _StubOrch(hm)

        asyncio.run(cb.handle_branch_action(
            orch, object(), owner, "chat_fork",
            _payload(chat_id, hm.get_latest_message_id(chat_id, owner)),
            submission_id="sub-3",
        ))

        refusal = orch.sent[-1]
        assert refusal["message"] == cb.UNAVAILABLE_MESSAGE
        assert refusal["code"] == "operation_failed"
        assert audit_recorder.events[-1].outcome_detail == "branch_support_unavailable"


def test_malformed_payloads_are_refused_without_audit(owner, audit_recorder):
    from tests.test_conversation_branches import branch_history

    orch = _StubOrch(branch_history())
    for payload in (
        {"chat_id": str(uuid.uuid4())},
        {"chat_id": str(uuid.uuid4()), "turn_id": "not-a-number"},
        {"chat_id": str(uuid.uuid4()), "turn_id": "3", "extra": True},
        {"chat_id": str(uuid.uuid4()), "turn_id": "3", "target_turn_id": None},
    ):
        asyncio.run(cb.handle_branch_action(
            orch, object(), owner, "chat_regenerate", payload, submission_id="sub-4",
        ))
    assert all(frame["code"] == "operation_failed" for frame in orch.sent)
    assert all(frame["message"] == cb.MESSAGES["invalid_request"] for frame in orch.sent)
    assert audit_recorder.events == []
    assert not orch.serialized


def test_summary_failure_still_completes_the_switch_and_records_it(
        owner, audit_recorder, monkeypatch):
    hm, chat_id, view = _branched(owner)
    target_id = view["path"][0].message_id
    orch = _StubOrch(hm)
    llm_records = []

    async def record_llm_call(_recorder, **kwargs):
        llm_records.append(kwargs)

    orch._resolve_llm_client_for = AsyncMock(side_effect=RuntimeError("llm down"))
    orch._record_llm_call = record_llm_call
    asyncio.run(cb.handle_branch_action(
        orch, object(), owner, "chat_branch_select",
        _payload(chat_id, view["path"][-1].message_id, target_turn_id=str(target_id),
                 summarize_departed=True),
    ))

    head = hm._history.call(
        hm._history.repository.conversations.branch_head,
        owner_id=owner, conversation_id=chat_id,
    )
    assert head.head_message_id == target_id
    event = audit_recorder.events[-1]
    assert event.action_type == "chat.branch_switched"
    assert event.inputs_meta["summarized"] is False
    assert any(kwargs.get("outcome") == "failure" for kwargs in llm_records)


def test_revised_message_dispatches_through_the_persisted_turn(owner, audit_recorder):
    hm, chat_id, view = _branched(
        owner,
        turns=(("user", "First wording"), ("assistant", "Reply"), ("user", "Follow-up")),
    )
    revised_id = view["path"][-1].message_id
    orch = _StubOrch(hm)
    asyncio.run(cb.dispatch_revised_message(
        orch, object(), user_id=owner,
        payload={"chat_id": chat_id, "message": "Revised wording",
                 "revised_turn_id": str(revised_id)},
        submission_id="sub-5",
    ))

    assert len(orch.serialized) == 1
    message, dispatched_chat, kwargs = orch.serialized[0]
    assert message == "Revised wording"
    assert kwargs["persisted_turn"]["message_id"] != revised_id
    chat = hm.get_chat(chat_id, user_id=owner)
    assert [message["content"] for message in chat["messages"]] == [
        "First wording", "Reply", "Revised wording",
    ]
    visible = hm._history.call(
        hm._history.repository.messages.list_visible, owner_id=owner,
        conversation_id=chat_id,
    )
    assert len(visible) == 4
    event = audit_recorder.events[-1]
    assert event.action_type == "chat.branch_created"
    assert event.inputs_meta["revised_message_id"] == str(revised_id)


def test_revised_message_refuses_assistant_turns(owner, audit_recorder):
    hm, chat_id, view = _branched(owner)
    reply_id = view["path"][-1].message_id
    orch = _StubOrch(hm)
    asyncio.run(cb.dispatch_revised_message(
        orch, object(), user_id=owner,
        payload={"chat_id": chat_id, "message": "Revised",
                 "revised_turn_id": str(reply_id)},
        submission_id="sub-7",
    ))

    assert not orch.serialized
    assert orch.sent[-1]["message"] == cb.MESSAGES["invalid_request"]


def test_fork_isolation_keeps_grants_bypass_approvals_cards_and_plans_behind(
        orchestrator_factory, owner, audit_recorder, monkeypatch):
    from tests.test_conversation_branches import branch_history

    async def scenario():
        orch = await asyncio.to_thread(orchestrator_factory)
        try:
            hm = branch_history()
            source = str(uuid.uuid4())
            hm.create_chat(source, user_id=owner)
            hm.add_message(source, "user", "hello", user_id=owner)
            monkeypatch.setattr(orch, "history", hm)
            sent_frames = []

            async def record_send(websocket, text):
                sent_frames.append(json.loads(text))

            async def record_broadcast(user_id=None):
                return None

            monkeypatch.setattr(orch, "_safe_send", record_send)
            monkeypatch.setattr(orch, "_broadcast_user_history", record_broadcast)

            orch._chain_budgets[source] = object()
            pending = SimpleNamespace(request_id="pending-1", chat_id=source)
            orch._hitl_pending_calls = {pending.request_id: pending}
            orch.computer_sessions.grants.create(
                owner, "host-1", "session-1", source, "task-1",
                {"kind": "command"}, {"command"},
                command={"argv0": "/bin/echo", "root": "/tmp"}, now=time.time(),
            )

            await cb.handle_branch_action(
                orch, object(), owner, "chat_fork",
                _payload(source, hm.get_latest_message_id(source, owner)),
                submission_id="sub-6",
            )
            created = [frame for frame in sent_frames if frame["type"] == "chat_created"]
            assert created, "fork must announce the new chat"
            new_chat_id = created[0]["payload"]["chat_id"]

            assert new_chat_id not in orch._chain_budgets
            assert all(
                call.chat_id != new_chat_id
                for call in orch._hitl_pending_calls.values()
            )
            grant_chat_ids = {
                record.chat_id
                for holdings in orch.computer_sessions.grants._by_session.values()
                for record in holdings.values()
            }
            assert source in grant_chat_ids and new_chat_id not in grant_chat_ids
            assert all(
                chat != new_chat_id
                for (_owner_scope, chat) in getattr(orch.computer_sessions, "_bypass", {})
            )
        finally:
            await orch._close_started_services()

    asyncio.run(scenario())


async def test_ws_actions_route_through_the_branch_handlers(owner, audit_recorder, monkeypatch):
    from orchestrator.orchestrator import Orchestrator

    hm, chat_id, view = _branched(owner)
    orch = Orchestrator.__new__(Orchestrator)
    orch.history = hm
    orch.ui_sessions = {}
    sent = []

    async def record_send(websocket, text):
        sent.append(json.loads(text))

    async def record_broadcast(user_id=None):
        return None

    orch._safe_send = record_send
    orch._broadcast_user_history = record_broadcast
    monkeypatch.setattr(
        "audit.hooks.record_ws_action", AsyncMock(), raising=False)
    ws = object()
    orch.ui_sessions[ws] = {"sub": owner}

    frame = json.dumps({
        "type": "ui_event", "action": "chat_fork",
        "session_id": chat_id,
        "payload": {"chat_id": chat_id, "turn_id": str(view["path"][-1].message_id)},
        "submission_id": str(uuid.uuid4()), "request_generation": str(uuid.uuid4()),
    })

    class _RecordingStub(_StubOrch):
        pass

    await Orchestrator.handle_ui_message(orch, ws, frame)
    created = [frame_ for frame_ in sent if frame_["type"] == "chat_created"]
    assert created, "the WS action must reach the fork handler"

    sent.clear()
    revised_frame = json.dumps({
        "type": "ui_event", "action": "chat_message",
        "session_id": chat_id,
        "payload": {"chat_id": chat_id, "message": "Revised",
                    "revised_turn_id": str(view["path"][0].message_id)},
        "submission_id": str(uuid.uuid4()), "request_generation": str(uuid.uuid4()),
    })
    serialized_calls = []

    async def record_serialized(websocket, message, chat, display, **kwargs):
        serialized_calls.append((message, chat, kwargs))

    orch._serialized_chat = record_serialized
    await Orchestrator.handle_ui_message(orch, ws, revised_frame)
    assert serialized_calls and serialized_calls[0][2].get("persisted_turn"), (
        "the revised chat_message must dispatch a persisted turn"
    )


async def test_summary_generation_records_usage_and_passes_the_phi_gate(
        owner, audit_recorder, monkeypatch):
    hm, chat_id, view = _branched(owner)
    orch = _StubOrch(hm)
    resolved = SimpleNamespace(model="m", base_url="http://test.invalid/v1")

    class _Client:
        _evidence_provider_capture = None

    async def resolve(websocket):
        return _Client(), orch._CredentialSource.USER, resolved

    recorded = {}

    async def context_call(websocket, chat, purpose, model, invoke, **kwargs):
        recorded["purpose"] = purpose
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=" Left the dusk plan. "))],
            usage=SimpleNamespace(total_tokens=21),
        )

    usage_reports = []

    async def usage_report(websocket, **kwargs):
        usage_reports.append(kwargs)

    async def record_llm(_recorder, **kwargs):
        recorded["llm"] = kwargs

    orch._resolve_llm_client_for = resolve
    orch._context_model_call = context_call
    orch._record_llm_call = record_llm
    orch._emit_llm_usage_report = usage_report

    async def gate(user_id, orch_ref):
        return None

    from personalization import phi_policy

    monkeypatch.setattr(phi_policy, "redaction_gate", lambda user_id, orch_ref: None)

    departed = (
        SimpleNamespace(role="user", content="hello"),
        SimpleNamespace(role="tool", content="ignored"),
        SimpleNamespace(role="assistant", content={"parts": [1]}),
        SimpleNamespace(role="user", content="   "),
    )
    summary = await cb.summarize_departed_messages(
        orch, object(), chat_id=chat_id, user_id=owner, departed=departed,
    )

    assert summary == "Left the dusk plan."
    assert recorded["purpose"] == "branch_summary"
    assert recorded["llm"]["outcome"] == "success"
    assert recorded["llm"]["total_tokens"] == 21
    assert usage_reports and usage_reports[0]["outcome"] == "success"


async def test_summary_redacts_phi_before_returning(owner, monkeypatch):
    hm, chat_id, view = _branched(owner)
    orch = _StubOrch(hm)
    resolved = SimpleNamespace(model="m", base_url=None)

    class _Client:
        _evidence_provider_capture = None

    orch._resolve_llm_client_for = AsyncMock(
        return_value=(_Client(), orch._CredentialSource.SYSTEM, resolved))
    orch._context_model_call = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Raw summary"))],
        usage=None,
    ))
    orch._record_llm_call = AsyncMock()

    class _Gate:
        async def redact(self, text):
            return "[REDACTED]", True

    from personalization import phi_policy

    monkeypatch.setattr(
        phi_policy,
        "redaction_gate",
        lambda user_id, orch_ref: SimpleNamespace(
            redact_for_storage=lambda text: (text.replace("Raw", "REDACTED"), True),
        ),
    )

    summary = await cb.summarize_departed_messages(
        orch, None, chat_id=chat_id, user_id=owner,
        departed=(SimpleNamespace(role="user", content="hello"),),
    )

    assert summary == "REDACTED summary"


async def test_summary_without_usable_content_returns_none(owner):
    hm, chat_id, view = _branched(owner)
    orch = _StubOrch(hm)
    assert await cb.summarize_departed_messages(
        orch, None, chat_id=chat_id, user_id=owner, departed=(),
    ) is None
    assert await cb.summarize_departed_messages(
        orch, None, chat_id=chat_id, user_id=owner,
        departed=(SimpleNamespace(role="user", content="   "),),
    ) is None


async def test_revised_message_with_summary_appends_the_marked_turn(
        owner, audit_recorder, monkeypatch):
    hm, chat_id, view = _branched(
        owner,
        turns=(("user", "First"), ("assistant", "Reply"), ("user", "Follow-up")),
    )
    revised_id = view["path"][-1].message_id
    orch = _StubOrch(hm)

    async def summary(*args, **kwargs):
        return "The departed plan."

    monkeypatch.setattr(cb, "summarize_departed_messages", summary)

    await cb.dispatch_revised_message(
        orch, object(), user_id=owner,
        payload={"chat_id": chat_id, "message": "Revised follow-up",
                 "revised_turn_id": str(revised_id), "summarize_departed": True},
        submission_id=str(uuid.uuid4()),
    )

    chat = hm.get_chat(chat_id, user_id=owner)
    contents = [message["content"] for message in chat["messages"]]
    assert contents[-1].startswith(BRANCH_SUMMARY_PREFIX)
    assert contents[-2] == "Revised follow-up"
    event = audit_recorder.events[-1]
    assert event.inputs_meta["summarized"] is True


async def test_audit_failures_never_break_the_action(owner, audit_recorder, monkeypatch):
    hm, chat_id, view = _branched(owner)
    orch = _StubOrch(hm)

    async def broken_record(event):
        raise RuntimeError("audit down")

    broken = _AuditRecorder()
    broken.record = broken_record
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: broken)

    await cb.handle_branch_action(
        orch, object(), owner, "chat_fork", _payload(chat_id, view["path"][-1].message_id),
        submission_id=str(uuid.uuid4()),
    )

    created = [frame for frame in orch.sent if frame["type"] == "chat_created"]
    assert created, "fork completes even when the audit write fails"


async def test_regenerate_with_summary_appends_the_marked_turn(
        owner, audit_recorder, monkeypatch):
    hm, chat_id, view = _branched(
        owner, turns=(("user", "Question"), ("assistant", "Reply"))
    )
    reply_id = view["path"][-1].message_id
    orch = _StubOrch(hm)

    async def summary(*args, **kwargs):
        return "The dusk attempt is parked."

    monkeypatch.setattr(cb, "summarize_departed_messages", summary)
    await cb.handle_branch_action(
        orch, object(), owner, "chat_regenerate",
        _payload(chat_id, reply_id, summarize_departed=True),
    )

    chat = hm.get_chat(chat_id, user_id=owner)
    contents = [message["content"] for message in chat["messages"]]
    assert contents[-1].startswith(BRANCH_SUMMARY_PREFIX)
    assert contents[-2] == "Question"
    event = audit_recorder.events[-1]
    assert event.inputs_meta["summarized"] is True
    assert orch.serialized and orch.serialized[0][2]["persisted_turn"]


async def test_regenerate_refuses_user_turns_and_off_branch_replies(owner, audit_recorder):
    hm, chat_id, view = _branched(
        owner, turns=(("user", "Question"), ("assistant", "Reply"))
    )
    first_id = view["path"][0].message_id
    orch = _StubOrch(hm)
    await cb.handle_branch_action(
        orch, object(), owner, "chat_regenerate", _payload(chat_id, first_id),
    )
    assert orch.sent[-1]["message"] == cb.MESSAGES["invalid_request"]
    await cb.handle_branch_action(
        orch, object(), owner, "chat_regenerate", _payload(chat_id, 99999),
    )
    assert orch.sent[-1]["message"] == cb.MESSAGES["invalid_request"]
    assert not orch.serialized


async def test_regenerate_dispatch_failure_is_refused(owner, audit_recorder, monkeypatch):
    hm, chat_id, view = _branched(
        owner, turns=(("user", "Question"), ("assistant", "Reply"))
    )
    reply_id = view["path"][-1].message_id
    orch = _StubOrch(hm)

    async def broken_serialized(*args, **kwargs):
        raise RuntimeError("dispatch down")

    orch._serialized_chat = broken_serialized
    await cb.handle_branch_action(
        orch, object(), owner, "chat_regenerate", _payload(chat_id, reply_id),
    )

    refusal = orch.sent[-1]
    assert refusal["message"] == cb.MESSAGES["branch_failed"]
    assert refusal["code"] == "operation_failed"


async def test_hydration_is_bound_and_failures_are_non_fatal(
        owner, audit_recorder, monkeypatch):
    from orchestrator.orchestrator import _CONNECTION_OPERATION_CONTEXT

    hm, chat_id, view = _branched(owner)
    orch = _StubOrch(hm)
    hydrations = []

    async def emit(websocket, **kwargs):
        hydrations.append(kwargs)

    operation = SimpleNamespace(
        connection_generation=uuid.uuid4(), request_generation=uuid.uuid4(),
    )
    orch._conversation_authority = lambda context, websocket: (
        (operation, None, None) if context is not None else None
    )
    orch._emit_hydration_snapshot = emit
    token = _CONNECTION_OPERATION_CONTEXT.set({"connection_generation": uuid.uuid4()})
    try:
        await cb.handle_branch_action(
            orch, object(), owner, "chat_branch_select",
            _payload(chat_id, view["path"][-1].message_id,
                     target_turn_id=str(view["path"][0].message_id)),
        )
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)

    assert len(hydrations) == 1
    assert hydrations[0]["connection_generation"] == operation.connection_generation

    async def broken_emit(websocket, **kwargs):
        raise RuntimeError("socket gone")

    orch._emit_hydration_snapshot = broken_emit
    token = _CONNECTION_OPERATION_CONTEXT.set({"connection_generation": uuid.uuid4()})
    try:
        await cb.handle_branch_action(
            orch, object(), owner, "chat_branch_select",
            _payload(chat_id, view["path"][-1].message_id,
                     target_turn_id=str(view["path"][0].message_id)),
        )
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    assert hydrations == [hydrations[0]], "snapshot failure must not fail the switch"


async def test_revised_message_refusals_are_audited(owner, audit_recorder):
    hm, chat_id, view = _branched(owner)
    orch = _StubOrch(hm)
    await cb.dispatch_revised_message(
        orch, object(), user_id="someone-else",
        payload={"chat_id": chat_id, "message": "Revised",
                 "revised_turn_id": str(view["path"][0].message_id)},
        submission_id=str(uuid.uuid4()),
    )
    assert audit_recorder.events[-1].outcome_detail == "owner_denied"
    assert orch.sent[-1]["message"] == cb.MESSAGES["chat_not_found"]
    await cb.dispatch_revised_message(
        orch, object(), user_id=owner,
        payload={"chat_id": chat_id, "message": "   "},
        submission_id=str(uuid.uuid4()),
    )
    assert orch.sent[-1]["message"] == cb.MESSAGES["invalid_request"]

    broken = _StubOrch(_StubOrch(hm.history) if False else hm)
    broken.history.branch_active_view = lambda *a, **k: None
    await cb.dispatch_revised_message(
        broken, object(), user_id=owner,
        payload={"chat_id": chat_id, "message": "Revised",
                 "revised_turn_id": str(view["path"][0].message_id)},
        submission_id=str(uuid.uuid4()),
    )
    assert broken.sent[-1]["message"] == cb.UNAVAILABLE_MESSAGE


async def test_revised_message_dispatch_failure_is_refused(owner, audit_recorder):
    hm, chat_id, view = _branched(
        owner,
        turns=(("user", "First"), ("assistant", "Reply"), ("user", "Follow-up")),
    )
    revised_id = view["path"][-1].message_id
    orch = _StubOrch(hm)

    async def broken_serialized(*args, **kwargs):
        raise RuntimeError("dispatch down")

    orch._serialized_chat = broken_serialized
    await cb.dispatch_revised_message(
        orch, object(), user_id=owner,
        payload={"chat_id": chat_id, "message": "Revised",
                 "revised_turn_id": str(revised_id)},
        submission_id=str(uuid.uuid4()),
    )

    assert orch.sent[-1]["message"] == cb.MESSAGES["branch_failed"]
