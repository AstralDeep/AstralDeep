"""Tests for proof-bound voice submission in orchestrator.py: content-free
owner-reconnectable operation identity and replaying only the durable ack on reconnect.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator.orchestrator import (
    ConnectionContext,
    Orchestrator,
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

