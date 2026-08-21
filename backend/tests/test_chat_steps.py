"""Tests for backend/orchestrator/chat_steps.py — ChatStepRecorder lifecycle.

Feature 014, T006. Covers:

* :meth:`start` persists an in-progress row and emits an in-progress event.
* :meth:`complete` flips status to ``completed`` with truncated result.
* :meth:`error` flips status to ``errored`` with redacted error message.
* :meth:`cancel_all_in_flight` marks every in-progress step ``cancelled``.
* PHI redaction is applied to args, result, and error message.
* Late completion after cancellation is dropped (R6 best-effort discard).
* ``messages.step_count`` is bumped per started step.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def db():
    with isolated_plane_runtime("chat_steps") as runtime:
        yield runtime


@pytest.fixture
def chat_and_message(db):
    """Create a real chat + user message so FK constraints are satisfied."""
    chat_id = f"pytest-{uuid.uuid4().hex[:12]}"
    user_id = "pytest-user"
    now = int(time.time() * 1000)
    db.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, user_id, "test", now, now),
    )
    db.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, user_id, "user", "hi", now),
    )
    msg_row = db.fetch_one(
        "SELECT id FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    )
    yield chat_id, user_id, msg_row["id"]
    db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))


class FakeWebSocket:
    pass


@pytest.fixture
def emitted():
    """Captures every payload sent through ``_safe_send`` substitute."""
    sent: list[dict] = []

    async def safe_send(_ws, data: str):
        sent.append(json.loads(data))
        return True

    return sent, safe_send


@pytest.fixture
def recorder(db, chat_and_message, emitted):
    from orchestrator.chat_steps import ChatStepRecorder

    chat_id, user_id, msg_id = chat_and_message
    _sent, safe_send = emitted
    return ChatStepRecorder(
        plane_runtime=db,
        plane_repositories=db.repositories,
        websocket=FakeWebSocket(),
        safe_send=safe_send,
        chat_id=chat_id,
        user_id=user_id,
        turn_message_id=msg_id,
    )


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


class TestStart:
    def test_persists_in_progress_row(self, db, recorder):
        step_id = asyncio.run(recorder.start("tool_call", "search_grants", {"q": "biomed"}))
        row = db.fetch_one("SELECT * FROM chat_steps WHERE id = ?", (step_id,))
        assert row is not None
        assert row["status"] == "in_progress"
        assert row["kind"] == "tool_call"
        assert row["name"] == "search_grants"
        assert row["ended_at"] is None

    def test_emits_in_progress_event(self, recorder, emitted):
        sent, _ = emitted
        step_id = asyncio.run(recorder.start("tool_call", "search_grants", {"q": "x"}))
        events = [e for e in sent if e["type"] == "chat_step" and e["step"]["id"] == step_id]
        assert len(events) == 1
        assert events[0]["chat_id"] == recorder.chat_id
        assert events[0]["step"]["status"] == "in_progress"
        assert events[0]["step"]["ended_at"] is None

    def test_bumps_messages_step_count(self, db, recorder):
        msg_id = recorder.turn_message_id
        before = db.fetch_one("SELECT step_count FROM messages WHERE id = ?", (msg_id,))
        asyncio.run(recorder.start("tool_call", "a", {}))
        asyncio.run(recorder.start("tool_call", "b", {}))
        asyncio.run(recorder.start("phase", "synthesis", {}))
        after = db.fetch_one("SELECT step_count FROM messages WHERE id = ?", (msg_id,))
        assert after["step_count"] == before["step_count"] + 3

    def test_phi_in_args_is_redacted(self, db, recorder):
        step_id = asyncio.run(recorder.start(
            "tool_call",
            "lookup_patient",
            {"name": "Jane Doe", "ssn": "123-45-6789"},
        ))
        row = db.fetch_one(
            "SELECT args_truncated FROM chat_steps WHERE id = ?",
            (step_id,),
        )
        assert "Jane Doe" not in row["args_truncated"]
        assert "123-45-6789" not in row["args_truncated"]
        assert "[REDACTED:phi]" in row["args_truncated"]


class TestComplete:
    def test_flips_status_and_persists_result(self, db, recorder):
        step_id = asyncio.run(recorder.start("tool_call", "search_grants", {"q": "x"}))
        asyncio.run(recorder.complete(step_id, {"found": 17, "top": "NSF-XYZ"}))
        row = db.fetch_one("SELECT * FROM chat_steps WHERE id = ?", (step_id,))
        assert row["status"] == "completed"
        assert row["ended_at"] is not None
        assert "17" in row["result_summary"]

    def test_emits_terminal_event(self, recorder, emitted):
        sent, _ = emitted
        step_id = asyncio.run(recorder.start("tool_call", "x", {}))
        sent.clear()
        asyncio.run(recorder.complete(step_id, {"ok": True}))
        events = [e for e in sent if e["type"] == "chat_step"]
        assert len(events) == 1
        assert events[0]["step"]["status"] == "completed"
        assert events[0]["step"]["ended_at"] is not None

    def test_phi_in_result_is_redacted(self, db, recorder):
        step_id = asyncio.run(recorder.start("tool_call", "x", {}))
        asyncio.run(recorder.complete(step_id, {"patient_name": "Jane Doe"}))
        row = db.fetch_one(
            "SELECT result_summary FROM chat_steps WHERE id = ?",
            (step_id,),
        )
        assert "Jane Doe" not in row["result_summary"]


class TestError:
    def test_flips_status_and_persists_error(self, db, recorder):
        step_id = asyncio.run(recorder.start("tool_call", "x", {}))
        asyncio.run(recorder.error(step_id, RuntimeError("boom")))
        row = db.fetch_one("SELECT * FROM chat_steps WHERE id = ?", (step_id,))
        assert row["status"] == "errored"
        assert row["ended_at"] is not None
        assert "boom" in row["error_message"]

    def test_phi_in_error_is_redacted(self, db, recorder):
        step_id = asyncio.run(recorder.start("tool_call", "x", {}))
        asyncio.run(recorder.error(
            step_id,
            "Failed to fetch record for SSN 123-45-6789",
        ))
        row = db.fetch_one(
            "SELECT error_message FROM chat_steps WHERE id = ?",
            (step_id,),
        )
        assert "123-45-6789" not in row["error_message"]
        assert "[REDACTED:ssn]" in row["error_message"]


class TestCancellation:
    def test_cancel_all_marks_every_in_flight(self, db, recorder):
        s1 = asyncio.run(recorder.start("tool_call", "a", {}))
        s2 = asyncio.run(recorder.start("tool_call", "b", {}))
        s3 = asyncio.run(recorder.start("phase", "c", {}))
        asyncio.run(recorder.complete(s2, {"ok": 1}))  # one already done
        asyncio.run(recorder.cancel_all_in_flight())

        rows = {r["id"]: r["status"] for r in db.fetch_all(
            "SELECT id, status FROM chat_steps WHERE id IN (?, ?, ?)",
            (s1, s2, s3),
        )}
        assert rows[s1] == "cancelled"
        assert rows[s2] == "completed"  # already terminal — untouched
        assert rows[s3] == "cancelled"

    def test_late_complete_after_cancel_is_dropped(self, db, recorder):
        step_id = asyncio.run(recorder.start("tool_call", "x", {}))
        asyncio.run(recorder.cancel_all_in_flight())
        # Late-arriving result should not flip status back to completed.
        asyncio.run(recorder.complete(step_id, {"late": True}))
        row = db.fetch_one("SELECT status FROM chat_steps WHERE id = ?", (step_id,))
        assert row["status"] == "cancelled"

    def test_is_terminal_reports_correctly(self, recorder):
        step_id = asyncio.run(recorder.start("tool_call", "x", {}))
        assert recorder.is_terminal(step_id) is False
        asyncio.run(recorder.complete(step_id, None))
        assert recorder.is_terminal(step_id) is True


class TestNoWebSocket:
    def test_recorder_works_without_a_websocket(self, db, chat_and_message):
        from orchestrator.chat_steps import ChatStepRecorder

        chat_id, user_id, msg_id = chat_and_message
        rec = ChatStepRecorder(
            plane_runtime=db,
            plane_repositories=db.repositories,
            websocket=None,
            safe_send=None,
            chat_id=chat_id,
            user_id=user_id,
            turn_message_id=msg_id,
        )
        # No exception raised; row still persisted.
        step_id = asyncio.run(rec.start("tool_call", "lonely", {"q": "x"}))
        row = db.fetch_one("SELECT status FROM chat_steps WHERE id = ?", (step_id,))
        assert row is not None
        assert row["status"] == "in_progress"


class TestLoggingDoesNotBreakLifecycle:
    """Regression for the live-only stuck-step bug.

    ``recorder.start`` logged with ``extra={..., "name": name}`` — but "name"
    is a reserved LogRecord attribute, so once the logger is INFO-enabled
    (as in the deployed container; pytest's WARNING default short-circuits
    record creation and hid this) the logging call itself raised KeyError
    *after* the row was persisted and the in-progress event emitted. Callers
    caught the exception and got ``step_id=None``, so complete()/error() were
    never invoked and every step stayed ``in_progress`` forever.
    """

    def test_full_lifecycle_with_info_logging_enabled(self, db, recorder):
        import logging

        steps_logger = logging.getLogger("Orchestrator.ChatSteps")
        old_level = steps_logger.level
        steps_logger.setLevel(logging.INFO)
        try:
            step_id = asyncio.run(recorder.start("tool_call", "get_cpu_info", {"q": 1}))
            assert step_id is not None
            asyncio.run(recorder.complete(step_id, {"ok": True}))
            row = db.fetch_one(
                "SELECT status, ended_at FROM chat_steps WHERE id = ?", (step_id,)
            )
            assert row["status"] == "completed"
            assert row["ended_at"] is not None
        finally:
            steps_logger.setLevel(old_level)
