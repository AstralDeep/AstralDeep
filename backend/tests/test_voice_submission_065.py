"""Proof-bound voice submission, reconnect, and privacy tests for Feature 065."""

from __future__ import annotations

import asyncio
import json
import types
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from llm_config import LLMUnavailable

from orchestrator.orchestrator import (
    ConnectionContext,
    Orchestrator,
    _VoiceDispatchContext,
)


def _id() -> str:
    return str(uuid.uuid4())


def _voice_frame(*, connection_generation: str | None = None) -> dict:
    connection_generation = connection_generation or _id()
    submission_id = _id()
    request_generation = _id()
    chat_id = _id()
    return {
        "type": "ui_event",
        "action": "chat_message",
        "session_id": chat_id,
        "submission_id": submission_id,
        "request_generation": request_generation,
        "connection_generation": connection_generation,
        "payload": {
            "chat_id": chat_id,
            "message": "Patient Jane Doe has record 123-45-6789",
            "submission_id": submission_id,
            "request_generation": request_generation,
            "connection_generation": connection_generation,
            "voice_origin": {
                "schema_version": "1",
                "session_id": _id(),
                "generation": 1,
                "media_grant_revision": 1,
                "turn_id": _id(),
                "client_turn_id": _id(),
                "chat_context_revision": 1,
                "source_participant_identity": f"worker-{uuid.uuid4().hex}",
                "detected_language": "en",
                "text_digest_sha256": "a" * 64,
                "transcript_proof": "b" * 64,
                "proof_expires_at": (
                    datetime.now(UTC) + timedelta(minutes=1)
                ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            },
        },
    }


def _context(websocket: object, generation: str) -> ConnectionContext:
    return ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=999999.0,
        connection_generation=uuid.UUID(generation),
        registered=True,
    )


def test_voice_operation_identity_is_content_free_and_owner_reconnectable() -> None:
    orchestrator = object.__new__(Orchestrator)
    orchestrator._ws_active_chat = {}
    websocket = object()
    raw = _voice_frame()
    context = _context(websocket, raw["connection_generation"])

    first = orchestrator._connection_frame(context, json.dumps(raw), raw)
    assert first is not None
    assert first.operation_kind == "voice_chat_message"
    assert first.read_only is False

    changed = json.loads(json.dumps(raw))
    changed["payload"]["message"] = "different secret transcript"
    changed["payload"]["voice_origin"]["transcript_proof"] = "c" * 64
    changed["payload"]["voice_origin"]["text_digest_sha256"] = "d" * 64
    second = orchestrator._connection_frame(
        context,
        json.dumps(changed),
        changed,
    )
    assert second is not None
    assert second.normalized_digest == first.normalized_digest

    changed_turn = json.loads(json.dumps(raw))
    changed_turn["payload"]["voice_origin"]["turn_id"] = _id()
    third = orchestrator._connection_frame(
        context,
        json.dumps(changed_turn),
        changed_turn,
    )
    assert third is not None
    assert third.normalized_digest != first.normalized_digest


@pytest.mark.asyncio
async def test_reconnect_replays_only_the_exact_durable_voice_ack() -> None:
    orchestrator = object.__new__(Orchestrator)
    websocket = object()
    raw = _voice_frame()
    context = _context(websocket, raw["connection_generation"])
    orchestrator._ws_active_chat = {}
    frame = orchestrator._connection_frame(context, json.dumps(raw), raw)
    assert frame is not None
    origin = raw["payload"]["voice_origin"]
    turn = SimpleNamespace(
        message_id=42,
        turn_id=origin["turn_id"],
        client_turn_id=origin["client_turn_id"],
        session_id=origin["session_id"],
        session_generation=origin["generation"],
        media_grant_revision=origin["media_grant_revision"],
        chat_context_revision=origin["chat_context_revision"],
        submission_id=raw["submission_id"],
        request_generation=raw["request_generation"],
        chat_id=raw["session_id"],
    )

    class Repository:
        def get_turn_by_submission(self, **kwargs):
            assert kwargs == {
                "user_id": "voice-owner",
                "submission_id": raw["submission_id"],
                "request_generation": raw["request_generation"],
            }
            return turn

    sent: list[dict] = []

    async def safe_send(_websocket, data):
        sent.append(json.loads(data))
        return True

    orchestrator.voice_services = SimpleNamespace(repository=Repository())
    orchestrator.ui_sessions = {websocket: {"sub": "voice-owner"}}
    orchestrator._safe_send = safe_send
    assert await orchestrator._replay_voice_ack_if_accepted(context, frame)
    assert sent == [
        {
            "type": "user_message_acked",
            "schema_version": "1",
            "connection_generation": raw["connection_generation"],
            "voice_turn_id": origin["turn_id"],
            "submission_id": raw["submission_id"],
            "request_generation": raw["request_generation"],
            "chat_id": raw["session_id"],
            "message_id": 42,
        }
    ]

    mismatched = SimpleNamespace(**vars(turn))
    mismatched.turn_id = _id()
    sent.clear()
    assert not await orchestrator._send_voice_ack_to_context(
        context,
        frame,
        mismatched,
    )
    assert sent == []


