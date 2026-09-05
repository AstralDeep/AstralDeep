"""Feature 068 â€” kiosk sign-in surface (/kiosk + /auth/kiosk/*).

Two properties carry the security of this surface and are pinned here:

* the device **handle never reaches the browser** (page script must not be able
  to redeem it at ``/api/auth/device/poll``, which relays raw tokens by design
  for the native watch), and
* **no token material** appears in any kiosk response body.

Plus the flag-off posture (the router is absent, so ``GET /`` is untouched) and
the issuing-client fix without which a kiosk session dies at the first silent
refresh.
"""
from __future__ import annotations

import base64
from contextlib import asynccontextmanager
import json
import time
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import web_auth as wa
from shared.feature_flags import FeatureFlags


def _jwt(payload: dict) -> str:
    """An unsigned JWT â€” these paths decode without verifying (JWKS runs later)."""
    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg(payload)}.x"


KIOSK_TOKEN = _jwt({"sub": "kiosk-user", "azp": "astral-kiosk"})
WEB_TOKEN = _jwt({"sub": "web-user", "azp": "astral-frontend"})


def _refresh_store(access):
    """Client-selection unit seam; durable CAS is covered with real PostgreSQL."""
    async def refresh(sid, *, owner_id, exchange):
        payload = await exchange("r1", access)
        return {"access_token": payload["access_token"],
                "refresh_token": payload.get("refresh_token", "r1")}
    return SimpleNamespace(refresh_credential=refresh)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEB_SESSION_SECRET", "kiosk-test-secret")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KIOSK_DEVICE_CLIENT", "astral-kiosk")
    monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
    wa._KIOSK_FLOWS.clear()
    wa._SESSIONS.clear()
    yield
    wa._KIOSK_FLOWS.clear()
    wa._SESSIONS.clear()


@pytest.fixture()
def client(monkeypatch):
    """An app with ONLY the kiosk router mounted (no orchestrator boot)."""
    # The durable store is unavailable in unit context; sessions stay in-memory.
    monkeypatch.setattr(wa, "_get_store", lambda: None)
    app = FastAPI()
    app.include_router(wa.kiosk_router)
    return TestClient(app)


class _FakeDeviceLogin:
    """Stands in for orchestrator.device_login at the module seam."""

    class DeviceLoginError(Exception):
        def __init__(self, msg="", code="device_login_error", status=500):
            super().__init__(msg)
            self.code = code
            self.status = status

    def __init__(self, poll_result=None, start_raises=None, poll_raises=None):
        self._poll_result = poll_result or {"status": "pending", "interval": 5}
        self._start_raises = start_raises
        self._poll_raises = poll_raises
        self.polled_with = []

    async def start(self, client_id, ip):
        if self._start_raises:
            raise self._start_raises
        return {
            "handle": "SECRET-HANDLE-DO-NOT-LEAK",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://iam.example.test/realms/Astral/device",
            "verification_uri_complete": "https://iam.example.test/realms/Astral/device?user_code=WDJB-MJHT",
            "qr_png_base64": "iVBORw0KGgo=",
            "expires_in": 600,
            "interval": 5,
        }

    async def poll(self, handle, ip):
        self.polled_with.append(handle)
        if self._poll_raises:
            raise self._poll_raises
        return self._poll_result


def _install(monkeypatch, fake):
    import orchestrator
    monkeypatch.setattr(orchestrator, "device_login", fake, raising=False)
    import sys
    monkeypatch.setitem(sys.modules, "orchestrator.device_login", fake)


# ---------------------------------------------------------------------------
# Flag-off posture
# ---------------------------------------------------------------------------

def test_kiosk_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("FF_KIOSK_LOGIN", raising=False)
    assert FeatureFlags().is_enabled("kiosk_login") is False


def test_flag_off_leaves_no_kiosk_route(monkeypatch):
    """With the flag off the router is never included â€” the path 404s because
    it does not exist, not because a handler refused it."""
    monkeypatch.delenv("FF_KIOSK_LOGIN", raising=False)
    app = FastAPI()
    if FeatureFlags().is_enabled("kiosk_login"):  # pragma: no cover - guard
        app.include_router(wa.kiosk_router)
    paths = {r.path for r in app.routes}
    assert "/kiosk" not in paths
    assert "/auth/kiosk/start" not in paths
    assert "/auth/kiosk/poll" not in paths


