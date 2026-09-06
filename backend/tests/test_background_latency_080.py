"""080-runtime-metrics: fixed-bucket background latency collector (US2).

Contract-first coverage for the intended public helper
``RuntimeObservability.observe_background_operation(operation)``.  The helper is
expected to read only the safe projection's ``state`` and the
``accepted_at``/``started_at``/``terminal_at`` timestamps, aggregate realized
queue-wait, execution and end-to-end latency into the exact fixed buckets from
``contracts/metrics.md``, and never propagate identifiers, task kinds, raw
terminal codes or payload dimensions.

Helper-dependent tests are EXPECTED RED until feature 080 adds the helper:
``_observe`` asserts the method exists before calling it. Collector contracts use
``SimpleNamespace`` projections so no coordinator or task lifecycle is required.
"""

from __future__ import annotations

import math
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from queue import SimpleQueue
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.work_admission import OperationState


_EXPECTED_RED = (
    "EXPECTED RED (080): RuntimeObservability must expose "
    "observe_background_operation(operation) recording fixed-bucket background "
    "latency aggregates from the safe projection's state and timestamps"
)

_BASE = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

# Exact cumulative upper bounds from contracts/metrics.md, in seconds.
_BUCKETS: tuple[tuple[str, float], ...] = (
    ("le_0_05", 0.05),
    ("le_0_1", 0.1),
    ("le_0_25", 0.25),
    ("le_0_5", 0.5),
    ("le_1", 1.0),
    ("le_2_5", 2.5),
    ("le_5", 5.0),
    ("le_10", 10.0),
    ("le_30", 30.0),
    ("le_60", 60.0),
    ("le_300", 300.0),
    ("le_900", 900.0),
    ("le_3600", 3600.0),
    ("le_inf", math.inf),
)
_BUCKET_TOKENS = frozenset(token for token, _ in _BUCKETS)
_PHASES = frozenset({"queue_wait", "execution", "end_to_end"})
_OUTCOMES = frozenset({"completed", "failed", "cancelled", "retryable"})
_SKIP_REASONS = frozenset(
    {"missing_timestamp", "invalid_timestamp", "invalid_order", "invalid_state"}
)


class _HugeDelta:
    """Elapsed-span stand-in reporting a finite-but-enormous number of seconds."""

    def total_seconds(self) -> float:
        return sys.float_info.max


class _HugeInstant(datetime):
    """Aware datetime subclass whose subtraction yields an overflow-scale span.

    Datetime/timedelta arithmetic cannot reach the double-precision maximum, so
    this narrowly-justified white-box injection is the only way to exercise the
    cumulative-sum overflow guard without inventing impossible calendar ranges.
    Ordering comparisons keep the inherited (valid) datetime semantics.
    """

    def __sub__(self, other):  # type: ignore[override]
        return _HugeDelta()


class _MissingOffset(tzinfo):
    def utcoffset(self, dt):
        return None


class _RaisingOffset(tzinfo):
    def utcoffset(self, dt):
        raise ValueError("private_timezone_failure")


def _observability() -> RuntimeObservability:
    return RuntimeObservability(deployment_instance="candidate_a")


def _observe(observability: RuntimeObservability, operation) -> None:
    method = getattr(observability, "observe_background_operation", None)
    assert callable(method), _EXPECTED_RED
    method(operation)


def _started_op(
    state: OperationState = OperationState.COMPLETED,
    *,
    queue_wait: float = 0.0,
    execution: float = 0.0,
) -> SimpleNamespace:
    accepted = _BASE
    started = accepted + timedelta(seconds=queue_wait)
    terminal = started + timedelta(seconds=execution)
    return SimpleNamespace(
        state=state,
        accepted_at=accepted,
        started_at=started,
        terminal_at=terminal,
    )


def _never_started_op(
    state: OperationState = OperationState.RETRYABLE,
    *,
    wait: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        state=state,
        accepted_at=_BASE,
        started_at=None,
        terminal_at=_BASE + timedelta(seconds=wait),
    )


def _value(observability: RuntimeObservability, name: str, **labels) -> float:
    matches = [
        sample
        for sample in observability.snapshot()
        if sample.name == name and dict(sample.labels) == labels
    ]
    assert len(matches) == 1, (
        name,
        labels,
        [(sample.name, dict(sample.labels)) for sample in observability.snapshot()],
    )
    return matches[0].value


