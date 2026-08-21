"""Transactional process binding coverage for Plane-backed in-process agents."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest


class _MaterializationService:
    def materialize_bytes(self, *_args, **_kwargs):
        raise AssertionError("binding tests must not materialize attachments")


def _plane_graph() -> SimpleNamespace:
    repositories = SimpleNamespace()
    return SimpleNamespace(
        runtime=SimpleNamespace(repositories=repositories),
        repositories=repositories,
        blobs=object(),
        attachment_materializer=_MaterializationService(),
    )


def _clear_shared_bindings(monkeypatch):
    from shared import attachment_materializer, attachment_resolver

    monkeypatch.setattr(attachment_resolver, "_PLANE_RUNTIME", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_REPOSITORIES", None)
    monkeypatch.setattr(attachment_resolver, "_PLANE_BLOBS", None)
    monkeypatch.setattr(attachment_materializer, "_MATERIALIZATION_SERVICE", None)
    return attachment_resolver, attachment_materializer


def _clear_general_bindings(monkeypatch):
    from agents.general import file_tools

    resolver, materializer = _clear_shared_bindings(monkeypatch)
    monkeypatch.setattr(file_tools, "_PRODUCTION_DEPENDENCIES", None)
    monkeypatch.setattr(file_tools, "_TEST_DEPENDENCIES", None)
    return file_tools, resolver, materializer


def _isolate_agent_base(monkeypatch, module) -> None:
    monkeypatch.setattr(module, "MCPServer", lambda: object())
    monkeypatch.setattr(
        module.BaseA2AAgent,
        "__init__",
        lambda _self, *_args, **_kwargs: None,
    )


@pytest.mark.asyncio
async def test_local_agent_second_bind_failure_rolls_back_created_resolver(
    monkeypatch,
) -> None:
    from orchestrator import local_agents

    resolver, materializer = _clear_shared_bindings(monkeypatch)
    graph = _plane_graph()
    conflicting_service = _MaterializationService()
    assert materializer.register_materialization_service(conflicting_service) is True
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda: [])
    orch = SimpleNamespace(
        runtime_composition=SimpleNamespace(plane=graph),
        local_agents={},
    )

    assert await local_agents.register_built_ins(orch) == []
    assert resolver._PLANE_RUNTIME is None
    assert resolver._PLANE_REPOSITORIES is None
    assert resolver._PLANE_BLOBS is None
    assert materializer._MATERIALIZATION_SERVICE is conflicting_service

    materializer.unregister_materialization_service(conflicting_service)
    assert await local_agents.register_built_ins(orch) == []
    assert resolver._PLANE_RUNTIME is graph.runtime
    assert resolver._PLANE_REPOSITORIES is graph.repositories
    assert resolver._PLANE_BLOBS is graph.blobs
    assert materializer._MATERIALIZATION_SERVICE is graph.attachment_materializer


@pytest.mark.asyncio
async def test_local_agent_rollback_does_not_clear_observed_resolver(
    monkeypatch,
) -> None:
    from orchestrator import local_agents

    resolver, materializer = _clear_shared_bindings(monkeypatch)
    graph = _plane_graph()
    conflicting_service = _MaterializationService()
    assert resolver.register_plane_runtime(
        graph.runtime,
        graph.repositories,
        graph.blobs,
    ) is True
    assert materializer.register_materialization_service(conflicting_service) is True
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda: [])
    orch = SimpleNamespace(
        runtime_composition=SimpleNamespace(plane=graph),
        local_agents={},
    )

    assert await local_agents.register_built_ins(orch) == []
    assert resolver._PLANE_RUNTIME is graph.runtime
    assert resolver._PLANE_REPOSITORIES is graph.repositories
    assert resolver._PLANE_BLOBS is graph.blobs
    assert materializer._MATERIALIZATION_SERVICE is conflicting_service


@pytest.mark.asyncio
async def test_local_agent_injects_exact_plane_services(monkeypatch) -> None:
    from orchestrator import local_agents
    from shared.feature_flags import flags
    from shared.protocol import AgentCard

    resolver, materializer = _clear_shared_bindings(monkeypatch)
    graph = _plane_graph()
    observed: list[tuple[object, ...]] = []

    class _PlaneConsumer:
        def __init__(
            self,
            *,
            plane_runtime,
            plane_repositories,
            plane_blobs,
            attachment_materialization_service,
        ):
            observed.append(
                (
                    plane_runtime,
                    plane_repositories,
                    plane_blobs,
                    attachment_materialization_service,
                )
            )
            self.card = AgentCard(
                name="Plane Consumer",
                description="binding test",
                agent_id="plane-consumer-1",
            )

    async def _register(_websocket, _message):
        return None

    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda: ["fake"])
    monkeypatch.setattr(local_agents, "_load_agent_class", lambda _name: _PlaneConsumer)
    monkeypatch.setattr(flags, "is_enabled", lambda _name: False)
    orch = SimpleNamespace(
        runtime_composition=SimpleNamespace(plane=graph),
        local_agents={},
        register_agent=_register,
    )

    assert await local_agents.register_built_ins(orch) == ["plane-consumer-1"]
    assert observed == [
        (
            graph.runtime,
            graph.repositories,
            graph.blobs,
            graph.attachment_materializer,
        )
    ]
    assert resolver._PLANE_RUNTIME is graph.runtime
    assert materializer._MATERIALIZATION_SERVICE is graph.attachment_materializer


def test_general_agent_second_bind_failure_rolls_back_and_recomposes(
    monkeypatch,
) -> None:
    from agents.general import general_agent as module

    file_tools, resolver, _materializer = _clear_general_bindings(monkeypatch)
    _isolate_agent_base(monkeypatch, module)
    graph = _plane_graph()
    other = _plane_graph()
    assert resolver.register_plane_runtime(
        other.runtime,
        other.repositories,
        other.blobs,
    ) is True

    with pytest.raises(RuntimeError, match="already bound"):
        module.GeneralAgent(
            plane_runtime=graph.runtime,
            plane_repositories=graph.repositories,
            plane_blobs=graph.blobs,
        )
    assert file_tools._PRODUCTION_DEPENDENCIES is None
    assert resolver._PLANE_RUNTIME is other.runtime

    resolver.unregister_plane_runtime(other.runtime, other.repositories, other.blobs)
    agent = module.GeneralAgent(
        plane_runtime=graph.runtime,
        plane_repositories=graph.repositories,
        plane_blobs=graph.blobs,
    )
    agent.close_plane_bindings()
    agent.close_plane_bindings()
    agent.close_plane_bindings()
    assert file_tools._PRODUCTION_DEPENDENCIES is None
    assert resolver._PLANE_RUNTIME is None


def test_general_agent_failure_does_not_clear_observed_file_binding(
    monkeypatch,
) -> None:
    from agents.general import general_agent as module

    file_tools, resolver, _materializer = _clear_general_bindings(monkeypatch)
    _isolate_agent_base(monkeypatch, module)
    graph = _plane_graph()
    other = _plane_graph()
    assert file_tools.register_plane_dependencies(
        graph.runtime,
        graph.repositories,
        graph.blobs,
    ) is True
    assert resolver.register_plane_runtime(
        other.runtime,
        other.repositories,
        other.blobs,
    ) is True

    with pytest.raises(RuntimeError, match="already bound"):
        module.GeneralAgent(
            plane_runtime=graph.runtime,
            plane_repositories=graph.repositories,
            plane_blobs=graph.blobs,
        )
    assert file_tools._PRODUCTION_DEPENDENCIES == (
        graph.runtime,
        graph.repositories,
        graph.blobs,
    )
    assert resolver._PLANE_RUNTIME is other.runtime


def test_general_agent_close_preserves_exact_replayed_application_bindings(
    monkeypatch,
) -> None:
    from agents.general import general_agent as module

    file_tools, resolver, _materializer = _clear_general_bindings(monkeypatch)
    _isolate_agent_base(monkeypatch, module)
    graph = _plane_graph()
    assert file_tools.register_plane_dependencies(
        graph.runtime,
        graph.repositories,
        graph.blobs,
    ) is True
    assert resolver.register_plane_runtime(
        graph.runtime,
        graph.repositories,
        graph.blobs,
    ) is True
    agent = module.GeneralAgent(
        plane_runtime=graph.runtime,
        plane_repositories=graph.repositories,
        plane_blobs=graph.blobs,
    )

    agent.close_plane_bindings()
    assert file_tools._PRODUCTION_DEPENDENCIES == (
        graph.runtime,
        graph.repositories,
        graph.blobs,
    )
    assert resolver._PLANE_RUNTIME is graph.runtime


def test_general_agent_post_bind_failure_releases_created_bindings(monkeypatch) -> None:
    from agents.general import general_agent as module

    file_tools, resolver, _materializer = _clear_general_bindings(monkeypatch)
    _isolate_agent_base(monkeypatch, module)
    graph = _plane_graph()

    class _FailingLogger:
        def info(self, *_args, **_kwargs):
            raise RuntimeError("logging failed")

    original_get_logger = module.logging.getLogger
    monkeypatch.setattr(
        module.logging,
        "getLogger",
        lambda name=None: (
            _FailingLogger()
            if name == "GeneralAgent"
            else original_get_logger(name)
        ),
    )
    with pytest.raises(RuntimeError, match="logging failed"):
        module.GeneralAgent(
            plane_runtime=graph.runtime,
            plane_repositories=graph.repositories,
            plane_blobs=graph.blobs,
        )
    assert file_tools._PRODUCTION_DEPENDENCIES is None
    assert resolver._PLANE_RUNTIME is None


def test_general_agent_identity_mismatch_never_unbinds_another_graph(
    monkeypatch,
) -> None:
    from agents.general import general_agent as module

    file_tools, resolver, _materializer = _clear_general_bindings(monkeypatch)
    _isolate_agent_base(monkeypatch, module)
    graph = _plane_graph()
    replacement = _plane_graph()
    agent = module.GeneralAgent(
        plane_runtime=graph.runtime,
        plane_repositories=graph.repositories,
        plane_blobs=graph.blobs,
    )
    resolver.unregister_plane_runtime(graph.runtime, graph.repositories, graph.blobs)
    file_tools.unregister_plane_dependencies(
        graph.runtime,
        graph.repositories,
        graph.blobs,
    )
    assert file_tools.register_plane_dependencies(
        replacement.runtime,
        replacement.repositories,
        replacement.blobs,
    ) is True
    assert resolver.register_plane_runtime(
        replacement.runtime,
        replacement.repositories,
        replacement.blobs,
    ) is True

    with pytest.raises(BaseExceptionGroup, match="binding cleanup failed"):
        agent.close_plane_bindings()
    assert file_tools._PRODUCTION_DEPENDENCIES == (
        replacement.runtime,
        replacement.repositories,
        replacement.blobs,
    )
    assert resolver._PLANE_RUNTIME is replacement.runtime
    assert resolver._PLANE_REPOSITORIES is replacement.repositories
    assert resolver._PLANE_BLOBS is replacement.blobs


def test_general_agent_close_reports_one_mismatch_after_other_cleanup(
    monkeypatch,
) -> None:
    from agents.general import general_agent as module

    file_tools, resolver, _materializer = _clear_general_bindings(monkeypatch)
    _isolate_agent_base(monkeypatch, module)
    graph = _plane_graph()
    replacement = _plane_graph()
    agent = module.GeneralAgent(
        plane_runtime=graph.runtime,
        plane_repositories=graph.repositories,
        plane_blobs=graph.blobs,
    )
    resolver.unregister_plane_runtime(graph.runtime, graph.repositories, graph.blobs)
    assert resolver.register_plane_runtime(
        replacement.runtime,
        replacement.repositories,
        replacement.blobs,
    ) is True

    with pytest.raises(RuntimeError, match="resolver unbind"):
        agent.close_plane_bindings()
    assert resolver._PLANE_RUNTIME is replacement.runtime
    assert file_tools._PRODUCTION_DEPENDENCIES is None


def test_ml_agent_second_bind_failure_rolls_back_and_recomposes(monkeypatch) -> None:
    from agents.ml_services import ml_services_agent as module

    resolver, materializer = _clear_shared_bindings(monkeypatch)
    _isolate_agent_base(monkeypatch, module)
    graph = _plane_graph()
    conflicting_service = _MaterializationService()
    assert materializer.register_materialization_service(conflicting_service) is True

    with pytest.raises(RuntimeError, match="already bound"):
        module.MlServicesAgent(
            plane_runtime=graph.runtime,
            plane_repositories=graph.repositories,
            plane_blobs=graph.blobs,
            attachment_materialization_service=graph.attachment_materializer,
        )
    assert resolver._PLANE_RUNTIME is None
    assert materializer._MATERIALIZATION_SERVICE is conflicting_service

    materializer.unregister_materialization_service(conflicting_service)
    agent = module.MlServicesAgent(
        plane_runtime=graph.runtime,
        plane_repositories=graph.repositories,
        plane_blobs=graph.blobs,
        attachment_materialization_service=graph.attachment_materializer,
    )
    agent.close_plane_bindings()
    agent.close_plane_bindings()
    assert resolver._PLANE_RUNTIME is None
    assert materializer._MATERIALIZATION_SERVICE is None


def test_ml_agent_failure_does_not_clear_observed_resolver(monkeypatch) -> None:
    from agents.ml_services import ml_services_agent as module

    resolver, materializer = _clear_shared_bindings(monkeypatch)
    _isolate_agent_base(monkeypatch, module)
    graph = _plane_graph()
    conflicting_service = _MaterializationService()
    assert resolver.register_plane_runtime(
        graph.runtime,
        graph.repositories,
        graph.blobs,
    ) is True
    assert materializer.register_materialization_service(conflicting_service) is True

    with pytest.raises(RuntimeError, match="already bound"):
        module.MlServicesAgent(
            plane_runtime=graph.runtime,
            plane_repositories=graph.repositories,
            plane_blobs=graph.blobs,
            attachment_materialization_service=graph.attachment_materializer,
        )
    assert resolver._PLANE_RUNTIME is graph.runtime
    assert resolver._PLANE_REPOSITORIES is graph.repositories
    assert resolver._PLANE_BLOBS is graph.blobs
    assert materializer._MATERIALIZATION_SERVICE is conflicting_service


def test_ml_agent_close_preserves_exact_replayed_application_bindings(
    monkeypatch,
) -> None:
    from agents.ml_services import ml_services_agent as module

    resolver, materializer = _clear_shared_bindings(monkeypatch)
    _isolate_agent_base(monkeypatch, module)
    graph = _plane_graph()
    assert resolver.register_plane_runtime(
        graph.runtime,
        graph.repositories,
        graph.blobs,
    ) is True
    assert materializer.register_materialization_service(
        graph.attachment_materializer
    ) is True
    agent = module.MlServicesAgent(
        plane_runtime=graph.runtime,
        plane_repositories=graph.repositories,
        plane_blobs=graph.blobs,
        attachment_materialization_service=graph.attachment_materializer,
    )

    agent.close_plane_bindings()
    assert resolver._PLANE_RUNTIME is graph.runtime
    assert materializer._MATERIALIZATION_SERVICE is graph.attachment_materializer


def test_ml_agent_identity_mismatch_never_unbinds_another_graph(monkeypatch) -> None:
    from agents.ml_services import ml_services_agent as module

    resolver, materializer = _clear_shared_bindings(monkeypatch)
    _isolate_agent_base(monkeypatch, module)
    graph = _plane_graph()
    replacement = _plane_graph()
    agent = module.MlServicesAgent(
        plane_runtime=graph.runtime,
        plane_repositories=graph.repositories,
        plane_blobs=graph.blobs,
        attachment_materialization_service=graph.attachment_materializer,
    )
    materializer.unregister_materialization_service(graph.attachment_materializer)
    resolver.unregister_plane_runtime(graph.runtime, graph.repositories, graph.blobs)
    assert resolver.register_plane_runtime(
        replacement.runtime,
        replacement.repositories,
        replacement.blobs,
    ) is True
    assert materializer.register_materialization_service(
        replacement.attachment_materializer
    ) is True

    with pytest.raises(BaseExceptionGroup, match="binding cleanup failed"):
        agent.close_plane_bindings()
    assert resolver._PLANE_RUNTIME is replacement.runtime
    assert resolver._PLANE_REPOSITORIES is replacement.repositories
    assert resolver._PLANE_BLOBS is replacement.blobs
    assert (
        materializer._MATERIALIZATION_SERVICE
        is replacement.attachment_materializer
    )


def test_ml_agent_close_reports_one_mismatch_after_other_cleanup(monkeypatch) -> None:
    from agents.ml_services import ml_services_agent as module

    resolver, materializer = _clear_shared_bindings(monkeypatch)
    _isolate_agent_base(monkeypatch, module)
    graph = _plane_graph()
    replacement = _plane_graph()
    agent = module.MlServicesAgent(
        plane_runtime=graph.runtime,
        plane_repositories=graph.repositories,
        plane_blobs=graph.blobs,
        attachment_materialization_service=graph.attachment_materializer,
    )
    materializer.unregister_materialization_service(graph.attachment_materializer)
    assert materializer.register_materialization_service(
        replacement.attachment_materializer
    ) is True

    with pytest.raises(RuntimeError, match="materializer unbind"):
        agent.close_plane_bindings()
    assert resolver._PLANE_RUNTIME is None
    assert (
        materializer._MATERIALIZATION_SERVICE
        is replacement.attachment_materializer
    )


@pytest.mark.parametrize(
    ("module_name", "agent_name"),
    (
        ("agents.general.general_agent", "GeneralAgent"),
        ("agents.ml_services.ml_services_agent", "MlServicesAgent"),
    ),
)
def test_agent_refuses_missing_plane_before_base_initialization(
    module_name: str,
    agent_name: str,
) -> None:
    module = importlib.import_module(module_name)
    with pytest.raises(RuntimeError, match="initialized AstralPlane"):
        getattr(module, agent_name)(plane_runtime=None)


@pytest.mark.parametrize(
    "module_name",
    (
        "agents.general.general_agent",
        "agents.ml_services.ml_services_agent",
    ),
)
def test_standalone_composition_uses_repository_manifest(
    monkeypatch,
    module_name: str,
) -> None:
    from orchestrator import plane_composition

    module = importlib.import_module(module_name)
    marker = object()
    observed = []

    def _compose(manifest):
        observed.append(manifest)
        return marker

    monkeypatch.setattr(plane_composition, "compose_plane_from_environment", _compose)
    assert module._compose_standalone_plane() is marker
    assert len(observed) == 1
    assert observed[0].is_absolute()
    assert observed[0].name == "astral-composition.json"
    assert observed[0].parent.name == "config"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "agent_name", "port"),
    (
        ("agents.general.general_agent", "GeneralAgent", 18091),
        ("agents.ml_services.ml_services_agent", "MlServicesAgent", None),
    ),
)
async def test_standalone_cancellation_unbinds_before_plane_close(
    monkeypatch,
    module_name: str,
    agent_name: str,
    port: int | None,
) -> None:
    module = importlib.import_module(module_name)
    events: list[str] = []
    graph = _plane_graph()

    class _Materializer:
        async def close(self):
            events.append("materializer-close")

    class _MaterializationCoordinator:
        def close(self):
            events.append("coordinator-close")

    class _PurgeCoordinator:
        def start(self):
            pytest.fail("standalone agents must not own continuous purge retry")

        async def close(self):
            events.append("purge-close")

    composition = SimpleNamespace(
        runtime=graph.runtime,
        repositories=graph.repositories,
        blobs=graph.blobs,
        attachment_materializer=_Materializer(),
        attachment_materializations=_MaterializationCoordinator(),
        attachment_purges=_PurgeCoordinator(),
        close=lambda: events.append("plane-close"),
    )

    class _CancelledAgent:
        def __init__(self, **_kwargs):
            pass

        async def run(self):
            events.append("run")
            raise asyncio.CancelledError

        def close_plane_bindings(self):
            events.append("unbind")

    monkeypatch.setattr(module, "_compose_standalone_plane", lambda: composition)
    monkeypatch.setattr(module, agent_name, _CancelledAgent)

    with pytest.raises(asyncio.CancelledError):
        await module._run_standalone(port)
    assert events == [
        "run",
        "materializer-close",
        "coordinator-close",
        "purge-close",
        "unbind",
        "plane-close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "agent_name", "port"),
    (
        ("agents.general.general_agent", "GeneralAgent", 18091),
        ("agents.ml_services.ml_services_agent", "MlServicesAgent", None),
    ),
)
async def test_standalone_constructor_failure_still_closes_plane_services(
    monkeypatch,
    module_name: str,
    agent_name: str,
    port: int | None,
) -> None:
    module = importlib.import_module(module_name)
    events: list[str] = []
    graph = _plane_graph()

    class _Materializer:
        async def close(self):
            events.append("materializer-close")

    class _MaterializationCoordinator:
        def close(self):
            events.append("coordinator-close")

    class _PurgeCoordinator:
        async def close(self):
            events.append("purge-close")

    composition = SimpleNamespace(
        runtime=graph.runtime,
        repositories=graph.repositories,
        blobs=graph.blobs,
        attachment_materializer=_Materializer(),
        attachment_materializations=_MaterializationCoordinator(),
        attachment_purges=_PurgeCoordinator(),
        close=lambda: events.append("plane-close"),
    )

    class _ConstructionFailure:
        def __init__(self, **_kwargs):
            raise RuntimeError("agent construction failed")

    monkeypatch.setattr(module, "_compose_standalone_plane", lambda: composition)
    monkeypatch.setattr(module, agent_name, _ConstructionFailure)

    with pytest.raises(RuntimeError, match="agent construction failed"):
        await module._run_standalone(port)
    assert events == [
        "materializer-close",
        "coordinator-close",
        "purge-close",
        "plane-close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "agent_name", "port"),
    (
        ("agents.general.general_agent", "GeneralAgent", 18091),
        ("agents.ml_services.ml_services_agent", "MlServicesAgent", None),
    ),
)
async def test_standalone_shutdown_waits_for_active_materialization(
    monkeypatch,
    module_name: str,
    agent_name: str,
    port: int | None,
) -> None:
    module = importlib.import_module(module_name)
    events: list[str] = []
    run_started = asyncio.Event()
    close_started = asyncio.Event()
    release_materialization = asyncio.Event()
    graph = _plane_graph()

    class _ActiveMaterializer:
        async def close(self):
            events.append("materializer-close-start")
            close_started.set()
            await release_materialization.wait()
            events.append("materializer-close-complete")

    class _MaterializationCoordinator:
        def close(self):
            events.append("coordinator-close")

    class _PurgeCoordinator:
        def start(self):
            pytest.fail("standalone agents must not own continuous purge retry")

        async def close(self):
            events.append("purge-close")

    composition = SimpleNamespace(
        runtime=graph.runtime,
        repositories=graph.repositories,
        blobs=graph.blobs,
        attachment_materializer=_ActiveMaterializer(),
        attachment_materializations=_MaterializationCoordinator(),
        attachment_purges=_PurgeCoordinator(),
        close=lambda: events.append("plane-close"),
    )

    class _RunningAgent:
        def __init__(self, **_kwargs):
            pass

        async def run(self):
            events.append("run")
            run_started.set()
            await asyncio.Future()

        def close_plane_bindings(self):
            events.append("unbind")

    monkeypatch.setattr(module, "_compose_standalone_plane", lambda: composition)
    monkeypatch.setattr(module, agent_name, _RunningAgent)
    task = asyncio.create_task(module._run_standalone(port))
    await run_started.wait()
    task.cancel()
    await close_started.wait()
    assert events == ["run", "materializer-close-start"]

    release_materialization.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == [
        "run",
        "materializer-close-start",
        "materializer-close-complete",
        "coordinator-close",
        "purge-close",
        "unbind",
        "plane-close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name",
    (
        "agents.general.general_agent",
        "agents.ml_services.ml_services_agent",
    ),
)
async def test_standalone_cleanup_surfaces_one_error_after_full_close(
    module_name: str,
) -> None:
    module = importlib.import_module(module_name)
    events: list[str] = []

    class _Materializer:
        async def close(self):
            events.append("materializer-close")

    class _MaterializationCoordinator:
        def close(self):
            events.append("coordinator-close")

    class _PurgeCoordinator:
        async def close(self):
            events.append("purge-close")

    class _FailingAgent:
        def close_plane_bindings(self):
            events.append("unbind")
            raise RuntimeError("unbind failed")

    composition = SimpleNamespace(
        attachment_materializer=_Materializer(),
        attachment_materializations=_MaterializationCoordinator(),
        attachment_purges=_PurgeCoordinator(),
        close=lambda: events.append("plane-close"),
    )

    with pytest.raises(RuntimeError, match="unbind failed"):
        await module._close_standalone_plane(_FailingAgent(), composition)
    assert events == [
        "materializer-close",
        "coordinator-close",
        "purge-close",
        "unbind",
        "plane-close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name",
    (
        "agents.general.general_agent",
        "agents.ml_services.ml_services_agent",
    ),
)
async def test_standalone_cleanup_attempts_every_boundary_after_failures(
    module_name: str,
) -> None:
    module = importlib.import_module(module_name)
    events: list[str] = []

    class _FailingMaterializer:
        async def close(self):
            events.append("materializer-close")
            raise RuntimeError("materializer close failed")

    class _FailingMaterializationCoordinator:
        def close(self):
            events.append("coordinator-close")
            raise RuntimeError("coordinator close failed")

    class _FailingPurgeCoordinator:
        async def close(self):
            events.append("purge-close")
            raise RuntimeError("purge close failed")

    class _FailingAgent:
        def close_plane_bindings(self):
            events.append("unbind")
            raise RuntimeError("unbind failed")

    def _close_plane():
        events.append("plane-close")
        raise RuntimeError("plane close failed")

    composition = SimpleNamespace(
        attachment_materializer=_FailingMaterializer(),
        attachment_materializations=_FailingMaterializationCoordinator(),
        attachment_purges=_FailingPurgeCoordinator(),
        close=_close_plane,
    )

    with pytest.raises(BaseExceptionGroup, match="standalone Plane cleanup failed"):
        await module._close_standalone_plane(_FailingAgent(), composition)
    assert events == [
        "materializer-close",
        "coordinator-close",
        "purge-close",
        "unbind",
        "plane-close",
    ]