# ---------------------------------------------------------------------------
# The handle must never reach the browser
# ---------------------------------------------------------------------------

def test_start_never_returns_the_device_handle(client, monkeypatch):
    fake = _FakeDeviceLogin()
    _install(monkeypatch, fake)
    res = client.post("/auth/kiosk/start")
    assert res.status_code == 200
    body = res.json()
    assert body["user_code"] == "WDJB-MJHT"
    assert body["qr_png_base64"]
    assert "handle" not in body
    assert "SECRET-HANDLE-DO-NOT-LEAK" not in res.text


def test_start_binds_the_flow_to_this_browser_with_a_strict_cookie(client, monkeypatch):
    _install(monkeypatch, _FakeDeviceLogin())
    res = client.post("/auth/kiosk/start")
    raw = res.headers["set-cookie"]
    assert wa.KIOSK_COOKIE in raw
    assert "HttpOnly" in raw
    assert "samesite=strict" in raw.lower()
    # The cookie carries an opaque signed id, never the handle itself.
    assert "SECRET-HANDLE-DO-NOT-LEAK" not in raw


def test_poll_without_the_cookie_cannot_reach_the_broker(client, monkeypatch):
    """A caller that did not start the flow here gets 'restart', and the broker
    is never polled on their behalf."""
    fake = _FakeDeviceLogin(poll_result={"status": "approved", "tokens": {"access_token": KIOSK_TOKEN}})
    _install(monkeypatch, fake)
    res = client.post("/auth/kiosk/poll")
    assert res.json() == {"status": "restart"}
    assert fake.polled_with == []
    assert wa.COOKIE_NAME not in res.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# Approval mints a cookie session server-side, and leaks no tokens
# ---------------------------------------------------------------------------

def test_approval_sets_the_session_cookie_and_returns_no_tokens(client, monkeypatch):
    fake = _FakeDeviceLogin(poll_result={
        "status": "approved",
        "tokens": {"access_token": KIOSK_TOKEN, "refresh_token": "refresh-abc"},
    })
    _install(monkeypatch, fake)
    monkeypatch.setattr(wa, "_audit", lambda *a, **k: _noop())

    client.post("/auth/kiosk/start")
    res = client.post("/auth/kiosk/poll")

    assert res.json() == {"status": "approved", "next": "/"}
    assert KIOSK_TOKEN not in res.text
    assert "refresh-abc" not in res.text
    raw = res.headers["set-cookie"]
    assert wa.COOKIE_NAME in raw and "HttpOnly" in raw
    # The session really exists and carries the tokens server-side.
    assert any(s.get("sub") == "kiosk-user" for s in wa._SESSIONS.values())


async def _noop():
    return None


def test_denied_no_access_is_reported_without_a_session(client, monkeypatch):
    fake = _FakeDeviceLogin(poll_result={"status": "denied", "reason": "denied_no_access"})
    _install(monkeypatch, fake)
    client.post("/auth/kiosk/start")
    res = client.post("/auth/kiosk/poll")
    assert res.json() == {"status": "denied", "reason": "denied_no_access"}
    assert wa.COOKIE_NAME not in res.headers.get("set-cookie", "")
    assert wa._SESSIONS == {}


def test_pending_relays_the_server_paced_interval(client, monkeypatch):
    _install(monkeypatch, _FakeDeviceLogin(poll_result={"status": "slow_down", "interval": 10}))
    client.post("/auth/kiosk/start")
    assert client.post("/auth/kiosk/poll").json() == {"status": "slow_down", "interval": 10}


def test_consumed_handle_asks_the_page_to_restart(client, monkeypatch):
    fake = _FakeDeviceLogin()
    err = _FakeDeviceLogin.DeviceLoginError("stale", code="invalid_handle", status=400)
    fake._poll_raises = err
    _install(monkeypatch, fake)
    client.post("/auth/kiosk/start")
    assert client.post("/auth/kiosk/poll").json() == {"status": "restart"}


