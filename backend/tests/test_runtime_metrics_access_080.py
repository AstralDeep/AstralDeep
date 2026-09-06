"""080-runtime-metrics: verified-admin gate on deployment diagnostics (US1).

These contract-first tests exercise the *real* FastAPI dependency chain for
``GET /api/runtime-reliability/metrics``.  Only the verified payload boundary
(``get_current_user_payload``) is overridden — never ``verify_admin`` itself —
so the genuine role-extraction and fail-closed denial logic runs against
synthetic principals without contacting Keycloak.

Against unchanged ``main`` the endpoint requires only ``require_user_id``, so an
ordinary authenticated (non-admin) principal receives the full snapshot; the
non-admin denial test below is therefore EXPECTED RED until feature 080 adds the
admin gate.  Owner-scoped operation reconciliation must stay available to the
same non-admin principal on both trees.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from orchestrator import api as api_module
from orchestrator.auth import get_current_user_payload
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionCoordinator,
)


_ADMIN_PAYLOAD = {"sub": "admin-1", "realm_access": {"roles": ["admin", "user"]}}
_NON_ADMIN_PAYLOAD = {"sub": "owner-a", "realm_access": {"roles": ["user"]}}
_INVALID = object()


@dataclass
class _Clock:
    current: datetime = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current


class _ObservabilitySpy:
    """Delegating collector that counts export snapshots without altering them."""

    def __init__(self, inner: RuntimeObservability) -> None:
        self._inner = inner
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return self._inner.snapshot()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _CoordinatorSpy:
    """Delegating coordinator that records admission-inspection access."""

    def __init__(self, inner: WorkAdmissionCoordinator) -> None:
        self._inner = inner
        self.inspect_calls: list[AdmissionClass] = []

    def inspect_admission_class(self, admission_class):
        self.inspect_calls.append(admission_class)
        return self._inner.inspect_admission_class(admission_class)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _coordinator(*, queue_limit: int = 2) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=None,
                active_limit=1,
                queue_limit=queue_limit,
                max_wait_ms=5_000 if queue_limit else None,
                config_revision="access-080",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=_Clock(),
        operation_retention=timedelta(hours=24),
    )


def _owner(user_id: str) -> OperationOwner:
    return OperationOwner(
        owner_scope=OwnerScope.USER,
        owner_user_id=user_id,
        connection_scope_id=None,
    )


def _request(label: str, *, owner: OperationOwner) -> OperationRequest:
    submission_id = uuid.uuid4()
    return OperationRequest(
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner=owner,
        submission_id=submission_id,
        idempotency_namespace="access_080",
        idempotency_key=str(submission_id),
        normalized_input_digest=hashlib.sha256(label.encode()).hexdigest(),
        chat_id=f"chat-{label}",
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )


def _payload_provider(payload):
    if payload is _INVALID:
        def _raise():
            raise HTTPException(status_code=401, detail="Invalid token")

        return _raise
    return lambda: dict(payload) if payload is not None else None


def _make_client(
    payload,
    *,
    coordinator: WorkAdmissionCoordinator | None = None,
) -> tuple[TestClient, _ObservabilitySpy, _CoordinatorSpy]:
    observability = _ObservabilitySpy(
        RuntimeObservability(
            clock=_Clock(),
            retention_seconds=86_400,
            deployment_instance="access_test",
        )
    )
    coordinator_spy = _CoordinatorSpy(coordinator or _coordinator())
    app = FastAPI()
    app.state.orchestrator = SimpleNamespace(
        work_admission=coordinator_spy,
        runtime_observability=observability,
        voice_services=None,
        _save_user_profile=Mock(),
    )
    app.include_router(api_module.operation_router)
    app.dependency_overrides[get_current_user_payload] = _payload_provider(payload)
    return TestClient(app), observability, coordinator_spy


def test_admin_principal_receives_no_store_snapshot() -> None:
    client, observability, coordinator = _make_client(_ADMIN_PAYLOAD)

    response = client.get("/api/runtime-reliability/metrics")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert isinstance(body["metrics"], list)
    # The verified-admin snapshot actually consulted the collector and the
    # effective-admission inspection path.
    assert observability.snapshot_calls >= 1
    assert AdmissionClass.INTERACTIVE in coordinator.inspect_calls


def test_non_admin_authenticated_principal_denied_without_collector_access(
    monkeypatch,
) -> None:
    # EXPECTED RED on unchanged main: without the admin gate this ordinary
    # authenticated principal receives 200 and the collector is consulted.
    client, observability, coordinator = _make_client(_NON_ADMIN_PAYLOAD)
    lookup = Mock(wraps=api_module._get_orchestrator)
    monkeypatch.setattr(api_module, "_get_orchestrator", lookup)

    response = client.get("/api/runtime-reliability/metrics")

    assert response.status_code == 403
    assert observability.snapshot_calls == 0
    assert coordinator.inspect_calls == []
    lookup.assert_not_called()
    client.app.state.orchestrator._save_user_profile.assert_not_called()


@pytest.mark.parametrize("payload", (None, _INVALID))
def test_unauthenticated_or_invalid_principal_denied_before_collector(payload) -> None:
    client, observability, coordinator = _make_client(payload)

    response = client.get("/api/runtime-reliability/metrics")

    assert response.status_code in (401, 403)
    assert observability.snapshot_calls == 0
    assert coordinator.inspect_calls == []
    client.app.state.orchestrator._save_user_profile.assert_not_called()


@pytest.mark.parametrize("subject", (None, ""))
def test_admin_role_without_subject_is_denied(subject) -> None:
    payload = {"realm_access": {"roles": ["admin"]}}
    if subject is not None:
        payload["sub"] = subject
    client, observability, coordinator = _make_client(payload)

    response = client.get("/api/runtime-reliability/metrics")

    assert response.status_code == 401
    assert observability.snapshot_calls == 0
    assert coordinator.inspect_calls == []
    client.app.state.orchestrator._save_user_profile.assert_not_called()


def test_absent_bearer_is_rejected_by_real_authentication_dependency() -> None:
    client, observability, coordinator = _make_client(None)
    client.app.dependency_overrides.clear()

    response = client.get("/api/runtime-reliability/metrics")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert observability.snapshot_calls == 0
    assert coordinator.inspect_calls == []
    client.app.state.orchestrator._save_user_profile.assert_not_called()


def test_openapi_describes_admin_access() -> None:
    client, _, _ = _make_client(_ADMIN_PAYLOAD)
    route = client.get("/openapi.json").json()["paths"][
        "/api/runtime-reliability/metrics"
    ]["get"]

    assert "admin" in route["description"].lower()
    assert "403" in route["responses"]


def test_non_admin_owner_can_still_reconcile_own_operation() -> None:
    coordinator = _coordinator()
    accepted = coordinator.submit(_request("owned", owner=_owner("owner-a")))
    client, observability, _ = _make_client(
        _NON_ADMIN_PAYLOAD,
        coordinator=coordinator,
    )

    response = client.get(f"/api/operations/{accepted.operation_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["operation_id"] == str(accepted.operation_id)
    assert body["owner_scope"] == "user"
    # Owner reconciliation is not a deployment-diagnostic read and does not
    # touch the metrics collector snapshot.
    assert observability.snapshot_calls == 0


def test_cross_owner_reconciliation_still_denied_for_authenticated_non_admin() -> None:
    coordinator = _coordinator()
    accepted = coordinator.submit(_request("private", owner=_owner("owner-a")))
    client, _, _ = _make_client(
        {"sub": "owner-b", "realm_access": {"roles": ["user"]}},
        coordinator=coordinator,
    )

    response = client.get(f"/api/operations/{accepted.operation_id}")

    assert response.status_code == 404
