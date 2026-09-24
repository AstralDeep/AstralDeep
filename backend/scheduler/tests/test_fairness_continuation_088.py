"""Tests that scheduler/store.py's scan-hint continuation only advances after a
successful commit, never serializes database work, and still advances past ineligible
handlers, including a real-store restart scenario.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from astralplane.repositories.scheduler import DueClaimBatch, DueScanContinuation
import scheduler.store as store_module


def _hint(number: int) -> DueScanContinuation:
    return DueScanContinuation(definition=(number, str(uuid.uuid4())))


def _store(monkeypatch, callback, *, commit=lambda: None):
    @contextmanager
    def transaction():
        yield object()
        commit()

    plane = SimpleNamespace(
        transaction=transaction,
        repository=SimpleNamespace(materialize_and_claim_due_for_administration=callback),
    )
    monkeypatch.setattr(store_module, "repository_from", lambda *a, **kw: (plane.repository, plane))
    monkeypatch.setattr(store_module, "PlaneRepositoryContext", lambda **kw: plane)
    return store_module.ScheduledJobStore(plane_runtime=plane)


def test_scan_hint_changes_only_after_successful_commit(monkeypatch):
    next_hint = _hint(1)
    received = []
    fail = [True]

    def claim(transaction, **kwargs):
        received.append(kwargs["continuation"])
        return DueClaimBatch((), (), (), next_hint)

    def commit():
        assert store._scan_hint is None
        if fail[0]:
            raise RuntimeError("synthetic commit refusal")

    store = _store(monkeypatch, claim, commit=commit)
    with pytest.raises(RuntimeError, match="commit refusal"):
        store.materialize_and_claim_due("test")
    assert store._scan_hint is None
    fail[0] = False
    assert store.materialize_and_claim_due("test") == ()
    assert store._scan_hint == next_hint
    assert received == [None, None]


def test_slow_old_scan_does_not_hold_hint_lock_or_overwrite_newer_completion(monkeypatch):
    first_in_database = threading.Event()
    release_first = threading.Event()
    next_hint, old_hint = _hint(2), _hint(1)
    count_lock = threading.Lock()
    calls = []

    def claim(transaction, **kwargs):
        with count_lock:
            calls.append(kwargs["continuation"])
            number = len(calls)
        if number == 1:
            first_in_database.set()
            assert release_first.wait(3)
            return DueClaimBatch((), (), (), old_hint)
        return DueClaimBatch((), (), (), next_hint)

    store = _store(monkeypatch, claim)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.materialize_and_claim_due, "first")
        try:
            assert first_in_database.wait(3)
            second = pool.submit(store.materialize_and_claim_due, "second")
            assert second.result(timeout=2) == ()
            assert store._scan_hint == next_hint
        finally:
            release_first.set()
        assert first.result(timeout=2) == ()
    assert store._scan_hint == next_hint
    assert calls == [None, None]
    assert store.materialize_and_claim_due("third") == ()
    assert calls[-1] == next_hint


def test_hint_advances_when_all_handlers_are_ineligible(monkeypatch):
    next_hint = _hint(3)

    def claim(transaction, **kwargs):
        assert kwargs["eligible"](SimpleNamespace()) is False
        return DueClaimBatch((), (), (str(uuid.uuid4()),), next_hint)

    monkeypatch.setattr(store_module.ScheduledJobStore, "_job_dict", staticmethod(lambda value: {}))
    store = _store(monkeypatch, claim)
    assert store.materialize_and_claim_due("test", eligibility=lambda job: False) == ()
    assert store._scan_hint == next_hint


@pytest.fixture(scope="module")
def scheduler_database():
    from tests.helpers.voice_plane_runtime import isolated_plane_runtime

    with isolated_plane_runtime("scheduler_fairness") as runtime:
        yield runtime


def test_real_store_advances_held_pages_and_restart_preserves_claims(scheduler_database):
    from datetime import UTC, datetime

    from scheduler.tests.test_occurrence_claims_060 import _due_job
    from scheduler.tests.plane_runtime import scheduled_job_store

    store = scheduled_job_store(scheduler_database)
    due = int(datetime.now(UTC).timestamp() * 1000) - 60_000
    jobs = [_due_job(store, f"fairness-{index}", due_ms=due + index) for index in range(66)]
    eligible_id = jobs[-1]["id"]
    observed = []

    def eligible(job):
        observed.append(job["id"])
        return job["id"] == eligible_id

    assert store.materialize_and_claim_due("first", limit=1, eligibility=eligible) == ()
    assert len(observed) == 32
    assert store.materialize_and_claim_due("first", limit=1, eligibility=eligible) == ()
    assert len(observed) == 64
    selected = store.materialize_and_claim_due("first", limit=1, eligibility=eligible)
    assert [claim.job["id"] for claim in selected] == [eligible_id]
    assert {job["id"] for job in jobs} <= set(observed)
    for job in jobs[:-1]:
        stored = scheduler_database.fetch_one(
            "SELECT next_run_at, status FROM scheduled_job WHERE id = ?", (job["id"],)
        )
        assert stored["next_run_at"] == job["next_run_at"]
        assert stored["status"] == "active"
    replacement = scheduled_job_store(scheduler_database)
    for _ in range(3):
        assert replacement.materialize_and_claim_due("replacement", limit=1, eligibility=eligible) == ()
    assert scheduler_database.fetch_one(
        "SELECT count(*) AS n FROM scheduled_occurrence WHERE job_id = ?", (eligible_id,)
    )["n"] == 1