def test_broker_unavailable_surfaces_its_status(client, monkeypatch):
    err = _FakeDeviceLogin.DeviceLoginError("device grant not configured",
                                            code="device_login_unavailable", status=503)
    _install(monkeypatch, _FakeDeviceLogin(start_raises=err))
    res = client.post("/auth/kiosk/start")
    assert res.status_code == 503
    assert res.json()["error"] == "device_login_unavailable"


def test_expired_flow_is_not_redeemable(client, monkeypatch):
    _install(monkeypatch, _FakeDeviceLogin())
    client.post("/auth/kiosk/start")
    for entry in wa._KIOSK_FLOWS.values():
        entry["created_at"] = time.time() - (wa._KIOSK_FLOW_TTL_SECONDS + 1)
    assert client.post("/auth/kiosk/poll").json() == {"status": "restart"}


def test_flow_table_is_bounded(client, monkeypatch):
    _install(monkeypatch, _FakeDeviceLogin())
    for i in range(wa._KIOSK_FLOW_MAX + 25):
        wa._KIOSK_FLOWS[f"seed-{i}"] = {"handle": "h", "created_at": time.time() - 1}
    client.post("/auth/kiosk/start")
    assert len(wa._KIOSK_FLOWS) <= wa._KIOSK_FLOW_MAX + 1


# ---------------------------------------------------------------------------
# The issuing-client fix (without it a kiosk session dies at first refresh)
# ---------------------------------------------------------------------------

def test_kiosk_client_defaults_to_the_watch_client(monkeypatch):
    """Out of the box the kiosk reuses the watch's device-grant client, so no
    new realm configuration is needed to turn the page on."""
    monkeypatch.delenv("KIOSK_DEVICE_CLIENT", raising=False)
    assert wa._kiosk_client_id() == "astral-watch"


def test_kiosk_client_is_overridable(monkeypatch):
    monkeypatch.setenv("KIOSK_DEVICE_CLIENT", "astral-kiosk")
    assert wa._kiosk_client_id() == "astral-kiosk"
    # An empty value falls back rather than sending "" to the broker.
    monkeypatch.setenv("KIOSK_DEVICE_CLIENT", "   ")
    assert wa._kiosk_client_id() == "astral-watch"


def test_session_client_id_reads_azp():
    assert wa._session_client_id({"access_token": KIOSK_TOKEN}) == "astral-kiosk"
    assert wa._session_client_id({"access_token": WEB_TOKEN}) == "astral-frontend"
    assert wa._session_client_id({}) == ""


@pytest.mark.asyncio
async def test_refresh_uses_the_issuing_client_and_withholds_the_web_secret(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://iam.example.test/realms/Astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "web-client-secret")
    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        async def aiter_bytes(self, chunk_size=8192):
            yield json.dumps({"access_token": KIOSK_TOKEN, "refresh_token": "r2"}).encode()

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        @asynccontextmanager
        async def stream(self, method, url, data=None, **kwargs):
            sent.update(data or {})
            yield _Resp()

    monkeypatch.setattr(wa.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(wa, "_get_store", lambda: _refresh_store(KIOSK_TOKEN))

    await wa._refresh_session("sid", {"access_token": KIOSK_TOKEN, "refresh_token": "r1"})

    assert sent["client_id"] == "astral-kiosk"
    assert "client_secret" not in sent, "the confidential secret must not be sent for a public client"


@pytest.mark.asyncio
async def test_refresh_still_sends_the_secret_for_the_web_client(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://iam.example.test/realms/Astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "web-client-secret")
    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        async def aiter_bytes(self, chunk_size=8192):
            yield json.dumps({"access_token": WEB_TOKEN}).encode()

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        @asynccontextmanager
        async def stream(self, method, url, data=None, **kwargs):
            sent.update(data or {})
            yield _Resp()

    monkeypatch.setattr(wa.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(wa, "_get_store", lambda: _refresh_store(WEB_TOKEN))

    await wa._refresh_session("sid", {"access_token": WEB_TOKEN, "refresh_token": "r1"})

    assert sent["client_id"] == "astral-frontend"
    assert sent["client_secret"] == "web-client-secret"
