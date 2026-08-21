"""Tests for GET /api/chats/{chat_id}/steps (feature 014, T020).

Covers contracts/chat_steps_rest.md:
* 401 without auth.
* 200 with empty list for an authenticated user's empty chat.
* 200 with sorted entries when steps exist.
* 403 when the chat exists but is owned by another user.
* 404 when the chat does not exist.
* Read-time interrupted healing: in-progress rows older than 30 s with no
  active task are reported as ``interrupted`` (not persisted).
* Defense-in-depth re-redaction on the read path.
* Cache-Control: no-store header.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402

os.environ["USE_MOCK_AUTH"] = "true"


MOCK_JWT_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJyZWFsbV9hY2Nlc3MiOnsicm9sZXMiOlsiYWRtaW4iLCJ1c2VyIl19LCJyZXNvdXJjZV9hY2Nlc3MiOnsiYXN0cmFsLWZyb250ZW5kIjp7InJvbGVzIjpbImFkbWluIiwidXNlciJdfX0sInN1YiI6ImRldi11c2VyLWlkIiwicHJlZmVycmVkX3VzZXJuYW1lIjoiRGV2VXNlciJ9."
    "fake-signature-ignore"
)
AUTH_HEADER = {"Authorization": f"Bearer {MOCK_JWT_TOKEN}"}


@pytest.fixture
def app_and_orch(plane_runtime):
    """Build a FastAPI test app with the chat router and a real History+TaskManager."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from orchestrator.api import chat_router
    from orchestrator.auth import auth_router
    from orchestrator.history import HistoryManager
    from orchestrator.task_state import TaskManager

    app = FastAPI(title="Chat Steps Test App")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    history = HistoryManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    orch = MagicMock()
    orch.history = history
    orch.task_manager = TaskManager()
    orch.plane_runtime = plane_runtime
    orch.runtime_composition = SimpleNamespace(
        plane=SimpleNamespace(
            runtime=plane_runtime,
            repositories=plane_runtime.repositories,
        )
    )

    app.state.orchestrator = orch
    app.include_router(chat_router)
    app.include_router(auth_router)
    return app, orch


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("chat_steps_api") as runtime:
        yield runtime


@pytest.fixture
def client(app_and_orch):
    from fastapi.testclient import TestClient

    app, _ = app_and_orch
    return TestClient(app)


@pytest.fixture
def orch(app_and_orch):
    _, orch = app_and_orch
    return orch


@pytest.fixture
def fresh_chat(orch):
    """Create a chat owned by the mock-auth user (dev-user-id)."""
    chat_id = orch.history.create_chat(user_id="dev-user-id")
    yield chat_id
    with orch.plane_runtime.transaction() as transaction:
        orch.plane_runtime.repositories.history.conversations.delete(
            transaction,
            owner_id="dev-user-id",
            conversation_id=chat_id,
        )


def _insert_step(runtime, *, chat_id, user_id, name, status, started_at, ended_at=None,
                 args=None, result=None, error=None):
    step_id = uuid.uuid4().hex
    with runtime.transaction() as transaction:
        repository = runtime.repositories.chat_steps
        record = repository.create_step(
            transaction,
            step_id=step_id,
            owner_id=user_id,
            conversation_id=chat_id,
            turn_message_id=None,
            kind="tool_call",
            name=name,
            args_truncated=args,
            args_was_truncated=False,
            started_at=started_at,
        )
        if status != "in_progress":
            record = repository.finish_step(
                transaction,
                owner_id=user_id,
                step_id=step_id,
                expected_status="in_progress",
                status=status,
                ended_at=ended_at,
                result_summary=result,
                result_was_truncated=False,
                error_message=error,
            )
    return record


class TestAuth:
    def test_401_without_token(self, client, fresh_chat):
        resp = client.get(f"/api/chats/{fresh_chat}/steps")
        assert resp.status_code == 401


class TestEmpty:
    def test_200_empty_list_when_no_steps(self, client, fresh_chat):
        resp = client.get(f"/api/chats/{fresh_chat}/steps", headers=AUTH_HEADER)
        assert resp.status_code == 200
        body = resp.json()
        assert body["chat_id"] == fresh_chat
        assert body["steps"] == []

    def test_cache_control_no_store(self, client, fresh_chat):
        resp = client.get(f"/api/chats/{fresh_chat}/steps", headers=AUTH_HEADER)
        assert resp.headers.get("cache-control") == "no-store"