@pytest.mark.asyncio
async def test_voice_dispatch_keeps_destination_bound_and_redacts_ws_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = object()
    raw = _voice_frame()
    user_id = "voice-owner"
    audited: list[dict] = []
    dispatched: list[dict] = []

    async def record_ws_action(**kwargs):
        audited.append(kwargs)

    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", record_ws_action)
    fake = SimpleNamespace()
    fake.ui_sessions = {websocket: {"sub": user_id}}
    fake._get_user_id = lambda _ws: user_id
    fake._parsed_ui_frame = Orchestrator._parsed_ui_frame
    fake._ws_active_chat = {id(websocket): _id()}
    original_active_chat = fake._ws_active_chat[id(websocket)]
    fake.cancelled_sessions = {}

    async def retire(_ws):
        raise AssertionError("voice submission must not retire typed welcome state")

    async def admit(_ws, msg, **kwargs):
        assert kwargs["chat_id"] == raw["session_id"]
        assert kwargs["message"] == raw["payload"]["message"]
        return _VoiceDispatchContext(
            admission=SimpleNamespace(canonical_text="Canonical spoken request"),
            connection_generation=raw["connection_generation"],
            origin=SimpleNamespace(**raw["payload"]["voice_origin"]),
        )

    async def serialized(_ws, message, chat_id, display_message, **kwargs):
        dispatched.append(
            {
                "message": message,
                "chat_id": chat_id,
                "voice_dispatch": kwargs["voice_dispatch"],
            }
        )

    async def safe_send(_ws, _data):
        return True

    fake._retire_welcome_canvas = retire
    fake._admit_voice_chat_message = admit
    fake._serialized_chat = serialized
    fake._safe_send = safe_send
    fake.handle_ui_message = types.MethodType(Orchestrator.handle_ui_message, fake)

    await fake.handle_ui_message(websocket, json.dumps(raw))
    await asyncio.sleep(0)

    assert len(dispatched) == 1
    assert dispatched[0]["message"] == "Canonical spoken request"
    assert dispatched[0]["chat_id"] == raw["session_id"]
    assert fake._ws_active_chat[id(websocket)] == original_active_chat
    assert len(audited) == 1
    audit_payload = audited[0]["payload"]
    serialized_audit = json.dumps(audit_payload, sort_keys=True)
    assert raw["payload"]["message"] not in serialized_audit
    assert raw["payload"]["voice_origin"]["transcript_proof"] not in serialized_audit
    assert raw["payload"]["voice_origin"]["text_digest_sha256"] not in serialized_audit
    assert "source_participant_identity" not in serialized_audit


@pytest.mark.asyncio
async def test_llm_unconfigured_rejects_admitted_voice_before_ack() -> None:
    websocket = object()
    raw = _voice_frame()
    origin = SimpleNamespace(**raw["payload"]["voice_origin"])
    turn = SimpleNamespace(
        submission_id=raw["submission_id"],
        request_generation=raw["request_generation"],
        chat_id=raw["session_id"],
    )
    voice_dispatch = _VoiceDispatchContext(
        admission=SimpleNamespace(
            canonical_text="Canonical spoken request",
            turn=turn,
        ),
        connection_generation=raw["connection_generation"],
        origin=origin,
    )
    orchestrator = object.__new__(Orchestrator)
    orchestrator._LLMUnavailable = LLMUnavailable
    orchestrator.audit_recorder = SimpleNamespace()
    orchestrator.history = SimpleNamespace(db=SimpleNamespace())
    orchestrator.rote = SimpleNamespace(
        get_profile=lambda _websocket: SimpleNamespace(
            device_type=SimpleNamespace(value="web")
        )
    )
    rejected: list[dict] = []
    audits: list[dict] = []
    renders: list[list[dict]] = []

    async def unavailable(_websocket):
        raise orchestrator._LLMUnavailable("not configured")

    async def reject(_websocket, **kwargs):
        rejected.append(kwargs)

    async def record(*args, **kwargs):
        audits.append(kwargs)

    async def render(_websocket, components):
        renders.append(components)

    orchestrator._resolve_llm_client_for = unavailable
    orchestrator._reject_voice_submission = reject
    orchestrator._record_llm_unconfigured = record
    orchestrator._llm_audit_principals = lambda _websocket: (
        "voice-owner",
        "voice-owner",
    )
    orchestrator.send_ui_render = render

    result = await orchestrator._handle_chat_message_impl(
        websocket,
        "Canonical spoken request",
        raw["session_id"],
        user_id="voice-owner",
        draft_agent_id="draft-agent",
        voice_dispatch=voice_dispatch,
    )

    assert result is None
    assert rejected == [
        {
            "user_id": "voice-owner",
            "origin": origin,
            "submission_id": raw["submission_id"],
            "request_generation": raw["request_generation"],
            "chat_id": raw["session_id"],
            "connection_generation": raw["connection_generation"],
            "reason": "permission_denied",
            "retry_policy": "none",
        }
    ]
    assert audits == [
        {
            "actor_user_id": "voice-owner",
            "auth_principal": "voice-owner",
            "feature": "chat_dispatch",
        }
    ]
    assert len(renders) == 1
    assert "Set up your AI provider" in renders[0][0]["message"]
