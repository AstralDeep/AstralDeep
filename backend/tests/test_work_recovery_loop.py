"""Tests for orchestrator.py's executor-failure recovery loop: one pass reports and
logs reschedules/refusals, tolerates pass failures, and stays quiet when idle.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.work_recovery import RecoveredOperation, RecoveryDisposition  # noqa: E402
from orchestrator.work_recovery import RecoveryReport  # noqa: E402


class _LoopStopped(BaseException):
    pass


def _bare_orch():
    from orchestrator.orchestrator import Orchestrator

    return Orchestrator.__new__(Orchestrator)


def _entry(disposition: RecoveryDisposition) -> RecoveredOperation:
    return RecoveredOperation(
        operation_id=uuid.uuid4(),
        disposition=disposition,
        recovery_operation_id=(
            uuid.uuid4() if disposition is RecoveryDisposition.RESCHEDULED else None
        ),
        refusal_code=(
            "capacity_exceeded"
            if disposition is RecoveryDisposition.RESCHEDULE_REFUSED
            else None
        ),
        retryable_refusal=disposition is RecoveryDisposition.RESCHEDULE_REFUSED,
        retry_after_ms=1000,
    )


def _service(calls: list, reports: list[RecoveryReport]):
    def recover():
        calls.append(1)
        if len(calls) > len(reports):
            raise _LoopStopped
        return reports[len(calls) - 1]

    return SimpleNamespace(recover_expired_executors=recover)


async def test_recovery_loop_reports_reschedules_and_refusals(
    monkeypatch, caplog
):
    from orchestrator import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "recovery_interval_seconds", lambda: 0)
    report = RecoveryReport(
        expired=2,
        recovered=(
            _entry(RecoveryDisposition.RESCHEDULED),
            _entry(RecoveryDisposition.RESCHEDULE_REFUSED),
        ),
    )
    calls: list = []
    orch = _bare_orch()
    orch.work_recovery = _service(calls, [report])

    with caplog.at_level("INFO", logger="Orchestrator"):
        with pytest.raises(_LoopStopped):
            await asyncio.wait_for(orch._work_recovery_loop(), timeout=10)

    assert len(calls) == 2
    summaries = [
        record for record in caplog.records
        if record.getMessage().startswith("work_recovery_pass")
    ]
    assert len(summaries) == 1
    assert "rescheduled=1" in summaries[0].getMessage()
    refusals = [
        record for record in caplog.records
        if record.getMessage().startswith("work_recovery_reschedule_refused")
    ]
    assert len(refusals) == 1


async def test_recovery_loop_survives_pass_failures(monkeypatch, caplog):
    from orchestrator import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "recovery_interval_seconds", lambda: 0)

    class _FailingService:
        def __init__(self) -> None:
            self.calls = 0

        def recover_expired_executors(self) -> RecoveryReport:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("plane unavailable")
            raise _LoopStopped

    orch = _bare_orch()
    service = _FailingService()
    orch.work_recovery = service

    with caplog.at_level("WARNING", logger="Orchestrator"):
        with pytest.raises(_LoopStopped):
            await asyncio.wait_for(orch._work_recovery_loop(), timeout=10)

    assert service.calls == 2
    assert any(
        record.getMessage() == "work_recovery_pass_failed"
        for record in caplog.records
    )


async def test_recovery_loop_is_quiet_when_idle(monkeypatch, caplog):
    from orchestrator import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "recovery_interval_seconds", lambda: 0)
    calls: list = []
    orch = _bare_orch()
    orch.work_recovery = _service(calls, [RecoveryReport(expired=0, recovered=())])

    with caplog.at_level("INFO", logger="Orchestrator"):
        with pytest.raises(_LoopStopped):
            await asyncio.wait_for(orch._work_recovery_loop(), timeout=10)

    assert len(calls) == 2
    assert not [
        record for record in caplog.records
        if record.getMessage().startswith("work_recovery_pass")
    ]