class TestPopulated:
    def test_returns_steps_in_started_at_order(self, client, orch, fresh_chat):
        now = int(time.time() * 1000)
        _insert_step(
            orch.plane_runtime,
            chat_id=fresh_chat, user_id="dev-user-id",
            name="zeta", status="completed",
            started_at=now + 200, ended_at=now + 300,
        )
        _insert_step(
            orch.plane_runtime,
            chat_id=fresh_chat, user_id="dev-user-id",
            name="alpha", status="completed",
            started_at=now, ended_at=now + 100,
        )

        resp = client.get(f"/api/chats/{fresh_chat}/steps", headers=AUTH_HEADER)
        assert resp.status_code == 200
        steps = resp.json()["steps"]
        assert [s["name"] for s in steps] == ["alpha", "zeta"]


class TestPermissions:
    def test_404_for_unknown_chat(self, client):
        resp = client.get(
            f"/api/chats/{uuid.uuid4()}/steps",
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 404

    def test_403_when_chat_belongs_to_another_user(self, client, orch):
        other_chat = orch.history.create_chat(user_id="someone-else")
        try:
            resp = client.get(
                f"/api/chats/{other_chat}/steps", headers=AUTH_HEADER,
            )
            assert resp.status_code == 403
        finally:
            with orch.plane_runtime.transaction() as transaction:
                orch.plane_runtime.repositories.history.conversations.delete(
                    transaction,
                    owner_id="someone-else",
                    conversation_id=other_chat,
                )


class TestInterruptedHealing:
    def test_stale_in_progress_with_no_active_task_is_interrupted(self, client, orch, fresh_chat):
        # 60-second-old in-progress row, no active task on the chat.
        long_ago = int(time.time() * 1000) - 60_000
        _insert_step(
            orch.plane_runtime,
            chat_id=fresh_chat, user_id="dev-user-id",
            name="stale", status="in_progress",
            started_at=long_ago,
        )
        resp = client.get(f"/api/chats/{fresh_chat}/steps", headers=AUTH_HEADER)
        assert resp.status_code == 200
        step = resp.json()["steps"][0]
        assert step["status"] == "interrupted"

    def test_persisted_row_is_not_mutated_by_healing(self, client, orch, fresh_chat):
        long_ago = int(time.time() * 1000) - 60_000
        persisted = _insert_step(
            orch.plane_runtime,
            chat_id=fresh_chat, user_id="dev-user-id",
            name="stale-2", status="in_progress",
            started_at=long_ago,
        )
        resp = client.get(f"/api/chats/{fresh_chat}/steps", headers=AUTH_HEADER)
        assert resp.json()["steps"][0]["status"] == "interrupted"

        # Underlying row still says in_progress.
        with orch.plane_runtime.transaction() as transaction:
            row = orch.plane_runtime.repositories.chat_steps.get_step(
                transaction,
                owner_id="dev-user-id",
                step_id=persisted.step_id,
            )
        assert row is not None
        assert row.status.value == "in_progress"

    def test_recent_in_progress_is_NOT_healed(self, client, orch, fresh_chat):
        recent = int(time.time() * 1000) - 1000  # 1 second old
        _insert_step(
            orch.plane_runtime,
            chat_id=fresh_chat, user_id="dev-user-id",
            name="recent", status="in_progress",
            started_at=recent,
        )
        resp = client.get(f"/api/chats/{fresh_chat}/steps", headers=AUTH_HEADER)
        assert resp.json()["steps"][0]["status"] == "in_progress"


class TestDefenseInDepthRedaction:
    def test_phi_in_result_is_re_redacted_on_read(self, client, orch, fresh_chat):
        # Simulate a row written by a code path that bypassed the recorder
        # (defense-in-depth scenario): raw PHI sitting in the result_summary
        # column. The endpoint MUST scrub before returning.
        now = int(time.time() * 1000)
        _insert_step(
            orch.plane_runtime,
            chat_id=fresh_chat, user_id="dev-user-id",
            name="legacy-row", status="completed",
            started_at=now, ended_at=now + 10,
            result="Patient SSN 123-45-6789 found in cohort.",
        )
        resp = client.get(f"/api/chats/{fresh_chat}/steps", headers=AUTH_HEADER)
        step = resp.json()["steps"][0]
        assert "123-45-6789" not in (step["result_summary"] or "")
        assert "[REDACTED:ssn]" in step["result_summary"]