def _bucket(observability, phase, result_code, token) -> float:
    return _value(
        observability,
        "background_operation_latency_seconds_bucket",
        deployment_instance="candidate_a",
        phase=phase,
        result_code=result_code,
        latency_bucket=token,
    )


def _count(observability, phase, result_code) -> float:
    return _value(
        observability,
        "background_operation_latency_seconds_count",
        deployment_instance="candidate_a",
        phase=phase,
        result_code=result_code,
    )


def _sum(observability, phase, result_code) -> float:
    return _value(
        observability,
        "background_operation_latency_seconds_sum",
        deployment_instance="candidate_a",
        phase=phase,
        result_code=result_code,
    )


def _skipped(observability, reason) -> float:
    return _value(
        observability,
        "background_operation_latency_skipped_total",
        deployment_instance="candidate_a",
        result_code=reason,
    )


def _names(observability) -> set[str]:
    return {sample.name for sample in observability.snapshot()}


def _has_phase(observability, phase) -> bool:
    return any(
        sample.labels.get("phase") == phase for sample in observability.snapshot()
    )


def _has_latency(observability) -> bool:
    return any(
        sample.name.startswith("background_operation_latency_seconds")
        for sample in observability.snapshot()
    )


# ---------------------------------------------------------------------------
# Golden aggregation
# ---------------------------------------------------------------------------


def test_started_terminal_records_all_three_phases() -> None:
    observability = _observability()

    _observe(observability, _started_op(queue_wait=0.5, execution=2.0))

    assert _count(observability, "queue_wait", "completed") == 1
    assert _count(observability, "execution", "completed") == 1
    assert _count(observability, "end_to_end", "completed") == 1
    assert _sum(observability, "queue_wait", "completed") == pytest.approx(0.5)
    assert _sum(observability, "execution", "completed") == pytest.approx(2.0)
    assert _sum(observability, "end_to_end", "completed") == pytest.approx(2.5)
    # Boundaries are inclusive; below-boundary buckets stay empty.
    assert _bucket(observability, "queue_wait", "completed", "le_0_5") == 1
    assert _bucket(observability, "queue_wait", "completed", "le_0_25") == 0
    assert _bucket(observability, "execution", "completed", "le_2_5") == 1
    assert _bucket(observability, "execution", "completed", "le_1") == 0
    assert _bucket(observability, "end_to_end", "completed", "le_2_5") == 1
    for phase in _PHASES:
        assert _bucket(observability, phase, "completed", "le_inf") == _count(
            observability, phase, "completed"
        )


def test_never_started_records_wait_and_end_to_end_without_execution() -> None:
    observability = _observability()

    _observe(observability, _never_started_op(OperationState.RETRYABLE, wait=3.0))

    assert _count(observability, "queue_wait", "retryable") == 1
    assert _count(observability, "end_to_end", "retryable") == 1
    assert _sum(observability, "queue_wait", "retryable") == pytest.approx(3.0)
    assert _sum(observability, "end_to_end", "retryable") == pytest.approx(3.0)
    # No execution sample may be fabricated for never-started work.
    assert not _has_phase(observability, "execution")


def test_different_utc_offsets_measure_elapsed_time() -> None:
    observability = _observability()
    operation = SimpleNamespace(
        state=OperationState.COMPLETED,
        accepted_at=_BASE,
        started_at=(_BASE + timedelta(seconds=1.25)).astimezone(
            timezone(timedelta(hours=5, minutes=30))
        ),
        terminal_at=(_BASE + timedelta(seconds=3.75)).astimezone(
            timezone(-timedelta(hours=4))
        ),
    )

    _observe(observability, operation)

    assert _sum(observability, "queue_wait", "completed") == pytest.approx(1.25)
    assert _sum(observability, "execution", "completed") == pytest.approx(2.5)
    assert _sum(observability, "end_to_end", "completed") == pytest.approx(3.75)
    for phase in _PHASES:
        assert _count(observability, phase, "completed") == 1


