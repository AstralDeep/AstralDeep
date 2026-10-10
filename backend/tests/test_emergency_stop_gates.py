"""Coverage for the emergency-stop enforcement seams: the admission-coordinator
submission gate, the binding's owner-scope gate, and the owner-authenticated REST
router's truthful status, denial, and recovery responses.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ["USE_MOCK_AUTH"] = "true"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from audit.schemas import AuditEventCreate, EVENT_CLASSES  # noqa: E402
from orchestrator.emergency_stop import (  # noqa: E402
    EmergencyStopCoordinator,
    EmergencyStopRefused,
)
from orchestrator.emergency_stop_api import emergency_stop_router  # noqa: E402
from orchestrator.emergency_stop_binding import (  # noqa: E402
    stop_denial,
    submission_gate,
)
from orchestrator.work_admission import (  # noqa: E402
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionCoordinator,
)


OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _token(sub: str, *, extra: dict | None = None) -> str:
    claims = {"sub": sub, "realm_access": {"roles": ["user"]}}
    claims.update(extra or {})
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def _auth(sub: str = OWNER, extra: dict | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(sub, extra=extra)}"}


def _config() -> list[AdmissionClassConfig]:
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
            config_revision="interactive-test",
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


def _schedule_owner(user_id: str = OWNER) -> OperationOwner:
    return OperationOwner(owner_scope=OwnerScope.SCHEDULE, owner_user_id=user_id,
                          connection_scope_id=None)


def _system_owner() -> OperationOwner:
    return OperationOwner(owner_scope=OwnerScope.SYSTEM, owner_user_id=None,
                          connection_scope_id=None)


class _RecordingStop:
    def __init__(self, stopped: set[str] | None = None, broken: bool = False):
        self.stopped = stopped or set()
        self.broken = broken

    def admission_allowed(self, owner_id):
        if self.broken:
            raise RuntimeError("durable state unavailable")
        return owner_id not in self.stopped


def _admission(stop) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=_config(),
        repository=InMemoryWorkAdmissionRepository(),
        clock=lambda: datetime.now(UTC),
        submission_gate=submission_gate(
            SimpleNamespace(emergency_stop=stop)),
    )


def test_emergency_stop_event_class_is_auditable():
    assert "emergency_stop" in EVENT_CLASSES
    event = AuditEventCreate(
        actor_user_id=OWNER,
        auth_principal=OWNER,
        event_class="emergency_stop",
        action_type="emergency_stop.engage",
        description="engage",
        correlation_id="corr-1",
        outcome="success",
        started_at=datetime.now(UTC),
    )
    assert event.event_class == "emergency_stop"


def test_submission_gate_blocks_engaged_owner_at_admission():
    stop = _RecordingStop(stopped={OWNER})
    admission = _admission(stop)
    refused = admission.submit(_request(_user_owner()))
    assert refused.accepted is False
    assert refused.code == "emergency_stop_active"
    assert refused.retryable is False
    allowed = admission.submit(_request(_user_owner(OTHER)))
    assert allowed.accepted is True


def test_submission_gate_blocks_scheduled_work_for_the_owning_user():
    stop = _RecordingStop(stopped={OWNER})
    refused = _admission(stop).submit(_request(_schedule_owner()))
    assert refused.accepted is False
    assert refused.code == "emergency_stop_active"


def test_submission_gate_keeps_system_and_maintenance_effects_available():
    stop = _RecordingStop(stopped={OWNER})
    admission = _admission(stop)
    assert admission.submit(_request(_system_owner())).accepted is True


def test_submission_gate_fails_closed_when_durable_state_is_unreadable():
    admission = _admission(_RecordingStop(broken=True))
    refused = admission.submit(_request(_user_owner()))
    assert refused.accepted is False
    assert refused.code == "emergency_stop_unavailable"


def test_submission_gate_absent_without_a_coordinator():
    assert submission_gate(SimpleNamespace()) is None
    gate = submission_gate(SimpleNamespace(emergency_stop=_RecordingStop()))
    assert gate(_request(_user_owner())) is None
    assert gate(SimpleNamespace(owner=None)) is None
    assert gate(_request(SimpleNamespace(
        owner_scope=OwnerScope.USER, owner_user_id=None))) is None


def test_stop_denial_resolves_owner_and_ignores_legacy():
    stop = _RecordingStop(stopped={OWNER})
    orch = SimpleNamespace(emergency_stop=stop)
    assert stop_denial(orch, OWNER) == "emergency_stop_active"
    assert stop_denial(orch, OTHER) is None
    assert stop_denial(orch, "legacy") is None
    assert stop_denial(orch, None) is None
    assert stop_denial(SimpleNamespace(), OWNER) is None


def _client(coordinator) -> TestClient:
    app = FastAPI()
    app.include_router(emergency_stop_router)
    app.state.orchestrator = SimpleNamespace(emergency_stop=coordinator)
    return TestClient(app, raise_server_exceptions=False)


def _store():
    class _Store:
        def __init__(self):
            self.events = []

        def persist(self, owner_id, action, detail):
            self.events.append((owner_id, action, dict(detail)))

        def load(self, owner_id):
            engaged = next((
                detail for event_owner, action, detail in reversed(self.events)
                if event_owner == owner_id and action == "emergency_stop.engage"), None)
            resumed = any(
                event_owner == owner_id and action == "emergency_stop.resume"
                for event_owner, action, _ in reversed(self.events))
            if engaged is not None and not resumed:
                return {"engaged": True,
                        "revision": int(engaged["revision"]),
                        "engaged_at": 1000.0,
                        "engaged_by": owner_id,
                        "reason": str(engaged.get("reason") or "")}
            return {"engaged": False, "revision": 0}

    return _Store()


def test_rest_status_reflects_running_and_stopped_states():
    store = _store()
    coordinator = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    client = _client(coordinator)
    running = client.get("/api/emergency-stop", headers=_auth())
    assert running.status_code == 200
    assert running.json()["engaged"] is False
    assert running.headers["Cache-Control"] == "no-store"
    engaged = client.post("/api/emergency-stop/stop",
                          json={"reason": "drill"}, headers=_auth())
    assert engaged.status_code == 200
    assert engaged.json()["engaged"] is True
    assert engaged.json()["revision"] == 1
    status = client.get("/api/emergency-stop", headers=_auth())
    assert status.json()["engaged"] is True
    assert status.json()["reason"] == "drill"


def test_rest_requires_the_owners_own_session():
    coordinator = EmergencyStopCoordinator()
    client = _client(coordinator)
    assert client.get("/api/emergency-stop").status_code == 401
    delegated = client.get("/api/emergency-stop", headers=_auth(
        extra={"act": {"sub": "agent-1"}}))
    assert delegated.status_code == 401
    machine = client.get("/api/emergency-stop", headers=_auth(
        extra={"machine_class": "worker"}))
    assert machine.status_code == 403
    engaged = client.post("/api/emergency-stop/stop", json={},
                          headers=_auth(extra={"act": {"sub": "agent-1"}}))
    assert engaged.status_code == 401


def test_rest_resume_is_revision_checked_and_owner_scoped():
    store = _store()
    coordinator = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    client = _client(coordinator)
    client.post("/api/emergency-stop/stop", json={"reason": "drill"}, headers=_auth())
    stale = client.post("/api/emergency-stop/resume",
                        json={"expected_revision": 9}, headers=_auth())
    assert stale.status_code == 409
    assert stale.json()["error"] == "emergency_stop_stale_revision"
    foreign = client.post("/api/emergency-stop/resume",
                          json={"expected_revision": 1}, headers=_auth(OTHER))
    assert foreign.status_code == 409
    assert foreign.json()["error"] == "emergency_stop_not_engaged"
    bad = client.post("/api/emergency-stop/resume",
                      json={"expected_revision": 0}, headers=_auth())
    assert bad.status_code == 422
    resumed = client.post("/api/emergency-stop/resume",
                          json={"expected_revision": 1}, headers=_auth())
    assert resumed.status_code == 200
    assert resumed.json()["engaged"] is False


def test_rest_surfaces_persistence_failure_as_unavailable():
    class _BrokenStore:
        def persist(self, owner_id, action, detail):
            raise OSError("durable store down")

        def load(self, owner_id):
            return {"engaged": False, "revision": 0}

    coordinator = EmergencyStopCoordinator(
        persist=_BrokenStore().persist, load_durable=_BrokenStore().load)
    client = _client(coordinator)
    engaged = client.post("/api/emergency-stop/stop",
                          json={"reason": "drill"}, headers=_auth())
    assert engaged.status_code == 503
    assert engaged.json()["error"] == "emergency_stop_persistence_failed"
    still = client.get("/api/emergency-stop", headers=_auth())
    assert still.json()["engaged"] is True
    assert still.json()["state"] == "persistence_pending"


def test_rest_resume_without_a_stop_conflicts():
    coordinator = EmergencyStopCoordinator(persist=lambda *a: None,
                                           load_durable=lambda owner: None)
    client = _client(coordinator)
    missing = client.post("/api/emergency-stop/resume",
                          json={"expected_revision": 1}, headers=_auth())
    assert missing.status_code == 409
    assert missing.json()["error"] == "emergency_stop_not_engaged"


def test_rest_verify_re_arms_after_restart():
    store = _store()
    first = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    assert client_engage(first) == 1
    survivor = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    client = _client(survivor)
    verified = client.post("/api/emergency-stop/verify", headers=_auth())
    assert verified.status_code == 200
    assert verified.json()["engaged"] is True
    assert verified.json()["revision"] == 1


def client_engage(coordinator) -> int:
    client = _client(coordinator)
    response = client.post("/api/emergency-stop/stop",
                           json={"reason": "drill"}, headers=_auth())
    assert response.status_code == 200
    return response.json()["revision"]


def test_rest_reports_unavailable_coordinator():
    app = FastAPI()
    app.include_router(emergency_stop_router)
    app.state.orchestrator = SimpleNamespace(emergency_stop=None)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/emergency-stop", headers=_auth()).status_code == 503


def test_durable_round_trip_through_the_binding_persistence():
    from orchestrator.emergency_stop_binding import build_loader, build_persistence

    class _Repo:
        def __init__(self):
            self.rows: list = []

        def insert(self, event):
            from audit.schemas import AuditEventDTO

            dto = AuditEventDTO(
                event_id=str(uuid.uuid4()),
                event_class=event.event_class,
                action_type=event.action_type,
                description=event.description,
                correlation_id=event.correlation_id,
                outcome=event.outcome,
                inputs_meta=event.inputs_meta,
                started_at=event.started_at,
                recorded_at=event.started_at,
            )
            self.rows.append(dto)
            return dto

        def list_for_user(self, owner_id, **_kwargs):
            return list(reversed(self.rows)), None

    orch = SimpleNamespace(audit_repo=_Repo())
    persist = build_persistence(orch)
    load = build_loader(orch)
    coordinator = EmergencyStopCoordinator(persist=persist, load_durable=load)
    engaged = asyncio.run(coordinator.engage(OWNER, reason="round trip"))
    assert engaged["persisted"] is True
    assert coordinator.admission_allowed(OWNER) is False
    resumed = asyncio.run(coordinator.resume(
        OWNER, expected_revision=engaged["revision"], actor_id=OWNER))
    assert resumed["engaged"] is False
    assert coordinator.admission_allowed(OWNER) is True


class _RefusingCoordinator:
    def __init__(self, code, status_code):
        self.code = code
        self.status_code = status_code

    async def resume(self, owner_id, *, expected_revision, actor_id=None, claims=None):
        raise EmergencyStopRefused(self.code, self.status_code)

    async def engage(self, owner_id, *, reason=None, claims=None):
        raise EmergencyStopRefused(self.code, self.status_code)

    def refresh(self, owner_id):
        raise EmergencyStopRefused(self.code, self.status_code)

    def status(self, owner_id):
        return {"engaged": False, "state": "running"}


def test_rest_maps_coordinator_denials_to_status_codes():
    for code, expected in (
        ("emergency_stop_resume_denied", 403),
        ("emergency_stop_invalid", 422),
        ("emergency_stop_stale_revision", 409),
    ):
        client = _client(_RefusingCoordinator(code, expected))
        response = client.post("/api/emergency-stop/resume",
                               json={"expected_revision": 1}, headers=_auth())
        assert response.status_code == expected, code
        assert response.json() == {"error": code}


def test_rest_verify_reports_durable_unavailable():
    client = _client(_RefusingCoordinator("emergency_stop_unavailable", 503))
    response = client.post("/api/emergency-stop/verify", headers=_auth())
    assert response.status_code == 503
    assert response.json() == {"error": "emergency_stop_unavailable"}


def test_rest_rejects_claims_without_a_subject():
    client = _client(EmergencyStopCoordinator())
    body = base64.urlsafe_b64encode(json.dumps(
        {"realm_access": {"roles": ["user"]}}).encode()).decode().rstrip("=")
    response = client.get(
        "/api/emergency-stop",
        headers={"Authorization": f"Bearer header.{body}.signature"})
    assert response.status_code == 401


def test_rest_falls_back_to_the_root_app_state():
    inner = FastAPI()
    inner.include_router(emergency_stop_router)
    coordinator = EmergencyStopCoordinator()
    inner._root_app = SimpleNamespace(state=SimpleNamespace(
        orchestrator=SimpleNamespace(emergency_stop=coordinator)))
    client = TestClient(inner, raise_server_exceptions=False)
    status = client.get("/api/emergency-stop", headers=_auth())
    assert status.status_code == 200
    assert status.json()["engaged"] is False
