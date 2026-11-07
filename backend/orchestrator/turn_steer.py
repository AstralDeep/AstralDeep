"""Owner steering of a running turn under turn_steer_v1: negotiation, a per-turn mailbox, and delivery
at the next tool boundary. A steer is owner text only; it never answers an approval and grants nothing.
"""

from __future__ import annotations

import json
import logging
import uuid

logger = logging.getLogger("Orchestrator.TurnSteer")

CAPABILITY = "turn_steer_v1"
MAX_MESSAGE_SCALARS = 20000
MAX_PENDING = 16
# The client repeats the standard envelope identity inside every ui_event payload.
_PAYLOAD_FIELDS = {"message", "client_message_id"}
_ENVELOPE_FIELDS = {"submission_id", "request_generation"}
_REFUSALS = {
    "stale_turn": "The turn ended before this steer could be applied.",
    "not_running": "No turn is running in this chat.",
    "approval_pending": "Answer the approval first; a steer cannot answer it.",
    "invalid_input": "This steer could not be read.",
    "capacity_exceeded": "Too many steers are waiting for this turn.",
}


def negotiated(profile, device, capabilities) -> bool:
    return (
        getattr(getattr(profile, "device_type", None), "value", None) == "macos"
        and getattr(profile, "console_contract", None) == "console/v2"
        and isinstance(device, dict)
        and device.get(CAPABILITY) is True
        and CAPABILITY in (capabilities or ())
    )


def acknowledge(profile: dict, enabled: bool) -> dict:
    """Adds the acknowledgment the client looks for; the durable queue is not offered yet."""
    if enabled and isinstance(profile.get("console"), dict):
        profile["console"]["turn_steer"] = {"version": 1, "queue_capable": False}
    return profile


def _client_message_id(value):
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return value if parsed is not None and parsed.version == 4 and str(parsed) == value else None


def _mailboxes(orch) -> dict:
    boxes = getattr(orch, "_turn_steers", None)
    if boxes is None:
        boxes = orch._turn_steers = {}
    return boxes


async def _send(orch, websocket, frame: dict) -> None:
    await orch._safe_send(websocket, json.dumps(frame))


async def _refuse(orch, websocket, chat_id, client_message_id, code) -> None:
    frame = {"type": "chat_steer_refused", "client_message_id": client_message_id,
             "code": code, "message": _REFUSALS[code]}
    if chat_id:
        frame["chat_id"] = chat_id
    await _send(orch, websocket, frame)
    logger.info("chat_steer.rejected code=%s", code)


async def receive(orch, websocket, user_id, chat_id, payload) -> None:
    payload = payload if isinstance(payload, dict) else {}
    client_message_id = _client_message_id(payload.get("client_message_id"))
    if client_message_id is None:
        return
    if (orch.ui_sessions.get(websocket) or {}).get("_turn_steer_v1") is not True:
        await _refuse(orch, websocket, chat_id, client_message_id, "invalid_input")
        return
    message = payload.get("message")
    text = message.strip() if isinstance(message, str) else ""
    if (not text or len(text) > MAX_MESSAGE_SCALARS
            or not _PAYLOAD_FIELDS <= set(payload) <= _PAYLOAD_FIELDS | _ENVELOPE_FIELDS):
        await _refuse(orch, websocket, chat_id, client_message_id, "invalid_input")
        return
    mailbox = _mailboxes(orch).get((id(websocket), chat_id)) if chat_id else None
    if mailbox is None:
        await _refuse(orch, websocket, chat_id, client_message_id, "not_running")
        return
    if any((call.owner, call.chat, call.websocket) == (user_id, chat_id, websocket)
           for call in getattr(orch, "_hitl_pending_calls", {}).values()):
        await _refuse(orch, websocket, chat_id, client_message_id, "approval_pending")
        return
    if len(mailbox) >= MAX_PENDING:
        await _refuse(orch, websocket, chat_id, client_message_id, "capacity_exceeded")
        return
    mailbox.append((client_message_id, text))


def open_turn(orch, websocket, chat_id) -> None:
    _mailboxes(orch)[(id(websocket), chat_id)] = []


async def deliver(orch, websocket, chat_id, user_id, stage, messages) -> int:
    """Appends waiting steers as owner messages before the next model call."""
    mailbox = _mailboxes(orch).get((id(websocket), chat_id))
    delivered = 0
    while mailbox:
        client_message_id, text = mailbox.pop(0)
        messages.append({"role": "user", "content": text})
        await orch._append_conversation_message(
            stage, chat_id=chat_id, user_id=user_id, role="user", content=text)
        await _send(orch, websocket, {"type": "chat_steer_recorded", "chat_id": chat_id,
                                      "client_message_id": client_message_id, "skipped_calls": []})
        logger.info("chat_steer.applied")
        delivered += 1
    return delivered


async def close_turn(orch, websocket, chat_id) -> None:
    for client_message_id, _text in _mailboxes(orch).pop((id(websocket), chat_id), None) or []:
        await _refuse(orch, websocket, chat_id, client_message_id, "stale_turn")
