"""Coverage for the emergency-stop enforcement seams: the admission-coordinator
submission gate, the binding's owner-scope gate, and the owner-authenticated REST
router's truthful status, denial, and recovery responses.
"""

from __future__ import annotations

import os
import sys
import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ["USE_MOCK_AUTH"] = "true"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from orchestrator.emergency_stop import (  # noqa: E402
    EmergencyStopCoordinator,
    LOCAL_RESPONDER,
)
from orchestrator.emergency_stop_api import emergency_stop_router  # noqa: E402
from orchestrator.work_admission import (  # noqa: E402
    AdmissionClass,
    AdmissionClassConfig,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionCoordinator,
)


OWNER = "11111111-1111-1111-1111-111111111111"


def _token(sub: str) -> str:
    import base64
    import json

    claims = {"sub": sub, "realm_access": {"roles": ["user"]}}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def _auth(sub: str = OWNER) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(sub)}"}


def _config(name: str = "interactive") -> list[AdmissionClassConfig]:
    return [
        AdmissionClassConfig(
            class_name=AdmissionClass.GLOBAL,
            parent_class_name=None,
            active_limit=8,
            queue_limit=0,
            max_wait_ms=None,
            config_revision="global-test",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.INTERACTIVE,
            parent_class_name=AdmissionClass.GLOBAL,
            active_limit=2,
            queue_limit=0,
            max_wait_ms=None,
            config_revision=name,
        ),
    ]


def _request(owner: OperationOwner) -> OperationRequest:
    return OperationRequest(
        operation_kind="chat",
        admission_class=AdmissionClass.INTERACTIVE,
        owner=owner,
        submission_id=uuid.uuid4(),
        idempotency_namespace=None,
        idempotency_key=None,
        normalized_input_digest=None,
        chat_id=None,
        parent_operation_id=None,
        connection_generation=None,
        request_generation=None,
    )


def _user_owner(user_id: str = OWNER) -> OperationOwner:
    return OperationOwner(owner_scope=OwnerScope.USER, owner_user_id=user_id,
                          connection_scope_id=None)


def _system_owner() -> OperationOwner:
    return OperationOwner(owner_scope=OwnerScope.SYSTEM, owner_user_id=None,
                          connection_scope_id=None)


def test_submission_gate_blocks_engaged_owner_at_admission():
    coordinator = EmergencyStopCoordinator()
    asyncio_status = asyncio.run(coordinator.engage(OWNER))
    from datetime import UTC, datetime

    from orchestrator.work_admission import InMemoryWorkAdmissionRepository

    admission = WorkAdmissionCoordinator(
        admission_classes=_config(),
        repository=InMemoryWorkAdmissionRepository(),
        clock=lambda: datetime.now(UTC),
        submission_gate=lambda request: (
            None if coordinator.admission_allowed(request.owner.owner_user_id or "")
            else "emergency_stop_active"),
    )
    refused = admission.submit(_request(_user_owner()))
    assert refused.accepted is False
    assert refused.code == "emergency_stop_active"
    assert refused.retryable is False
    accepted = admission.submit(_request(_user_owner("22222222-2222-2222-2222-222222222222")))
    assert accepted.accepted is True
    assert asyncio_status["state"] == "stopped"


def test_submission_gate_leaves_system_scope_untouched():
    coordinator = EmergencyStopCoordinator()
    asyncio.run(coordinator.engage(OWNER))
    from orchestrator.emergency_stop_binding import submission_gate

    gate = submission_gate(SimpleNamespace(emergency_stop=coordinator))
    assert gate is not None
    assert gate(_request(_system_owner())) is None
    assert gate(_request(_user_owner())) == "emergency_stop_active"
    resumed = asyncio.run(coordinator.resume(OWNER, expected_revision=1, actor_id=OWNER))
    assert resumed["engaged"] is False
    assert gate(_request(_user_owner())) is None


def test_submission_gate_is_none_without_a_coordinator():
    from orchestrator.emergency_stop_binding import submission_gate

    assert submission_gate(SimpleNamespace()) is None


def _client(coordinator) -> TestClient:
    app = FastAPI()
    app.include_router(emergency_stop_router)
    app.state.orchestrator = SimpleNamespace(emergency_stop=coordinator)
    return TestClient(app)


def test_status_endpoint_reports_running_then_stopped():
    coordinator = EmergencyStopCoordinator()
    client = _client(coordinator)
    running = client.get("/api/emergency-stop", headers=_auth())
    assert running.status_code == 200
    assert running.json()["engaged"] is False
    assert running.json()["state"] == "running"
    engaged = client.post("/api/emergency-stop/stop", headers=_auth(),
                          json={"reason": "drill"})
    assert engaged.status_code == 200
    body = engaged.json()
    assert body["engaged"] is True and body["state"] == "stopped"
    assert body["reason"] == "drill"
    assert body["responders"][0]["responder"] == LOCAL_RESPONDER
    assert running.headers["cache-control"] == "no-store"


def test_status_endpoint_requires_authentication():
    client = _client(EmergencyStopCoordinator())
    assert client.get("/api/emergency-stop").status_code == 401


