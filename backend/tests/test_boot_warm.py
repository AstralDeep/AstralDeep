"""Tests for orchestrator.py's boot-time warm-up: the JWKS cache loop warms and
refreshes without blocking startup, and the PHI analyzer pre-warms on a daemon thread
honoring FF_PHI_WARM.
"""

from __future__ import annotations

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
