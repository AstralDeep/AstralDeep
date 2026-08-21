"""Feature 028 — FR-023/FR-011: real audit hooks write real rows.

Exercises the REAL ``audit.hooks`` recording helpers (no monkeypatching the
hooks themselves) against the live Postgres ``audit_events`` table:

* ``record_workspace_event`` — workspace mutations land under
  ``event_class='conversation'`` with ``action_type='workspace.<action>'``;
  denials round-trip ``outcome='failure'`` plus scalar ``detail`` fields.
* ``record_auth_event`` — ``auth.logout`` / ``auth.token_refresh_failed``
  land under ``event_class='auth'``.
* FR-011 noise rule — ``web_auth._refresh_session``'s SUCCESS path emits
  zero audit events (functionally and by source inspection); the refusal
  path audits ``auth.token_refresh_failed`` end-to-end through the real
  hook into the database.

Every test uses uuid-unique user ids and purges its own audit rows in a
``finally`` block (the append-only trigger requires the ``audit.allow_purge``
session GUC, mirroring the retention CLI).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
import uuid
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
from orchestrator import web_auth  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db():
    with isolated_plane_runtime("workspace_audit") as runtime:
        yield runtime


@pytest.fixture()
def recorder(db, tmp_path):
    """A REAL Recorder over the live audit_events table, wired into the
    process-global slot that audit.hooks reads. The retry queue is pointed
    at a tmp file so a transient DB failure can never leak rows into the
    running orchestrator's drain loop."""
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
    """Delete this test's rows. audit_events is append-only behind a trigger;
    deletion requires the audit.allow_purge GUC (same path as the retention CLI)."""
    with db.transaction() as transaction:
        transaction.execute("SET LOCAL audit.allow_purge = 'true'")
        for uid in user_ids:
            transaction.execute(
                "DELETE FROM audit_events WHERE actor_user_id = %s",
                (uid,),
            )


def _uid() -> str:
    return f"pytest-wah-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# record_workspace_event (FR-023)
# ---------------------------------------------------------------------------


def test_component_added_writes_conversation_success_row(db, recorder):
    """028 FR-023: component_added lands as one conversation-class row with
    action_type='workspace.component_added', default outcome=success."""
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
        assert row["outcome"] == "success"  # default, not passed explicitly
        assert row["actor_user_id"] == user_id
        assert row["auth_principal"] == user_id  # direct user action: principal == actor
        assert row["conversation_id"] == chat_id
        assert row["inputs_meta"] == {"component_id": component_id}
        assert row["description"] == "Workspace component added"  # generated default
        assert row["recorded_at"] is not None
    finally:
        _purge_audit(db, user_id)


def test_action_denied_failure_with_detail_roundtrips(db, recorder):
    """028 FR-023: action_denied with outcome='failure' + detail{'reason':...}
    round-trips; only scalar detail values enter inputs_meta."""
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
        # non-scalar detail values are dropped (data-minimization posture)
        assert "nested" not in row["inputs_meta"]
        assert "tags" not in row["inputs_meta"]
    finally:
        _purge_audit(db, user_id)


def test_workspace_hook_noop_guards(db, recorder):
    """The hook is a silent no-op without a wired Recorder and for the
    'legacy'/empty pseudo-users — no rows, no exceptions."""
    user_id = _uid()

    # 1) No recorder wired -> nothing written even for a real user.
    set_recorder(None)
    try:
        asyncio.run(record_workspace_event(
            user_id=user_id, action="component_added", chat_id="c1",
        ))
        assert _audit_rows(db, user_id) == []
    finally:
        set_recorder(recorder)

    # 2) Recorder wired, but unauthenticated pseudo-users never record.
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


# ---------------------------------------------------------------------------
# record_auth_event (FR-011 — the events that ARE audited)
# ---------------------------------------------------------------------------


def test_logout_writes_auth_row(db, recorder):
    """028 FR-011: logout lands under event_class='auth' as 'auth.logout'."""
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
    """028 FR-011: a refused silent refresh is audited as
    'auth.token_refresh_failed' with outcome='failure'."""
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


