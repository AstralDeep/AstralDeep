"""Verifies final-input admission against real migrated Plane transactions and uniqueness.
Synthetic fixtures cover overlap rollback, concurrency, reconstruction, and cancelled final evaluation.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from prompt_optimization.fixture import build_fixture
from prompt_optimization.holdout import HoldoutReuseError, OWNER_ID, PlaneHoldoutSeals
from tests.helpers.plane_template import engine_clone


@pytest.fixture
def runtime(plane_template):
    with engine_clone(plane_template) as (runtime, _database):
        assert runtime.health().ready
        yield runtime


def _record(runtime, run_id):
    with runtime.transaction() as transaction:
        return runtime.repositories.quality_audit.get_run(transaction, owner_id=OWNER_ID, run_id=run_id)


def test_committed_final_exposure_survives_reconstruction_and_crash(runtime):
    fixture = build_fixture()
    first = PlaneHoldoutSeals(runtime)
    first.seal(fixture, "crashed-run", context_digest="a" * 64)
    records = [_record(runtime, run_id) for run_id in first.receipt_ids(fixture)]
    assert all(record.status == "running" for record in records)
    assert all(record.system_state["evaluation_context_digest"] == "a" * 64 for record in records)
    reconstructed = PlaneHoldoutSeals(runtime)
    with pytest.raises(HoldoutReuseError):
        reconstructed.assert_available(fixture)
    with pytest.raises(HoldoutReuseError):
        reconstructed.seal(fixture, "crashed-run", context_digest="a" * 64)
    assert [_record(runtime, run_id) for run_id in first.receipt_ids(fixture)] == records


def test_parallel_final_exposure_admits_only_one_run(runtime):
    fixture = build_fixture()
    barrier = Barrier(2)

    def expose(run_id):
        barrier.wait()
        try:
            PlaneHoldoutSeals(runtime).seal(fixture, run_id, context_digest="a" * 64)
            return "admitted"
        except HoldoutReuseError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(expose, ["first", "second"])) == ["admitted", "refused"]
    records = [_record(runtime, run_id) for run_id in PlaneHoldoutSeals.receipt_ids(fixture)]
    assert len({record.system_state["optimization_run_id"] for record in records}) == 1


def test_partial_overlap_rolls_back_every_fresh_claim(runtime):
    fixture = build_fixture()
    seals = PlaneHoldoutSeals(runtime)
    seals.seal(fixture, "original", context_digest="a" * 64)
    cases = list(fixture.cases("holdout"))
    cases[0] = replace(cases[0], case_id="new-final-case", request="A genuinely fresh synthetic task")
    overlap = build_fixture(holdout=tuple(cases))
    with pytest.raises(HoldoutReuseError):
        seals.seal(overlap, "later", context_digest="a" * 64)
    fresh_ids = set(seals.receipt_ids(overlap)) - set(seals.receipt_ids(fixture))
    assert len(fresh_ids) == 1
    assert all(_record(runtime, run_id) is None for run_id in fresh_ids)
    assert all(_record(runtime, run_id).system_state["optimization_run_id"] == "original"
               for run_id in seals.receipt_ids(fixture))


def test_database_failure_rolls_back_claim_bundle_without_exposure(runtime, monkeypatch):
    fixture = build_fixture()
    repository = runtime.repositories.quality_audit
    create = repository.create_run
    calls = []

    def interrupted(transaction, record):
        calls.append(record.run_id)
        if len(calls) == 2:
            raise RuntimeError("injected write failure")
        return create(transaction, record)

    monkeypatch.setattr(repository, "create_run", interrupted)
    with pytest.raises(RuntimeError, match="injected write failure"):
        PlaneHoldoutSeals(runtime).seal(fixture, "interrupted", context_digest="a" * 64)
    assert all(_record(runtime, run_id) is None for run_id in PlaneHoldoutSeals.receipt_ids(fixture))


def test_partial_final_cancellation_keeps_durable_failed_receipts(runtime):
    from prompt_optimization.tests.test_optimizer import _scripted_run

    run, adapter, state = _scripted_run()
    run.holdout_seals = PlaneHoldoutSeals(runtime)
    complete = adapter.complete
    holdout_ids = {case.case_id for case in run.fixture.cases("holdout")}

    def interrupted(kind, subject, messages, max_output_chars, max_attempts):
        result = complete(kind, subject, messages, max_output_chars, max_attempts)
        if subject in holdout_ids:
            state["cancelled"] = True
        return result

    adapter.complete = interrupted
    result = run.execute()
    assert result.status == "cancelled"
    assert len([record for record in run.budget.records if record.phase == "holdout"]) == 1
    assert all(_record(runtime, run_id).status == "failed"
               for run_id in run.holdout_seals.receipt_ids(run.fixture))
    with pytest.raises(HoldoutReuseError):
        PlaneHoldoutSeals(runtime).assert_available(run.fixture)
