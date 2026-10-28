"""Drives chat_rename, chat_pin, chat_archive and get_history include_archived through a live
Orchestrator's ui_event dispatch over PostgreSQL, so the owner-only actions, their refusals and
the history_list fields run end-to-end.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.test_orchestrator_offloop_paths import (  # noqa: F401
    USER_ID,
    _frames,
    chat_env,
    orch,
    registered_ws,
)

pytestmark = pytest.mark.asyncio


async def _send(orch, ws, action, payload):  # noqa: F811
    await orch.handle_ui_message(ws, json.dumps({"type": "ui_event", "action": action, "payload": payload}))


async def test_rename_pin_and_archive_reach_history_list(orch, registered_ws, chat_env):  # noqa: F811
    ws, chat_id = registered_ws, chat_env
    orch.ui_clients.append(ws)
    await asyncio.to_thread(orch.history.add_message, chat_id, "user", "hello", user_id=USER_ID)
    await _send(orch, ws, "chat_rename", {"chat_id": chat_id, "title": " Renamed\tchat "})
    await _send(orch, ws, "chat_pin", {"chat_id": chat_id})
    row = next(c for c in _frames(ws, "history_list")[-1]["chats"] if c["id"] == chat_id)
    assert (row["title"], row["pinned"], row["archived"]) == ("Renamed chat", True, False)
    await _send(orch, ws, "chat_archive", {"chat_id": chat_id})
    assert all(c["id"] != chat_id for c in _frames(ws, "history_list")[-1]["chats"])
    await _send(orch, ws, "get_history", {"include_archived": True})
    row = next(c for c in _frames(ws, "history_list")[-1]["chats"] if c["id"] == chat_id)
    assert row["archived"] is True
    for action in ("chat_unpin", "chat_unarchive"):
        await _send(orch, ws, action, {"chat_id": chat_id})
    row = next(c for c in _frames(ws, "history_list")[-1]["chats"] if c["id"] == chat_id)
    assert (row["pinned"], row["archived"]) == (False, False)


async def test_a_foreign_chat_is_refused_through_dispatch(orch, registered_ws):  # noqa: F811
    other = await asyncio.to_thread(orch.history.create_chat, user_id="someone-else")
    try:
        await asyncio.to_thread(orch.history.add_message, other, "user", "theirs", user_id="someone-else")
        await _send(orch, registered_ws, "chat_rename", {"chat_id": other, "title": "mine now"})
        error = _frames(registered_ws, "error")[-1]
        assert (error["code"], error["action"]) == ("chat_not_found", "chat_rename")
        chat = await asyncio.to_thread(orch.history.get_chat, other, user_id="someone-else")
        assert chat["title"] != "mine now"
    finally:
        await asyncio.to_thread(orch.history.delete_chat, other, user_id="someone-else")