@pytest.mark.parametrize(
    ("accepted_minute", "started_minute", "terminal_minute", "elapsed"),
    (
        pytest.param(20, 30, 40, (600, 4200, 4800), id="repeated-hour-elapsed"),
        pytest.param(50, 55, 5, (300, 600, 900), id="wall-clock-order-reverses"),
    ),
)
def test_same_timezone_dst_fold_measures_elapsed_time(
    accepted_minute: int,
    started_minute: int,
    terminal_minute: int,
    elapsed: tuple[int, int, int],
) -> None:
    observability = _observability()
    zone = ZoneInfo("America/New_York")
    operation = SimpleNamespace(
        state=OperationState.COMPLETED,
        accepted_at=datetime(2026, 11, 1, 1, accepted_minute, tzinfo=zone, fold=0),
        started_at=datetime(2026, 11, 1, 1, started_minute, tzinfo=zone, fold=0),
        terminal_at=datetime(2026, 11, 1, 1, terminal_minute, tzinfo=zone, fold=1),
    )

    _observe(observability, operation)

    for phase, seconds in zip(("queue_wait", "execution", "end_to_end"), elapsed):
        assert _sum(observability, phase, "completed") == seconds
        assert _count(observability, phase, "completed") == 1
        assert _bucket(observability, phase, "completed", "le_inf") == 1
    assert "background_operation_latency_skipped_total" not in _names(observability)


def test_zero_duration_increments_buckets_and_count_but_not_sum() -> None:
    observability = _observability()

    _observe(observability, _started_op(queue_wait=0.0, execution=0.0))

    for token, _bound in _BUCKETS:
        assert _bucket(observability, "execution", "completed", token) == 1
    assert _count(observability, "execution", "completed") == 1
    assert _sum(observability, "execution", "completed") == 0.0


@pytest.mark.parametrize(
    "duration",
    (0.0, 0.05, 0.1, 0.5, 2.5, 5.0, 60.0, 5000.0),
)
def test_execution_buckets_are_cumulative_and_boundary_inclusive(
    duration: float,
) -> None:
    observability = _observability()

    _observe(observability, _started_op(queue_wait=0.0, execution=duration))

    for token, bound in _BUCKETS:
        expected = 1 if bound >= duration else 0
        assert _bucket(observability, "execution", "completed", token) == expected, (
            token,
            bound,
            duration,
        )
    assert _count(observability, "execution", "completed") == 1
    assert _sum(observability, "execution", "completed") == pytest.approx(duration)
    assert _bucket(observability, "execution", "completed", "le_inf") == 1


def test_cumulative_counts_and_sum_accumulate_atomically() -> None:
    observability = _observability()

    _observe(observability, _started_op(queue_wait=0.0, execution=0.5))
    _observe(observability, _started_op(queue_wait=0.0, execution=2.0))

    assert _count(observability, "execution", "completed") == 2
    assert _sum(observability, "execution", "completed") == pytest.approx(2.5)
    assert _bucket(observability, "execution", "completed", "le_0_5") == 1
    assert _bucket(observability, "execution", "completed", "le_2_5") == 2
    assert _bucket(observability, "execution", "completed", "le_inf") == 2


def test_closed_phase_and_outcome_vocabulary() -> None:
    observability = _observability()

    for state in (
        OperationState.COMPLETED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.RETRYABLE,
    ):
        _observe(observability, _started_op(state, queue_wait=0.5, execution=0.5))

    samples = observability.snapshot()
    phases = {
        sample.labels["phase"] for sample in samples if "phase" in sample.labels
    }
    outcomes = {
        sample.labels["result_code"]
        for sample in samples
        if "phase" in sample.labels
    }
    tokens = {
        sample.labels["latency_bucket"]
        for sample in samples
        if "latency_bucket" in sample.labels
    }
    assert phases == _PHASES
    assert outcomes == _OUTCOMES
    assert tokens <= _BUCKET_TOKENS


# ---------------------------------------------------------------------------
# Safety boundary — no identity, task kind, terminal code or payload leaks
# ---------------------------------------------------------------------------


