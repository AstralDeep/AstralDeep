"""Tests for the first-connection guide (orchestrator/connection_guide.py and
projection_surfaces/connection.py): endpoint attestation, honest per-service checks,
the /api/connection/status route, surface delivery, and the registration-time push.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import connection_guide as cg


@pytest.fixture()
def configured_guide_database():
    if not any(os.getenv(name) for name in ("DATABASE_URL", "ASTRALPLANE_TEST_DATABASE_URL", "DB_HOST")):
        pytest.skip("PostgreSQL connection is not configured for registration integration")


def _worker_readiness(**overrides):
    values = {
        "ready": True, "reason": "ready", "worker_count": 2,
        "capacity_total": 8, "capacity_available": 5, "profile": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeOrch:
    def __init__(self, *, plane_ok=True, publication_ready=True,
                 voice_services="absent", worker_readiness=None):
        self.plane_ok = plane_ok
        self.publication_ready = publication_ready
        self.voice_services = voice_services
        self.worker_readiness = worker_readiness

    def _probe_plane_readiness(self):
        if not self.plane_ok:
            raise RuntimeError("AstralPlane runtime is not ready")

    class generated_agent_publication_service:
        @staticmethod
        async def readiness():
            return SimpleNamespace(ready=True)


def _publication_service(ready: bool):
    class _Service:
        @staticmethod
        async def readiness():
            return SimpleNamespace(ready=ready)
    return _Service


@pytest.fixture()
def production_auth(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://sso.example/realms/Astral")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)


def _check(checks, key):
    return next(c for c in checks if c["key"] == key)


class _FakeEndpoint:
    def __init__(self, configured_env):
        self.configured_env = configured_env


def test_endpoint_origin_normalization():
    assert cg._origin_of("http://localhost:8001") == "http://localhost:8001"
    assert cg._origin_of("https://astral.example") == "https://astral.example"
    assert cg._origin_of("astral.example:8443") == "http://astral.example:8443"
    assert cg._origin_of("http://LOCALHOST:80") == "http://localhost"
    assert cg._origin_of("") == ""
    assert cg._origin_of("http://[::1:bad") == ""


def test_endpoint_report_verified_match(monkeypatch):
    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8001")
    report = cg.endpoint_report("http://localhost:8001")
    assert report["verified"] is True
    assert report["configured"] == ["http://localhost:8001"]
    assert "sign-in only on this server's own sign-in page" in report["guidance"]


def test_endpoint_report_mismatch_is_reported_honestly(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://astral.example")
    report = cg.endpoint_report("http://localhost:8001")
    assert report["verified"] is False
    assert "https://astral.example" in report["guidance"]
    assert "http://localhost:8001" in report["guidance"]


def test_endpoint_report_without_configuration_names_the_compose_default(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)
    report = cg.endpoint_report(None)
    assert report["configured"] == []
    assert report["verified"] is False
    assert "http://localhost:8001" in report["guidance"]


def test_endpoint_report_uses_both_configured_variables(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://astral.example")
    monkeypatch.setenv("BACKEND_PUBLIC_URL", "http://localhost:8001")
    report = cg.endpoint_report("http://localhost:8001")
    assert report["verified"] is True
    assert report["configured"] == ["https://astral.example", "http://localhost:8001"]


def _status_app(orch) -> TestClient:
    app = FastAPI()
    app.include_router(cg.connection_router)
    app.state.orchestrator = orch
    return TestClient(app)


def _fake_send_recorder(delivered=True):
    sent = []

    async def _send(_websocket, payload):
        sent.append(payload)
        return delivered

    return sent, _send


class _PushOrch:
    def __init__(self, delivered=True, device_type="browser"):
        self.delivered = delivered
        self.device_type = device_type
        self.sent, self._send = _fake_send_recorder(delivered)
        self.open_surfaces = {}
        self.websocket = object()
        self.ui_sessions = {self.websocket: {"sub": "user"}}
        self._connection_contexts = {id(self.websocket): SimpleNamespace(
            websocket=self.websocket, connection_scope_id="scope-1", connection_generation="generation-1",
            registered=True, closing=False, work_registrations_pending=0)}
        self._probe_plane_readiness = lambda: None
        self.generated_agent_publication_service = _publication_service(True
        )
        self.rote = SimpleNamespace(get_profile=self.get_profile)

    async def _safe_send(self, websocket, payload):
        return await self._send(websocket, payload)

    def get_profile(self, websocket):
        from rote.capabilities import DeviceProfile

        return DeviceProfile.from_dict({"device_type": self.device_type})


def test_endpoint_origin_without_host_is_empty():
    assert cg._origin_of("http://") == ""
    assert cg._origin_of("///") == ""

