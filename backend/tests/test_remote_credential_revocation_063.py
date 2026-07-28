"""Feature 063 T028/T066 — FR-015 machine-credential revocation legs.

Logout (web ``/auth/logout`` + 044 native ``/api/auth/logout``) and account
removal each destroy the user's stored machine credentials as part of the
existing revocation flow; machine delete destroys that machine's row (FK
cascade); a deleted machine/credential cascades to tracked-job orphaning at
the next poll (FR-046); and a failing credential leg never blocks local
sign-out (fail-open).

Uses the live Postgres from shared.database defaults per
test_logout_revocation.py conventions: every row is keyed by a uuid4 user_id
so parallel runs never collide, and rows are cleaned up.
"""
import asyncio
import secrets
import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import remote_jobs, remote_machines, web_auth
from orchestrator.credential_manager import CredentialManager
from orchestrator.session_store import WebSessionStore
from shared.database import Database


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, cookies=None, query_params=None, base_url="http://localhost:8001/"):
        self.cookies = cookies or {}
        self.query_params = query_params or {}
        self.base_url = base_url


@pytest.fixture(scope="module")
def db():
    return Database()


@pytest.fixture(scope="module")
def credmgr(db):
    return CredentialManager(db=db)


@pytest.fixture()
def store(db, monkeypatch):
    """A WebSessionStore with a real Fernet key, wired into web_auth."""
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    s = WebSessionStore(db=db)
    monkeypatch.setattr(web_auth, "_get_store", lambda: s)
    return s


