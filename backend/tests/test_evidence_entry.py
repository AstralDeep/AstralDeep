"""Tests for evidence service lifetime: shutdown ordering and startup construction in orchestrator/orchestrator.py."""


import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import evidence_context
from orchestrator.context_usage import ContextUsage
from orchestrator.evidence_context import EvidenceContext
from orchestrator.orchestrator import Orchestrator
from shared.feature_flags import flags
from tests.test_evidence_dispatch import (
    response,
)
from tests.test_context_presentation import (
    publication_runtime as publication_runtime,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def entry_controls(monkeypatch):
    monkeypatch.setattr(flags, "is_enabled", lambda name: name == "observation_packing")
    monkeypatch.setenv("FF_LLM_STREAMING", "false")


def lifetime_host(monkeypatch):
    from orchestrator import orchestrator as module

    monkeypatch.setattr(module, "_unbind_orchestrator_process_consumers", lambda _host: None)
    host = object.__new__(Orchestrator)
    host._startup_background_tasks = set()
    host.async_task_manager = SimpleNamespace(drain=AsyncMock(return_value=0), stop_retention_sweep=AsyncMock())
    host.runtime_composition = SimpleNamespace(start=MagicMock(), close=AsyncMock())
    host.human_request_boundary = SimpleNamespace(close=MagicMock())
    host._owned_audit_recorder = SimpleNamespace(close=AsyncMock())
    return host


async def test_shutdown_drains_late_provider_charge_before_closing_audit_and_runtime(monkeypatch):
    host = lifetime_host(monkeypatch)
    order, entered, finish = [], asyncio.Event(), asyncio.Event()

    async def record(event):
        order.append(event.outputs_meta["usage_record"]["phase"])

    async def physical():
        entered.set()
        await finish.wait()
        return response()

    ledger = ContextUsage(record)
    host._evidence_context = EvidenceContext(host, usage=ledger)
    host._evidence_context.start()
    task = asyncio.create_task(ledger.model_call(owner_id="alice", conversation_id="conversation",
        purpose="chat_dispatch", model="synthetic-model", invoke=physical))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def audit_close():
        totals = await ledger.totals("alice", "conversation")
        assert totals["pending"] == 0 and totals["usage"]["total_tokens"] == {"known": 10, "unknown": 0}
        order.append("audit_closed")

    host._owned_audit_recorder.close.side_effect = audit_close
    closing = asyncio.create_task(host._close_started_services())
    await asyncio.sleep(0)
    assert not closing.done() and "audit_closed" not in order
    finish.set()
    await closing
    assert order[-2:] == ["settled", "audit_closed"]
    assert host._evidence_context._sweep_task is None
    host.runtime_composition.close.assert_awaited_once()
    await host._close_started_services()
    host._owned_audit_recorder.close.assert_awaited_once()


@pytest.mark.parametrize("failure", ["ordinary", "cancellation"])
async def test_evidence_shutdown_failure_still_closes_other_authority_services(monkeypatch, failure):
    host = lifetime_host(monkeypatch)
    error = RuntimeError("synthetic shutdown failure") if failure == "ordinary" else asyncio.CancelledError()
    host._evidence_context = SimpleNamespace(close=AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await host._close_started_services()
    host.human_request_boundary.close.assert_called_once()
    host._owned_audit_recorder.close.assert_awaited_once()
    host.runtime_composition.close.assert_awaited_once()
    assert host._started_services_close_task is None


@pytest.mark.parametrize("enabled", [False, True])
async def test_startup_constructs_evidence_service_only_when_enabled_and_starts_once(monkeypatch, enabled):
    from orchestrator import boot_posture
    from orchestrator import orchestrator as module

    monkeypatch.setattr(flags, "is_enabled", lambda name: enabled and name == "observation_packing")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.delenv("A2A_EXTERNAL_AGENTS", raising=False)
    monkeypatch.setattr(boot_posture, "assert_production_posture", lambda: None)
    host = lifetime_host(monkeypatch)
    host.voice_services = None
    host.generated_agent_publication_service = SimpleNamespace(
        recover_once=AsyncMock(return_value=SimpleNamespace(degraded_publication_ids=())),
        start=MagicMock(), close=AsyncMock(),
    )

    async def idle(*_args):
        await asyncio.Event().wait()

    def track(coroutine, *, name):
        coroutine.close()

    host._track_startup_background_task = track
    host.explicit_notes = SimpleNamespace(expiry_loop=idle)
    host._jwks_warm_loop = idle
    host._personal_agent_watchdog_task = None
    host._personal_agent_watchdog_loop = idle
    host._start_phi_warm = MagicMock()
    host.lifecycle_manager = SimpleNamespace(reconcile_orphaned_draft_permissions=lambda: 0,
        reconcile_legacy_directory_ownership=lambda: None)
    host._monitor_agents = idle
    host.task_manager = SimpleNamespace(prune_missing=MagicMock())
    host.async_task_manager.start_retention_sweep = MagicMock()
    serving = AsyncMock()
    monkeypatch.setattr(module.uvicorn, "Server", lambda _config: SimpleNamespace(serve=serving))
    if enabled:
        assert evidence_context.get_context(host) is not None
    else:
        assert evidence_context.get_context(host) is None
    before = getattr(host, "_evidence_context", None)
    await host._run_started_server()
    serving.assert_awaited_once()
    if enabled:
        assert host._evidence_context is before and host._evidence_context._sweep_task is not None
        running = host._evidence_context._sweep_task
        host._evidence_context.start()
        assert host._evidence_context._sweep_task is running
    else:
        assert not hasattr(host, "_evidence_context")
    await host._close_started_services()
    if enabled:
        assert host._evidence_context._sweep_task is None


