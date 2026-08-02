"""Correlated no-selected-chat activation tests for Feature 065."""

from __future__ import annotations

import asyncio
import json
import types
import uuid
from types import SimpleNamespace

import pytest

from orchestrator.orchestrator import Orchestrator
from shared.protocol import ChatCreated, Message


def _id() -> str:
    return str(uuid.uuid4())


def _correlated_new_chat() -> dict[str, object]:
    connection_generation = _id()
    submission_id = _id()
    request_generation = _id()
    return {
        "type": "ui_event",
        "action": "new_chat",
        "schema_version": "1",
        "connection_generation": connection_generation,
        "submission_id": submission_id,
        "request_generation": request_generation,
        "payload": {
            "schema_version": "1",
            "connection_generation": connection_generation,
            "submission_id": submission_id,
            "request_generation": request_generation,
        },
    }


@pytest.mark.asyncio
async def test_correlated_new_chat_returns_the_complete_strict_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = object()
    request = _correlated_new_chat()
    chat_id = _id()
    created_for: list[str] = []
    sent: list[dict[str, object]] = []

    async def record_ws_action(**_kwargs: object) -> None:
        return None

    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", record_ws_action)

    fake = SimpleNamespace(
        ui_sessions={websocket: {"sub": "voice-owner"}},
        history=SimpleNamespace(
            create_chat=lambda *, user_id: (
                created_for.append(user_id) or chat_id
            )
        ),
    )
    fake._get_user_id = lambda _websocket: "voice-owner"
    fake._parsed_ui_frame = Orchestrator._parsed_ui_frame

    async def safe_send(_websocket: object, raw: str) -> bool:
        sent.append(json.loads(raw))
        return True

    fake._safe_send = safe_send
    fake.handle_ui_message = types.MethodType(Orchestrator.handle_ui_message, fake)

    await fake.handle_ui_message(websocket, json.dumps(request))
    await asyncio.sleep(0)

    assert created_for == ["voice-owner"]
    assert len(sent) == 1
    response = Message.from_json(json.dumps(sent[0]))
    assert isinstance(response, ChatCreated)
    assert response.connection_generation == request["connection_generation"]
    assert response.submission_id == request["submission_id"]
    assert response.request_generation == request["request_generation"]
    assert response.payload == {
        "schema_version": "1",
        "chat_id": chat_id,
        "from_message": False,
        "connection_generation": request["connection_generation"],
        "submission_id": request["submission_id"],
        "request_generation": request["request_generation"],
    }


@pytest.mark.asyncio
async def test_legacy_new_chat_keeps_its_legacy_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = object()
    chat_id = _id()
    sent: list[dict[str, object]] = []

    async def record_ws_action(**_kwargs: object) -> None:
        return None

    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", record_ws_action)
    fake = SimpleNamespace(
        ui_sessions={websocket: {"sub": "typed-owner"}},
        history=SimpleNamespace(create_chat=lambda *, user_id: chat_id),
    )
    fake._get_user_id = lambda _websocket: "typed-owner"
    fake._parsed_ui_frame = Orchestrator._parsed_ui_frame

    async def safe_send(_websocket: object, raw: str) -> bool:
        sent.append(json.loads(raw))
        return True

    fake._safe_send = safe_send
    fake.handle_ui_message = types.MethodType(Orchestrator.handle_ui_message, fake)

    await fake.handle_ui_message(
        websocket,
        json.dumps({"type": "ui_event", "action": "new_chat", "payload": {}}),
    )
    await asyncio.sleep(0)

    assert sent == [
        {
            "type": "chat_created",
            "payload": {"chat_id": chat_id, "from_message": False},
        }
    ]
