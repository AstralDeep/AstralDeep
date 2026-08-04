"""Bug A regression (2026-08-03): the first answer of a new chat vanished.

Live symptom, reproduced on the iOS simulator against the real stack: start
a new chat in ANY client (``new_chat`` → ``chat_created`` → ``chat_message``
WITH the created id), send a first message, get no response and no UI update;
repeating the message works.

Root cause chain:
- ``_chat_narrative`` persists a multi-paragraph answer (>280 chars, a blank
  line, or a leading heading — the common shape of a fresh answer) as
  ``Card(title=…, content=[Text(answer)])``. That card is chat-rail
  narrative and NEVER enters the workspace.
- Feature 063's ``_rail_parts`` dropped every non-``text`` component from the
  snapshot transcript, so the committed conversation snapshot contained the
  user message but NOT the answer (and the canvas was empty too).
- Feature 066's operation-chat adoption made the end-of-turn commit snapshot
  actually publish — and the authoritative snapshot then REPLACED the live
  view answer-less on every client, wiping the transient render ~ms after it
  appeared. A terse repeat answer (single line ≤280 chars) stayed a bare
  ``Text`` and survived, which is why repeating "worked".

The fix lifts the words out of TEXT-ONLY wrapper chrome in ``_rail_parts``
(orchestrator/history.py). This file pins the end-to-end truth over the REAL
ingress path: new_chat → chat_message(chat_id) with a scripted two-paragraph
LLM answer must deliver a commit ``conversation_snapshot`` whose transcript
carries the assistant's words. Requires the docker-compose Postgres; skipped
where unreachable.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

pytestmark = pytest.mark.asyncio

# Two paragraphs — the exact shape _chat_narrative wraps in a Card.
ANSWER = (
    "9 + 10 = 21.\n\n"
    "Just kidding — it is actually 19; the 21 is a meme answer."
)


class _CaptureSocket:
    def __init__(self):
        self.frames: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.frames.append(json.loads(text))

    async def send(self, text: str) -> None:
        self.frames.append(json.loads(text))


@pytest.fixture
def orch(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    from orchestrator.orchestrator import Orchestrator

    try:
        o = Orchestrator()
    except Exception as exc:  # pragma: no cover — env-dependent
        pytest.skip(f"orchestrator/database unavailable: {exc}")
    return o


def _seed_user(orch, user_id: str) -> None:
    orch._llm_store.set_sync(
        user_id,
        provider="custom",
        base_url="http://test.invalid/v1",
        model="test-model",
        api_key="test-key",
    )

    async def fake_llm(_client, _messages, tools=None, **_kwargs):
        msg = SimpleNamespace(
            role="assistant",
            content=ANSWER,
            tool_calls=None,
            reasoning_content=None,
        )
        usage = SimpleNamespace(
            prompt_tokens=1, completion_tokens=1, total_tokens=2
        )
        return msg, usage

    orch._call_llm = fake_llm


def _connect(orch, user_id: str):
    ws = _CaptureSocket()
    orch.ui_sessions[ws] = {"sub": user_id, "preferred_username": user_id}
    orch.ui_clients.append(ws)
    orch.rote.register_device(ws, {})
    context = orch._new_connection_context(ws)
    context.registered = True
    context.connection_generation = uuid.uuid4()
    return ws, context


def _ui_event(action: str, payload: dict, connection_generation) -> str:
    return json.dumps(
        {
            "type": "ui_event",
            "action": action,
            "payload": payload,
            "submission_id": str(uuid.uuid4()),
            "request_generation": str(uuid.uuid4()),
            "connection_generation": str(connection_generation),
        }
    )


async def _drain(context, ws, settle: float = 0.25, timeout: float = 30.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last = -1
    while loop.time() < deadline:
        pending = [
            t
            for t in asyncio.all_tasks()
            if not t.done()
            and t is not asyncio.current_task()
            and "connection-" in (t.get_name() or "")
        ]
        if not pending and len(ws.frames) == last:
            return
        last = len(ws.frames)
        await asyncio.sleep(settle)


async def _cleanup(orch, user_id: str, chat_id: str | None) -> None:
    if chat_id:
        try:
            await asyncio.to_thread(
                orch.history.delete_chat, chat_id, user_id=user_id
            )
        except Exception:
            pass


async def test_first_turn_card_narrative_survives_commit_snapshot(orch):
    """new_chat → first chat_message: the commit snapshot carries the answer."""
    user_id = f"buga-{uuid.uuid4().hex[:10]}"
    # set_sync is a synchronous DB write — keep it off the event-loop thread
    # (LOOP_GUARD_ENFORCE=1 in CI raises on it).
    await asyncio.to_thread(_seed_user, orch, user_id)
    ws, context = _connect(orch, user_id)
    chat_id = None
    try:
        assert await orch._route_ui_frame(
            context, _ui_event("new_chat", {}, context.connection_generation)
        )
        await _drain(context, ws)
        created = [f for f in ws.frames if f.get("type") == "chat_created"]
        assert created, f"no chat_created: {[f.get('type') for f in ws.frames]}"
        chat_id = created[0]["payload"]["chat_id"]
        ws.frames.clear()

        # The client sends the id it was given — the chat EXISTS branch.
        assert await orch._route_ui_frame(
            context,
            _ui_event(
                "chat_message",
                {"message": "What is 9+10", "chat_id": chat_id},
                context.connection_generation,
            ),
        )
        await _drain(context, ws)

        snapshots = [
            f
            for f in ws.frames
            if f.get("type") == "conversation_snapshot"
            and f.get("chat_id") == chat_id
            and f.get("snapshot_purpose") == "commit"
        ]
        assert snapshots, (
            "no commit conversation_snapshot delivered: "
            f"{[f.get('type') for f in ws.frames]}"
        )
        snapshot = snapshots[-1]
        assistant_text = " ".join(
            part.get("text", "")
            for message in snapshot.get("transcript", [])
            if message.get("role") == "assistant"
            for part in message.get("parts", [])
            if part.get("type") == "text"
        )
        # Pre-fix: the assistant message reduced to zero rail parts and was
        # omitted — the snapshot held only the user's message and every
        # client rendered the turn answer-less.
        assert "actually 19" in assistant_text, (
            "assistant answer missing from the committed snapshot: "
            f"{json.dumps(snapshot)[:600]}"
        )
    finally:
        await _cleanup(orch, user_id, chat_id)


async def test_snapshot_transcript_lifts_card_narrative_words(orch):
    """build_snapshot-level pin: a persisted Card narrative keeps its words."""
    user_id = f"buga-{uuid.uuid4().hex[:10]}"
    chat_id = None
    try:
        chat_id = await asyncio.to_thread(
            orch.history.create_chat, user_id=user_id
        )
        await asyncio.to_thread(
            orch.history.add_message, chat_id, "user", "hello", user_id=user_id
        )
        card = {
            "type": "card",
            "title": "Response",
            "variant": "default",
            "content": [
                {"type": "text", "content": ANSWER, "variant": "markdown"}
            ],
        }
        await asyncio.to_thread(
            orch.history.add_message,
            chat_id,
            "assistant",
            json.dumps([card]),
            user_id=user_id,
        )
        snapshot = await asyncio.to_thread(
            lambda: orch.conversation_commits.build_snapshot(
                chat_id=chat_id,
                owner_user_id=user_id,
                connection_generation=str(uuid.uuid4()),
                request_generation=str(uuid.uuid4()),
                snapshot_purpose="hydration",
            )
        )
        assistant = [
            m for m in snapshot["transcript"] if m["role"] == "assistant"
        ]
        assert assistant, "assistant message omitted from snapshot transcript"
        texts = [
            p["text"] for p in assistant[0]["parts"] if p["type"] == "text"
        ]
        assert any("actually 19" in t for t in texts), texts
    finally:
        await _cleanup(orch, user_id, chat_id)
