"""LETS posture on ``/readyz`` and the admin-only ``GET /lets/health``.

Pins: flag-off keeps the pre-existing ``/readyz`` keys byte-identical and adds
exactly ``{"mode": "off", "status": "disabled"}``; enforce + blocked is the only
LETS-driven 503; shadow degradation stays 200 but says so; the admin route is
role-gated and never emits a path or secret.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from astralplane import PlaneRuntime
from astralplane.authority import AuthorityRepository
from fastapi import FastAPI
from fastapi.testclient import TestClient

import orchestrator.lets_composition as composition_module
from orchestrator.auth import get_current_user_payload
from orchestrator.lets_composition import compose_lets_runtime
from orchestrator.lets_config import (
    INITIAL_GOVERNED_COHORTS,
    LetsConfigLoad,
    LetsHostConfig,
    LetsReadiness,
    SecretFileReference,
)
from orchestrator.lets_health import (
    health_report,
    observe_lets_runtime,
    project_runtime_health,
    readiness_entry,
)
from orchestrator.lets_health_api import lets_router, readyz_body

LEGACY_READYZ_KEYS = ("status", "db", "generated_agent_publication", "agents")
SECRET_PATH = "/etc/astral/lets/service-token-must-not-escape"


class FakePlane(PlaneRuntime):
    def __init__(self) -> None:  # no pool: composition never touches Plane here
        pass

    @contextmanager
    def transaction(self, **_options: object) -> Iterator[object]:
        yield object()


class FakeClient:
    def close(self) -> None:  # pragma: no cover - shutdown only
        pass


def _load(mode: str, *, config: LetsHostConfig | None, configured: bool = True) -> LetsConfigLoad:
    if mode == "off":
        status = "disabled"
    elif configured:
        status = "configured"
    else:
        status = "blocked" if mode == "enforce" else "degraded"
    blocked = status == "blocked"
    return LetsConfigLoad(
        config=config,
        readiness=LetsReadiness(
            mode=mode,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            reason=f"lets_{mode}_{status}",
            application_ready=not blocked,
            lets_configured=configured and mode != "off",
            governed_effects_permitted=not blocked,
            diagnostic_only=mode == "shadow",
        ),
    )


def _config(mode: str) -> LetsHostConfig:
    return LetsHostConfig(
        master_enabled=mode != "off",
        mode=mode,  # type: ignore[arg-type]
        environment="test",
        governed_cohorts=INITIAL_GOVERNED_COHORTS,
        governed_agent_allowlist=("agent-a",),
        warden_origin="https://warden.internal:8443" if mode != "off" else None,
        service_token_file=(
            SecretFileReference(Path(SECRET_PATH), 64) if mode != "off" else None
        ),
    )


def _compose(monkeypatch: pytest.MonkeyPatch, mode: str, *, reachable: bool):
    config = _config(mode)
    monkeypatch.setattr(
        composition_module,
        "load_lets_config",
        lambda _environ=None: _load(mode, config=config),
    )
    if reachable:
        monkeypatch.setattr(
            composition_module,
            "create_lets_warden_client",
            lambda _config: FakeClient(),
        )
    else:
        monkeypatch.setattr(
            composition_module,
            "create_lets_warden_client",
            lambda _config: (_ for _ in ()).throw(RuntimeError(SECRET_PATH)),
        )
    return compose_lets_runtime(plane=FakePlane(), repository=AuthorityRepository())


def _orch(runtime: object | None, *, bind: bool = True) -> SimpleNamespace:
    orch = SimpleNamespace(agent_cards={"a": object(), "b": object()})
    if bind:
        orch.lets_runtime = runtime
    return orch


# ── /readyz ────────────────────────────────────────────────────────────────


def test_off_readyz_keeps_legacy_shape_and_adds_only_the_disabled_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _compose(monkeypatch, "off", reachable=True)
    body, code = readyz_body(_orch(runtime))

    assert code == 200
    assert tuple(body)[: len(LEGACY_READYZ_KEYS)] == LEGACY_READYZ_KEYS
    assert tuple(body)[len(LEGACY_READYZ_KEYS):] == ("lets",)
    legacy = {key: body[key] for key in LEGACY_READYZ_KEYS}
    assert legacy == {
        "status": "ok",
        "db": "ok",
        "generated_agent_publication": "ok",
        "agents": 2,
    }
    assert body["lets"] == {"mode": "off", "status": "disabled"}
    assert observe_lets_runtime(runtime) is None


def test_off_readyz_without_bound_composition_reads_env_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("FF_LETS_EXTERNAL_WARDEN", "LETS_MODE"):
        monkeypatch.delenv(name, raising=False)
    body, code = readyz_body(_orch(None, bind=False))
    assert code == 200
    assert body["lets"] == {"mode": "off", "status": "disabled"}


def test_enforce_reachable_at_boot_is_ready_and_governed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _compose(monkeypatch, "enforce", reachable=True)
    observation = observe_lets_runtime(runtime)
    assert observation is not None
    assert observation.status == "healthy"
    assert observation.observed_at_ns == runtime.composed_at_ns > 0

    body, code = readyz_body(_orch(runtime))
    assert code == 200
    assert body["status"] == "ok"
    assert body["lets"] == {
        "mode": "enforce",
        "status": "healthy",
        "reason": "lets_healthy",
        "governed_effects_permitted": True,
        "governed_dispatch_ready": True,
        "retryable": False,
        "observed_at_ns": runtime.composed_at_ns,
    }


def test_enforce_blocked_turns_readiness_into_503() -> None:
    # An enforce process whose composition exists but bound no client: the
    # constructor normally refuses to boot, so this pins the fail-closed
    # projection should it ever be observed (e.g. a client torn down later).
    runtime = SimpleNamespace(
        loaded=_load("enforce", config=_config("enforce")),
        ready=False,
        composed_at_ns=7,
    )
    body, code = readyz_body(_orch(runtime))

    assert code == 503
    assert body["status"] == "degraded"
    assert body["db"] == "ok"
    assert body["lets"] == {
        "mode": "enforce",
        "status": "blocked",
        "reason": "lets_unavailable",
        "governed_effects_permitted": False,
        "governed_dispatch_ready": False,
        "retryable": True,
        "observed_at_ns": 7,
    }


def test_enforce_invalid_configuration_is_blocked_503() -> None:
    runtime = SimpleNamespace(
        loaded=_load("enforce", config=None, configured=False),
        ready=False,
    )
    body, code = readyz_body(_orch(runtime))
    assert code == 503
    assert body["lets"]["status"] == "blocked"
    assert body["lets"]["reason"] == "lets_configuration_invalid"


def test_shadow_unreachable_warden_is_degraded_but_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _compose(monkeypatch, "shadow", reachable=False)
    assert runtime.client is None

    body, code = readyz_body(_orch(runtime))
    assert code == 200
    assert body["status"] == "ok"
    assert body["lets"]["mode"] == "shadow"
    assert body["lets"]["status"] == "degraded"
    assert body["lets"]["reason"] == "lets_unavailable"
    assert body["lets"]["governed_effects_permitted"] is True
    assert body["lets"]["governed_dispatch_ready"] is False
    assert body["lets"]["retryable"] is True
    assert SECRET_PATH not in json.dumps(body)


def test_shadow_invalid_configuration_is_degraded_but_ready() -> None:
    runtime = SimpleNamespace(
        loaded=_load("shadow", config=None, configured=False),
        ready=False,
    )
    body, code = readyz_body(_orch(runtime))
    assert code == 200
    assert body["lets"]["status"] == "degraded"
    assert body["lets"]["reason"] == "lets_configuration_invalid"


def test_shadow_reachable_is_healthy_probe_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _compose(monkeypatch, "shadow", reachable=True)
    body, code = readyz_body(_orch(runtime))
    assert code == 200
    assert body["lets"]["status"] == "healthy"
    assert body["lets"]["governed_dispatch_ready"] is False


def test_unbound_active_mode_is_projected_as_starting_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No composition bound but the environment asks for enforce: the fallback
    # load refuses (master flag absent) and readiness fails closed.
    monkeypatch.delenv("FF_LETS_EXTERNAL_WARDEN", raising=False)
    monkeypatch.setenv("LETS_MODE", "enforce")
    body, code = readyz_body(_orch(None, bind=False))
    assert code == 503
    assert body["lets"]["status"] == "blocked"
    assert body["lets"]["reason"] == "lets_configuration_invalid"


def test_projection_rejects_missing_posture() -> None:
    with pytest.raises(TypeError):
        project_runtime_health(None)
    with pytest.raises(TypeError):
        readiness_entry(object())  # type: ignore[arg-type]


# ── GET /lets/health ───────────────────────────────────────────────────────


def _app(runtime: object | None, roles: list[str] | None) -> TestClient:
    app = FastAPI()
    app.state.orchestrator = _orch(runtime)
    app.include_router(lets_router)
    if roles is not None:
        payload = {"sub": "u", "realm_access": {"roles": roles}}
        app.dependency_overrides[get_current_user_payload] = lambda: payload
    return TestClient(app)


def test_lets_health_requires_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
    response = _app(None, None).get("/lets/health")
    assert response.status_code == 401


def test_lets_health_requires_admin_role(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _compose(monkeypatch, "shadow", reachable=True)
    response = _app(runtime, ["user"]).get("/lets/health")
    assert response.status_code == 403


def test_lets_health_admin_gets_redacted_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _compose(monkeypatch, "shadow", reachable=False)
    response = _app(runtime, ["admin"]).get("/lets/health")
    assert response.status_code == 200
    report = response.json()

    assert report["composition_bound"] is True
    assert report["health"]["mode"] == "shadow"
    assert report["health"]["component_status"] == "degraded"
    assert report["health"]["operator_code"] == "lets_unavailable"
    assert report["readiness"] == {
        "mode": "shadow",
        "status": "configured",
        "reason": "lets_shadow_configured",
        "application_ready": True,
        "lets_configured": True,
        "governed_effects_permitted": True,
        "diagnostic_only": True,
    }
    assert report["config"] == json.loads(json.dumps(runtime.config.redacted()))
    assert report["config"]["service_token_file"] == "<configured>"
    assert report["config"]["governed_agent_allowlist_count"] == 1
    assert "governed_agent_allowlist" not in report["config"]
    text = response.text
    assert SECRET_PATH not in text
    assert "/etc/" not in text
    assert "agent-a" not in text


def test_lets_health_off_reports_disabled_without_lets_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _compose(monkeypatch, "off", reachable=True)
    report = health_report(runtime)
    assert report["health"]["component_status"] == "disabled"
    assert report["readiness"]["status"] == "disabled"
    assert report["config"]["mode"] == "off"
    assert report["config"]["warden_origin"] is None
