"""Tests for orchestrator.py's boot-time warm-up: the JWKS cache loop warms and
refreshes without blocking startup, and the PHI analyzer pre-warms on a daemon thread
honoring FF_PHI_WARM.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.orchestrator import Orchestrator  # noqa: E402

pytestmark = pytest.mark.asyncio


def _bare_orch():
    return Orchestrator.__new__(Orchestrator)


async def test_jwks_warm_skips_under_mock_auth(monkeypatch):
    from shared import jwks_cache
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/x")
    calls = []

    async def _fake_get(url, **kw):
        calls.append(url)
        return {"keys": []}

    monkeypatch.setattr(jwks_cache, "get_jwks", _fake_get)
    await asyncio.wait_for(_bare_orch()._jwks_warm_loop(), timeout=1)
    assert calls == []


async def test_jwks_warm_skips_without_authority(monkeypatch):
    from shared import jwks_cache
    monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
    monkeypatch.delenv("KEYCLOAK_AUTHORITY", raising=False)
    calls = []

    async def _fake_get(url, **kw):
        calls.append(url)
        return {"keys": []}

    monkeypatch.setattr(jwks_cache, "get_jwks", _fake_get)
    await asyncio.wait_for(_bare_orch()._jwks_warm_loop(), timeout=1)
    assert calls == []


async def test_jwks_warm_fetches_then_refreshes(monkeypatch):
    from shared import jwks_cache
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/x")
    monkeypatch.setenv("JWKS_REFRESH_SECONDS", "0.01")
    warm_calls, refresh_calls = [], []

    async def _fake_get(url, **kw):
        warm_calls.append(url)
        return {"keys": []}

    async def _fake_fetch(url):
        refresh_calls.append(url)
        return {"keys": []}

    monkeypatch.setattr(jwks_cache, "get_jwks", _fake_get)
    monkeypatch.setattr(jwks_cache, "_fetch", _fake_fetch)
    task = asyncio.create_task(_bare_orch()._jwks_warm_loop())
    for _ in range(200):
        await asyncio.sleep(0.005)
        if refresh_calls:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    expected = "https://idp.example/realms/x/protocol/openid-connect/certs"
    assert warm_calls == [expected]
    assert refresh_calls and refresh_calls[0] == expected


async def test_jwks_warm_failure_backs_off_without_crashing(monkeypatch):
    from shared import jwks_cache
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/x")
    attempts = []

    async def _fake_get(url, **kw):
        attempts.append(url)
        raise ConnectionError("idp down")

    monkeypatch.setattr(jwks_cache, "get_jwks", _fake_get)
    task = asyncio.create_task(_bare_orch()._jwks_warm_loop())
    await asyncio.sleep(0.05)
    assert not task.done(), "loop must keep retrying, not die"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(attempts) == 1


async def test_phi_warm_does_not_block_startup(monkeypatch):
    from personalization import phi_gate as phi_module
    monkeypatch.delenv("FF_PHI_WARM", raising=False)
    loaded = threading.Event()

    def _slow_gate():
        time.sleep(0.3)
        loaded.set()
        return object()

    monkeypatch.setattr(phi_module, "get_phi_gate", _slow_gate)
    started = time.monotonic()
    _bare_orch()._start_phi_warm()
    assert time.monotonic() - started < 0.2, "startup must not wait on the load"
    assert loaded.wait(timeout=3), "warm thread must eventually build the gate"


async def test_phi_warm_respects_kill_switch(monkeypatch):
    from personalization import phi_gate as phi_module
    monkeypatch.setenv("FF_PHI_WARM", "false")
    called = threading.Event()
    monkeypatch.setattr(phi_module, "get_phi_gate", lambda: called.set())
    _bare_orch()._start_phi_warm()
    time.sleep(0.2)
    assert not called.is_set()
