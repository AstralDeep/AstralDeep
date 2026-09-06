"""Feature-060 authenticated operation reconciliation API contracts (T030)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import api as api_module
from orchestrator.auth import get_current_user_payload, require_user_id
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


_SAFE_OPERATION_FIELDS = {
    "operation_id",
    "operation_kind",
    "admission_class",
    "owner_scope",
    "chat_id",
    "parent_operation_id",
    "connection_generation",
    "request_generation",
    "state",
    "phase_code",
    "terminal_code",
    "safe_summary",
    "retry_after_ms",
    "state_revision",
    "accepted_at",
    "queue_deadline_at",
    "started_at",
    "terminal_at",
    "updated_at",
    "purge_after",
}
_FORBIDDEN_FIELDS = {
    "owner_user_id",
    "connection_scope_id",
    "idempotency_namespace",
    "idempotency_key",
    "normalized_input_digest",
    "execution_generation",
    "execution_lease_token",
    "cancel_requested_at",
}


@dataclass
class _Clock:
    current: datetime = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current


def _coordinator(*, queue_limit: int = 2) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=None,
                active_limit=1,
                queue_limit=queue_limit,
                max_wait_ms=5_000 if queue_limit else None,
                config_revision="api-060",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=_Clock(),
        operation_retention=timedelta(hours=24),
    )


def _owner(user_id: str, scope: OwnerScope = OwnerScope.USER) -> OperationOwner:
    return OperationOwner(
        owner_scope=scope,
        owner_user_id=(None if scope is OwnerScope.CONNECTION else user_id),
        connection_scope_id=(
            uuid.uuid4() if scope is OwnerScope.CONNECTION else None
        ),
    )


def _request(
    label: str,
    *,
    owner: OperationOwner,
    submission_id: uuid.UUID | None = None,
) -> OperationRequest:
    submission_id = submission_id or uuid.uuid4()
    return OperationRequest(
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner=owner,
        submission_id=submission_id,
        idempotency_namespace="api_060",
        idempotency_key=str(submission_id),
        normalized_input_digest=hashlib.sha256(label.encode()).hexdigest(),
        chat_id=f"chat-{label}",
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )


def _client(
    coordinator: WorkAdmissionCoordinator,
    *,
    user_id: str = "owner-a",
) -> TestClient:
    operation_router = getattr(api_module, "operation_router", None)
    assert operation_router is not None, (
        "EXPECTED RED (T030): orchestrator.api must expose operation_router"
    )
    app = FastAPI()
    app.state.orchestrator = SimpleNamespace(
        work_admission=coordinator,
        runtime_observability=RuntimeObservability(
            clock=_Clock(),
            retention_seconds=86_400,
            deployment_instance="api_test",
        ),
    )
    app.include_router(operation_router)
    app.dependency_overrides[require_user_id] = lambda: user_id
    return TestClient(app)


def test_operation_query_returns_exact_safe_projection_and_no_store() -> None:
    coordinator = _coordinator()
    request = _request("visible", owner=_owner("owner-a"))
    accepted = coordinator.submit(request)

    response = _client(coordinator).get(f"/api/operations/{accepted.operation_id}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == _SAFE_OPERATION_FIELDS
    assert not set(body).intersection(_FORBIDDEN_FIELDS)
    assert body["operation_id"] == str(accepted.operation_id)
    assert body["owner_scope"] == "user"
    assert body["state"] == "running"
    assert body["accepted_at"].endswith("Z")
    assert body["started_at"].endswith("Z")
    assert body["terminal_at"] is None


def test_submission_query_returns_original_accepted_operation_envelope() -> None:
    coordinator = _coordinator()
    submission_id = uuid.uuid4()
    accepted = coordinator.submit(
        _request(
            "accepted-submission",
            owner=_owner("owner-a"),
            submission_id=submission_id,
        )
    )

    response = _client(coordinator).get(
        f"/api/operation-submissions/{submission_id}"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["accepted"] is True
    assert response.json()["operation"]["operation_id"] == str(
        accepted.operation_id
    )
    assert set(response.json()["operation"]) == _SAFE_OPERATION_FIELDS


def test_submission_query_returns_definitive_safe_refusal() -> None:
    coordinator = _coordinator(queue_limit=0)
    coordinator.submit(_request("occupy", owner=_owner("owner-a")))
    submission_id = uuid.uuid4()
    refused = coordinator.submit(
        _request(
            "refused",
            owner=_owner("owner-a"),
            submission_id=submission_id,
        )
    )
    assert refused.accepted is False

    response = _client(coordinator).get(
        f"/api/operation-submissions/{submission_id}"
    )

    assert response.status_code == 200
    assert response.json() == {
        "accepted": False,
        "code": "capacity_exceeded",
        "retryable": True,
        "retry_after_ms": refused.retry_after_ms,
    }


@pytest.mark.parametrize("resource", ("operations", "operation-submissions"))
def test_unknown_invalid_and_cross_owner_identity_share_not_found(
    resource: str,
) -> None:
    coordinator = _coordinator()
    submission_id = uuid.uuid4()
    accepted = coordinator.submit(
        _request(
            "private",
            owner=_owner("owner-a"),
            submission_id=submission_id,
        )
    )
    visible_id = (
        accepted.operation_id if resource == "operations" else submission_id
    )
    path = f"/api/{resource}/{visible_id}"

    cross_owner = _client(coordinator, user_id="owner-b").get(path)
    unknown = _client(coordinator).get(f"/api/{resource}/{uuid.uuid4()}")
    malformed = _client(coordinator).get(f"/api/{resource}/not-a-uuid")

    assert cross_owner.status_code == unknown.status_code == malformed.status_code == 404
    assert cross_owner.json() == unknown.json() == malformed.json()


def test_authenticated_user_can_reconcile_own_schedule_scoped_operation() -> None:
    coordinator = _coordinator()
    accepted = coordinator.submit(
        _request(
            "schedule",
            owner=_owner("owner-a", OwnerScope.SCHEDULE),
        )
    )

    response = _client(coordinator).get(f"/api/operations/{accepted.operation_id}")

    assert response.status_code == 200
    assert response.json()["owner_scope"] == "schedule"


def test_connection_owned_identity_is_not_authorized_by_uuid_possession() -> None:
    coordinator = _coordinator()
    accepted = coordinator.submit(
        _request(
            "connection-only",
            owner=_owner("owner-a", OwnerScope.CONNECTION),
        )
    )

    response = _client(coordinator).get(
        f"/api/operations/{accepted.operation_id}"
    )
    unknown = _client(coordinator).get(f"/api/operations/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json() == unknown.json()


def test_openapi_documents_both_authenticated_reconciliation_paths() -> None:
    schema = _client(_coordinator()).get("/openapi.json").json()

    for path in (
        "/api/operations/{operation_id}",
        "/api/operation-submissions/{submission_id}",
    ):
        operation = schema["paths"][path]["get"]
        assert operation["summary"]
        assert "authenticated" in operation["description"].lower()
        assert "200" in operation["responses"]
        assert "404" in operation["responses"]

    metrics = schema["paths"]["/api/runtime-reliability/metrics"]["get"]
    assert metrics["summary"]
    assert "authenticated" in metrics["description"].lower()
    assert "200" in metrics["responses"]


def test_authenticated_metrics_export_refreshes_effective_admission_gauges() -> None:
    client = _client(_coordinator())
    # Feature 080 gates deployment diagnostics behind the existing verified-admin
    # role.  Override only the verified-payload boundary so the genuine
    # ``verify_admin`` role check runs against a synthetic admin principal; never
    # override ``verify_admin`` itself or weaken the owner-isolation fixtures.
    client.app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": "owner-a",
        "realm_access": {"roles": ["admin", "user"]},
    }

    response = client.get("/api/runtime-reliability/metrics")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    metrics = response.json()["metrics"]
    names = {sample["name"] for sample in metrics}
    assert {
        "operation_active_limit",
        "operation_queue_limit",
        "operation_queue_max_wait_ms",
        "operation_retention_seconds",
        "operation_active_count",
        "operation_queued_count",
        "operation_oldest_queued_age_seconds",
        "operation_oldest_running_age_seconds",
    } <= names
    assert all(
        set(sample["labels"])
        <= {
            "deployment_instance",
            "effect_kind",
            "job_type",
            "operation_kind",
            "phase",
            "result_code",
        }
        for sample in metrics
    )