# ---------------------------------------------------------------------------
# FR-011 noise rule: successful silent refreshes are NOT audited
# ---------------------------------------------------------------------------


class _FakeTokenResponse:
    def __init__(self, payload=None, fail=False):
        self._payload = payload or {}
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise httpx.HTTPStatusError(
                "400 Bad Request", request=None, response=None,
            )

    def json(self):
        return self._payload


def _fake_async_client(payload=None, fail=False):
    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            return _FakeTokenResponse(payload, fail=fail)

    return _FakeAsyncClient


def _refresh_env(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "http://keycloak.test/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(web_auth, "_get_store", lambda: None)


def test_refresh_session_success_path_is_audit_silent(monkeypatch):
    """028 FR-011: a SUCCESSFUL silent refresh updates tokens and emits
    ZERO audit events (token_refresh success is noise, never recorded)."""
    _refresh_env(monkeypatch)
    monkeypatch.setattr(
        web_auth.httpx, "AsyncClient",
        _fake_async_client({"access_token": "new-at", "refresh_token": "new-rt"}),
    )

    audit_calls = []

    async def capture_audit(action, sub, description, *, outcome="success"):
        audit_calls.append((action, sub, outcome))

    monkeypatch.setattr(web_auth, "_audit", capture_audit)

    kill_calls = []
    real_kill = web_auth._kill_session

    async def capture_kill(sid, sess, **kw):
        kill_calls.append(sid)
        await real_kill(sid, sess, **kw)

    monkeypatch.setattr(web_auth, "_kill_session", capture_kill)

    user_id = _uid()
    sess = {
        "access_token": "old-at", "refresh_token": "rt-old",
        "sub": user_id, "created_at": time.time(),
    }
    out = asyncio.run(web_auth._refresh_session(f"sid-{uuid.uuid4().hex[:8]}", sess))

    assert out is sess  # session survives, refreshed in place
    assert out["access_token"] == "new-at"
    assert out["refresh_token"] == "new-rt"
    assert audit_calls == []  # the noise rule: success is silent
    assert kill_calls == []


def test_refresh_session_source_confines_audit_to_failure_branch():
    """028 FR-011 (grep-level): _refresh_session never calls _audit directly;
    auditing is delegated to _kill_session, invoked exactly once with
    audit_action='token_refresh_failed' on the refusal branch."""
    src = inspect.getsource(web_auth._refresh_session)
    assert "_audit(" not in src, "success path must not audit"
    assert src.count('audit_action="token_refresh_failed"') == 1
    assert src.count("_kill_session(") == 1
    # The audit call itself lives in _kill_session, gated on audit_action.
    kill_src = inspect.getsource(web_auth._kill_session)
    assert "_audit(" in kill_src
    assert "if audit_action:" in kill_src


def test_refresh_session_refusal_audits_token_refresh_failed_end_to_end(db, recorder, monkeypatch):
    """028 FR-011: a refresh REFUSED by the IdP kills the session and writes
    a real 'auth.token_refresh_failed' failure row through the unpatched
    _audit -> record_auth_event -> Recorder -> Postgres pipeline."""
    _refresh_env(monkeypatch)
    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _fake_async_client(fail=True))

    user_id = _uid()
    sid = f"sid-{uuid.uuid4().hex[:12]}"
    sess = {
        "access_token": "old-at", "refresh_token": "rt-dead",
        "sub": user_id, "created_at": time.time(),
    }
    web_auth._SESSIONS[sid] = sess
    try:
        out = asyncio.run(web_auth._refresh_session(sid, sess))
        assert out is None  # dead session: interactive login required
        assert sid not in web_auth._SESSIONS

        rows = _audit_rows(db, user_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_class"] == "auth"
        assert row["action_type"] == "auth.token_refresh_failed"
        assert row["outcome"] == "failure"
        assert row["actor_user_id"] == user_id
        assert "refresh" in row["description"].lower()
    finally:
        web_auth._SESSIONS.pop(sid, None)
        _purge_audit(db, user_id)
