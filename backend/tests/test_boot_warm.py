"""Tests for orchestrator.py's boot-time warm-up: the JWKS cache loop warms and
refreshes without blocking startup, and the PHI analyzer pre-warms on a daemon thread
honoring FF_PHI_WARM.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.orchestrator import Orchestrator  # noqa: E402

pytestmark = pytest.mark.asyncio


def _bare_orch():
    return Orchestrator.__new__(Orchestrator)


class _LoopStopped(BaseException):
    pass


async def test_jwks_warm_skips_under_mock_auth(monkeypatch):
    from shared import jwks_cache
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/x")
    calls = []

    async def _fake_get(url, **kw):
        calls.append(url)
        raise _LoopStopped

    monkeypatch.setattr(jwks_cache, "get_jwks", _fake_get)
    await _bare_orch()._jwks_warm_loop()
    assert calls == []


async def test_jwks_warm_skips_without_authority(monkeypatch):
    from shared import jwks_cache
    monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
    monkeypatch.delenv("KEYCLOAK_AUTHORITY", raising=False)
    calls = []

    async def _fake_get(url, **kw):
        calls.append(url)
        raise _LoopStopped

    monkeypatch.setattr(jwks_cache, "get_jwks", _fake_get)
    await _bare_orch()._jwks_warm_loop()
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
        raise _LoopStopped

    monkeypatch.setattr(jwks_cache, "get_jwks", _fake_get)
    monkeypatch.setattr(jwks_cache, "_fetch", _fake_fetch)
    with pytest.raises(_LoopStopped):
        await _bare_orch()._jwks_warm_loop()
    expected = "https://idp.example/realms/x/protocol/openid-connect/certs"
    assert warm_calls == [expected]
    assert refresh_calls == [expected]


async def test_jwks_warm_failure_backs_off_without_crashing(monkeypatch):
    from shared import jwks_cache
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/x")
    attempts, attempted = [], asyncio.Event()

    async def _fake_get(url, **kw):
        attempts.append(url)
        attempted.set()
        raise ConnectionError("idp down")

    monkeypatch.setattr(jwks_cache, "get_jwks", _fake_get)
    task = asyncio.create_task(_bare_orch()._jwks_warm_loop())
    await attempted.wait()
    assert not task.done(), "loop must keep retrying, not die"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(attempts) == 1


async def test_phi_warm_does_not_block_startup(monkeypatch):
    from personalization import phi_gate as phi_module
    monkeypatch.delenv("FF_PHI_WARM", raising=False)
    caller, release, loads = threading.current_thread(), threading.Event(), []

    def _blocked_gate():
        loads.append(threading.current_thread())
        # A synchronous regression reaches here on the caller and must fail, not deadlock
        if loads[-1] is not caller:
            release.wait()
        return object()

    monkeypatch.setattr(phi_module, "get_phi_gate", _blocked_gate)
    before = set(threading.enumerate())
    try:
        _bare_orch()._start_phi_warm()
        assert caller not in loads, "startup must not wait on the load"
        workers = [thread for thread in threading.enumerate()
                   if thread not in before and thread.name == "phi-warm"]
    finally:
        release.set()
    assert len(workers) == 1 and workers[0].daemon
    workers[0].join()
    assert loads == workers, "warm thread must eventually build the gate"


async def test_phi_warm_respects_kill_switch(monkeypatch):
    from personalization import phi_gate as phi_module
    monkeypatch.setenv("FF_PHI_WARM", "false")
    called = threading.Event()
    monkeypatch.setattr(phi_module, "get_phi_gate", lambda: called.set())
    before = set(threading.enumerate())
    _bare_orch()._start_phi_warm()
    assert not [thread for thread in threading.enumerate()
                if thread not in before and thread.name == "phi-warm"]
    assert not called.is_set()
