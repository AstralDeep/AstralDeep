"""Feature 063 T028/T066 — FR-015 machine-credential revocation legs.

Logout (web ``/auth/logout`` + 044 native ``/api/auth/logout``) and account
removal each destroy the user's stored machine credentials as part of the
existing revocation flow; machine delete destroys that machine's row (FK
cascade); a deleted machine/credential cascades to tracked-job orphaning at
the next poll (FR-046); and a failing credential leg never blocks local
sign-out (fail-open).

Uses one isolated current AstralPlane PostgreSQL runtime; every row is keyed by
a uuid4 user_id and cleanup uses the typed account-retirement boundary.
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
from tests.helpers.session_plane_runtime import (
    isolated_plane_runtime,
    purge_revocations,
    web_session_store,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, cookies=None, query_params=None, base_url="http://localhost:8001/"):
        self.cookies = cookies or {}
        self.query_params = query_params or {}
        self.base_url = base_url


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("remote_credential") as runtime:
        yield runtime


@pytest.fixture()
def credmgr(plane_runtime, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return CredentialManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )


@pytest.fixture()
def store(plane_runtime, monkeypatch):
    """A WebSessionStore with a real Fernet key, wired into web_auth."""
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    s = web_session_store(plane_runtime)
    monkeypatch.setattr(web_auth, "_get_store", lambda: s)
    return s


@pytest.fixture()
def real_auth_env(monkeypatch):
    """Mock auth OFF + a Keycloak authority so the revocation block runs."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "http://keycloak.test/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)


def _seed_machine(plane_runtime, credmgr, user_id, label="dgx"):
    machine_id = remote_machines.create_machine(
        plane_runtime,
        user_id,
        label,
        "10.0.0.5",
        22,
        "me",
        "linux",
        "cluster",
    )
    credmgr.set_machine_credential(machine_id, user_id, "password", "hunter2", None)
    return machine_id


def _credential_exists(plane_runtime, user_id, machine_id):
    with plane_runtime.transaction() as transaction:
        return (
            plane_runtime.repositories.credentials.get_machine_credential(
                transaction,
                owner_id=user_id,
                machine_id=machine_id,
            )
            is not None
        )


def _machine_ids(plane_runtime, user_id):
    return tuple(
        machine["machine_id"]
        for machine in remote_machines.list_machines(plane_runtime, user_id)
    )


def _cleanup(plane_runtime, user_id, credmgr):
    remote_machines.purge_user_remote_compute(
        plane_runtime,
        user_id,
        credential_manager=credmgr,
    )
    purge_revocations(plane_runtime, (user_id,))


def _stub_offline_grants(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.offline_grant.get_offline_grant_store",
        lambda: SimpleNamespace(revoke_for_user=lambda _uid: 0),
    )


# ---------------------------------------------------------------------------
# Web logout leg (FR-015 via /auth/logout)
# ---------------------------------------------------------------------------

def test_web_logout_destroys_machine_credentials(
    plane_runtime, credmgr, store, monkeypatch, real_auth_env
):
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
    monkeypatch.setattr(web_auth, "_CREDENTIAL_MANAGER", credmgr)
    _stub_offline_grants(monkeypatch)

    try:
        first = _seed_machine(plane_runtime, credmgr, user_id, "dgx")
        second = _seed_machine(plane_runtime, credmgr, user_id, "hpc")
        assert _credential_exists(plane_runtime, user_id, first)
        assert _credential_exists(plane_runtime, user_id, second)

        req = _FakeRequest(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid)})
        resp = asyncio.run(web_auth.auth_logout(req))

        assert resp.status_code == 303
        assert not _credential_exists(plane_runtime, user_id, first)
        assert not _credential_exists(plane_runtime, user_id, second)
        assert set(_machine_ids(plane_runtime, user_id)) == {first, second}
    finally:
        web_auth._SESSIONS.pop(sid, None)
        store.delete(sid)
        _cleanup(plane_runtime, user_id, credmgr)


def test_web_logout_survives_credential_leg_failure(
    plane_runtime, credmgr, store, monkeypatch, real_auth_env
):
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
    monkeypatch.setattr(web_auth, "_CREDENTIAL_MANAGER", credmgr)
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
        purge_revocations(plane_runtime, (user_id,))


# ---------------------------------------------------------------------------
# Native logout leg (044 /api/auth/logout parity)
# ---------------------------------------------------------------------------

