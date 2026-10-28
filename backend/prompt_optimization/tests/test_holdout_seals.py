"""Checks final qualification admission against durable Plane run metadata and failed exposure.
The PostgreSQL suite exercises the same repository against an isolated migrated database.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Lock
from types import SimpleNamespace

import pytest
from astralplane.repositories import RepositoryConflictError

from prompt_optimization import optimizer
from prompt_optimization.fixture import build_fixture


class _RunStorage:
    def __init__(self):
        self.runs = {}
        self.lock = Lock()
        self.repositories = SimpleNamespace(quality_audit=self)

    @contextmanager
    def transaction(self):
        with self.lock:
            before = dict(self.runs)
            try:
                yield self
            except BaseException:
                self.runs = before
                raise

    def get_run(self, _tx, *, owner_id, run_id):
        record = self.runs.get(run_id)
        return record if record is not None and record.owner_id == owner_id else None

    def create_run(self, _tx, record):
        if record.run_id in self.runs:
            raise RepositoryConflictError("qualification run exists")
        self.runs[record.run_id] = record
        return record

    def finish_run(self, _tx, **_kwargs):
        return True


def test_seal_survives_adapter_reconstruction_and_same_run_retry():
    storage = _RunStorage()
    fixture = build_fixture()
    first = optimizer.PlaneHoldoutSeals(storage)
    first.seal(fixture, "attempt-one", context_digest="a" * 64)
    second = optimizer.PlaneHoldoutSeals(storage)
    with pytest.raises(optimizer.HoldoutReuseError):
        second.assert_available(fixture)
    with pytest.raises(optimizer.HoldoutReuseError):
        second.seal(fixture, "attempt-one", context_digest="a" * 64)
    record = next(iter(storage.runs.values()))
    assert record.status == "running"
    assert set(record.system_state) == {"optimization_run_id", "fixture_digest", "holdout_digest", "claim_id", "input_digest", "evaluation_context_digest"}
    assert record.system_state["optimization_run_id"] == "attempt-one"


def test_parallel_seals_admit_exactly_one_final_exposure():
    storage = _RunStorage()
    fixture = build_fixture()

    def expose(run_id):
        try:
            optimizer.PlaneHoldoutSeals(storage).seal(fixture, run_id, context_digest="a" * 64)
            return "admitted"
        except optimizer.HoldoutReuseError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(expose, ["first", "second"])) == ["admitted", "refused"]


def test_repository_failure_never_admits_final_exposure():
    storage = _RunStorage()

    def unavailable(*_args):
        raise RuntimeError("database unavailable")

    storage.create_run = unavailable
    with pytest.raises(RuntimeError, match="database unavailable"):
        optimizer.PlaneHoldoutSeals(storage).seal(build_fixture(), "first", context_digest="a" * 64)



def test_partially_overlapping_holdout_rolls_back_all_fresh_claims():
    from dataclasses import replace

    storage = _RunStorage()
    original = build_fixture()
    seals = optimizer.PlaneHoldoutSeals(storage)
    seals.seal(original, "original", context_digest="a" * 64)
    before = dict(storage.runs)
    cases = list(original.cases("holdout"))
    cases[0] = replace(cases[0], case_id="new-case", request="A fresh synthetic final input")
    overlap = build_fixture(holdout=tuple(cases))
    with pytest.raises(optimizer.HoldoutReuseError):
        seals.seal(overlap, "later", context_digest="a" * 64)
    assert storage.runs == before


def test_crash_after_receipt_commit_does_not_restore_availability():
    storage = _RunStorage()
    fixture = build_fixture()
    optimizer.PlaneHoldoutSeals(storage).seal(fixture, "crashed", context_digest="a" * 64)
    with pytest.raises(optimizer.HoldoutReuseError):
        optimizer.PlaneHoldoutSeals(storage).assert_available(fixture)
    assert all(record.finished_at is None for record in storage.runs.values())


def test_exposure_requires_a_bound_evaluation_context():
    storage = _RunStorage()
    with pytest.raises(ValueError, match="context digest"):
        optimizer.PlaneHoldoutSeals(storage).seal(build_fixture(), "first", context_digest="unbound")
    assert storage.runs == {}