def test_no_secret_taskkind_or_terminal_code_labels_propagate() -> None:
    observability = _observability()
    operation = SimpleNamespace(
        state=OperationState.FAILED,
        accepted_at=_BASE,
        started_at=_BASE + timedelta(seconds=1),
        terminal_at=_BASE + timedelta(seconds=3),
        terminal_code="secret_terminal_reason",
        operation_kind="user-supplied-kind",
        owner_user_id="user-abc123",
        chat_id="chat-secret",
        safe_summary="do not export me",
    )

    _observe(observability, operation)

    allowed = {"deployment_instance", "phase", "result_code", "latency_bucket"}
    for sample in observability.snapshot():
        assert set(sample.labels) <= allowed
        serialized = repr(dict(sample.labels)).lower()
        for forbidden in (
            "secret_terminal_reason",
            "user-supplied-kind",
            "user-abc123",
            "chat-secret",
            "do not export",
        ):
            assert forbidden not in serialized
    result_codes = {
        sample.labels.get("result_code")
        for sample in observability.snapshot()
        if "result_code" in sample.labels
    }
    # The coarse outcome, never the raw terminal code, is exported.
    assert result_codes <= {"failed"}


@pytest.mark.parametrize(
    "token",
    ("le_private_patient", "le_0_050", "le_nan", "le_infinity", "le_1e3"),
)
def test_public_record_rejects_non_contract_latency_bucket_tokens(token: str) -> None:
    observability = _observability()
    before = observability.snapshot()

    with pytest.raises(ValueError):
        observability.record(
            "background_operation_latency_seconds_bucket",
            labels={
                "deployment_instance": "candidate_a",
                "phase": "execution",
                "result_code": "completed",
                "latency_bucket": token,
            },
        )

    assert observability.snapshot() == before


# ---------------------------------------------------------------------------
# Omission signals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ("accepted_at", "terminal_at"))
def test_missing_required_timestamp_omits_and_records_skip(field: str) -> None:
    observability = _observability()
    projection = dict(
        state=OperationState.COMPLETED,
        accepted_at=_BASE,
        started_at=_BASE + timedelta(seconds=1),
        terminal_at=_BASE + timedelta(seconds=2),
    )
    projection[field] = None

    _observe(observability, SimpleNamespace(**projection))

    assert _skipped(observability, "missing_timestamp") == 1
    assert not _has_latency(observability)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("accepted_at", datetime(2026, 7, 15, 12, 0)),  # naive
        ("terminal_at", datetime(2026, 7, 15, 12, 0, 5)),  # naive
        ("accepted_at", "2026-07-15T12:00:00Z"),  # wrong type
        ("terminal_at", 1_700_000_000),  # wrong type
        ("started_at", 12345),  # wrong type on a started operation
    ),
)
def test_naive_or_malformed_timestamp_omits_and_records_invalid_timestamp(
    field: str,
    value,
) -> None:
    observability = _observability()
    projection = dict(
        state=OperationState.COMPLETED,
        accepted_at=_BASE,
        started_at=_BASE + timedelta(seconds=1),
        terminal_at=_BASE + timedelta(seconds=2),
    )
    projection[field] = value

    _observe(observability, SimpleNamespace(**projection))

    assert _skipped(observability, "invalid_timestamp") == 1
    assert not _has_latency(observability)


@pytest.mark.parametrize("field", ("accepted_at", "started_at", "terminal_at"))
@pytest.mark.parametrize("offset_kind", ("missing", "raising"))
def test_invalid_timezone_omits_without_exporting_failure_detail(
    field: str,
    offset_kind: str,
) -> None:
    observability = _observability()
    operation = _started_op(queue_wait=0.5, execution=0.5)
    invalid_zone = _MissingOffset() if offset_kind == "missing" else _RaisingOffset()
    setattr(operation, field, getattr(operation, field).replace(tzinfo=invalid_zone))

    _observe(observability, operation)

    assert _skipped(observability, "invalid_timestamp") == 1
    assert not _has_latency(observability)
    assert "private_timezone_failure" not in repr(observability.snapshot())


@pytest.mark.parametrize(
    "seconds",
    (
        pytest.param(True, id="boolean"),
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="infinite"),
        pytest.param(-1.0, id="negative"),
    ),
)
def test_malformed_elapsed_span_omits_all_latency(seconds: float | bool) -> None:
    # Real timedelta cannot return these values. Keep calendar ordering valid
    # and inject only the elapsed-span boundary used by the collector.
    class MalformedDelta:
        def total_seconds(self):
            return seconds

    class MalformedInstant(datetime):
        def __sub__(self, other):  # type: ignore[override]
            return MalformedDelta()

    observability = _observability()
    operation = _started_op(queue_wait=0.5, execution=0.5)
    operation.terminal_at = MalformedInstant(2026, 7, 15, 12, 0, 1, tzinfo=UTC)

    _observe(observability, operation)

    assert _skipped(observability, "invalid_timestamp") == 1
    assert not _has_latency(observability)
    assert len(observability.snapshot()) == 1


