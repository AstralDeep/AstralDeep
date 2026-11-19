"""Checks a new chat on the orchestrator's UI socket greets with the welcome examples."""

from __future__ import annotations

import asyncio
import json
import types
import uuid
from types import SimpleNamespace

import pytest

from orchestrator.orchestrator import Orchestrator


async def _noop_ws_action(**_kwargs: object) -> None:
    return None


@pytest.mark.asyncio
async def test_new_chat_greets_with_the_welcome_examples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", _noop_ws_action)
    websocket = object()
    chat_id = str(uuid.uuid4())
    sent: list[dict[str, object]] = []
    rendered: list[tuple[list, bool]] = []

    fake = SimpleNamespace(
        ui_sessions={websocket: {"sub": "owner-066"}},
        history=SimpleNamespace(create_chat=lambda *, user_id: chat_id),
        _ws_welcome={},
    )
    fake._get_user_id = lambda _websocket: "owner-066"
    fake._parsed_ui_frame = Orchestrator._parsed_ui_frame
    fake.compute_tools_available_for_user = lambda _user_id: True

    async def send_ui_render(_websocket: object, components, speak=True) -> None:
        rendered.append((components, speak))

    fake.send_ui_render = send_ui_render

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

    assert len(rendered) == 1
    components, speak = rendered[0]
    assert speak is False
    assert components and all(isinstance(c, dict) for c in components)
    assert fake._ws_welcome[id(websocket)] is True
    assert sent == [
        {
            "type": "chat_created",
            "payload": {"chat_id": chat_id, "from_message": False},
        }
    ]
