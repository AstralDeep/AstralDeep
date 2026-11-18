"""Tests for remote-job completion wakeups (orchestrator/remote_jobs.py +
orchestrator/work_continuations.py): the outcome classifier, subscriber
discovery, bounded-retry wake delivery, the atomic terminal apply+wake, the
owner wait-job subscription API, and the real Plane seams against Postgres.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from astralplane.repositories.tracked_jobs import TrackedJobRecord

from orchestrator import remote_jobs as rj
from orchestrator.work_continuations import WorkJobWaitRequest
from orchestrator.remote_transport import RemoteResult, Verdict, set_transport

MACHINE = "a" * 32
TRACKED = "b" * 32
SCHEDULER_JOB = "4242"


def _event_key(tracked_job_id: str = TRACKED) -> str:
    return rj.WAKE_EVENT_PREFIX + tracked_job_id


def _tracked_record(row, *, last_polled_at=None):
    return TrackedJobRecord(
        tracked_job_id=row["tracked_job_id"], owner_id=row["owner_user_id"],
        machine_id=row["machine_id"], scheduler_job_id=row["scheduler_job_id"],
        conversation_id=None, submit_marker=None, output_path=None,
        component_id=None, job_name="", state="submitted", exit_code=None,
        terminal=False, notify_on_finish=False, notified=False, fail_count=0,
        created_at=row["created_at"], last_polled_at=last_polled_at, finished_at=None)


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


class TestPollOneWakeIntegration:
    @pytest.fixture(autouse=True)
    def _silent_fanout(self, monkeypatch):
        async def _push(orch, row, **kw):
            return None
        async def _notify(orch, row, state, exit_code):
            return None
        monkeypatch.setattr(rj, "_push_component", _push)
        monkeypatch.setattr(rj, "_notify_finish", _notify)


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


def _orch(db=None):
    return SimpleNamespace(plane_repository_source=db or SimpleNamespace(),
                           credential_manager=object())


@pytest.fixture(autouse=True)
def _reset_transport():
    yield
    set_transport(None)


def _hex32(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


