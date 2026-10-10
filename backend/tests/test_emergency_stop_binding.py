"""Host-wiring coverage for the emergency-stop binding: audit-backed persistence and
re-arm loader, denial auditing, the interruption hook, mount, and the fail-closed
admission guards.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from audit.schemas import AuditEventDTO  # noqa: E402
from orchestrator.emergency_stop import (  # noqa: E402
    EmergencyStopCoordinator,
    EmergencyStopRefused,
)
from orchestrator.emergency_stop_binding import (  # noqa: E402
    EVENT_CLASS,
    background_admission_guard,
    build_denial_audit,
    build_interrupt,
    build_loader,
    build_persistence,
    mount,
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


OWNER = "binding-owner-1"


class _FakeAuditRepo:
    def __init__(self):
        self.rows: list[AuditEventDTO] = []

    def insert(self, event) -> AuditEventDTO:
        dto = AuditEventDTO(
            event_id=str(uuid.uuid4()),
            event_class=event.event_class,
            action_type=event.action_type,
            description=event.description,
            correlation_id=event.correlation_id,
            outcome=event.outcome,
            inputs_meta=event.inputs_meta,
            outputs_meta=event.outputs_meta,
            started_at=event.started_at,
            recorded_at=event.started_at,
        )
        self.rows.append((event.actor_user_id, dto))
        return dto

    def last(self) -> AuditEventDTO:
        return self.rows[-1][1]

    def list_for_user(self, owner_id, **_kwargs):
        return [dto for event_owner, dto in reversed(self.rows)
                if event_owner == owner_id], None


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


def test_persistence_writes_a_durable_audit_event():
    repo = _FakeAuditRepo()
    persist = build_persistence(SimpleNamespace(audit_repo=repo))
    persist(OWNER, "emergency_stop.engage",
            {"revision": 2, "reason": "drill", "engaged_by": OWNER})
    assert len(repo.rows) == 1
    event = repo.last()
    assert event.event_class == EVENT_CLASS
    assert event.action_type == "emergency_stop.engage"
    assert event.outcome == "success"
    assert event.inputs_meta["revision"] == 2
    assert event.inputs_meta["engaged_by"] == OWNER
    assert event.started_at.tzinfo is not None


def test_persistence_requires_the_audit_repository():
    persist = build_persistence(SimpleNamespace())
    with pytest_runtime_error():
        persist(OWNER, "emergency_stop.engage", {})


def pytest_runtime_error():
    import pytest

    return pytest.raises(RuntimeError)


def test_loader_replays_engage_and_resume_events():
    repo = _FakeAuditRepo()
    orch = SimpleNamespace(audit_repo=repo)
    persist = build_persistence(orch)
    load = build_loader(orch)
    assert load(OWNER) == {"engaged": False, "revision": 0}
    persist(OWNER, "emergency_stop.engage",
            {"revision": 3, "reason": "drill", "engaged_by": OWNER,
             "engaged_at": "2026-10-10T00:00:00+00:00"})
    engaged = load(OWNER)
    assert engaged["engaged"] is True
    assert engaged["revision"] == 3
    assert engaged["engaged_by"] == OWNER
    assert engaged["reason"] == "drill"
    assert engaged["engaged_at"] == repo.last().recorded_at.timestamp()
    persist(OWNER, "emergency_stop.resume", {"revision": 3})
    resumed = load(OWNER)
    assert resumed == {"engaged": False, "revision": 3}


def test_loader_ignores_failure_outcomes_and_foreign_owners():
    repo = _FakeAuditRepo()
    persist = build_persistence(SimpleNamespace(audit_repo=repo))
    persist(OWNER, "emergency_stop.engage", {"revision": 1})
    repo.last().outcome = "failure"
    assert build_loader(SimpleNamespace(audit_repo=repo))(OWNER) == {
        "engaged": False, "revision": 0}
    other = "binding-owner-2"
    persist(other, "emergency_stop.engage", {"revision": 5})
    assert build_loader(SimpleNamespace(audit_repo=repo))(OWNER) == {
        "engaged": False, "revision": 0}
    assert build_loader(SimpleNamespace(audit_repo=repo))(other)["revision"] == 5


def test_loader_tolerates_malformed_metadata():
    repo = _FakeAuditRepo()
    persist = build_persistence(SimpleNamespace(audit_repo=repo))
    persist(OWNER, "emergency_stop.engage", {"revision": "not-a-number"})
    repo.last().inputs_meta = {"revision": "bogus", "engaged_at": "garbage"}
    engaged = build_loader(SimpleNamespace(audit_repo=repo))(OWNER)
    assert engaged["engaged"] is True
    assert engaged["revision"] == 1
    assert engaged["engaged_at"] == repo.last().recorded_at.timestamp()
    repo.last().recorded_at = None
    assert build_loader(SimpleNamespace(audit_repo=repo))(OWNER)["engaged_at"] is None


def test_event_time_handles_naive_and_bad_timestamps():
    from orchestrator.emergency_stop_binding import _event_time

    naive = SimpleNamespace(recorded_at=datetime(2026, 10, 10, 12, 0, 0),
                            inputs_meta={})
    expected = datetime(2026, 10, 10, 12, 0, 0,
                        tzinfo=timezone.utc).timestamp()
    assert _event_time(naive) == expected
    assert _event_time(SimpleNamespace(recorded_at="nope", inputs_meta={})) is None
    assert _event_time(SimpleNamespace(
        recorded_at=None,
        inputs_meta={"engaged_at": "2026-10-10T12:00:00+00:00"})) == expected
    assert _event_time(SimpleNamespace(
        recorded_at=None,
        inputs_meta={"engaged_at": "2026-10-10T12:00:00"})) == expected
    assert _event_time(SimpleNamespace(
        recorded_at=None, inputs_meta={"engaged_at": "garbage"})) is None


def test_denial_audit_records_failure_outcomes():
    recorded: list[dict] = []

    async def fake_record_generic(**kwargs):
        recorded.append(kwargs)

    import audit.hooks as hooks_module

    original = hooks_module.record_generic
    hooks_module.record_generic = fake_record_generic
    try:
        record = build_denial_audit(SimpleNamespace())
        asyncio.run(record("emergency_stop.resume", "failure", owner_id=OWNER,
                           detail={"denial": "emergency_stop_resume_denied"}))
    finally:
        hooks_module.record_generic = original
    assert recorded[0]["event_class"] == EVENT_CLASS
    assert recorded[0]["outcome"] == "failure"
    assert recorded[0]["outcome_detail"] == "emergency_stop_resume_denied"
    assert recorded[0]["claims"] == {"sub": OWNER}


def test_interrupt_cancels_tasks_and_pauses_sessions():
    cancelled: list[str] = []
    paused: list[str] = []

    class _Manager:
        async def list_for_user(self, user_id, limit):
            assert limit >= 1
            return [SimpleNamespace(task_id="task-1"),
                    SimpleNamespace(task_id="task-2")]

        async def cancel(self, task_id):
            cancelled.append(task_id)
            return task_id == "task-1"

    class _Sessions:
        def live_for_owner(self, owner_id):
            assert owner_id == OWNER
            return [SimpleNamespace(active=lambda: True),
                    SimpleNamespace(active=lambda: False)]

        async def pause(self, session, reason, *, push):
            paused.append(reason)
            assert push is False

    interrupt = build_interrupt(SimpleNamespace(
        async_task_manager=_Manager(), computer_sessions=_Sessions()))
    counts = asyncio.run(interrupt(OWNER))
    assert counts == {"tasks": 1, "sessions": 1}
    assert cancelled == ["task-1", "task-2"]
    assert paused == ["emergency_stop"]


def test_interrupt_tolerates_manager_and_session_failures():
    class _BrokenManager:
        async def list_for_user(self, user_id, limit):
            return [SimpleNamespace(task_id="task-1")]

        async def cancel(self, task_id):
            raise RuntimeError("cancel unavailable")

    class _BrokenSessions:
        def live_for_owner(self, owner_id):
            return [SimpleNamespace(active=lambda: True)]

        async def pause(self, session, reason, *, push):
            raise RuntimeError("pause unavailable")

    interrupt = build_interrupt(SimpleNamespace(
        async_task_manager=_BrokenManager(), computer_sessions=_BrokenSessions()))
    assert asyncio.run(interrupt(OWNER)) == {"tasks": 0, "sessions": 0}
    empty = build_interrupt(SimpleNamespace())
    assert asyncio.run(empty(OWNER)) == {"tasks": 0, "sessions": 0}


def test_mount_wires_the_full_coordinator():
    repo = _FakeAuditRepo()
    cancelled: list[str] = []

    class _Manager:
        async def list_for_user(self, user_id, limit):
            return [SimpleNamespace(task_id="t-1")]

        async def cancel(self, task_id):
            cancelled.append(task_id)
            return True

    orch = SimpleNamespace(audit_repo=repo, async_task_manager=_Manager())
    coordinator = mount(orch)
    assert orch.emergency_stop is coordinator
    status = asyncio.run(coordinator.engage(OWNER, reason="mount"))
    assert status["persisted"] is True
    assert status["interrupted"] == {"tasks": 1, "sessions": 0}
    assert cancelled == ["t-1"]
    assert coordinator.admission_allowed(OWNER) is False


def test_background_admission_guard_reports_stopped_owners():
    class _Stop:
        def admission_allowed(self, owner_id):
            return owner_id != OWNER

    guard = background_admission_guard(SimpleNamespace(emergency_stop=_Stop()))
    assert asyncio.run(guard(OWNER)) == "emergency_stop_active"
    assert asyncio.run(guard("binding-owner-2")) is None
    assert asyncio.run(guard("legacy")) is None


def test_guards_fail_closed_when_the_coordinator_raises():
    class _BrokenStop:
        def admission_allowed(self, owner_id):
            raise RuntimeError("durable state unavailable")

    orch = SimpleNamespace(emergency_stop=_BrokenStop())
    assert stop_denial(orch, OWNER) == "emergency_stop_unavailable"
    gate = submission_gate(orch)
    assert gate(_request(_user_owner())) == "emergency_stop_unavailable"


def test_submission_gate_via_binding_blocks_only_the_stopped_owner():
    class _Stop:
        def __init__(self):
            self.stopped = {OWNER}

        def admission_allowed(self, owner_id):
            return owner_id not in self.stopped

    admission = WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=None,
                active_limit=2,
                queue_limit=0,
                max_wait_ms=None,
                config_revision="binding-test",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=lambda: datetime.now(UTC),
        submission_gate=submission_gate(
            SimpleNamespace(emergency_stop=_Stop())),
    )
    assert admission.submit(_request(_user_owner())).accepted is False
    assert admission.submit(
        _request(_user_owner("binding-owner-2"))).accepted is True


def test_end_to_end_restart_cycle_through_binding_wiring():
    repo = _FakeAuditRepo()
    orch = SimpleNamespace(audit_repo=repo)
    first = mount(orch)
    engaged = asyncio.run(first.engage(OWNER, reason="cycle"))
    assert engaged["revision"] == 1
    survivor = mount(orch)
    assert survivor.admission_allowed(OWNER) is False
    resumed = asyncio.run(survivor.resume(
        OWNER, expected_revision=1, actor_id=OWNER))
    assert resumed["engaged"] is False
    assert survivor.admission_allowed(OWNER) is True
    next_cycle = asyncio.run(survivor.engage(OWNER, reason="second"))
    assert next_cycle["revision"] == 2


def test_denial_audit_hook_failure_does_not_break_resume():
    repo = _FakeAuditRepo()

    async def broken(**kwargs):
        raise RuntimeError("audit sink down")

    import audit.hooks as hooks_module

    original = hooks_module.record_generic
    hooks_module.record_generic = broken
    try:
        coordinator = EmergencyStopCoordinator(
            persist=build_persistence(SimpleNamespace(audit_repo=repo)),
            load_durable=build_loader(SimpleNamespace(audit_repo=repo)),
            audit_denial=build_denial_audit(SimpleNamespace()),
        )
    finally:
        hooks_module.record_generic = original
    asyncio.run(coordinator.engage(OWNER))
    with pytest_denial_refused():
        asyncio.run(coordinator.resume(OWNER, expected_revision=9, actor_id=OWNER))
    assert coordinator.admission_allowed(OWNER) is False


def pytest_denial_refused():
    import pytest

    return pytest.raises(EmergencyStopRefused)
