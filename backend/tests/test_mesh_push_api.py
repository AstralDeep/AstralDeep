"""Checks publisher-only owner registration and capability-only empty wakes through the HTTP boundary and real Plane.
APNs is a recording transport, so denials and failures exercise the real registry without third-party services."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audit.repository import AuditRepository
from orchestrator.auth import verify_user
from orchestrator.mesh_push_api import mesh_push_router
from orchestrator.mesh_push_gateway import MeshPushPublisher, PublisherWakeLimiter, publisher_for
from orchestrator.mesh_push_registry import MeshPushRegistry
from orchestrator.push_providers import PushProviderError
from shared.node_identity import canonical_owner_id
from tests.test_mesh_push_registry import KEY, MESH, DEVICE, OTHER, Clock, plane  # noqa: F401

ISSUER = "https://issuer.example/realms/owners"


class Apns:
    configured = True

    def __init__(self):
        self.tokens = []
        self.error = None

    def send_wake(self, *, device_token):
        if self.error:
            raise self.error
        self.tokens.append(device_token)


@pytest.fixture
def host(plane):  # noqa: F811
    clock = Clock()
    store = MeshPushRegistry(plane_runtime=plane, audit_repository=AuditRepository(plane_runtime=plane),
                            encryption_key=lambda: KEY, clock=clock)
    adapter = Apns()
    service = MeshPushPublisher(registry=store, adapter=adapter)
    app = FastAPI()
    app.state.orchestrator = SimpleNamespace(mesh_push_publisher=service)
    app.include_router(mesh_push_router)
    principal = {"iss": ISSUER, "sub": uuid4().hex}
    app.dependency_overrides[verify_user] = lambda: principal
    return SimpleNamespace(app=app, client=TestClient(app, raise_server_exceptions=False), principal=principal,
                           store=store, clock=clock, adapter=adapter, service=service)


def register(host, revision=0, token="private-phone-token"):
    return host.client.post("/api/push/mesh/destinations", json={"mesh_id": MESH, "device_id": DEVICE,
        "token": token, "expected_revision": revision})


def wake(host, capability, **extra):
    return host.client.post("/api/push/mesh/wake", headers={"Authorization": "Bearer " + capability},
                            json={"mesh_id": MESH, "device_id": DEVICE, **extra})


def test_owner_registration_and_empty_wake_never_require_node_iam(host):
    response = register(host)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"mesh_id", "device_id", "revision", "route"}
    assert set(body["route"]) == {"publisher", "capability", "expires_at_ms"}
    assert body["route"]["publisher"] == "astral" and "private-phone-token" not in response.text
    assert host.adapter.tokens == []
    host.app.dependency_overrides[verify_user] = lambda: pytest.fail("a wake requested IAM")
    assert wake(host, body["route"]["capability"]).status_code == 204
    assert host.adapter.tokens == ["private-phone-token"]


def test_registration_requires_verified_owner_and_cannot_claim_a_body_owner(host):
    host.app.dependency_overrides[verify_user] = lambda: {}
    assert register(host).status_code == 401
    host.app.dependency_overrides[verify_user] = lambda: {"sub": "owner"}
    assert register(host).status_code == 401
    host.app.dependency_overrides[verify_user] = lambda: host.principal
    response = host.client.post("/api/push/mesh/destinations", json={"mesh_id": MESH, "device_id": DEVICE,
        "token": "private-phone-token", "expected_revision": 0, "owner_id": "somebody-else"})
    assert response.status_code == 422 and host.adapter.tokens == []


def test_status_and_revoke_are_owner_scoped_and_revision_fenced(host):
    first = register(host).json()
    query = {"mesh_id": MESH, "device_id": DEVICE}
    current = host.client.get("/api/push/mesh/destinations", params=query)
    assert current.status_code == 200 and "capability" not in current.text and "private-phone-token" not in current.text
    host.app.dependency_overrides[verify_user] = lambda: {"iss": ISSUER, "sub": "different-owner"}
    assert host.client.get("/api/push/mesh/destinations", params=query).status_code == 404
    host.app.dependency_overrides[verify_user] = lambda: host.principal
    endpoint = "/api/push/mesh/destinations"
    assert host.client.request("DELETE", endpoint, json={**query, "expected_revision": 2}).status_code == 409
    deleted = host.client.request("DELETE", endpoint, json={**query, "expected_revision": first["revision"]})
    assert deleted.status_code == 200 and deleted.json()["state"] == "revoked"
    assert wake(host, first["route"]["capability"]).status_code == 403 and host.adapter.tokens == []


@pytest.mark.parametrize("extra", [{"message": "secret clinical text"}, {"chat_id": "chat"}, {"owner_id": "owner"}])
def test_wake_rejects_every_content_or_owner_field(host, extra):
    capability = register(host).json()["route"]["capability"]
    response = wake(host, capability, **extra)
    assert response.status_code == 422 and host.adapter.tokens == []
    assert "secret clinical text" not in response.text and capability not in response.text


def test_wake_refuses_wrong_target_expiry_and_shared_rate_without_side_effects(host):
    body = register(host).json()
    capability = body["route"]["capability"]
    wrong = host.client.post("/api/push/mesh/wake", headers={"Authorization": "Bearer " + capability},
                             json={"mesh_id": MESH, "device_id": OTHER})
    assert wrong.status_code == 403 and host.adapter.tokens == []
    assert wake(host, capability).status_code == 204
    assert wake(host, capability).status_code == 429
    assert host.adapter.tokens == ["private-phone-token"]
    host.clock.now = body["route"]["expires_at_ms"]
    assert wake(host, capability).status_code == 403
    assert host.adapter.tokens == ["private-phone-token"]


def test_provider_failure_is_unavailable_and_invalid_token_retires_only_its_generation(host):
    body = register(host).json()
    capability = body["route"]["capability"]
    host.adapter.error = PushProviderError(transient=True, code="provider_credentials_rejected", detail="private-phone-token")
    response = wake(host, capability)
    assert response.status_code == 503 and "private-phone-token" not in response.text
    host.clock.now += 300_000
    host.adapter.error = PushProviderError(transient=False, code="token_unregistered")
    assert wake(host, capability).status_code == 410
    owner = canonical_owner_id(ISSUER, host.principal["sub"])
    assert host.store.status(owner, mesh_id=MESH, device_id=DEVICE).state == "revoked"


def test_revocation_between_admission_and_provider_prevents_actual_send(host, monkeypatch):
    body = register(host).json()
    admit = host.store.admit

    def revoked(*args, **kwargs):
        target = admit(*args, **kwargs)
        host.store.revoke(target.owner_id, mesh_id=target.mesh_id, device_id=target.device_id, expected_revision=target.revision)
        return target

    monkeypatch.setattr(host.store, "admit", revoked)
    assert wake(host, body["route"]["capability"]).status_code == 403
    assert host.adapter.tokens == []


def test_publisher_absence_node_profiles_and_unconfigured_apns_are_unavailable(host, monkeypatch):
    for profile in ["personal", "institution"]:
        monkeypatch.setenv("ASTRAL_NODE_PROFILE", profile)
        assert register(host).status_code == 503
        monkeypatch.setattr("orchestrator.push_providers.adapters_from_env", lambda: pytest.fail("a node read APNs credentials"))
        assert publisher_for(SimpleNamespace(), enabled=True) is None
    monkeypatch.delenv("ASTRAL_NODE_PROFILE")
    assert publisher_for(SimpleNamespace(), enabled=False) is None
    host.adapter.configured = False
    assert register(host).status_code == 503
    host.app.state.orchestrator.mesh_push_publisher = None
    assert register(host).status_code == 503


def test_headers_and_bodies_are_bounded_before_authentication_or_registry_work(host):
    assert host.client.post("/api/push/mesh/wake", content=b"x" * 8193).status_code == 413
    assert host.client.post("/api/push/mesh/wake", content=b"not json").status_code == 422
    assert host.client.post("/api/push/mesh/wake", json=[]).status_code == 422
    assert host.client.post("/api/push/mesh/wake", json={"mesh_id": MESH, "device_id": DEVICE}).status_code == 403
    assert host.client.post("/api/push/mesh/wake", headers={"Authorization": "Bearer " + "x" * 2049},
                             json={"mesh_id": MESH, "device_id": DEVICE}).status_code == 403
    assert host.adapter.tokens == []


def test_public_caller_limiter_is_bounded_and_unknown_keys_cannot_grow_it():
    clock = Clock()
    limiter = PublisherWakeLimiter(capacity=2, burst=2, window_seconds=60, clock=lambda: clock.now / 1000)
    assert limiter.admit("one") and limiter.admit("one")
    assert not limiter.admit("one")
    assert limiter.admit("two")
    for number in range(100):
        assert not limiter.admit(str(number))
    clock.now += 60_000
    assert limiter.admit("three")


def test_explicit_publisher_factory_uses_only_application_plane_and_is_unavailable_without_it(host, plane, monkeypatch):  # noqa: F811
    from orchestrator.plane_repository_context import ApplicationPlaneSource

    orch = SimpleNamespace(plane_repository_source=ApplicationPlaneSource(plane, plane.repositories),
                           audit_repo=host.store.audit_repository)
    monkeypatch.delenv("ASTRAL_NODE_PROFILE", raising=False)
    monkeypatch.setenv("ASTRAL_MESH_PUSH_PUBLISHER_ENABLED", "1")
    looked_up = []
    monkeypatch.setattr("orchestrator.push_providers.adapters_from_env", lambda: looked_up.append(True) or {"apns": host.adapter})
    configured = publisher_for(orch)
    assert configured is not None and configured.available and configured.registry.runtime is plane
    assert looked_up == [True]
    assert publisher_for(SimpleNamespace(), enabled=True, adapter=host.adapter) is None
    host.adapter.configured = False
    assert publisher_for(orch, enabled=True, adapter=host.adapter) is None
    monkeypatch.delenv("ASTRAL_MESH_PUSH_PUBLISHER_ENABLED")
    assert publisher_for(orch) is None


@pytest.mark.asyncio
async def test_direct_gateway_missing_configuration_and_unexpected_provider_failure_are_safe(host, monkeypatch):
    from orchestrator.mesh_push_registry import MeshPushError

    owner = canonical_owner_id(ISSUER, host.principal["sub"])
    created = await asyncio.to_thread(host.store.register, owner, mesh_id=MESH, device_id=DEVICE, token="private-phone-token", expected_revision=0)
    host.adapter.configured = False
    with pytest.raises(MeshPushError, match="publisher_unavailable"):
        await host.service.wake(created.capability, mesh_id=MESH, device_id=DEVICE)
    host.adapter.configured = True
    host.adapter.error = RuntimeError("private-phone-token")
    with pytest.raises(MeshPushError, match="publisher_unavailable"):
        await host.service.wake(created.capability, mesh_id=MESH, device_id=DEVICE)
    assert host.adapter.tokens == []
    host.clock.now += 300_000
    current = host.store.current

    def unavailable(target):
        host.adapter.configured = False
        return current(target)

    monkeypatch.setattr(host.store, "current", unavailable)
    with pytest.raises(MeshPushError, match="publisher_unavailable"):
        await host.service.wake(created.capability, mesh_id=MESH, device_id=DEVICE)
    assert host.adapter.tokens == []


def test_duplicate_extra_and_stale_request_fields_are_refused_without_token_echo(host):
    first = register(host)
    assert first.status_code == 200
    assert register(host).status_code == 409
    endpoint = "/api/push/mesh/destinations"
    assert host.client.get(endpoint, params={"mesh_id": MESH, "device_id": DEVICE, "extra": "x"}).status_code == 422
    assert host.client.get(endpoint, params=[("mesh_id", MESH), ("mesh_id", OTHER), ("device_id", DEVICE)]).status_code == 422
    raw = ('{"mesh_id":"' + MESH + '","device_id":"' + DEVICE + '","device_id":"' + OTHER + '"}')
    assert host.client.post("/api/push/mesh/wake", content=raw).status_code == 422
    missing = host.client.request("DELETE", endpoint, json={"mesh_id": OTHER, "device_id": DEVICE, "expected_revision": 1})
    assert missing.status_code == 404


def test_actual_orchestrator_composes_disabled_publisher_without_operator_setup(orchestrator_factory, monkeypatch):
    monkeypatch.delenv("ASTRAL_MESH_PUSH_PUBLISHER_ENABLED", raising=False)
    orch = orchestrator_factory()
    assert orch.mesh_push_publisher is None