def test_native_logout_destroys_machine_credentials(
    plane_runtime, credmgr, monkeypatch
):
    user_id = f"u-{uuid.uuid4()}"

    async def ok(token, client_id=None):
        return True

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", ok)
    monkeypatch.setattr(web_auth, "_CREDENTIAL_MANAGER", credmgr)
    _stub_offline_grants(monkeypatch)

    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-desktop,astral-mobile")
    from orchestrator.auth import auth_router, get_current_user_payload
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": user_id, "preferred_username": user_id}

    try:
        machine_id = _seed_machine(plane_runtime, credmgr, user_id)
        assert _credential_exists(plane_runtime, user_id, machine_id)

        r = TestClient(app).post(
            "/api/auth/logout",
            json={"refresh_token": "rt-native", "client_id": "astral-desktop"})

        assert r.status_code == 200 and r.json()["outcome"] == "revoked"
        assert not _credential_exists(plane_runtime, user_id, machine_id)
    finally:
        _cleanup(plane_runtime, user_id, credmgr)


# ---------------------------------------------------------------------------
# Account-removal leg (purge_user_remote_compute hook)
# ---------------------------------------------------------------------------

def test_account_removal_purges_credentials_machines_and_jobs(
    plane_runtime, credmgr
):
    """Account removal destroys the user's machine credentials AND their
    remote_machine / tracked_job rows (data-model.md 'Retirement & revocation');
    a second sweep finds nothing (idempotent)."""
    user_id = f"u-{uuid.uuid4()}"
    try:
        machine_id = _seed_machine(plane_runtime, credmgr, user_id)
        remote_jobs.create_tracked_job(
            plane_runtime, owner_user_id=user_id, machine_id=machine_id, chat_id=None,
            scheduler_job_id="77", submit_marker=None, output_path=None,
            component_id=None, job_name="train", notify_on_finish=False)

        counts = remote_machines.purge_user_remote_compute(
            plane_runtime, user_id, credential_manager=credmgr)

        assert counts == {"credentials": 1, "machines": 1, "jobs": 1}
        assert not _credential_exists(plane_runtime, user_id, machine_id)
        assert _machine_ids(plane_runtime, user_id) == ()
        with plane_runtime.transaction() as transaction:
            assert plane_runtime.repositories.tracked_jobs.list_for_owner(
                transaction,
                owner_id=user_id,
                include_terminal=True,
            ) == ()

        assert remote_machines.purge_user_remote_compute(
            plane_runtime, user_id, credential_manager=credmgr) == \
            {"credentials": 0, "machines": 0, "jobs": 0}
    finally:
        _cleanup(plane_runtime, user_id, credmgr)


# ---------------------------------------------------------------------------
# Machine-delete leg + tracked-job orphaning cascade (FR-046)
# ---------------------------------------------------------------------------

def test_machine_delete_destroys_only_that_machines_credential(
    plane_runtime, credmgr
):
    """Deleting one machine destroys that machine's credential row via the FK
    ON DELETE CASCADE alone; a sibling machine's credential is untouched."""
    user_id = f"u-{uuid.uuid4()}"
    try:
        m1 = _seed_machine(plane_runtime, credmgr, user_id, "dgx")
        m2 = _seed_machine(plane_runtime, credmgr, user_id, "hpc")

        assert remote_machines.delete_machine(plane_runtime, user_id, m1) is True

        assert not _credential_exists(plane_runtime, user_id, m1)
        assert _credential_exists(plane_runtime, user_id, m2)
    finally:
        _cleanup(plane_runtime, user_id, credmgr)


def test_deleted_credential_and_machine_orphan_tracked_job_at_poll(
    plane_runtime, credmgr
):
    """FR-046 / data-model: 'if machine_id's row or its credential is gone at
    poll time, tracking stops' — the delete legs cascade to job orphaning via
    the poller's probe, not synchronously."""
    user_id = f"u-{uuid.uuid4()}"
    try:
        machine_id = _seed_machine(plane_runtime, credmgr, user_id)
        tid = remote_jobs.create_tracked_job(
            plane_runtime, owner_user_id=user_id, machine_id=machine_id, chat_id=None,
            scheduler_job_id="88", submit_marker=None, output_path=None,
            component_id=None, job_name="train", notify_on_finish=False)
        orch = SimpleNamespace(
            plane_repository_source=plane_runtime,
            credential_manager=credmgr,
        )
        with plane_runtime.transaction() as transaction:
            record = plane_runtime.repositories.tracked_jobs.get(
                transaction,
                owner_id=user_id,
                tracked_job_id=tid,
            )
        assert record is not None
        row = remote_jobs._record_to_dict(record)

        # Credential-delete leg: machine still present, secret gone → orphan.
        credmgr.delete_machine_credential(machine_id, user_id)
        assert remote_jobs._probe_state(orch, row) == {"orphan": True}

        # Machine-delete leg: inventory row gone → orphan.
        remote_machines.delete_machine(plane_runtime, user_id, machine_id)
        assert remote_jobs._probe_state(orch, row) == {"orphan": True}
    finally:
        _cleanup(plane_runtime, user_id, credmgr)
