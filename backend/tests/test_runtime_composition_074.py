"""Application binding tests for Plane-backed LETS composition."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import orchestrator.runtime_composition as runtime_module
from orchestrator.runtime_composition import (
    AstralRuntimeComposition,
    compose_astral_runtime,
)


class _LifecycleManager:
    def __init__(self) -> None:
        self.lifecycle = object()

    def bind_governed_lifecycle(self, lifecycle: object) -> None:
        self.lifecycle = lifecycle


class _Host:
    def __init__(self) -> None:
        self.lifecycle_manager = _LifecycleManager()
        self.dispatch_binding: dict[str, object] | None = None

    def bind_governed_final_dispatch(self, **values: object) -> None:
        self.dispatch_binding = values


class _AttachmentPurges:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.started = False

    def start(self):
        self.events.append("purges.start")
        self.started = True
        return "purges"

    async def stop(self) -> None:
        self.events.append("purges.stop")
        self.started = False

    async def close(self) -> None:
        self.events.append("purges.close")
        self.started = False

    def abort(self) -> None:
        self.events.append("purges.abort")


class _AttachmentMaterializer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close(self) -> None:
        self.events.append("materializer.close")

    def materialize_bytes(self, **_values: object) -> object:
        return object()


class _AttachmentMaterializations:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("materializations.close")


def _composition(
    *, mode: str | None = "enforce"
) -> tuple[AstralRuntimeComposition, list[str]]:
    events: list[str] = []
    repositories = SimpleNamespace(authority=object())
    attachment_purges = _AttachmentPurges(events)
    attachment_materializer = _AttachmentMaterializer(events)
    attachment_materializations = _AttachmentMaterializations(events)
    plane = SimpleNamespace(
        runtime=object(),
        repositories=repositories,
        blobs=object(),
        attachment_purges=attachment_purges,
        attachment_materializer=attachment_materializer,
        attachment_materializations=attachment_materializations,
        close=lambda: events.append("plane.close"),
    )
    config = None if mode is None else SimpleNamespace(mode=mode)
    lets = SimpleNamespace(
        config=config,
        authorization_gateway=None if mode is None else object(),
        lifecycle=None if mode is None else object(),
        byo_lifecycle=None if mode is None else object(),
        client=None,
        start_reconcilers=lambda: (
            ("lifecycle", "effects") if mode not in {None, "off"} else ()
        ),
        stop=_async_event(events, "lets.stop"),
    )
    return AstralRuntimeComposition(plane=plane, lets=lets), events  # type: ignore[arg-type]


def _async_event(events: list[str], name: str):
    async def record() -> None:
        events.append(name)

    return record


def test_active_composition_binds_lifecycle_dispatch_and_recovery() -> None:
    runtime, _events = _composition()
    host = _Host()

    runtime.bind(host)

    assert host.lifecycle_manager.lifecycle is runtime.lets.lifecycle
    assert host.governed_byo_lifecycle is runtime.lets.byo_lifecycle
    assert host.plane_runtime is runtime.plane.runtime
    assert host.plane_repositories is runtime.plane.repositories
    assert host.attachment_purge_coordinator is runtime.plane.attachment_purges
    assert (
        host.attachment_materialization_service
        is runtime.plane.attachment_materializer
    )
    assert host.dispatch_binding == {
        "gateway": runtime.lets.authorization_gateway,
        "plane": runtime.plane.runtime,
        "authority_repository": runtime.plane.repositories.authority,
    }
    assert runtime.start() == ("lifecycle", "effects", "purges")

    # Binding is idempotent after the complete graph has been published.
    runtime.bind(host)
    assert host.dispatch_binding is not None


def test_bind_and_start_fail_before_required_lifecycle_state() -> None:
    runtime, _events = _composition()
    with pytest.raises(RuntimeError, match="not bound"):
        runtime.start()
    with pytest.raises(RuntimeError, match="lifecycle manager"):
        runtime.bind(SimpleNamespace())


def test_off_composition_never_rebinds_final_dispatch() -> None:
    runtime, _events = _composition(mode="off")
    host = _Host()

    runtime.bind(host)

    assert host.dispatch_binding is None
    assert runtime.start() == ("purges",)


def test_invalid_shadow_composition_remains_nonblocking_and_unbound() -> None:
    runtime, _events = _composition(mode=None)
    host = _Host()
    original_lifecycle = host.lifecycle_manager.lifecycle

    runtime.bind(host)

    assert host.lifecycle_manager.lifecycle is original_lifecycle
    assert host.governed_byo_lifecycle is None
    assert host.dispatch_binding is None


@pytest.mark.asyncio
async def test_close_orders_lets_before_plane_and_is_idempotent() -> None:
    runtime, events = _composition()

    await runtime.close()
    await runtime.close()

    assert events == [
        "materializer.close",
        "materializations.close",
        "purges.close",
        "lets.stop",
        "plane.close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "barrier",
    ["materializer", "materializations", "purges", "lets", "plane"],
)
async def test_concurrent_repeatedly_cancelled_close_joins_one_shared_task(
    barrier: str,
) -> None:
    runtime, events = _composition()
    async_entered = asyncio.Event()
    async_release = asyncio.Event()
    sync_entered = threading.Event()
    sync_release = threading.Event()
    close_threads: list[str] = []

    if barrier == "materializer":
        async def _materializer_close() -> None:
            events.append("materializer.close")
            async_entered.set()
            await async_release.wait()

        runtime.plane.attachment_materializer.close = _materializer_close
    elif barrier == "materializations":
        def _materializations_close() -> None:
            close_threads.append(threading.current_thread().name)
            events.append("materializations.close")
            sync_entered.set()
            assert sync_release.wait(timeout=5)

        runtime.plane.attachment_materializations.close = _materializations_close
    elif barrier == "purges":
        async def _purges_close() -> None:
            events.append("purges.close")
            async_entered.set()
            await async_release.wait()

        runtime.plane.attachment_purges.close = _purges_close
    elif barrier == "lets":
        async def _lets_stop() -> None:
            events.append("lets.stop")
            async_entered.set()
            await async_release.wait()

        runtime.lets.stop = _lets_stop
    else:
        def _plane_close() -> None:
            close_threads.append(threading.current_thread().name)
            events.append("plane.close")
            sync_entered.set()
            assert sync_release.wait(timeout=5)

        runtime.plane.close = _plane_close

    first = asyncio.create_task(runtime.close())
    if barrier in {"materializations", "plane"}:
        assert await asyncio.wait_for(
            asyncio.to_thread(sync_entered.wait, 2),
            timeout=3,
        )
    else:
        await asyncio.wait_for(async_entered.wait(), timeout=2)
    shared = runtime._close_task
    assert shared is not None

    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)

    assert runtime._close_task is shared
    assert second.done() is False
    if barrier in {"materializations", "plane"}:
        sync_release.set()
    else:
        async_release.set()

    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    await runtime.close()

    assert runtime._lifecycle_state == "closed"
    assert events.count("materializer.close") == 1
    assert events.count("materializations.close") == 1
    assert events.count("purges.close") == 1
    assert events.count("lets.stop") == 1
    assert events.count("plane.close") == 1
    if barrier in {"materializations", "plane"}:
        assert len(close_threads) == 1
        assert close_threads[0].startswith("astral-runtime-close")


@pytest.mark.asyncio
async def test_failed_final_plane_close_is_retryable_without_reopening_runtime() -> None:
    runtime, events = _composition()
    attempts = 0

    def _plane_close() -> None:
        nonlocal attempts
        attempts += 1
        events.append("plane.close")
        if attempts == 1:
            raise RuntimeError("blob_store_busy")

    runtime.plane.close = _plane_close

    with pytest.raises(RuntimeError, match="blob_store_busy"):
        await runtime.close()

    assert runtime._lifecycle_state == "close_failed"
    assert runtime._close_task is None
    with pytest.raises(RuntimeError, match="closing"):
        runtime.start()

    await runtime.close()

    assert runtime._lifecycle_state == "closed"
    assert attempts == 2
    assert events == [
        "materializer.close",
        "materializations.close",
        "purges.close",
        "lets.stop",
        "plane.close",
        "materializer.close",
        "materializations.close",
        "purges.close",
        "lets.stop",
        "plane.close",
    ]


def test_compose_failure_closes_initialized_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    plane = SimpleNamespace(
        runtime=object(),
        repositories=SimpleNamespace(authority=object()),
        close=lambda: events.append("plane.close"),
    )
    monkeypatch.setattr(
        runtime_module,
        "compose_plane_from_environment",
        lambda *_args, **_kwargs: plane,
    )
    monkeypatch.setattr(
        runtime_module,
        "compose_lets_runtime",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    with pytest.raises(RuntimeError, match="failed"):
        compose_astral_runtime("composition.json", environ={})

    assert events == ["plane.close"]


def test_abort_closes_an_unstarted_partial_graph_synchronously() -> None:
    runtime, events = _composition()
    runtime.lets.client = SimpleNamespace(close=lambda: events.append("client.close"))

    runtime.abort()
    runtime.abort()

    assert events == ["purges.abort", "client.close", "plane.close"]


def test_abort_rejects_started_graph_and_preserves_retry_after_failure() -> None:
    runtime, _events = _composition()
    runtime.plane.attachment_purges.started = True
    with pytest.raises(RuntimeError, match="requires async close"):
        runtime.abort()

    runtime.plane.attachment_purges.started = False
    runtime.plane.close = lambda: (_ for _ in ()).throw(RuntimeError("busy"))
    with pytest.raises(RuntimeError, match="busy"):
        runtime.abort()
    assert runtime._lifecycle_state == "close_failed"
    with pytest.raises(RuntimeError, match="closing"):
        runtime.abort()


@pytest.mark.asyncio
async def test_close_of_closed_graph_is_noop_and_invalid_inflight_state_is_rejected() -> None:
    runtime, _events = _composition()
    runtime.abort()
    await runtime.close()

    other, _other_events = _composition()
    other._lifecycle_state = "closing"
    with pytest.raises(RuntimeError, match="closing"):
        await other.close()
    other._lifecycle_state = "open"
    other.abort()


def test_compose_success_returns_one_application_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane = SimpleNamespace(
        runtime=object(),
        repositories=SimpleNamespace(authority=object()),
    )
    lets = object()
    monkeypatch.setattr(
        runtime_module,
        "compose_plane_from_environment",
        lambda *_args, **_kwargs: plane,
    )
    monkeypatch.setattr(
        runtime_module,
        "compose_lets_runtime",
        lambda **_kwargs: lets,
    )

    runtime = compose_astral_runtime("composition.json", environ={})

    assert runtime.plane is plane
    assert runtime.lets is lets


def test_orchestrator_orders_plane_before_legacy_db_and_recovery_before_agents() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "orchestrator" / "orchestrator.py"
    ).read_text(encoding="utf-8")

    assert source.index("compose_astral_runtime(composition_manifest)") < source.index(
        "self.history = HistoryManager("
    )
    assert source.index("self.lifecycle_manager = AgentLifecycleManager") < source.index(
        "self.runtime_composition.bind(self)"
    )
    assert source.index("self.runtime_composition.start()") < source.index(
        "await local_agents.register_built_ins(self)"
    )
    assert "await runtime_composition.close()" in source


@pytest.mark.asyncio
async def test_bound_process_consumers_are_exactly_unbound_before_plane_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.general import file_tools
    from shared import attachment_materializer, attachment_resolver

    monkeypatch.setattr(file_tools, "_PRODUCTION_DEPENDENCIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_RUNTIME", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_REPOSITORIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_BLOBS", None)
    monkeypatch.setattr(attachment_materializer, "_MATERIALIZATION_SERVICE", None)

    runtime, events = _composition()
    assert file_tools.register_plane_dependencies(
        runtime.plane.runtime,
        runtime.plane.repositories,
        runtime.plane.blobs,
    )
    assert attachment_resolver.register_plane_runtime(
        runtime.plane.runtime,
        runtime.plane.repositories,
        runtime.plane.blobs,
    )
    assert attachment_materializer.register_materialization_service(
        runtime.plane.attachment_materializer
    )

    await runtime.close()

    assert file_tools._PRODUCTION_DEPENDENCIES is None
    assert attachment_resolver._PLANE_RUNTIME is None
    assert attachment_resolver._PLANE_REPOSITORIES is None
    assert attachment_resolver._PLANE_BLOBS is None
    assert attachment_materializer._MATERIALIZATION_SERVICE is None
    assert events[-1] == "plane.close"

    replacement, _replacement_events = _composition()
    assert file_tools.register_plane_dependencies(
        replacement.plane.runtime,
        replacement.plane.repositories,
        replacement.plane.blobs,
    )
    assert attachment_resolver.register_plane_runtime(
        replacement.plane.runtime,
        replacement.plane.repositories,
        replacement.plane.blobs,
    )
    assert attachment_materializer.register_materialization_service(
        replacement.plane.attachment_materializer
    )
    replacement.abort()


@pytest.mark.asyncio
async def test_unbind_mismatch_preserves_foreign_binding_but_closes_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.general import file_tools
    from shared import attachment_materializer, attachment_resolver

    monkeypatch.setattr(file_tools, "_PRODUCTION_DEPENDENCIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_RUNTIME", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_REPOSITORIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_BLOBS", None)
    foreign_service = SimpleNamespace(materialize_bytes=lambda **_values: None)
    monkeypatch.setattr(
        attachment_materializer,
        "_MATERIALIZATION_SERVICE",
        foreign_service,
    )

    runtime, events = _composition()
    file_tools.register_plane_dependencies(
        runtime.plane.runtime,
        runtime.plane.repositories,
        runtime.plane.blobs,
    )
    attachment_resolver.register_plane_runtime(
        runtime.plane.runtime,
        runtime.plane.repositories,
        runtime.plane.blobs,
    )

    with pytest.raises(RuntimeError, match="does not own"):
        await runtime.close()

    assert attachment_materializer._MATERIALIZATION_SERVICE is foreign_service
    assert attachment_resolver._PLANE_RUNTIME is None
    assert file_tools._PRODUCTION_DEPENDENCIES is None
    assert "plane.close" in events


def test_constructor_transaction_rolls_back_after_process_binding_and_recomposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.general import file_tools
    from orchestrator import orchestrator as orchestrator_module
    from orchestrator import offline_grant, web_auth
    from shared import attachment_materializer, attachment_resolver

    monkeypatch.setattr(file_tools, "_PRODUCTION_DEPENDENCIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_RUNTIME", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_REPOSITORIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_BLOBS", None)
    monkeypatch.setattr(attachment_materializer, "_MATERIALIZATION_SERVICE", None)
    monkeypatch.setattr(web_auth, "_STORE", None)
    monkeypatch.setattr(web_auth, "_CREDENTIAL_MANAGER", None)
    monkeypatch.setattr(offline_grant, "_APPLICATION_STORE", None)

    runtime, events = _composition()

    @orchestrator_module._transactional_runtime_construction
    def _fail_after_binding(owner) -> None:
        owner.runtime_composition = runtime
        owner.credential_manager = object()
        owner.web_sessions = object()
        owner.offline_grants = object()
        web_auth.bind_credential_manager(owner.credential_manager)
        web_auth.bind_session_store(owner.web_sessions)
        offline_grant.bind_offline_grant_store(owner.offline_grants)
        attachment_resolver.register_plane_runtime(
            runtime.plane.runtime,
            runtime.plane.repositories,
            runtime.plane.blobs,
        )
        attachment_materializer.register_materialization_service(
            runtime.plane.attachment_materializer
        )
        file_tools.register_plane_dependencies(
            runtime.plane.runtime,
            runtime.plane.repositories,
            runtime.plane.blobs,
        )
        raise RuntimeError("failed immediately after file-tool registration")

    with pytest.raises(RuntimeError, match="immediately after file-tool"):
        _fail_after_binding(SimpleNamespace())

    assert events[-1] == "plane.close"
    assert web_auth._STORE is None
    assert web_auth._CREDENTIAL_MANAGER is None
    assert offline_grant._APPLICATION_STORE is None
    assert file_tools._PRODUCTION_DEPENDENCIES is None
    assert attachment_resolver._PLANE_RUNTIME is None
    assert attachment_materializer._MATERIALIZATION_SERVICE is None

    replacement, _replacement_events = _composition()
    assert file_tools.register_plane_dependencies(
        replacement.plane.runtime,
        replacement.plane.repositories,
        replacement.plane.blobs,
    )
    replacement.abort()


class _StartAsyncTasks:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def drain(self, *, timeout_seconds: float) -> int:
        assert timeout_seconds == 5.0
        self.events.append("async_tasks.drain")
        return 0

    async def stop_retention_sweep(self) -> None:
        self.events.append("async_tasks.stop_retention")


@pytest.mark.asyncio
async def test_startup_recovers_generated_publications_before_background_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator import session_store
    from orchestrator.orchestrator import Orchestrator

    events: list[str] = []
    monkeypatch.setattr(
        session_store,
        "assert_production_posture",
        lambda: events.append("posture"),
    )

    class _Runtime:
        def start(self) -> None:
            events.append("runtime.start")

    class _Publications:
        async def recover_once(self):
            events.append("publications.recover")
            return SimpleNamespace(degraded_publication_ids=())

        def start(self) -> None:
            events.append("publications.start")
            raise RuntimeError("stop after publication startup boundary")

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.runtime_composition = _Runtime()
    orchestrator.generated_agent_publication_service = _Publications()

    with pytest.raises(RuntimeError, match="publication startup boundary"):
        await Orchestrator._run_started_server(orchestrator)

    assert events == [
        "posture",
        "runtime.start",
        "publications.recover",
        "publications.start",
    ]


@pytest.mark.asyncio
async def test_pre_serve_failure_closes_started_graph_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.general import file_tools
    from orchestrator.orchestrator import Orchestrator
    from shared import attachment_materializer, attachment_resolver

    monkeypatch.setattr(file_tools, "_PRODUCTION_DEPENDENCIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_RUNTIME", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_REPOSITORIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_BLOBS", None)
    monkeypatch.setattr(attachment_materializer, "_MATERIALIZATION_SERVICE", None)

    runtime, events = _composition()
    runtime.bind(_Host())
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.runtime_composition = runtime
    orchestrator.generated_agent_publication_service = SimpleNamespace(
        close=_async_event(events, "publications.close")
    )
    orchestrator.async_task_manager = _StartAsyncTasks(events)
    orchestrator.voice_services = SimpleNamespace(
        close=_async_event(events, "voice.close")
    )
    orchestrator._startup_background_tasks = set()
    orchestrator._personal_agent_watchdog_task = None
    orchestrator._remote_job_poll_task = None
    orchestrator._external_agent_discovery_task = None
    orchestrator._scheduler_loop = None

    async def _fail_after_recovery() -> None:
        runtime.start()
        events.append("pre_serve.failure")
        raise RuntimeError("pre-serve construction failed")

    orchestrator._run_started_server = _fail_after_recovery

    with pytest.raises(RuntimeError, match="pre-serve construction"):
        await Orchestrator.start(orchestrator)
    await Orchestrator._close_started_services(orchestrator)

    assert events == [
        "purges.start",
        "pre_serve.failure",
        "publications.close",
        "async_tasks.drain",
        "async_tasks.stop_retention",
        "voice.close",
        "materializer.close",
        "materializations.close",
        "purges.close",
        "lets.stop",
        "plane.close",
    ]


@pytest.mark.asyncio
async def test_voice_cancellation_still_closes_runtime_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.general import file_tools
    from orchestrator.orchestrator import Orchestrator
    from shared import attachment_materializer, attachment_resolver

    monkeypatch.setattr(file_tools, "_PRODUCTION_DEPENDENCIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_RUNTIME", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_REPOSITORIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_BLOBS", None)
    monkeypatch.setattr(attachment_materializer, "_MATERIALIZATION_SERVICE", None)

    runtime, events = _composition()
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.runtime_composition = runtime
    orchestrator.async_task_manager = _StartAsyncTasks(events)
    orchestrator._startup_background_tasks = set()
    orchestrator._personal_agent_watchdog_task = None
    orchestrator._remote_job_poll_task = None
    orchestrator._external_agent_discovery_task = None
    orchestrator._scheduler_loop = None

    async def _cancelled_voice_close() -> None:
        events.append("voice.close")
        raise asyncio.CancelledError()

    orchestrator.voice_services = SimpleNamespace(close=_cancelled_voice_close)

    with pytest.raises(asyncio.CancelledError):
        await Orchestrator._close_started_services(orchestrator)

    assert events.count("voice.close") == 1
    assert events.count("plane.close") == 1
    assert runtime._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_orchestrator_close_retries_shared_runtime_busy_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.general import file_tools
    from orchestrator.orchestrator import Orchestrator
    from shared import attachment_materializer, attachment_resolver

    monkeypatch.setattr(file_tools, "_PRODUCTION_DEPENDENCIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_RUNTIME", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_REPOSITORIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_BLOBS", None)
    monkeypatch.setattr(attachment_materializer, "_MATERIALIZATION_SERVICE", None)

    runtime, events = _composition()
    attempts = 0
    first_entered = threading.Event()
    release_first = threading.Event()

    def _plane_close() -> None:
        nonlocal attempts
        attempts += 1
        events.append("plane.close")
        if attempts == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
            raise RuntimeError("blob_store_busy")

    runtime.plane.close = _plane_close
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.runtime_composition = runtime
    orchestrator.async_task_manager = _StartAsyncTasks(events)
    orchestrator.voice_services = SimpleNamespace(
        close=_async_event(events, "voice.close")
    )
    orchestrator._startup_background_tasks = set()
    orchestrator._personal_agent_watchdog_task = None
    orchestrator._remote_job_poll_task = None
    orchestrator._external_agent_discovery_task = None
    orchestrator._scheduler_loop = None

    first = asyncio.create_task(Orchestrator._close_started_services(orchestrator))
    assert await asyncio.wait_for(
        asyncio.to_thread(first_entered.wait, 2),
        timeout=3,
    )
    shared = orchestrator._started_services_close_task
    second = asyncio.create_task(Orchestrator._close_started_services(orchestrator))
    await asyncio.sleep(0)
    assert orchestrator._started_services_close_task is shared
    release_first.set()

    failures = await asyncio.gather(first, second, return_exceptions=True)
    assert all(
        isinstance(error, RuntimeError) and "blob_store_busy" in str(error)
        for error in failures
    )
    assert orchestrator._started_services_close_task is None
    assert runtime._lifecycle_state == "close_failed"

    await Orchestrator._close_started_services(orchestrator)

    assert attempts == 2
    assert runtime._lifecycle_state == "closed"
    assert orchestrator._started_services_close_task.done()


@pytest.mark.asyncio
async def test_orchestrator_close_keeps_successful_cache_after_waiter_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.general import file_tools
    from orchestrator.orchestrator import Orchestrator
    from shared import attachment_materializer, attachment_resolver

    monkeypatch.setattr(file_tools, "_PRODUCTION_DEPENDENCIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_RUNTIME", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_REPOSITORIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_BLOBS", None)
    monkeypatch.setattr(attachment_materializer, "_MATERIALIZATION_SERVICE", None)

    runtime, events = _composition()
    voice_entered = asyncio.Event()
    release_voice = asyncio.Event()

    async def _held_voice_close() -> None:
        events.append("voice.close")
        voice_entered.set()
        await release_voice.wait()

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.runtime_composition = runtime
    orchestrator.async_task_manager = _StartAsyncTasks(events)
    orchestrator.voice_services = SimpleNamespace(close=_held_voice_close)
    orchestrator._startup_background_tasks = set()
    orchestrator._personal_agent_watchdog_task = None
    orchestrator._remote_job_poll_task = None
    orchestrator._external_agent_discovery_task = None
    orchestrator._scheduler_loop = None

    waiter = asyncio.create_task(Orchestrator._close_started_services(orchestrator))
    await asyncio.wait_for(voice_entered.wait(), timeout=2)
    shared = orchestrator._started_services_close_task
    waiter.cancel()
    await asyncio.sleep(0)
    waiter.cancel()
    release_voice.set()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert orchestrator._started_services_close_task is shared
    assert shared.done() and shared.exception() is None
    await Orchestrator._close_started_services(orchestrator)

    assert events.count("voice.close") == 1
    assert events.count("plane.close") == 1
