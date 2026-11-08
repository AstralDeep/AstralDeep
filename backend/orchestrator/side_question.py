"""Answers an owner's side question under turn_steer_v1 from a snapshot of the conversation, with no
tools and no delegation. Nothing is written to history; the answer goes only to the asking socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

logger = logging.getLogger("Orchestrator.SideQuestion")

MAX_SCALARS = 20000
TRANSCRIPT_MESSAGES = 40
_FIELDS = {"question", "client_message_id"}
_ENVELOPE = {"submission_id", "request_generation"}
_SYSTEM = ("Answer the owner's side question using only the conversation so far. You have no tools. "
           "Be brief, and say so if the conversation does not contain the answer.")


def _client_message_id(value):
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return value if parsed is not None and parsed.version == 4 and str(parsed) == value else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(filter(None, (_text(part) for part in content)))
    if isinstance(content, dict):
        for key in ("content", "text", "message", "title"):
            value = content.get(key)
            if isinstance(value, (str, list, dict)):
                return _text(value)
    return ""


async def _transcript(orch, chat_id, user_id) -> list:
    if not chat_id:
        return []
    chat = await asyncio.to_thread(orch.history.get_chat, chat_id, user_id=user_id)
    turns = []
    for message in (chat or {}).get("messages", [])[-TRANSCRIPT_MESSAGES:]:
        role, text = message.get("role"), _text(message.get("content")).strip()
        if role in {"user", "assistant"} and text:
            turns.append({"role": role, "content": text[:MAX_SCALARS]})
    return turns


async def answer(orch, websocket, user_id, chat_id, payload) -> None:
    payload = payload if isinstance(payload, dict) else {}
    client_message_id = _client_message_id(payload.get("client_message_id"))
    if client_message_id is None:
        return
    question = payload.get("question")
    question = question.strip() if isinstance(question, str) else ""
    if ((orch.ui_sessions.get(websocket) or {}).get("_turn_steer_v1") is not True or not question
            or len(question) > MAX_SCALARS or not _FIELDS <= set(payload) <= _FIELDS | _ENVELOPE):
        reply = "This side question could not be read."
    else:
        try:
            messages = [{"role": "system", "content": _SYSTEM},
                        *await _transcript(orch, chat_id, user_id),
                        {"role": "user", "content": question}]
            message, _usage = await orch._call_llm(websocket, messages, None, feature="side_question")
            reply = (getattr(message, "content", None) or "").strip()[:MAX_SCALARS]
        except Exception:
            logger.warning("side question failed", exc_info=True)
            reply = ""
        reply = reply or "The side question could not be answered right now. Try again."
    logger.info("side_question.answered")
    await orch._safe_send(websocket, json.dumps(
        {"type": "side_question_result", "client_message_id": client_message_id, "answer": reply}))
