"""Tests for persistent_agents/runtime.py: one supervisor runs real discovery and stops,
a second initialization preserves the existing pair, failed initialization closes
only its own store, and startup respects the feature flag.
"""

import asyncio
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from orchestrator.session_store import WebSessionStore
from orchestrator.work_admission import WorkAdmissionCoordinator
from orchestrator.orchestrator import Orchestrator
from persistent_agents import runtime as composition
from persistent_agents.runner import AssignmentRunner
from persistent_agents.store import AssignmentStore
from persistent_agents.tests.test_engine_postgres import plane as plane


@pytest.fixture
def host(plane, monkeypatch):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr("personalization.phi_gate.get_phi_gate", lambda: object())
    return SimpleNamespace(
        runtime_composition=SimpleNamespace(plane=SimpleNamespace(
            runtime=plane, repositories=plane.repositories)),
        web_sessions=WebSessionStore(plane_runtime=plane),
        work_admission=WorkAdmissionCoordinator.from_plane(plane_runtime=plane),
        persistent_assignments=None,
        persistent_assignment_runner=None,
    )


@pytest.mark.asyncio
async def test_one_existing_supervisor_runs_real_discovery_and_stops(host, monkeypatch):
    ticked = asyncio.Event()
    original = AssignmentRunner.tick

    async def tick(runner):
        await original(runner)
        ticked.set()

    monkeypatch.setattr(AssignmentRunner, "tick", tick)
    runner = composition.start_assignment_runtime(host)
    try:
        assert runner is host.persistent_assignment_runner
        service = host.persistent_assignments
        assert runner.fixed_research_ready(service=service, sessions=host.web_sessions)
        assert service.approval_executor.runner is runner
        await asyncio.wait_for(ticked.wait(), 5)
        assert runner._loop is not None and not runner._loop.done()
        assert runner._active == {}
    finally:
        await runner.stop()
        runner.store.close()
    assert runner._loop.done()
    assert not runner.fixed_research_ready(service=service, sessions=host.web_sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["persistent_assignments", "persistent_assignment_runner"])
async def test_second_initialization_preserves_existing_owned_pair(host, field):
    sentinel = object()
    setattr(host, field, sentinel)
    with pytest.raises(RuntimeError, match="already initialized"):
        composition.start_assignment_runtime(host)
    assert getattr(host, field) is sentinel
    other = "persistent_assignments" if field == "persistent_assignment_runner" else "persistent_assignment_runner"
    assert getattr(host, other) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_sessions", "wrong_runtime", "bridge", "start", "cancel"])
async def test_failed_initialization_closes_only_its_store_and_exposes_no_pair(host, monkeypatch, failure):
    closed = []
    original = AssignmentStore.close

    def close(store):
        closed.append(store)
        original(store)

    monkeypatch.setattr(AssignmentStore, "close", close)
    error = asyncio.CancelledError if failure == "cancel" else RuntimeError

    def refuse(*args, **kwargs):
        raise error("synthetic initialization refusal")

    if failure == "missing_sessions":
        del host.web_sessions
        error = AttributeError
    elif failure == "wrong_runtime":
        host.web_sessions = WebSessionStore(plane_runtime=SimpleNamespace(
            repositories=host.runtime_composition.plane.repositories))
    elif failure == "bridge":
        monkeypatch.setattr(composition, "AssignmentApprovalBridge", refuse)
    else:
        monkeypatch.setattr(AssignmentRunner, "start", refuse)
    with pytest.raises(error):
        composition.start_assignment_runtime(host)
    assert len(closed) == 1
    assert host.persistent_assignments is None
    assert host.persistent_assignment_runner is None
    with host.runtime_composition.plane.runtime.transaction() as tx:
        assert tx.fetch_one("SELECT 1 AS value")["value"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_actual_server_startup_respects_flag_and_installs_the_qualified_pair(host, monkeypatch, enabled):
    from shared.feature_flags import flags

    class BeforeHTTP(Exception):
        pass

    events = []
    monkeypatch.setattr(flags, "is_enabled", lambda name: name == "persistent_agents" and enabled)
    monkeypatch.setenv("ASTRAL_ENV", "development")
    host.runtime_composition.start = lambda: events.append("runtime")

    async def recover():
        events.append("recovery")
        return SimpleNamespace(degraded_publication_ids=())

    host.generated_agent_publication_service = SimpleNamespace(
        recover_once=recover, start=lambda: events.append("publication-loop"))

    async def idle(*args, **kwargs):
        await asyncio.Event().wait()

    def track(coroutine, *, name):
        coroutine.close()
        events.append(name)
        if name == "generated-agent-relaunch":
            raise BeforeHTTP

    host._track_startup_background_task = track
    host._jwks_warm_loop = idle
    host._personal_agent_watchdog_loop = idle
    host._personal_agent_watchdog_task = None
    host._monitor_agents = idle
    host._start_phi_warm = lambda: None
    host.lifecycle_manager = SimpleNamespace(
        reconcile_orphaned_draft_permissions=lambda: 0,
        reconcile_legacy_directory_ownership=lambda: None)
    try:
        with pytest.raises(BeforeHTTP):
            await Orchestrator._run_started_server(host)
        assert events[:3] == ["runtime", "recovery", "publication-loop"]
        assert events[-1] == "generated-agent-relaunch"
        if enabled:
            runner = host.persistent_assignment_runner
            assert runner.fixed_research_ready(service=host.persistent_assignments,
                                                sessions=host.web_sessions)
            assert isinstance(runner._loop, asyncio.Task) and not runner._loop.done()
        else:
            assert host.persistent_assignments is None and host.persistent_assignment_runner is None
    finally:
        runner = host.persistent_assignment_runner
        if runner is not None:
            await runner.stop()
            runner.store.close()
        watchdog = host._personal_agent_watchdog_task
        if watchdog is not None:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
