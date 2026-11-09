"""Owner steering of a running turn under turn_steer_v1: negotiation, a per-turn mailbox, and delivery
at the next tool boundary. A steer is owner text only; it never answers an approval and grants nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from orchestrator.typesafe_routing.security_policy import Verdict

logger = logging.getLogger("Orchestrator.TurnSteer")

CAPABILITY = "turn_steer_v1"
MAX_MESSAGE_SCALARS = 20000
# The web client reports one of these depending on its window size.
WEB_DEVICE_TYPES = frozenset({"browser", "tablet", "mobile"})
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
_UNSAVED = "This steer could not be saved, so it was not applied."
_NOTICE_TASKS: set = set()


class _Mailbox:
    __slots__ = ("applied", "pending", "seen")

    def __init__(self, request) -> None:
        self.applied: list = [request] if request else []
        self.pending: list[tuple[str, str]] = []
        self.seen: set[str] = set()


def negotiated(profile, device, capabilities) -> bool:
    device_type = getattr(getattr(profile, "device_type", None), "value", None)
    mac_console = device_type == "macos" and getattr(profile, "console_contract", None) == "console/v2"
    return (
        (mac_console or device_type in WEB_DEVICE_TYPES)
        and isinstance(device, dict)
        and device.get(CAPABILITY) is True
        and CAPABILITY in (capabilities or ())
    )


def acknowledge(profile: dict, enabled: bool) -> dict:
    # The durable queue is not offered yet, so the client hides its affordances.
    if not enabled:
        return profile
    ack = {"version": 1, "queue_capable": False}
    if isinstance(profile.get("console"), dict):
        profile["console"]["turn_steer"] = ack
    elif profile.get("device_type") in WEB_DEVICE_TYPES:
        # The web client has no console profile, so its acknowledgment sits on the profile itself.
        profile["turn_steer"] = ack
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


def _key(websocket, chat_id, user_id):
    return (id(websocket), chat_id, user_id)


async def _send(orch, websocket, frame: dict) -> None:
    await orch._safe_send(websocket, json.dumps(frame))


async def _refuse(orch, websocket, chat_id, client_message_id, code, message=None) -> None:
    frame = {"type": "chat_steer_refused", "client_message_id": client_message_id,
             "code": code, "message": message or _REFUSALS[code]}
    if chat_id:
        frame["chat_id"] = chat_id
    await _send(orch, websocket, frame)
    logger.info("chat_steer.rejected code=%s", code)


def _approval_pending(orch, websocket, user_id, chat_id) -> bool:
    now = time.monotonic()
    return any(call.owner == user_id and call.chat == chat_id and call.websocket is websocket
               and call.expires > now
               for call in getattr(orch, "_hitl_pending_calls", {}).values())


def _notify_phi(orch, websocket, chat_id, user_id, text) -> None:
    try:
        task = asyncio.get_running_loop().create_task(
            orch._notify_phi_if_detected(websocket, chat_id, user_id, text))
    except Exception:
        logger.debug("chat_steer phi notice scheduling failed (non-fatal)", exc_info=True)
        return
    _NOTICE_TASKS.add(task)
    task.add_done_callback(_NOTICE_TASKS.discard)


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
    mailbox = _mailboxes(orch).get(_key(websocket, chat_id, user_id)) if chat_id and user_id else None
    if mailbox is None:
        await _refuse(orch, websocket, chat_id, client_message_id, "not_running")
        return
    if client_message_id in mailbox.seen:
        logger.info("chat_steer.duplicate")
        return
    if _approval_pending(orch, websocket, user_id, chat_id):
        await _refuse(orch, websocket, chat_id, client_message_id, "approval_pending")
        return
    if len(mailbox.pending) >= MAX_PENDING:
        await _refuse(orch, websocket, chat_id, client_message_id, "capacity_exceeded")
        return
    mailbox.seen.add(client_message_id)
    mailbox.pending.append((client_message_id, text))


def open_turn(orch, websocket, chat_id, user_id, request=None) -> None:
    _mailboxes(orch)[_key(websocket, chat_id, user_id)] = _Mailbox(request)


def running_texts(orch, websocket, chat_id, user_id) -> list:
    mailbox = _mailboxes(orch).get(_key(websocket, chat_id, user_id))
    return list(mailbox.applied) if mailbox else []


async def deliver(orch, websocket, chat_id, user_id, stage, messages, *, screened=False) -> int:
    mailbox = _mailboxes(orch).get(_key(websocket, chat_id, user_id))
    delivered = 0
    while mailbox is not None and mailbox.pending:
        client_message_id, text = mailbox.pending[0]
        try:
            await orch._append_conversation_message(
                stage, chat_id=chat_id, user_id=user_id, role="user", content=text)
        except Exception:
            mailbox.pending.pop(0)
            logger.warning("chat_steer.save_failed", exc_info=True)
            await _refuse(orch, websocket, chat_id, client_message_id, "stale_turn", _UNSAVED)
            continue
        mailbox.pending.pop(0)
        mailbox.applied.append(text)
        messages.append({"role": "user", "content": text})
        _notify_phi(orch, websocket, chat_id, user_id, text)
        await _send(orch, websocket, {"type": "chat_steer_recorded", "chat_id": chat_id,
                                      "client_message_id": client_message_id, "skipped_calls": []})
        logger.info("chat_steer.applied")
        delivered += 1
    if delivered and screened:
        # The turn's TypeSafe verdict scored only the original message, never this steer.
        orch._typesafe_set_turn_verdict(chat_id, Verdict.CONFIRM_TOOLS)
    return delivered


def close_turn(orch, websocket, chat_id, user_id) -> list[str]:
    mailbox = _mailboxes(orch).pop(_key(websocket, chat_id, user_id), None)
    return [client_message_id for client_message_id, _text in mailbox.pending] if mailbox else []


async def hand_back(orch, websocket, chat_id, unapplied) -> None:
    for client_message_id in unapplied:
        await _refuse(orch, websocket, chat_id, client_message_id, "stale_turn")
