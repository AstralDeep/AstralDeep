"""Actual startup schedules private-note expiry and joins it before Plane closes."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.orchestrator import Orchestrator
from personalization.explicit_note_service import ExplicitNoteService
from tests.test_explicit_note_service_postgres_088 import (
    notes as notes, api as api, fixture as fixture, plane as plane,
    research_service as research_service, service as service,
    signing_key as signing_key, source_service as source_service, apply, rows,
)
from tests.test_runtime_composition_074 import _StartAsyncTasks

runtime = plane


@pytest.mark.asyncio
async def test_actual_startup_owns_expiry_after_recovery_and_before_plane_shutdown(notes, monkeypatch):
    from orchestrator import session_store
    from shared.feature_flags import flags

    await apply(notes, expires_at=time.time_ns() // 1_000_000 + 350)
    await asyncio.sleep(.4)
    observed, history = asyncio.Event(), []
    original = ExplicitNoteService.expire_batch

    async def batch(self, *, limit):
        result = await original(self, limit=limit)
        observed.set()
        return result

    monkeypatch.setattr(ExplicitNoteService, "expire_batch", batch)
    monkeypatch.setattr(session_store, "assert_production_posture", lambda: history.append("posture"))
    host = Orchestrator.__new__(Orchestrator)
    host.async_task_manager = _StartAsyncTasks(history)
    host.human_request_boundary = notes.boundary
    host.explicit_notes = notes.service

    async def recover():
        history.append("recover")
        return SimpleNamespace(degraded_publication_ids=())

    async def close_plane():
        assert worker.done() and notes.boundary.closed
        history.append("plane.closed")

    host.runtime_composition = SimpleNamespace(
        start=lambda: history.append("runtime.start"), close=close_plane)
    host.generated_agent_publication_service = SimpleNamespace(
        recover_once=recover, start=lambda: history.append("publication.start"), close=AsyncMock())

    class ReachedFleetStartup(BaseException):
        """Stop before the unrelated agent fleet, network and provider startup."""

    def stop_at_fleet(_):
        raise ReachedFleetStartup

    with monkeypatch.context() as boot:
        boot.setattr(flags, "is_enabled", stop_at_fleet)
        with pytest.raises(ReachedFleetStartup):
            await host._run_started_server()
    assert history == ["posture", "runtime.start", "recover", "publication.start"]
    worker, = host._startup_background_tasks
    assert worker.get_name() == "explicit-note-expiry"
    try:
        await asyncio.wait_for(observed.wait(), 3)
        assert rows(notes)[0]["ciphertext"] is None
    finally:
        await host._close_started_services()
    assert worker.cancelled() and history[-1] == "plane.closed"
    assert not host._startup_background_tasks
