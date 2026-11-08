"""Answers an owner's side question under turn_steer_v1 from a snapshot of the conversation, with no
tools and no delegation. Nothing is written to history; the answer goes only to the asking socket.
The running turn's request and applied steers come from turn_steer.py's owner-keyed mailbox.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from orchestrator import turn_steer

logger = logging.getLogger("Orchestrator.SideQuestion")

MAX_SCALARS = 20000
TRANSCRIPT_MESSAGES = 10
TRANSCRIPT_MESSAGE_SCALARS = 2000
_FIELDS = {"question", "client_message_id"}
_ENVELOPE = {"submission_id", "request_generation"}
_SYSTEM = ("Answer the owner's side question using only the conversation so far. You have no tools. "
           "Be brief, and say so if the conversation does not contain the answer.")
_UNREADABLE = "This side question could not be read."
_BUSY = "Another side question is still being answered. Ask again when it finishes."
_UNANSWERED = "The side question could not be answered right now. Try again."


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
        return "\n".join(filter(None, (_text(content.get(key)) for key in ("title", "content", "text", "message"))))
    return ""


async def _transcript(orch, websocket, chat_id, user_id) -> list:
    if not chat_id:
        return []
    chat = await asyncio.to_thread(orch.history.get_chat, chat_id, user_id=user_id)
    turns = []
    for message in (chat or {}).get("messages", [])[-TRANSCRIPT_MESSAGES:]:
        role, text = message.get("role"), _text(message.get("content")).strip()
        if role in {"user", "assistant"} and text:
            turns.append({"role": role, "content": text})
    # A running turn publishes its request and steers only when it finishes.
    published = {turn["content"] for turn in turns if turn["role"] == "user"}
    for text in turn_steer.running_texts(orch, websocket, chat_id, user_id):
        text = _text(text).strip()
        if text and text not in published:
            turns.append({"role": "user", "content": text})
    return [{"role": turn["role"], "content": turn["content"][:TRANSCRIPT_MESSAGE_SCALARS]}
            for turn in turns[-TRANSCRIPT_MESSAGES:]]


def _in_flight(orch) -> set:
    sockets = getattr(orch, "_side_questions_in_flight", None)
    if sockets is None:
        sockets = orch._side_questions_in_flight = set()
    return sockets


async def _model_reply(orch, websocket, user_id, chat_id, question) -> str:
    try:
        messages = [{"role": "system", "content": _SYSTEM},
                    *await _transcript(orch, websocket, chat_id, user_id),
                    {"role": "user", "content": question}]
        message, _usage = await orch._call_llm(websocket, messages, None, feature="side_question")
        reply = (getattr(message, "content", None) or "").strip()[:MAX_SCALARS]
    except Exception:
        logger.warning("side question failed", exc_info=True)
        reply = ""
    return reply or _UNANSWERED


async def answer(orch, websocket, user_id, chat_id, payload) -> None:
    payload = payload if isinstance(payload, dict) else {}
    client_message_id = _client_message_id(payload.get("client_message_id"))
    if client_message_id is None:
        return
    question = payload.get("question")
    question = question.strip() if isinstance(question, str) else ""
    in_flight = _in_flight(orch)
    if ((orch.ui_sessions.get(websocket) or {}).get("_turn_steer_v1") is not True or not question
            or len(question) > MAX_SCALARS or not _FIELDS <= set(payload) <= _FIELDS | _ENVELOPE):
        reply = _UNREADABLE
    elif id(websocket) in in_flight:
        logger.info("side_question.busy")
        reply = _BUSY
    else:
        in_flight.add(id(websocket))
        try:
            reply = await _model_reply(orch, websocket, user_id, chat_id, question)
        finally:
            in_flight.discard(id(websocket))
        logger.info("side_question.answered")
    await orch._safe_send(websocket, json.dumps(
        {"type": "side_question_result", "client_message_id": client_message_id, "answer": reply}))
