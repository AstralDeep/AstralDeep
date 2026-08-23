from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Iterator

import pytest
from astralplane import PlaneRuntime
from astralplane.authority import AuthorityPopulation, AuthorityRepository

import orchestrator.lets_composition as composition_module
from orchestrator.lets_composition import LetsCompositionError, compose_lets_runtime
from orchestrator.lets_config import (
    INITIAL_GOVERNED_COHORTS,
    LetsConfigLoad,
    LetsHostConfig,
    LetsReadiness,
)


class Transaction:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def fetch_all(self, statement: str, parameters: object = ()):
        self.calls.append((statement, parameters))
        cutoff = datetime(2026, 8, 14, 11, 59, tzinfo=UTC)
        return (
            {"owner_id": "owner-a", "recovery_at": cutoff},
            {"owner_id": "owner-b", "recovery_at": cutoff + timedelta(seconds=1)},
        )


class FakePlane(PlaneRuntime):
    def __init__(self) -> None:
        self.transactions: list[Transaction] = []

    @contextmanager
    def transaction(self, **_options: object) -> Iterator[Transaction]:
        transaction = Transaction()
        self.transactions.append(transaction)
        yield transaction


class FakeClient:
    def __init__(self) -> None:
        self.closed = False
        self.probes = 0

    def probe(self) -> None:
        self.probes += 1

    def close(self) -> None:
        self.closed = True


def _load(mode: str, *, config: LetsHostConfig | None = None) -> LetsConfigLoad:
    status = "disabled" if mode == "off" else "configured"
    return LetsConfigLoad(
        config=config,
        readiness=LetsReadiness(
            mode=mode,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            reason=f"lets_{mode}",
            application_ready=True,
            lets_configured=mode != "off",
            governed_effects_permitted=True,
            diagnostic_only=mode == "shadow",
        ),
    )


def _config(mode: str) -> LetsHostConfig:
    return LetsHostConfig(
        master_enabled=mode != "off",
        mode=mode,  # type: ignore[arg-type]
        environment="test",
        governed_cohorts=INITIAL_GOVERNED_COHORTS,
        governed_agent_allowlist=(),
    )


def test_off_composition_builds_no_client_and_discovers_no_recovery_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config("off")
    monkeypatch.setattr(
        composition_module,
        "load_lets_config",
        lambda _environ=None: _load("off", config=config),
    )
    monkeypatch.setattr(
        composition_module,
        "create_lets_warden_client",
        lambda _config: (_ for _ in ()).throw(
            AssertionError("off mode must not create a LETS client")
        ),
    )
    plane = FakePlane()
    runtime = compose_lets_runtime(
        plane=plane,
        repository=AuthorityRepository(),
    )

    assert runtime.ready is True
    assert runtime.client is None
    assert runtime.lifecycle is not None
    assert runtime.recovery_owner_ids() == ()
    assert runtime.start_reconcilers() == ()
    assert plane.transactions == []
    assert runtime.lifecycle.latest_binding(
        owner_id="owner-a",
        agent_id="agent-a",
        population=AuthorityPopulation.SERVER_DYNAMIC,
    ) is None


def test_invalid_enforce_configuration_blocks_composition() -> None:
    with pytest.raises(LetsCompositionError, match="missing_warden_url"):
        compose_lets_runtime(
            plane=FakePlane(),
            repository=AuthorityRepository(),
            environ={
                "ASTRAL_ENV": "test",
                "FF_LETS_EXTERNAL_WARDEN": "true",
                "LETS_MODE": "enforce",
            },
        )


@pytest.mark.asyncio
async def test_active_composition_wires_plane_gateway_lifecycle_and_owner_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config("enforce")
    client = FakeClient()
    monkeypatch.setattr(
        composition_module,
        "load_lets_config",
        lambda _environ=None: _load("enforce", config=config),
    )
    monkeypatch.setattr(
        composition_module,
        "create_lets_warden_client",
        lambda _config: client,
    )
    plane = FakePlane()
    repository = AuthorityRepository()
    runtime = compose_lets_runtime(plane=plane, repository=repository)

    assert runtime.ready is True
    assert runtime.authorization_gateway is not None
    assert runtime.lifecycle_service is not None
    assert runtime.lifecycle is not None
    assert runtime.byo_lifecycle is not None
    assert runtime.lifecycle_reconciler is not None
    assert runtime.effect_reconciler is not None
    assert runtime.recovery_owner_ids(
        now=datetime(2026, 8, 14, 12, tzinfo=UTC),
        effect_stale_after=timedelta(minutes=1),
    ) == ("owner-a", "owner-b")
    statement, parameters = plane.transactions[0].calls[0]
    assert "UNION ALL" in statement
    assert parameters == (
        datetime(2026, 8, 14, 12, tzinfo=UTC),
        datetime(2026, 8, 14, 11, 59, tzinfo=UTC),
        200,
    )

    assert client.probes == 1  # first live reachability probe at composition
    assert runtime.reachability is not None
    await runtime.stop()
    assert client.closed is True


def test_shadow_client_failure_is_nonblocking_without_fabricated_ready_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config("shadow")
    monkeypatch.setattr(
        composition_module,
        "load_lets_config",
        lambda _environ=None: _load("shadow", config=config),
    )
    monkeypatch.setattr(
        composition_module,
        "create_lets_warden_client",
        lambda _config: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    runtime = compose_lets_runtime(
        plane=FakePlane(),
        repository=AuthorityRepository(),
    )

    assert runtime.loaded.readiness.application_ready is True
    assert runtime.ready is False
    assert runtime.client is None
    assert runtime.authorization_gateway is None
    assert runtime.lifecycle is None


def test_invalid_probe_interval_refuses_active_composition_but_not_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environ = {"LETS_HEALTH_PROBE_INTERVAL_SECONDS": "0"}
    enforce = _config("enforce")
    monkeypatch.setattr(
        composition_module,
        "load_lets_config",
        lambda _environ=None: _load("enforce", config=enforce),
    )
    monkeypatch.setattr(
        composition_module,
        "create_lets_warden_client",
        lambda _config: FakeClient(),
    )
    with pytest.raises(LetsCompositionError, match="^invalid_health_probe_interval$"):
        compose_lets_runtime(
            plane=FakePlane(), repository=AuthorityRepository(), environ=environ
        )

    off = _config("off")
    monkeypatch.setattr(
        composition_module,
        "load_lets_config",
        lambda _environ=None: _load("off", config=off),
    )
    runtime = compose_lets_runtime(
        plane=FakePlane(), repository=AuthorityRepository(), environ=environ
    )
    assert runtime.reachability is None


def test_client_without_a_probe_seam_is_refused_in_enforce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoProbe:
        def close(self) -> None:
            pass

    enforce = _config("enforce")
    monkeypatch.setattr(
        composition_module,
        "load_lets_config",
        lambda _environ=None: _load("enforce", config=enforce),
    )
    monkeypatch.setattr(
        composition_module,
        "create_lets_warden_client",
        lambda _config: NoProbe(),
    )
    with pytest.raises(LetsCompositionError, match="^client_configuration$"):
        compose_lets_runtime(plane=FakePlane(), repository=AuthorityRepository())