@pytest.mark.parametrize(
    ("accepted", "started", "terminal"),
    (
        (_BASE, _BASE + timedelta(seconds=1), _BASE - timedelta(seconds=1)),
        (_BASE, _BASE - timedelta(seconds=1), _BASE + timedelta(seconds=2)),
        (_BASE, _BASE + timedelta(seconds=5), _BASE + timedelta(seconds=2)),
    ),
)
def test_inverted_timestamps_omit_and_record_invalid_order(
    accepted,
    started,
    terminal,
) -> None:
    observability = _observability()

    _observe(
        observability,
        SimpleNamespace(
            state=OperationState.COMPLETED,
            accepted_at=accepted,
            started_at=started,
            terminal_at=terminal,
        ),
    )

    assert _skipped(observability, "invalid_order") == 1
    assert not _has_latency(observability)


@pytest.mark.parametrize(
    "state",
    (
        OperationState.QUEUED,
        OperationState.RUNNING,
        pytest.param([], id="unhashable-list"),
        pytest.param({"private_state": "private_detail"}, id="unhashable-mapping"),
        pytest.param(None, id="missing-state"),
        pytest.param("private_patient_state", id="raw-secret-state"),
    ),
)
def test_non_terminal_state_omits_and_records_invalid_state(
    state: object,
) -> None:
    observability = _observability()

    _observe(observability, _started_op(state, queue_wait=0.5, execution=0.5))

    assert _skipped(observability, "invalid_state") == 1
    assert not _has_latency(observability)
    assert "private_" not in repr(observability.snapshot())


def test_skip_reasons_use_closed_vocabulary_without_offending_data() -> None:
    observability = _observability()

    _observe(
        observability,
        SimpleNamespace(
            state=OperationState.COMPLETED,
            accepted_at=None,
            started_at=None,
            terminal_at=_BASE,
        ),
    )
    _observe(
        observability,
        SimpleNamespace(
            state=OperationState.COMPLETED,
            accepted_at=datetime(2026, 7, 15, 12, 0),  # naive
            started_at=None,
            terminal_at=_BASE,
        ),
    )
    _observe(observability, _started_op(OperationState.QUEUED, execution=0.5))
    _observe(
        observability,
        SimpleNamespace(
            state=OperationState.COMPLETED,
            accepted_at=_BASE,
            started_at=None,
            terminal_at=_BASE - timedelta(seconds=1),
        ),
    )

    skip_samples = [
        sample
        for sample in observability.snapshot()
        if sample.name == "background_operation_latency_skipped_total"
    ]
    assert skip_samples
    for sample in skip_samples:
        assert set(sample.labels) == {"deployment_instance", "result_code"}
        assert sample.labels["result_code"] in _SKIP_REASONS


# ---------------------------------------------------------------------------
# Overflow defense — no partial mutation
# ---------------------------------------------------------------------------


def test_overflow_would_be_total_is_rejected_without_partial_mutation() -> None:
    observability = _observability()
    # Seed every cumulative sum at the maximum finite double so any further
    # valid addition overflows.  See _HugeInstant for the narrowly-justified
    # white-box injection that produces an overflow-scale duration.
    for phase in ("queue_wait", "execution", "end_to_end"):
        key = (
            "background_operation_latency_seconds_sum",
            (
                ("deployment_instance", "candidate_a"),
                ("phase", phase),
                ("result_code", "completed"),
            ),
        )
        observability._values[key] = sys.float_info.max

    operation = SimpleNamespace(
        state=OperationState.COMPLETED,
        accepted_at=_BASE,
        started_at=_HugeInstant(2026, 7, 15, 12, 0, 1, tzinfo=UTC),
        terminal_at=_HugeInstant(2026, 7, 15, 12, 0, 2, tzinfo=UTC),
    )

    try:
        _observe(observability, operation)
    except (ArithmeticError, ValueError, OverflowError):
        # A fail-closed rejection is acceptable; it must not mutate partially.
        pass

    for phase in ("queue_wait", "execution", "end_to_end"):
        assert _sum(observability, phase, "completed") == sys.float_info.max
        assert not any(
            sample.name
            in {
                "background_operation_latency_seconds_count",
                "background_operation_latency_seconds_bucket",
            }
            and sample.labels.get("phase") == phase
            for sample in observability.snapshot()
        )
    # Every retained value stays finite and non-negative.
    for sample in observability.snapshot():
        assert math.isfinite(sample.value)
        assert sample.value >= 0


