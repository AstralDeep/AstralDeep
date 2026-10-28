"""Applies the owner's chat_rename, chat_pin, chat_unpin, chat_archive and chat_unarchive
ui_event actions through HistoryManager, refusing malformed, foreign or unknown chats with a
bounded error frame, auditing each change and refreshing every owner socket's history list.
"""

from __future__ import annotations

import asyncio
import json
import logging
import unicodedata
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("ChatOrganization")

TITLE_LIMIT = 120
ACTIONS = {
    "chat_rename": None,
    "chat_pin": ("pinned", True),
    "chat_unpin": ("pinned", False),
    "chat_archive": ("archived", True),
    "chat_unarchive": ("archived", False),
}
_AUDIT_ACTIONS = {
    "chat_rename": "chat.renamed", "chat_pin": "chat.pinned", "chat_unpin": "chat.unpinned",
    "chat_archive": "chat.archived", "chat_unarchive": "chat.unarchived",
}
MESSAGES = {
    "invalid_request": "That chat request is not valid.",
    "invalid_title": f"A chat title needs 1 to {TITLE_LIMIT} visible characters.",
    "chat_not_found": "That chat is not available.",
    "chat_update_failed": "The chat could not be updated. Try again.",
}


def sanitized_title(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    kept = "".join(" " if unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} else ch
                   for ch in unicodedata.normalize("NFC", value))
    title = " ".join(kept.split())
    return title if 0 < len(title) <= TITLE_LIMIT else None


async def _refuse(orch, websocket, action: str, code: str) -> None:
    await orch._safe_send(websocket, json.dumps({"type": "error", "code": code, "message": MESSAGES[code],
                                                 "action": action}))


async def _audit(user_id: str, action: str, chat_id: str) -> None:
    from audit.recorder import get_recorder, make_correlation_id
    from audit.schemas import AuditEventCreate

    recorder = get_recorder()
    if recorder is None:
        return
    try:
        await recorder.record(AuditEventCreate(
            actor_user_id=user_id, auth_principal=user_id, event_class="conversation",
            action_type=_AUDIT_ACTIONS[action], description=_AUDIT_ACTIONS[action],
            conversation_id=chat_id, correlation_id=make_correlation_id(), outcome="success",
            started_at=datetime.now(timezone.utc)))
    except Exception:  # noqa: BLE001
        logger.warning("chat organization audit failed (%s)", action, exc_info=True)


async def handle(orch, websocket, user_id: str, action: str, payload: Any) -> None:
    allowed = {"chat_id", "title"} if action == "chat_rename" else {"chat_id"}
    chat_id = payload.get("chat_id") if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or set(payload) - allowed or not isinstance(chat_id, str)
            or not 0 < len(chat_id) <= 256):
        await _refuse(orch, websocket, action, "invalid_request")
        return
    try:
        if action == "chat_rename":
            title = sanitized_title(payload.get("title"))
            if title is None:
                await _refuse(orch, websocket, action, "invalid_title")
                return
            changed = await asyncio.to_thread(orch.history.rename_chat, chat_id, title, user_id=user_id)
        else:
            flag, value = ACTIONS[action]
            changed = await asyncio.to_thread(orch.history.set_chat_flag, chat_id, flag, value, user_id=user_id)
    except Exception:  # noqa: BLE001
        logger.warning("chat organization failed (%s)", action, exc_info=True)
        await _refuse(orch, websocket, action, "chat_update_failed")
        return
    if not changed:
        await _refuse(orch, websocket, action, "chat_not_found")
        return
    await _audit(user_id, action, chat_id)
    await orch._broadcast_user_history(user_id=user_id)