@pytest.fixture()
def real_auth_env(monkeypatch):
    """Mock auth OFF + a Keycloak authority so the revocation block runs."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "http://keycloak.test/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)


def _seed_machine(db, credmgr, user_id, label="dgx"):
    machine_id = remote_machines.create_machine(
        db, user_id, label, "10.0.0.5", 22, "me", "linux", "cluster")
    credmgr.set_machine_credential(machine_id, user_id, "password", "hunter2", None)
    return machine_id


def _cred_rows(db, user_id):
    return db.fetch_all(
        "SELECT machine_id FROM machine_credential WHERE owner_user_id = ? ORDER BY machine_id",
        (user_id,))


def _machine_rows(db, user_id):
    return db.fetch_all(
        "SELECT machine_id FROM remote_machine WHERE owner_user_id = ?", (user_id,))


def _cleanup(db, user_id):
    db.execute("DELETE FROM tracked_job WHERE owner_user_id = ?", (user_id,))
    db.execute("DELETE FROM machine_credential WHERE owner_user_id = ?", (user_id,))
    db.execute("DELETE FROM remote_machine WHERE owner_user_id = ?", (user_id,))
    db.execute("DELETE FROM auth_revocation_queue WHERE user_id = ?", (user_id,))


def _stub_offline_grants(monkeypatch):
    from orchestrator.offline_grant import OfflineGrantStore
    monkeypatch.setattr(OfflineGrantStore, "revoke_for_user", lambda self, uid: 0)


# ---------------------------------------------------------------------------
# Web logout leg (FR-015 via /auth/logout)
# ---------------------------------------------------------------------------

def test_web_logout_destroys_machine_credentials(db, credmgr, store, monkeypatch, real_auth_env):
    """Sign-out destroys every machine_credential row the user owns, riding the
    same revocation flow as the refresh token + offline grants. The machine
    inventory itself survives — logout revokes secrets, not machines."""
    user_id = f"u-{uuid.uuid4()}"
    sid = secrets.token_urlsafe(24)
    store.create(sid, user_id=user_id, access_token="at", refresh_token=f"rt-{uuid.uuid4()}",
                 hard_max_seconds=web_auth.HARD_MAX_SECONDS)

    async def ok(token, client_id=None):
        return True

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", ok)
    _stub_offline_grants(monkeypatch)

    try:
        _seed_machine(db, credmgr, user_id, "dgx")
        _seed_machine(db, credmgr, user_id, "hpc")
        assert len(_cred_rows(db, user_id)) == 2

        req = _FakeRequest(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid)})
        resp = asyncio.run(web_auth.auth_logout(req))

        assert resp.status_code == 303
        assert _cred_rows(db, user_id) == []            # FR-015: secrets destroyed
        assert len(_machine_rows(db, user_id)) == 2     # inventory persists
    finally:
        web_auth._SESSIONS.pop(sid, None)
        store.delete(sid)
        _cleanup(db, user_id)


def test_web_logout_survives_credential_leg_failure(db, store, monkeypatch, real_auth_env):
    """Fail-open: a broken credential store never blocks the local sign-out —
    the session still dies, the refresh leg still runs, and logout redirects."""
    user_id = f"u-{uuid.uuid4()}"
    sid = secrets.token_urlsafe(24)
    refresh = f"rt-{uuid.uuid4()}"
    store.create(sid, user_id=user_id, access_token="at", refresh_token=refresh,
                 hard_max_seconds=web_auth.HARD_MAX_SECONDS)

    revoked = []

    async def ok(token, client_id=None):
        revoked.append(token)
        return True

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", ok)
    _stub_offline_grants(monkeypatch)

    def boom(self, uid):
        raise RuntimeError("credential store down")

    monkeypatch.setattr(CredentialManager, "remove_machine_credentials_for_user", boom)

    req = _FakeRequest(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid)})
    try:
        resp = asyncio.run(web_auth.auth_logout(req))
        assert resp.status_code == 303                 # local sign-out completed
        assert sid not in web_auth._SESSIONS
        assert store.get(sid) is None
        assert revoked == [refresh]                    # refresh leg still ran
    finally:
        web_auth._SESSIONS.pop(sid, None)
        store.delete(sid)
        _cleanup(db, user_id)


# ---------------------------------------------------------------------------
# Native logout leg (044 /api/auth/logout parity)
# ---------------------------------------------------------------------------

def test_native_logout_destroys_machine_credentials(db, credmgr, monkeypatch):
    user_id = f"u-{uuid.uuid4()}"

    async def ok(token, client_id=None):
        return True

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", ok)
    _stub_offline_grants(monkeypatch)

    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-desktop,astral-mobile")
    from orchestrator.auth import auth_router, get_current_user_payload
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": user_id, "preferred_username": user_id}

    try:
        _seed_machine(db, credmgr, user_id)
        assert len(_cred_rows(db, user_id)) == 1

        r = TestClient(app).post(
            "/api/auth/logout",
            json={"refresh_token": "rt-native", "client_id": "astral-desktop"})

        assert r.status_code == 200 and r.json()["outcome"] == "revoked"
        assert _cred_rows(db, user_id) == []           # FR-015 parity with web
    finally:
        _cleanup(db, user_id)


# ---------------------------------------------------------------------------
# Account-removal leg (purge_user_remote_compute hook)
# ---------------------------------------------------------------------------

def test_account_removal_purges_credentials_machines_and_jobs(db, credmgr):
    """Account removal destroys the user's machine credentials AND their
    remote_machine / tracked_job rows (data-model.md 'Retirement & revocation');
    a second sweep finds nothing (idempotent)."""
    user_id = f"u-{uuid.uuid4()}"
    try:
        machine_id = _seed_machine(db, credmgr, user_id)
        remote_jobs.create_tracked_job(
            db, owner_user_id=user_id, machine_id=machine_id, chat_id=None,
            scheduler_job_id="77", submit_marker=None, output_path=None,
            component_id=None, job_name="train", notify_on_finish=False)

        counts = remote_machines.purge_user_remote_compute(
            db, user_id, credential_manager=credmgr)

        assert counts == {"credentials": 1, "machines": 1, "jobs": 1}
        assert _cred_rows(db, user_id) == []
        assert _machine_rows(db, user_id) == []
        assert db.fetch_all(
            "SELECT 1 FROM tracked_job WHERE owner_user_id = ?", (user_id,)) == []

        assert remote_machines.purge_user_remote_compute(
            db, user_id, credential_manager=credmgr) == \
            {"credentials": 0, "machines": 0, "jobs": 0}
    finally:
        _cleanup(db, user_id)


# ---------------------------------------------------------------------------
# Machine-delete leg + tracked-job orphaning cascade (FR-046)
# ---------------------------------------------------------------------------

def test_machine_delete_destroys_only_that_machines_credential(db, credmgr):
    """Deleting one machine destroys that machine's credential row via the FK
    ON DELETE CASCADE alone; a sibling machine's credential is untouched."""
    user_id = f"u-{uuid.uuid4()}"
    try:
        m1 = _seed_machine(db, credmgr, user_id, "dgx")
        m2 = _seed_machine(db, credmgr, user_id, "hpc")

        assert remote_machines.delete_machine(db, user_id, m1) is True

        assert [r["machine_id"] for r in _cred_rows(db, user_id)] == [m2]
    finally:
        _cleanup(db, user_id)


def test_deleted_credential_and_machine_orphan_tracked_job_at_poll(db, credmgr):
    """FR-046 / data-model: 'if machine_id's row or its credential is gone at
    poll time, tracking stops' — the delete legs cascade to job orphaning via
    the poller's probe, not synchronously."""
    user_id = f"u-{uuid.uuid4()}"
    try:
        machine_id = _seed_machine(db, credmgr, user_id)
        tid = remote_jobs.create_tracked_job(
            db, owner_user_id=user_id, machine_id=machine_id, chat_id=None,
            scheduler_job_id="88", submit_marker=None, output_path=None,
            component_id=None, job_name="train", notify_on_finish=False)
        orch = SimpleNamespace(history=SimpleNamespace(db=db), credential_manager=credmgr)
        row = dict(db.fetch_one(
            "SELECT * FROM tracked_job WHERE tracked_job_id = ?", (tid,)))

        # Credential-delete leg: machine still present, secret gone → orphan.
        credmgr.delete_machine_credential(machine_id)
        assert remote_jobs._probe_state(orch, row) == {"orphan": True}

        # Machine-delete leg: inventory row gone → orphan.
        remote_machines.delete_machine(db, user_id, machine_id)
        assert remote_jobs._probe_state(orch, row) == {"orphan": True}
    finally:
        _cleanup(db, user_id)