# ---------------------------------------------------------------------------
# Concurrency and snapshot consistency
# ---------------------------------------------------------------------------


def _run_threads(workers: list[Callable[[], None]], *, timeout: float = 5.0) -> None:
    errors: SimpleQueue[BaseException] = SimpleQueue()

    def guarded(worker: Callable[[], None]) -> None:
        try:
            worker()
        except BaseException as exc:
            errors.put(exc)

    threads = [
        threading.Thread(target=guarded, args=(worker,), daemon=True)
        for worker in workers
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    assert not any(thread.is_alive() for thread in threads), (
        "latency test workers exceeded their bounded join deadline"
    )
    if not errors.empty():
        raise errors.get_nowait()


def test_concurrent_observations_are_counted_exactly_and_consistently() -> None:
    observability = _observability()
    threads = 8
    per_thread = 40
    barrier = threading.Barrier(threads, timeout=2.0)

    def worker() -> None:
        barrier.wait()
        for _ in range(per_thread):
            _observe(observability, _started_op(queue_wait=0.0, execution=1.0))

    _run_threads([worker] * threads)

    total = threads * per_thread
    assert _count(observability, "execution", "completed") == total
    assert _sum(observability, "execution", "completed") == pytest.approx(total * 1.0)
    assert _bucket(observability, "execution", "completed", "le_1") == total
    assert _bucket(observability, "execution", "completed", "le_0_5") == 0
    assert _bucket(observability, "execution", "completed", "le_inf") == total
    values = [
        _bucket(observability, "execution", "completed", token)
        for token, _ in _BUCKETS
    ]
    assert values == sorted(values)
    assert values[-1] == _count(observability, "execution", "completed")


def test_snapshot_is_atomic_with_respect_to_concurrent_updates() -> None:
    observability = _observability()
    observer_count = 4
    observations_per_worker = 80
    barrier = threading.Barrier(observer_count + 1, timeout=2.0)
    # Seed a complete observation so an empty or phase-incomplete snapshot
    # cannot be mistaken for a legitimate not-yet-observed family.
    _observe(observability, _started_op(queue_wait=0.5, execution=1.0))

    def assert_complete_snapshot() -> None:
        samples = observability.snapshot()
        counts = {}
        sums = {}
        final_buckets = {}
        for sample in samples:
            if sample.name.startswith("background_operation_latency_seconds"):
                assert sample.labels["result_code"] == "completed"
                phase = sample.labels["phase"]
                if sample.name == "background_operation_latency_seconds_count":
                    counts[phase] = sample.value
                elif sample.name == "background_operation_latency_seconds_sum":
                    sums[phase] = sample.value
                elif sample.labels.get("latency_bucket") == "le_inf":
                    final_buckets[phase] = sample.value
        assert set(counts) == set(sums) == set(final_buckets) == _PHASES
        count = counts["execution"]
        assert count >= 1
        assert all(value == count for value in counts.values())
        assert final_buckets == counts
        assert sums == {
            "queue_wait": count * 0.5,
            "execution": count,
            "end_to_end": count * 1.5,
        }

    def observer() -> None:
        barrier.wait()
        for _ in range(observations_per_worker):
            _observe(observability, _started_op(queue_wait=0.5, execution=1.0))
            time.sleep(0)

    def checker() -> None:
        barrier.wait()
        for _ in range(160):
            assert_complete_snapshot()
            time.sleep(0)

    _run_threads([observer] * observer_count + [checker])
    assert_complete_snapshot()
    assert _count(observability, "execution", "completed") == (
        1 + observer_count * observations_per_worker
    )
