"""Tests for the audit hooks (backend/audit/hooks.py, backend/audit/recorder.py) against
a live Postgres audit_events table: workspace and auth events land with correct
class/outcome, and successful silent refresh stays audit-silent.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("AUDIT_HMAC_SECRET", "pytest-audit-secret")
os.environ.setdefault("AUDIT_HMAC_KEY_ID", "k1")

from audit.hooks import record_auth_event, record_workspace_event  # noqa: E402
from audit.recorder import Recorder, get_recorder, set_recorder  # noqa: E402
from audit.repository import AuditRepository  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


@pytest.fixture(scope="module")
def db():
    with isolated_plane_runtime("workspace_audit") as runtime:
        yield runtime


@pytest.fixture()
def recorder(db, tmp_path):
    prev = get_recorder()
    rec = Recorder(
        AuditRepository(
            plane_runtime=db,
            plane_repositories=db.repositories,
        ),
        retry_queue=tmp_path / "audit-retry.jsonl",
    )
    set_recorder(rec)
    yield rec
    set_recorder(prev)


def _audit_rows(db, user_id):
    return list(db.fetch_all(
        "SELECT * FROM audit_events WHERE actor_user_id = ? "
        "ORDER BY recorded_at ASC, event_id ASC",
        (user_id,),
    ))


def _purge_audit(db, *user_ids):
    with db.transaction() as transaction:
        transaction.execute("SET LOCAL audit.allow_purge = 'true'")
        for uid in user_ids:
            transaction.execute(
                "DELETE FROM audit_events WHERE actor_user_id = %s",
                (uid,),
            )


def _uid() -> str:
    return f"pytest-wah-{uuid.uuid4().hex[:12]}"


def test_component_added_writes_conversation_success_row(db, recorder):
    user_id = _uid()
    chat_id = f"chat-{uuid.uuid4().hex[:12]}"
    component_id = f"wc_{uuid.uuid4().hex[:16]}"
    try:
        asyncio.run(record_workspace_event(
            user_id=user_id,
            action="component_added",
            chat_id=chat_id,
            component_id=component_id,
        ))
        rows = _audit_rows(db, user_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_class"] == "conversation"
        assert row["action_type"] == "workspace.component_added"
        assert row["outcome"] == "success"
        assert row["actor_user_id"] == user_id
        assert row["auth_principal"] == user_id
        assert row["conversation_id"] == chat_id
        assert row["inputs_meta"] == {"component_id": component_id}
        assert row["description"] == "Workspace component added"
        assert row["recorded_at"] is not None
    finally:
        _purge_audit(db, user_id)


def test_action_denied_failure_with_detail_roundtrips(db, recorder):
    user_id = _uid()
    chat_id = f"chat-{uuid.uuid4().hex[:12]}"
    component_id = f"wc_{uuid.uuid4().hex[:16]}"
    try:
        asyncio.run(record_workspace_event(
            user_id=user_id,
            action="action_denied",
            chat_id=chat_id,
            component_id=component_id,
            outcome="failure",
            detail={
                "reason": "unsupported_kind:bogus",
                "attempt": 2,
                "nested": {"never": "stored"},
                "tags": ["never", "stored"],
            },
        ))
        rows = _audit_rows(db, user_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_class"] == "conversation"
        assert row["action_type"] == "workspace.action_denied"
        assert row["outcome"] == "failure"
        assert row["conversation_id"] == chat_id
        assert row["inputs_meta"]["reason"] == "unsupported_kind:bogus"
        assert row["inputs_meta"]["attempt"] == 2
        assert row["inputs_meta"]["component_id"] == component_id
        assert "nested" not in row["inputs_meta"]
        assert "tags" not in row["inputs_meta"]
    finally:
        _purge_audit(db, user_id)


def test_workspace_hook_noop_guards(db, recorder):
    user_id = _uid()

    set_recorder(None)
    try:
        asyncio.run(record_workspace_event(
            user_id=user_id, action="component_added", chat_id="c1",
        ))
        assert _audit_rows(db, user_id) == []
    finally:
        set_recorder(recorder)

    legacy_before = len(_audit_rows(db, "legacy"))
    asyncio.run(record_workspace_event(
        user_id="legacy", action="component_added", chat_id="c1",
    ))
    asyncio.run(record_workspace_event(
        user_id="", action="component_added", chat_id="c1",
    ))
    assert len(_audit_rows(db, "legacy")) == legacy_before
    assert _audit_rows(db, "") == []
    assert _audit_rows(db, user_id) == []


def test_logout_writes_auth_row(db, recorder):
    user_id = _uid()
    try:
        asyncio.run(record_auth_event(
            claims={"sub": user_id, "preferred_username": "sam", "azp": "astral-frontend"},
            action="logout",
            description="User signed out; session and refresh credential revoked",
        ))
        rows = _audit_rows(db, user_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_class"] == "auth"
        assert row["action_type"] == "auth.logout"
        assert row["outcome"] == "success"
        assert row["actor_user_id"] == user_id
        assert row["auth_principal"] == user_id
        assert row["inputs_meta"]["preferred_username"] == "sam"
        assert row["inputs_meta"]["azp"] == "astral-frontend"
        assert row["description"] == "User signed out; session and refresh credential revoked"
    finally:
        _purge_audit(db, user_id)


def test_token_refresh_failed_writes_auth_failure_row(db, recorder):
    user_id = _uid()
    try:
        asyncio.run(record_auth_event(
            claims={"sub": user_id},
            action="token_refresh_failed",
            description="Silent token refresh refused by the identity provider",
            outcome="failure",
            outcome_detail="refresh_token revoked at IdP",
        ))
        rows = _audit_rows(db, user_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_class"] == "auth"
        assert row["action_type"] == "auth.token_refresh_failed"
        assert row["outcome"] == "failure"
        assert row["outcome_detail"] == "refresh_token revoked at IdP"
    finally:
        _purge_audit(db, user_id)


class _FakeTokenResponse:
    def __init__(self, payload=None, fail=False):
        self._payload = payload or {}
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise httpx.HTTPStatusError(
                "400 Bad Request", request=None, response=None,
            )

    async def aiter_bytes(self, chunk_size):
        body = json.dumps(self._payload).encode()
        for offset in range(0, len(body), chunk_size):
            yield body[offset:offset + chunk_size]


def _fake_async_client(payload=None, fail=False):
    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        @asynccontextmanager
        async def stream(self, method, url, *, data, follow_redirects):
            assert method == "POST" and follow_redirects is False
            assert data["grant_type"] == "refresh_token"
            yield _FakeTokenResponse(payload, fail=fail)

    return _FakeAsyncClient