def test_resume_endpoint_denies_stale_revision_and_foreign_owner():
    coordinator = EmergencyStopCoordinator()
    client = _client(coordinator)
    client.post("/api/emergency-stop/stop", headers=_auth(), json={})
    stale = client.post("/api/emergency-stop/resume", headers=_auth(),
                        json={"expected_revision": 99})
    assert stale.status_code == 409
    assert stale.json()["error"] == "emergency_stop_stale_revision"
    other = "22222222-2222-2222-2222-222222222222"
    foreign = client.post("/api/emergency-stop/resume", headers=_auth(other),
                          json={"expected_revision": 1})
    assert foreign.status_code == 409
    assert foreign.json()["error"] == "emergency_stop_not_engaged"
    assert client.get("/api/emergency-stop", headers=_auth()).json()["engaged"] is True
    assert client.get("/api/emergency-stop", headers=_auth(other)).json()["engaged"] is False
    resumed = client.post("/api/emergency-stop/resume", headers=_auth(),
                          json={"expected_revision": 1})
    assert resumed.status_code == 200
    assert resumed.json()["engaged"] is False


def test_resume_endpoint_refuses_when_not_engaged():
    client = _client(EmergencyStopCoordinator())
    response = client.post("/api/emergency-stop/resume", headers=_auth(),
                           json={"expected_revision": 1})
    assert response.status_code == 409
    assert response.json()["error"] == "emergency_stop_not_engaged"


def test_verify_endpoint_refreshes_remote_responder_states():
    async def probe(owner_id, responder):
        return "acknowledged"

    coordinator = EmergencyStopCoordinator(
        remote_responders=lambda owner_id: ["machine-a"],
        probe_responder=probe,
    )
    client = _client(coordinator)
    client.post("/api/emergency-stop/stop", headers=_auth(), json={})
    verified = client.post("/api/emergency-stop/verify", headers=_auth())
    assert verified.status_code == 200
    body = verified.json()
    assert body["state"] == "stopped"
    assert [row["state"] for row in body["responders"]] == ["acknowledged", "acknowledged"]


def test_endpoints_report_unavailable_without_a_coordinator():
    app = FastAPI()
    app.include_router(emergency_stop_router)
    app.state.orchestrator = SimpleNamespace()
    client = TestClient(app)
    assert client.get("/api/emergency-stop", headers=_auth()).status_code == 503
    assert client.post("/api/emergency-stop/stop", headers=_auth(), json={}).status_code == 503


def test_invalid_reason_is_a_request_validation_error():
    client = _client(EmergencyStopCoordinator())
    response = client.post("/api/emergency-stop/stop", headers=_auth(),
                           json={"reason": "x" * 281})
    assert response.status_code == 422


def test_refusal_mapper_covers_every_denial_code():
    from fastapi.responses import JSONResponse

    from orchestrator.emergency_stop import EmergencyStopRefused
    from orchestrator.emergency_stop_api import _refused
    from persistent_agents.models import AssignmentError

    cases = {
        EmergencyStopRefused("emergency_stop_not_engaged", 409): (409, "emergency_stop_not_engaged"),
        EmergencyStopRefused("emergency_stop_stale_revision", 409): (409, "emergency_stop_stale_revision"),
        EmergencyStopRefused("emergency_stop_resume_denied", 403): (403, "emergency_stop_resume_denied"),
        EmergencyStopRefused("emergency_stop_invalid", 422): (422, "emergency_stop_invalid"),
        EmergencyStopRefused("emergency_stop_unavailable", 503): (503, "emergency_stop_unavailable"),
    }
    for exc, (status, code) in cases.items():
        response = _refused(exc)
        assert response.status_code == status
        assert response.body is not None
        assert code.encode() in response.body
    gate = _refused(AssignmentError("emergency_stop_active", 423))
    assert gate.status_code == 423
    assert isinstance(gate, JSONResponse)


def test_status_endpoint_reports_unreachable_state_when_durable_read_fails():
    def loader(owner_id):
        raise RuntimeError("audit down")

    client = _client(EmergencyStopCoordinator(rearm_loader=loader))
    response = client.get("/api/emergency-stop", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["engaged"] is True
    assert body["state"] == "unreachable"


def test_verify_endpoint_fails_closed_when_durable_read_fails():
    def loader(owner_id):
        raise RuntimeError("audit down")

    client = _client(EmergencyStopCoordinator(rearm_loader=loader))
    response = client.post("/api/emergency-stop/verify", headers=_auth())
    assert response.status_code == 503
    assert response.json()["error"] == "emergency_stop_unavailable"


def test_endpoints_require_an_authenticated_user_role():
    import base64
    import json

    claims = {"sub": OWNER, "realm_access": {"roles": ["guest"]}}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    client = _client(EmergencyStopCoordinator())
    response = client.get("/api/emergency-stop",
                          headers={"Authorization": f"Bearer header.{body}.signature"})
    assert response.status_code == 403


def test_status_endpoint_without_any_orchestrator_is_unavailable():
    app = FastAPI()
    app.include_router(emergency_stop_router)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/emergency-stop", headers=_auth())
    assert response.status_code in (401, 503)


def test_delegated_and_machine_principals_cannot_drive_the_stop():
    import base64
    import json as jsonlib

    client = _client(EmergencyStopCoordinator())

    def _token_with(extra):
        claims = {"sub": OWNER, "realm_access": {"roles": ["user"]}, **extra}
        body = base64.urlsafe_b64encode(jsonlib.dumps(claims).encode()).decode().rstrip("=")
        return f"header.{body}.signature"

    for extra in ({"act": {"sub": "agent-1"}}, {"machine_class": "scheduled_turn"}):
        headers = {"Authorization": f"Bearer {_token_with(extra)}"}
        assert client.post("/api/emergency-stop/stop", headers=headers,
                           json={}).status_code in (401, 403)
        assert client.post("/api/emergency-stop/resume", headers=headers,
                           json={"expected_revision": 1}).status_code in (401, 403)
        assert client.get("/api/emergency-stop", headers=headers).status_code in (401, 403)
