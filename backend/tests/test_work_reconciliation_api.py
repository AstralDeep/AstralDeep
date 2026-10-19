"""Tests for the /api/async-tasks effect-reconcile route: strict authentication and
typed error mapping, with the reconciliation service boundary kept fake-backed.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ["USE_MOCK_AUTH"] = "true"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.api import async_task_router  # noqa: E402
from orchestrator.work_reconciliation import (  # noqa: E402
    ReconciliationRefused,
)

OWNER = "owner-user-id"
STRANGER = "stranger-user-id"


def _token(sub: str) -> str:
    body = base64.b64encode(json.dumps({
        "sub": sub,
        "preferred_username": sub,
        "realm_access": {"roles": ["user"]},
    }).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def _auth(sub: str) -> dict:
    return {"Authorization": f"Bearer {_token(sub)}"}


BODY = {
    "attempt_nonce": "a" * 32,
    "argument_digest": "b" * 64,
    "target_kind": "mesh_member",
    "target_id": "member-1",
    "outcome": "confirmed_not_applied",
}


def _service():
    service = MagicMock()
    service.report.return_value = {
        "operation_id": "task-1",
        "attempt_nonce": "a" * 32,
        "outcome": "confirmed_not_applied",
        "applied": False,
        "operation_state": "retryable",
    }
    return service


def _app(service) -> TestClient:
    orch = MagicMock()
    orch.effect_reconciliation = service
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(async_task_router)
    return TestClient(app)


def test_route_requires_authentication():
    response = _app(_service()).post("/api/async-tasks/task-1/effect-reconcile", json=BODY)
    assert response.status_code == 401


def test_owner_report_is_reconciled_through_the_service_boundary():
    service = _service()
    response = _app(service).post(
        "/api/async-tasks/task-1/effect-reconcile", json=BODY, headers=_auth(OWNER)
    )
    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert service.report.call_count == 1
    assert service.report.call_args.args[0] == OWNER
    assert service.report.call_args.args[1] == "task-1"
    assert service.report.call_args.args[2].attempt_nonce == "a" * 32


def test_malformed_reports_fail_closed_as_invalid():
    malformed = dict(BODY)
    malformed["outcome"] = "retry"
    response = _app(_service()).post(
        "/api/async-tasks/task-1/effect-reconcile", json=malformed, headers=_auth(OWNER)
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "reconcile_report_invalid"
    missing = dict(BODY)
    missing.pop("attempt_nonce")
    response = _app(_service()).post(
        "/api/async-tasks/task-1/effect-reconcile", json=missing, headers=_auth(OWNER)
    )
    assert response.status_code == 422


@pytest.mark.parametrize("code,status,detail", [
    ("work_not_found", 404, "work_not_found"),
    ("reconcile_attempt_unknown", 404, "reconcile_attempt_unknown"),
    ("reconcile_report_stale", 409, "reconcile_report_stale"),
    ("reconcile_target_unavailable", 409, "reconcile_target_unavailable"),
    ("reconcile_execution_current", 409, "reconcile_execution_current"),
    ("reconcile_outcome_conflict", 409, "reconcile_outcome_conflict"),
    ("reconcile_authority_unavailable", 503, "reconcile_authority_unavailable"),
])
def test_refusals_map_to_their_typed_status(code, status, detail):
    service = MagicMock()
    service.report.side_effect = ReconciliationRefused(code, status=status)
    response = _app(service).post(
        "/api/async-tasks/task-1/effect-reconcile", json=BODY, headers=_auth(OWNER)
    )
    assert response.status_code == status
    assert response.json()["detail"] == detail
