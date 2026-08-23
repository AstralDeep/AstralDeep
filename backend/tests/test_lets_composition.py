from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
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


def _minted_config(mode: str, seed_path: Path) -> LetsHostConfig:
    from orchestrator.lets_config import (
        AuthenticatedTrustManifest,
        LetsIdentityConfig,
        SecretFileReference,
    )

    digest_a = "sha256:" + "1" * 64
    digest_b = "sha256:" + "2" * 64
    return LetsHostConfig(
        master_enabled=True,
        mode=mode,  # type: ignore[arg-type]
        environment="production",
        governed_cohorts=INITIAL_GOVERNED_COHORTS,
        governed_agent_allowlist=(),
        warden_origin="https://warden.example",
        service_token_file=None,
        identity=LetsIdentityConfig(
            seed_file=SecretFileReference(path=seed_path, size_bytes=32),
            kid="astral-orch/ed25519-1",
            issuer="https://astral.example/lets-identity",
            audience="astral-lets-warden",
            subject="astraldeep-orchestrator",
            scopes=("lets.lease.issue", "lets.lease.manage", "lets.branch.revoke"),
            token_ttl_seconds=120,
        ),
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        policy_digest=digest_a,
        machine_digest=digest_b,
        default_allocation=(2, 2, 2, 2, 2, 2),
        default_ttl_seconds=60,
        request_timeout_seconds=2.5,
        request_attempts=2,
        trust_manifest=AuthenticatedTrustManifest(
            path=Path("C:/synthetic/trust-manifest.json"),
            sha256="3" * 64,
            tenant_id="tenant-a",
            envelope_id="envelope-a",
            config_epoch=7,
            warden_id="warden-a",
            policy_digest=digest_a,
            machine_digest=digest_b,
            max_lease_ttl_ns=120_000_000_000,
        ),
    )


@pytest.mark.asyncio
async def test_minted_identity_composes_a_per_request_minting_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from orchestrator.lets_client import MintingLETSClient

    seed = tmp_path / "identity.seed"
    seed.write_bytes(bytes(range(32)))
    config = _minted_config("enforce", seed)
    monkeypatch.setattr(
        composition_module,
        "load_lets_config",
        lambda _environ=None: _load("enforce", config=config),
    )
    runtime = compose_lets_runtime(plane=FakePlane(), repository=AuthorityRepository())

    assert runtime.ready is True
    assert runtime.client is not None
    assert isinstance(runtime.client._client, MintingLETSClient)
    assert "authorization" not in runtime.client._client._client.headers
    assert config.service_identity_mode == "minted"
    assert config.redacted()["identity"] == {
        "seed_file": "<configured>",
        "scope_count": 3,
        "token_ttl_seconds": 120,
    }
    await runtime.stop()


def test_shadow_minted_identity_with_unreadable_seed_degrades_without_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _minted_config("shadow", tmp_path / "missing.seed")
    monkeypatch.setattr(
        composition_module,
        "load_lets_config",
        lambda _environ=None: _load("shadow", config=config),
    )
    runtime = compose_lets_runtime(plane=FakePlane(), repository=AuthorityRepository())

    assert runtime.loaded.readiness.application_ready is True
    assert runtime.ready is False
    assert runtime.client is None


def test_enforce_minted_identity_with_unreadable_seed_blocks_with_stable_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _minted_config("enforce", tmp_path / "missing.seed")
    monkeypatch.setattr(
        composition_module,
        "load_lets_config",
        lambda _environ=None: _load("enforce", config=config),
    )
    with pytest.raises(LetsCompositionError, match="^credential_unavailable$") as raised:
        compose_lets_runtime(plane=FakePlane(), repository=AuthorityRepository())
    assert str(tmp_path) not in repr(raised.value)
