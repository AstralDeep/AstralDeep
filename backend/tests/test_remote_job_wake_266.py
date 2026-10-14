"""Tests for remote-job completion wakeups (orchestrator/remote_jobs.py +
orchestrator/work_continuations.py): the outcome classifier, subscriber
discovery, bounded-retry wake delivery, the atomic terminal apply+wake, the
owner wait-job subscription API, and the real Plane seams against Postgres.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from astralplane.repositories import RepositoryConflictError, RepositoryError
from astralplane.repositories.tracked_jobs import TrackedJobRecord
from persistent_agents.tests.test_engine_postgres import plane as plane
from tests.test_request_session_authority_088 import signing_key as signing_key
from tests.test_work_continuations_postgres_088 import current as _current
from tests.test_work_continuations_postgres_088 import read_records as read_records
from tests.test_work_continuations_postgres_088 import value as value

from orchestrator import remote_jobs as rj
from orchestrator.plane_repository_context import ApplicationPlaneSource
from orchestrator.work_continuations import WorkContinuationService, WorkJobWaitRequest
from orchestrator.remote_transport import MachineTarget, RemoteResult, Verdict, set_transport
from persistent_agents.runtime_values import digest as _rv_digest
from tests.helpers.remote_plane_runtime import make_remote_wake_plane_source

OWNER = "owner"
MACHINE = "a" * 32
TRACKED = "b" * 32
SCHEDULER_JOB = "4242"


def _event_key(tracked_job_id: str = TRACKED) -> str:
    return rj.WAKE_EVENT_PREFIX + tracked_job_id


def _wake_plane(*subscribers):
    storage = SimpleNamespace(jobs={}, machines={}, credentials={})
    source = make_remote_wake_plane_source(storage)
    plane = SimpleNamespace(
        storage=storage, plane_runtime=source.plane_runtime,
        plane_repositories=source.plane_repositories)
    assignments = plane.plane_runtime.repositories.assignments
    for row in subscribers:
        assignments.register_subscriber(row)
    return plane


def _subscriber(assignment_id="a1", *, owner=OWNER, event_key=None, revision=100,
                state_version=1, phase="awaiting_event", lifecycle="active"):
    return {
        "assignment_id": assignment_id, "owner_id": owner,
        "operation": {"control": {"wait": {"event_key": event_key or _event_key(),
                                           "source_revision": revision}}},
        "state_version": state_version, "instruction_revision": 1,
        "control_epoch": 1, "lifecycle": lifecycle, "phase": phase,
        "wake_generation": 0,
    }


def _job_row(*, owner=OWNER, tracked=TRACKED, state="running", terminal=False,
             fail_count=0, last_polled_at=100, created_at=50, scheduler_job_id=SCHEDULER_JOB):
    return {"tracked_job_id": tracked, "owner_user_id": owner, "machine_id": MACHINE,
            "scheduler_job_id": scheduler_job_id, "state": state, "terminal": terminal,
            "fail_count": fail_count, "last_polled_at": last_polled_at, "created_at": created_at}


def _tracked_record(row, *, last_polled_at=None):
    return TrackedJobRecord(
        tracked_job_id=row["tracked_job_id"], owner_id=row["owner_user_id"],
        machine_id=row["machine_id"], scheduler_job_id=row["scheduler_job_id"],
        conversation_id=None, submit_marker=None, output_path=None,
        component_id=None, job_name="", state="submitted", exit_code=None,
        terminal=False, notify_on_finish=False, notified=False, fail_count=0,
        created_at=row["created_at"], last_polled_at=last_polled_at, finished_at=None)


def _seed_job(plane, row):
    with plane.plane_runtime.transaction() as tx:
        plane.plane_runtime.repositories.tracked_jobs.create(
            tx, _tracked_record(row, last_polled_at=row["last_polled_at"]))


@pytest.fixture(autouse=True)
def isolate_unit_audit_boundary(monkeypatch):
    from tests.helpers.remote_plane_runtime import InMemoryPlaneRuntime

    real_audit = rj._audit_completion_wakes

    def append(runtime, tx, **metadata):
        if isinstance(runtime, InMemoryPlaneRuntime):
            return
        return real_audit(runtime, tx, **metadata)

    monkeypatch.setattr(rj, "_audit_completion_wakes", append)


class TestJobOutcome:
    def test_authoritative_success(self):
        assert rj._job_outcome("COMPLETED", "0:0") == "completed"
        assert rj._job_outcome("COMPLETED", "") == "completed"

    def test_completed_with_nonzero_exit_is_a_failure(self):
        assert rj._job_outcome("COMPLETED", "2:0") == "failed"

    def test_scheduler_failures_are_failures(self):
        for state in ("FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "DEADLINE"):
            assert rj._job_outcome(state, "1:0") == "failed"

    def test_cancellation_is_its_own_outcome(self):
        assert rj._job_outcome("CANCELLED by 7", None) == "cancelled"
        assert rj._job_outcome("REVOKED", None) == "cancelled"

    def test_orphaned_is_not_completed(self):
        assert rj._job_outcome("orphaned", None) == "orphaned"

    def test_heuristic_completion_is_unconfirmed(self):
        assert rj._job_outcome("COMPLETED", "", confirmed=False) == "unconfirmed"


class TestProbeConfirmationMarker:
    def test_sacct_completion_is_confirmed(self, monkeypatch):
        monkeypatch.setattr("orchestrator.remote_machines.build_target",
                            lambda *a, **k: MachineTarget(
                                machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                                username="me", cred_type="password", secret="x",
                                host_key_fingerprint="SHA256:pinned"))
        set_transport(_Scripted({"squeue": (_SQUEUE_EMPTY, 0), "sacct": (_SACCT_DONE, 0)}))
        out = rj._probe_state(_orch(), _row(state="running"))
        assert out["confirmed"] is True and out["terminal"] is True

    def test_gone_from_both_is_unconfirmed(self, monkeypatch):
        monkeypatch.setattr("orchestrator.remote_machines.build_target",
                            lambda *a, **k: MachineTarget(
                                machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                                username="me", cred_type="password", secret="x",
                                host_key_fingerprint="SHA256:pinned"))
        set_transport(_Scripted({"squeue": (_SQUEUE_EMPTY, 0), "sacct": (_SQUEUE_EMPTY, 0)}))
        out = rj._probe_state(_orch(), _row(state="running"))
        assert out == {"transient": True, "machine": "dgx"}


class TestWakeSubscribers:
    def test_matching_subscriber_wakes_with_bound_revision(self):
        plane = _wake_plane(_subscriber())
        row = _job_row()
        _seed_job(plane, row)
        record = rj._apply_and_wake(plane, row, state="COMPLETED", exit_code="0:0",
                                    outcome_name="completed")
        sub = plane.storage.assignments["a1"]
        assert sub["phase"] == "waiting" and sub["wake_generation"] == 1
        assert sub["operation"]["control"]["wait"] is None
        assert sub["watermarks"][_event_key()] == record.finished_at
        assert plane.storage.jobs[TRACKED]["terminal"] is True

    def test_atomicity_both_or_neither(self):
        plane = _wake_plane(_subscriber())
        row = _job_row()
        _seed_job(plane, row)
        rj._apply_and_wake(plane, row, state="FAILED", exit_code="1:0",
                           outcome_name="failed")
        assert plane.storage.jobs[TRACKED]["terminal"] is True
        assert plane.storage.assignments["a1"]["phase"] == "waiting"

    def test_repeated_delivery_is_a_replay(self):
        plane = _wake_plane(_subscriber())
        row = _job_row()
        _seed_job(plane, row)
        rj._apply_and_wake(plane, row, state="COMPLETED", exit_code="0:0",
                           outcome_name="completed")
        before = plane.storage.assignments["a1"]["wake_generation"]
        woken, skipped = rj._wake_subscribers(
            plane.plane_runtime.repositories.assignments,
            plane.plane_runtime.transaction().__enter__(),
            owner_id=OWNER, event_key=_event_key(), event_id="rj-wake:" + TRACKED,
            revision=plane.storage.jobs[TRACKED]["finished_at"],
            event_digest="same")
        assert woken == [] and skipped == []
        assert plane.storage.assignments["a1"]["wake_generation"] == before

    def test_late_event_conflicts_and_is_skipped(self):
        plane = _wake_plane(_subscriber())
        assignments = plane.plane_runtime.repositories.assignments
        woken, skipped = rj._wake_subscribers(
            assignments, plane.plane_runtime.transaction().__enter__(),
            owner_id=OWNER, event_key=_event_key(), event_id="rj-wake-late",
            revision=50, event_digest="stale")
        assert woken == [] and len(skipped) == 1

    def test_outcome_is_bound_into_the_event_digest(self):
        plane = _wake_plane(_subscriber())
        row = _job_row()
        _seed_job(plane, row)
        assignments = plane.plane_runtime.repositories.assignments
        rj._apply_and_wake(plane, row, state="COMPLETED", exit_code="0:0",
                           outcome_name="completed")
        skipped = []
        rj._try_wake(assignments, plane.plane_runtime.transaction().__enter__(),
                     owner_id=OWNER, assignment_id="a1",
                     event_id="rj-wake:" + TRACKED, event_key=_event_key(),
                     revision=plane.storage.jobs[TRACKED]["finished_at"],
                     event_digest="different-outcome", woken=[], skipped=skipped)
        assert "idempotency" in skipped[0]["reason"]

    def test_wrong_event_key_subscriber_is_ignored(self):
        plane = _wake_plane(_subscriber(event_key="owner:" + uuid4().hex))
        row = _job_row()
        _seed_job(plane, row)
        rj._apply_and_wake(plane, row, state="COMPLETED", exit_code="0:0",
                           outcome_name="completed")
        assert plane.storage.assignments["a1"]["phase"] == "awaiting_event"

    def test_wrong_owner_subscriber_is_never_scanned(self):
        plane = _wake_plane(_subscriber(owner="someone-else"))
        row = _job_row()
        _seed_job(plane, row)
        rj._apply_and_wake(plane, row, state="COMPLETED", exit_code="0:0",
                           outcome_name="completed")
        assert plane.storage.assignments["a1"]["phase"] == "awaiting_event"

    def test_subscribers_across_pages_all_wake(self):
        subs = [_subscriber(f"a{i:03d}") for i in range(150)]
        plane = _wake_plane(*subs)
        row = _job_row()
        _seed_job(plane, row)
        rj._apply_and_wake(plane, row, state="COMPLETED", exit_code="0:0",
                           outcome_name="completed")
        assert all(plane.storage.assignments[s["assignment_id"]]["phase"] == "waiting"
                   for s in subs)

    def test_transient_repository_error_retries_bounded_then_raises(self):
        attempts = []

        class _Flaky:
            def list_operations(self, tx, *, owner_id, limit=100, after_id=None):
                return [SimpleNamespace(assignment=SimpleNamespace(**_subscriber()))]

            def get_operation(self, tx, *, owner_id, assignment_id):
                return SimpleNamespace(assignment=SimpleNamespace(**_subscriber()))

            def accept_wake(self, tx, **kwargs):
                attempts.append(kwargs)
                raise RepositoryError("pool exhausted")

        plane = _wake_plane(_subscriber())
        row = _job_row()
        _seed_job(plane, row)
        with pytest.raises(RepositoryError):
            rj._wake_subscribers(_Flaky(), plane.plane_runtime.transaction().__enter__(),
                                 owner_id=OWNER, event_key=_event_key(),
                                 event_id="rj-wake:" + TRACKED, revision=2000,
                                 event_digest="d")
        assert len(attempts) == rj._WAKE_RETRIES + 1

    def test_permanent_conflict_skips_without_retry(self):
        attempts = []

        class _Refusing:
            def list_operations(self, tx, *, owner_id, limit=100, after_id=None):
                return [SimpleNamespace(assignment=SimpleNamespace(**_subscriber()))]

            def get_operation(self, tx, *, owner_id, assignment_id):
                return SimpleNamespace(assignment=SimpleNamespace(**_subscriber()))

            def accept_wake(self, tx, **kwargs):
                attempts.append(kwargs)
                raise RepositoryConflictError("assignment not waiting")

        plane = _wake_plane(_subscriber())
        row = _job_row()
        _seed_job(plane, row)
        woken, skipped = rj._wake_subscribers(
            _Refusing(), plane.plane_runtime.transaction().__enter__(),
            owner_id=OWNER, event_key=_event_key(), event_id="rj-wake:" + TRACKED,
            revision=2000, event_digest="d")
        assert woken == [] and len(attempts) == 1
        assert skipped[0]["assignment_id"] == "a1"


class TestPollOneWakeIntegration:
    @pytest.fixture(autouse=True)
    def _silent_fanout(self, monkeypatch):
        async def _push(orch, row, **kw):
            return None
        async def _notify(orch, row, state, exit_code):
            return None
        monkeypatch.setattr(rj, "_push_component", _push)
        monkeypatch.setattr(rj, "_notify_finish", _notify)

    async def test_terminal_poll_wakes_and_classifies(self, monkeypatch):
        plane = _wake_plane(_subscriber())
        row = _job_row(last_polled_at=100)
        _seed_job(plane, row)
        monkeypatch.setattr(rj, "_probe_state",
                            lambda o, r: {"state": "COMPLETED", "terminal": True,
                                          "exit_code": "0:0", "confirmed": True})
        await rj._poll_one(_orch(plane), dict(row))
        assert plane.storage.assignments["a1"]["phase"] == "waiting"
        assert plane.storage.jobs[TRACKED]["state"] == "COMPLETED"

    async def test_unconfirmed_poll_preserves_unresolved_tracking_without_a_wake(self, monkeypatch):
        plane = _wake_plane(_subscriber())
        row = _job_row(last_polled_at=100)
        _seed_job(plane, row)
        monkeypatch.setattr(rj, "_probe_state",
                            lambda o, r: {"state": "COMPLETED", "terminal": True,
                                          "exit_code": "", "confirmed": False})
        await rj._poll_one(_orch(plane), dict(row))
        assert plane.storage.jobs[TRACKED]["terminal"] is False
        assert plane.storage.jobs[TRACKED]["fail_count"] == 1
        assert plane.storage.assignments["a1"]["phase"] == "awaiting_event"
        assert plane.storage.assignments["a1"].get("wake_receipts", {}) == {}

    async def test_orphan_wakes_subscriber_without_counting_as_completed(self, monkeypatch):
        plane = _wake_plane(_subscriber())
        row = _job_row(last_polled_at=100)
        _seed_job(plane, row)
        monkeypatch.setattr(rj, "_probe_state", lambda o, r: {"orphan": True})
        await rj._poll_one(_orch(plane), dict(row))
        assert plane.storage.jobs[TRACKED]["state"] == "orphaned"
        assert plane.storage.assignments["a1"]["phase"] == "waiting"

    async def test_non_terminal_poll_does_not_wake(self, monkeypatch):
        plane = _wake_plane(_subscriber())
        row = _job_row(last_polled_at=100)
        _seed_job(plane, row)
        monkeypatch.setattr(rj, "_probe_state",
                            lambda o, r: {"state": "RUNNING", "terminal": False,
                                          "exit_code": None, "confirmed": True})
        await rj._poll_one(_orch(plane), dict(row))
        assert plane.storage.assignments["a1"]["phase"] == "awaiting_event"


class TestWorkJobWaitRequestValidation:
    def _body(self, **changes):
        fields = {"expected_revision": 1, "submission_id": str(uuid4()),
                  "tracked_job_id": TRACKED, "machine_id": MACHINE,
                  "scheduler_job_id": SCHEDULER_JOB}
        fields.update(changes)
        return WorkJobWaitRequest(**fields)

    def test_valid_body(self):
        self._body()

    def test_non_hex_tracked_job_id_rejected(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            self._body(tracked_job_id="g" * 32)

    def test_dashed_32_char_id_rejected(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            self._body(tracked_job_id="g" * 15 + "-" + "g" * 16)

    def test_dashed_uuid_rejected(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            self._body(tracked_job_id=str(uuid4()))

    def test_non_numeric_scheduler_job_rejected(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            self._body(scheduler_job_id="4242x")

    def test_extra_field_rejected(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            self._body(surprise=1)


class _Scripted:
    def __init__(self, table):
        self.table = table

    def run(self, target, argv, *, timeout, retryable=False):
        r = self.table.get(argv[0])
        stdout, exit_status = r if not isinstance(r, Verdict) else ("", 0)
        return RemoteResult(verdict=Verdict.OK, machine=target.label,
                            stdout=stdout, exit_status=exit_status)


_SQUEUE_EMPTY = '{"jobs":[]}'
_SACCT_DONE = ('{"jobs":[{"job_id":5,"name":"t","state":{"current":["COMPLETED"]},'
               '"time":{"elapsed":60},"exit_code":{"return_code":{"number":0}},"partition":"gpu"}]}')


def _orch(db=None):
    return SimpleNamespace(plane_repository_source=db or SimpleNamespace(),
                           credential_manager=object())


def _row(**over):
    r = {"tracked_job_id": TRACKED, "scheduler_job_id": SCHEDULER_JOB, "state": "running",
         "terminal": False, "owner_user_id": OWNER, "machine_id": MACHINE, "chat_id": "c1",
         "component_id": "au_rjob_5", "notify_on_finish": False, "notified": False,
         "fail_count": 0, "output_path": None}
    r.update(over)
    return r


@pytest.fixture(autouse=True)
def _reset_transport():
    yield
    set_transport(None)





def _hex32(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


@pytest.fixture
async def job_value(value):

    orch = value.reads.assignments.orch
    orch.plane_repository_source = ApplicationPlaneSource(
        plane_runtime=value.runtime, plane_repositories=value.runtime.repositories)
    yield value


def _plane_job_db(value):
    return SimpleNamespace(plane_runtime=value.runtime,
                           plane_repositories=value.runtime.repositories)


def _create_tracked_job(value, *, owner=OWNER, scheduler_job_id=SCHEDULER_JOB,
                        created_at=1000, terminal=False):
    record = TrackedJobRecord(
        tracked_job_id=_hex32("tracked-" + scheduler_job_id), owner_id=owner,
        machine_id=MACHINE, scheduler_job_id=scheduler_job_id, conversation_id=None,
        submit_marker=None, output_path=None, component_id=None, job_name="train",
        state="running" if not terminal else "COMPLETED", exit_code=None,
        terminal=terminal, notify_on_finish=False, notified=False, fail_count=0,
        created_at=created_at, last_polled_at=created_at,
        finished_at=created_at if terminal else None)
    with value.runtime.transaction() as tx:
        value.runtime.repositories.tracked_jobs.create(tx, record)
    return record


async def _job_wait_body(value, **changes):
    from orchestrator.work_continuations import WorkJobWaitRequest as W
    record = await _current(value)
    fields = {"expected_revision": record.state_version, "submission_id": str(uuid4()),
              "tracked_job_id": _hex32("tracked-" + SCHEDULER_JOB),
              "machine_id": MACHINE, "scheduler_job_id": SCHEDULER_JOB}
    fields.update(changes)
    return W(**fields)


async def test_wait_job_subscribes_with_job_source_and_revision(job_value):
    value = job_value
    _create_tracked_job(value)
    service = WorkContinuationService(value.reads.assignments)
    body = await _job_wait_body(value)
    reply = await service.wait_job(value.identity, body, caller=value.caller)
    assert reply["applied"] is True and reply["operation"]["phase"] == "awaiting_event"
    record = await _current(value)
    wait = record.operation["control"]["wait"]
    assert wait == {"event_key": rj.WAKE_EVENT_PREFIX + _hex32("tracked-" + SCHEDULER_JOB),
                    "source_revision": 1000}


async def test_wait_job_replay_is_idempotent_and_audits_once(job_value):
    value = job_value
    _create_tracked_job(value)
    service = WorkContinuationService(value.reads.assignments)
    body = await _job_wait_body(value)
    first = await service.wait_job(value.identity, body, caller=value.caller)
    second = await service.wait_job(value.identity, body, caller=value.caller)
    assert first["applied"] is True and second["applied"] is False
    events, _ = value.audit.list_for_user(OWNER)
    assert [e.action_type for e in events].count("assignment_wait") == 1
    assert value.audit.verify_chain(OWNER) is None


async def test_wait_job_denies_unknown_mismatched_terminal_and_foreign_jobs(job_value):
    value = job_value
    _create_tracked_job(value)
    service = WorkContinuationService(value.reads.assignments)
    from persistent_agents.models import AssignmentError

    with pytest.raises(AssignmentError) as error:
        await service.wait_job(value.identity,
                               await _job_wait_body(value, scheduler_job_id="999999"),
                               caller=value.caller)
    assert (error.value.code, error.value.status_code) == ("work_not_found", 404)

    with pytest.raises(AssignmentError) as error:
        await service.wait_job(value.identity,
                               await _job_wait_body(value, tracked_job_id=_hex32("other")),
                               caller=value.caller)
    assert error.value.code == "work_not_found"

    with pytest.raises(AssignmentError) as error:
        await service.wait_job(value.identity,
                               await _job_wait_body(value, machine_id=_hex32("other-machine")),
                               caller=value.caller)
    assert error.value.code == "work_not_found"

    _create_tracked_job(value, scheduler_job_id="777", terminal=True)
    with pytest.raises(AssignmentError) as error:
        await service.wait_job(value.identity,
                               await _job_wait_body(value, scheduler_job_id="777",
                                              tracked_job_id=_hex32("tracked-777")),
                               caller=value.caller)
    assert (error.value.code, error.value.status_code) == ("work_job_already_terminal", 409)


async def test_wait_job_never_sees_another_owners_job(job_value):
    value = job_value
    _create_tracked_job(value, owner="other", scheduler_job_id="888")
    service = WorkContinuationService(value.reads.assignments)
    from persistent_agents.models import AssignmentError
    with pytest.raises(AssignmentError) as error:
        await service.wait_job(value.identity,
                               await _job_wait_body(value, scheduler_job_id="888",
                                              tracked_job_id=_hex32("tracked-888")),
                               caller=value.caller)
    assert error.value.code == "work_not_found"


async def test_poller_wakes_the_subscribed_operation_across_the_real_seam(job_value):
    value = job_value
    record = _create_tracked_job(value)
    service = WorkContinuationService(value.reads.assignments)
    body = await _job_wait_body(value)
    await service.wait_job(value.identity, body, caller=value.caller)
    before = await _current(value)

    db = _plane_job_db(value)
    row = {"tracked_job_id": record.tracked_job_id, "owner_user_id": OWNER,
           "machine_id": MACHINE, "scheduler_job_id": SCHEDULER_JOB,
           "fail_count": 0, "last_polled_at": 1000}
    applied = rj._apply_and_wake(db, row, state="COMPLETED", exit_code="0:0",
                                 outcome_name="completed")
    assert applied.terminal is True and applied.finished_at is not None

    after = await _current(value)
    assert after.phase == "waiting" and after.wake_generation == before.wake_generation + 1
    assert after.operation["control"]["wait"] is None
    assert (after.operation["control"]["watermarks"]
            [rj.WAKE_EVENT_PREFIX + record.tracked_job_id]) == applied.finished_at
    events, _ = value.audit.list_for_user(OWNER)
    assert sum(event.action_type == "remote_job.completion_wake" for event in events) == 1
    assert value.audit.verify_chain(OWNER) is None


    with value.runtime.transaction() as tx:
        woken, skipped = rj._wake_subscribers(
            value.runtime.repositories.assignments, tx,
            owner_id=OWNER, event_key=rj.WAKE_EVENT_PREFIX + record.tracked_job_id,
            event_id="rj-wake:" + record.tracked_job_id, revision=applied.finished_at,
            event_digest=_rv_digest({"event": "remote_job_terminal"}))
    assert woken == [] and skipped == []
    again = await _current(value)
    assert again.wake_generation == after.wake_generation


async def test_late_terminal_event_conflicts_at_the_real_seam(job_value):
    value = job_value
    record = _create_tracked_job(value)
    service = WorkContinuationService(value.reads.assignments)
    await service.wait_job(value.identity, await _job_wait_body(value), caller=value.caller)
    before = await _current(value)

    with value.runtime.transaction() as tx:
        woken, skipped = rj._wake_subscribers(
            value.runtime.repositories.assignments, tx,
            owner_id=OWNER, event_key=rj.WAKE_EVENT_PREFIX + record.tracked_job_id,
            event_id="rj-wake:" + record.tracked_job_id, revision=500,
            event_digest=_rv_digest("stale"))
    assert woken == [] and len(skipped) == 1
    after = await _current(value)
    assert after.phase == "awaiting_event"
    assert after.wake_generation == before.wake_generation


async def test_poll_once_wakes_end_to_end_with_a_scripted_probe(job_value, monkeypatch):
    value = job_value
    record = _create_tracked_job(value)
    service = WorkContinuationService(value.reads.assignments)
    await service.wait_job(value.identity, await _job_wait_body(value), caller=value.caller)
    before = await _current(value)

    row = {"tracked_job_id": record.tracked_job_id, "owner_user_id": OWNER,
           "machine_id": MACHINE, "scheduler_job_id": SCHEDULER_JOB,
           "state": "running", "terminal": False, "fail_count": 0,
           "last_polled_at": 1000, "created_at": 1000, "chat_id": None,
           "component_id": None, "notify_on_finish": False, "notified": False,
           "output_path": None}
    monkeypatch.setattr(rj, "_probe_state",
                        lambda o, r: {"state": "COMPLETED", "terminal": True,
                                      "exit_code": "0:0", "confirmed": True})
    monkeypatch.setattr(rj, "list_open", lambda source, limit=200: [dict(row)])
    await rj.poll_once(SimpleNamespace(plane_repository_source=_plane_job_db(value)))

    after = await _current(value)
    assert after.phase == "waiting"
    assert after.wake_generation == before.wake_generation + 1
    with value.runtime.transaction() as tx:
        assert value.runtime.repositories.tracked_jobs.get(
            tx, owner_id=OWNER,
            tracked_job_id=record.tracked_job_id).terminal is True


async def test_wait_job_http_route_dispatches_to_the_service(job_value):
    from orchestrator.work_api import wait_job_work
    value = job_value
    _create_tracked_job(value)
    value.reads.assignments.orch.persistent_assignments = value.reads.assignments
    request = SimpleNamespace(
        state=SimpleNamespace(_work_control_caller=value.caller),
        app=SimpleNamespace(state=SimpleNamespace(orchestrator=value.reads.assignments.orch)))
    body = await _job_wait_body(value)
    reply = await wait_job_work(value.identity, body, request)
    assert reply.status_code == 200
    import json as _json
    payload = _json.loads(reply.body.decode())
    assert payload["applied"] is True and payload["operation"]["phase"] == "awaiting_event"


async def test_wait_job_refuses_a_malformed_repository_result(job_value, monkeypatch):
    value = job_value
    _create_tracked_job(value)
    service = WorkContinuationService(value.reads.assignments)
    from persistent_agents.models import AssignmentError

    def _malformed(self, tx, **kwargs):
        return SimpleNamespace(assignment=None)

    monkeypatch.setattr(type(value.runtime.repositories.assignments),
                        "set_owner_event_wait", _malformed)
    with pytest.raises(AssignmentError) as error:
        await service.wait_job(value.identity, await _job_wait_body(value),
                               caller=value.caller)
    assert (error.value.code, error.value.status_code) == ("work_control_unavailable", 503)


def test_vanished_subscriber_is_skipped_silently():
    class _Ghost:
        def list_operations(self, tx, *, owner_id, limit=100, after_id=None):
            return [SimpleNamespace(assignment=SimpleNamespace(**_subscriber()))]

        def get_operation(self, tx, *, owner_id, assignment_id):
            return None

    plane = _wake_plane()
    woken, skipped = rj._wake_subscribers(
        _Ghost(), plane.plane_runtime.transaction().__enter__(),
        owner_id=OWNER, event_key=_event_key(), event_id="rj-wake:x",
        revision=2000, event_digest="d")
    assert woken == [] and skipped == []


async def test_same_second_terminal_observation_wakes_the_registered_job(job_value, monkeypatch):
    value = job_value
    record = _create_tracked_job(value)
    body = await _job_wait_body(value)
    await WorkContinuationService(value.reads.assignments).wait_job(
        value.identity, body, caller=value.caller)
    monkeypatch.setattr(rj.time, "time", lambda: record.last_polled_at)
    row = rj._record_to_dict(record)
    result = rj._apply_and_wake(_plane_job_db(value), row, state="COMPLETED",
                                exit_code="0:0", outcome_name="completed")
    assert result.terminal
    current = await _current(value)
    assert current.phase == "waiting"
    assert current.operation["control"]["wait"] is None


async def test_remote_completion_before_registration_transaction_never_strands_waiter(job_value, monkeypatch):
    import asyncio
    from persistent_agents.models import AssignmentError

    value = job_value
    job = _create_tracked_job(value)
    body = await _job_wait_body(value)
    service = WorkContinuationService(value.reads.assignments)
    entering, release = asyncio.Event(), asyncio.Event()
    original_transaction = WorkContinuationService._transaction

    async def held_transaction(self, callback, caller):
        entering.set()
        await release.wait()
        return await original_transaction(self, callback, caller)

    monkeypatch.setattr(WorkContinuationService, "_transaction", held_transaction)
    waiting = asyncio.create_task(service.wait_job(value.identity, body, caller=value.caller))
    try:
        async with asyncio.timeout(10):
            await entering.wait()
            terminal = await asyncio.to_thread(
                rj._apply_and_wake, _plane_job_db(value), rj._record_to_dict(job),
                state="COMPLETED", exit_code="0:0", outcome_name="completed")
            assert terminal.terminal
            release.set()
            result = (await asyncio.gather(waiting, return_exceptions=True))[0]
            assert isinstance(result, AssignmentError)
            assert result.code == "work_job_already_terminal"
            assert (await _current(value)).operation.get("control", {}).get("wait") is None
    finally:
        release.set()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_remote_wake_audit_failure_rolls_back_terminal_state_and_restart_can_retry(job_value, monkeypatch):
    import asyncio

    value = job_value
    job = _create_tracked_job(value)
    body = await _job_wait_body(value)
    service = WorkContinuationService(value.reads.assignments)
    await service.wait_job(value.identity, body, caller=value.caller)
    original_audit = rj._audit_completion_wakes

    def unavailable_audit(*args, **options):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(rj, "_audit_completion_wakes", unavailable_audit)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        await asyncio.to_thread(
            rj._apply_and_wake, _plane_job_db(value), rj._record_to_dict(job),
            state="COMPLETED", exit_code="0:0", outcome_name="completed")
    with value.runtime.transaction() as tx:
        assert value.runtime.repositories.tracked_jobs.get(
            tx, owner_id=OWNER, tracked_job_id=job.tracked_job_id).terminal is False
    assert (await _current(value)).phase == "awaiting_event"
    monkeypatch.setattr(rj, "_audit_completion_wakes", original_audit)
    terminal = await asyncio.to_thread(
        rj._apply_and_wake, _plane_job_db(value), rj._record_to_dict(job),
        state="COMPLETED", exit_code="0:0", outcome_name="completed")
    assert terminal.terminal
    after = await _current(value)
    assert after.phase == "waiting"
    replay = await service.wait_job(value.identity, body, caller=value.caller)
    assert replay["applied"] is False
    assert (await _current(value)).wake_generation == after.wake_generation
    events, _ = value.audit.list_for_user(OWNER)
    assert sum(event.action_type == "remote_job.completion_wake" for event in events) == 1
    assert value.audit.verify_chain(OWNER) is None
