"""Concurrency tests for the legacy device-login poll routes (auth.py watch route and
web_auth.py kiosk route): simultaneous polls of one handle must reach the scripted
IdP at most once, lose the race as slow_down, and leave the handle reusable.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import auth, device_login as dl, web_auth  # noqa: E402

ISSUER = "https://idp.example/realms/astral"
DEVICE_EP = ISSUER + "/protocol/openid-connect/auth/device"
TOKEN_EP = ISSUER + "/protocol/openid-connect/token"


def start_body(**over):
    body = {
        "device_code": "dev-code-route",
        "user_code": "WDJB-MJHT",
        "verification_uri": ISSUER + "/device",
        "verification_uri_complete": ISSUER + "/device?user_code=WDJB-MJHT",
        "expires_in": 600,
        "interval": 5,
    }
    body.update(over)
    return body


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("FF_DEVICE_LOGIN", "1")
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEB_SESSION_SECRET", "route-single-flight-secret")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", ISSUER)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KEYCLOAK_DEVICE_CLIENTS", "astral-watch,astral-kiosk")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-frontend,astral-watch,astral-kiosk")
    monkeypatch.setenv("KIOSK_DEVICE_CLIENT", "astral-kiosk")
    dl.reset_state()
    web_auth._KIOSK_FLOWS.clear()
    yield
    dl.reset_state()
    web_auth._KIOSK_FLOWS.clear()


def _scripted_idp(monkeypatch, calls, entered, release):
    async def discovery(url):
        return 200, {"token_endpoint": TOKEN_EP,
                     "device_authorization_endpoint": DEVICE_EP}

    async def post_form(url, data):
        if url == DEVICE_EP:
            return 200, start_body()
        calls.append((url, dict(data)))
        entered.set()
        await release.wait()
        return 400, {"error": "authorization_pending"}

    monkeypatch.setattr(dl, "_default_get_json", discovery)
    monkeypatch.setattr(dl, "_default_post_form", post_form)


@pytest.mark.asyncio
async def test_watch_route_concurrent_polls_reach_the_idp_once(monkeypatch):
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()
    _scripted_idp(monkeypatch, calls, entered, release)
    app = FastAPI()
    app.include_router(auth.auth_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://watch.invalid") as client:
        started = await client.post("/api/auth/device/start",
                                    json={"client": "astral-watch"})
        assert started.status_code == 200
        handle = started.json()["handle"]
        dl._POLL_STATE[dl._handle_digest(handle)]["next_ok"] = 0.0
        first = asyncio.create_task(
            client.post("/api/auth/device/poll", json={"handle": handle}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = await client.post("/api/auth/device/poll",
                                       json={"handle": handle})
            assert second.status_code == 200
            assert second.json() == {"status": "slow_down", "interval": 5}
        finally:
            release.set()
        first_response = await first
        assert first_response.status_code == 200
        assert first_response.json() == {"status": "pending", "interval": 5}
        assert len(calls) == 1
        dl._POLL_STATE[dl._handle_digest(handle)]["next_ok"] = 0.0
        third = await client.post("/api/auth/device/poll", json={"handle": handle})
        assert third.status_code == 200
        assert third.json() == {"status": "pending", "interval": 5}
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_kiosk_route_concurrent_polls_reach_the_idp_once(monkeypatch):
    monkeypatch.setattr(web_auth, "_get_store", lambda: None)
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()
    _scripted_idp(monkeypatch, calls, entered, release)
    app = FastAPI()
    app.include_router(web_auth.kiosk_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://kiosk.invalid") as client:
        started = await client.post("/auth/kiosk/start")
        assert started.status_code == 200
        handle = web_auth._KIOSK_FLOWS[
            web_auth._unsign(started.cookies[web_auth.KIOSK_COOKIE])]["handle"]
        dl._POLL_STATE[dl._handle_digest(handle)]["next_ok"] = 0.0
        first = asyncio.create_task(client.post("/auth/kiosk/poll"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = await client.post("/auth/kiosk/poll")
            assert second.status_code == 200
            assert second.json() == {"status": "slow_down", "interval": 5}
        finally:
            release.set()
        first_response = await first
        assert first_response.status_code == 200
        assert first_response.json() == {"status": "pending", "interval": 5}
        assert len(calls) == 1
        dl._POLL_STATE[dl._handle_digest(handle)]["next_ok"] = 0.0
        third = await client.post("/auth/kiosk/poll")
        assert third.status_code == 200
        assert third.json() == {"status": "pending", "interval": 5}
        assert len(calls) == 2
