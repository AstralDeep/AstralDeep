"""Tests defining the metric-name, age and label-safety contract for
RuntimeObservability (backend/orchestrator/orchestrator.py, work_admission.py):
capacity, queue-wait, retention, refusal and scheduler counters, rejecting unsafe
labels.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestrator import orchestrator as orchestrator_module
from orchestrator.work_admission import AdmissionClass, AdmissionClassStatus


_NOW = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)
_EXPECTED_RED = (
    "EXPECTED RED (T030): orchestrator.orchestrator must expose "
    "RuntimeObservability with admission gauges and bounded operation, "
    "scheduler, and effect counters"
)
_ALLOWED_LABELS = {
    "deployment_instance",
    "effect_kind",
    "job_type",
    "operation_kind",
    "phase",
    "result_code",
}
_FORBIDDEN_LABELS = {
    "chat_id",
    "chat_text",
    "credential",
    "credentials",
    "instruction",
    "message",
    "operation_payload",
    "output",
    "owner_user_id",
    "payload",
    "prompt",
    "target",
    "target_chat_id",
    "token",
    "user_id",
}


def _observability() -> Any:
    observability_type = getattr(
        orchestrator_module,
        "RuntimeObservability",
        None,
    )
    assert observability_type is not None, _EXPECTED_RED
    return observability_type(
        clock=lambda: _NOW,
        retention_seconds=86_400,
        deployment_instance="candidate_a",
    )


def _sample_dict(sample: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(sample) and not isinstance(sample, type):
        value = dataclasses.asdict(sample)
    elif isinstance(sample, Mapping):
        value = dict(sample)
    else:
        value = {
            "name": sample.name,
            "value": sample.value,
            "labels": dict(sample.labels),
        }
    assert set(value) == {"name", "value", "labels"}
    assert isinstance(value["name"], str)
    assert isinstance(value["value"], (int, float))
    assert isinstance(value["labels"], Mapping)
    value["labels"] = dict(value["labels"])
    return value


def _samples(observability: Any) -> list[dict[str, Any]]:
    return [_sample_dict(sample) for sample in observability.snapshot()]


def _sample(
    samples: list[dict[str, Any]],
    name: str,
    **labels: str,
) -> dict[str, Any]:
    matches = [
        sample
        for sample in samples
        if sample["name"] == name and sample["labels"] == labels
    ]
    assert len(matches) == 1, (name, labels, samples)
    return matches[0]


def test_effective_capacity_queue_wait_retention_counts_and_oldest_ages() -> None:
    observability = _observability()
    status = AdmissionClassStatus(
        class_name=AdmissionClass.INTERACTIVE,
        parent_class_name=AdmissionClass.GLOBAL,
        active_limit=20,
        queue_limit=100,
        max_wait_ms=5_000,
        active_count=7,
        queued_count=11,
        oldest_queued_at=_NOW - timedelta(seconds=13.25),
        oldest_running_at=_NOW - timedelta(seconds=31.5),
    )

    observability.observe_admission(
        status,
        operation_kind="connection_frame",
    )

    labels = {
        "deployment_instance": "candidate_a",
        "operation_kind": "connection_frame",
    }
    samples = _samples(observability)
    assert _sample(samples, "operation_active_limit", **labels)["value"] == 20
    assert _sample(samples, "operation_queue_limit", **labels)["value"] == 100
    assert _sample(samples, "operation_queue_max_wait_ms", **labels)["value"] == 5_000
    assert _sample(samples, "operation_retention_seconds", **labels)["value"] == 86_400
    assert _sample(samples, "operation_active_count", **labels)["value"] == 7
    assert _sample(samples, "operation_queued_count", **labels)["value"] == 11
    assert _sample(
        samples,
        "operation_oldest_queued_age_seconds",
        **labels,
    )["value"] == pytest.approx(13.25)
    assert _sample(
        samples,
        "operation_oldest_running_age_seconds",
        **labels,
    )["value"] == pytest.approx(31.5)


def test_refusal_duplicate_cancellation_and_terminal_counters_are_distinct() -> None:
    observability = _observability()

    observability.record_operation(
        "refused",
        operation_kind="background_chat",
        result_code="capacity_exceeded",
    )
    observability.record_operation(
        "duplicate_submission_suppressed",
        operation_kind="background_chat",
    )
    observability.record_operation(
        "duplicate_submission_suppressed",
        operation_kind="background_chat",
    )
    observability.record_operation(
        "duplicate_terminal_suppressed",
        operation_kind="background_chat",
    )
    observability.record_operation(
        "cancelled",
        operation_kind="background_chat",
        result_code="cancelled_by_user",
    )
    observability.record_operation(
        "terminal",
        operation_kind="background_chat",
        result_code="completed",
    )

    common = {
        "deployment_instance": "candidate_a",
        "operation_kind": "background_chat",
    }
    samples = _samples(observability)
    assert _sample(
        samples,
        "operation_refused_total",
        **common,
        result_code="capacity_exceeded",
    )["value"] == 1
    assert _sample(
        samples,
        "operation_duplicate_submission_suppressed_total",
        **common,
    )["value"] == 2
    assert _sample(
        samples,
        "operation_duplicate_terminal_suppressed_total",
        **common,
    )["value"] == 1
    assert _sample(
        samples,
        "operation_cancelled_total",
        **common,
        result_code="cancelled_by_user",
    )["value"] == 1
    assert _sample(
        samples,
        "operation_terminal_total",
        **common,
        result_code="completed",
    )["value"] == 1


def test_scheduler_claim_recovery_terminal_and_effect_outcomes_are_reported() -> None:
    observability = _observability()

    observability.record_scheduler(
        "claim_recovered",
        job_type="scheduled_chat",
        result_code="lease_expired",
    )
    observability.record_scheduler(
        "terminal",
        job_type="scheduled_chat",
        result_code="completed",
    )
    for event, result_code in (
        ("reserved", None),
        ("published", "completed"),
        ("deduplicated", "duplicate_suppressed"),
        ("conflict", "effect_idempotency_conflict"),
    ):
        observability.record_effect(
            event,
            effect_kind="chat_history",
            result_code=result_code,
        )

    samples = _samples(observability)
    scheduler_labels = {
        "deployment_instance": "candidate_a",
        "job_type": "scheduled_chat",
    }
    assert _sample(
        samples,
        "scheduler_claim_recovered_total",
        **scheduler_labels,
        result_code="lease_expired",
    )["value"] == 1
    assert _sample(
        samples,
        "scheduler_terminal_total",
        **scheduler_labels,
        result_code="completed",
    )["value"] == 1

    effect_labels = {
        "deployment_instance": "candidate_a",
        "effect_kind": "chat_history",
    }
    for event, result_code in (
        ("reserved", None),
        ("published", "completed"),
        ("deduplicated", "duplicate_suppressed"),
        ("conflict", "effect_idempotency_conflict"),
    ):
        labels = dict(effect_labels)
        if result_code is not None:
            labels["result_code"] = result_code
        assert _sample(
            samples,
            f"scheduler_effect_{event}_total",
            **labels,
        )["value"] == 1


def test_retention_and_disconnect_drain_diagnostics_are_bounded() -> None:
    observability = _observability()

    observability.observe_retention(purged_count=3, lag_seconds=12.5)
    observability.observe_retention(purged_count=2, lag_seconds=0)
    observability.observe_disconnect_drain(
        duration_seconds=4.25,
        remainder=2,
    )

    labels = {"deployment_instance": "candidate_a"}
    samples = _samples(observability)
    assert _sample(
        samples,
        "operation_retention_purged_total",
        **labels,
    )["value"] == 5
    assert _sample(
        samples,
        "operation_retention_purge_lag_seconds",
        **labels,
    )["value"] == 0
    assert _sample(
        samples,
        "operation_disconnect_drain_duration_seconds",
        **labels,
    )["value"] == pytest.approx(4.25)
    assert _sample(
        samples,
        "operation_disconnect_drain_remainder",
        **labels,
    )["value"] == 2


@pytest.mark.parametrize("forbidden_label", sorted(_FORBIDDEN_LABELS))
def test_telemetry_rejects_payload_identity_and_credential_label_names(
    forbidden_label: str,
) -> None:
    observability = _observability()

    with pytest.raises(ValueError, match="label|sensitive|allowed"):
        observability.record(
            "operation_refused_total",
            value=1,
            labels={
                "deployment_instance": "candidate_a",
                "operation_kind": "connection_frame",
                forbidden_label: "do_not_export",
            },
        )


@pytest.mark.parametrize(
    "unsafe_value",
    (
        "user-5ef1d2c0",
        "chat text with clinical details",
        "send this instruction",
        '{"payload":"private"}',
        "Bearer eyJhbGciOiJIUzI1NiJ9",
        "postgres://user:password@db/astral",
        "https://user:secret@example.invalid/target",
    ),
)
def test_telemetry_rejects_unsafe_values_even_under_allowed_label_names(
    unsafe_value: str,
) -> None:
    observability = _observability()

    with pytest.raises(ValueError, match="label|safe|snake_case"):
        observability.record_operation(
            "refused",
            operation_kind=unsafe_value,
            result_code="capacity_exceeded",
        )


def test_exported_samples_have_only_the_contract_label_vocabulary() -> None:
    observability = _observability()
    observability.record_operation(
        "terminal",
        operation_kind="maintenance",
        result_code="failed",
        phase="cleanup",
    )
    observability.record_scheduler(
        "cancelled",
        job_type="scheduled_chat",
        result_code="cancelled_job_deleted",
    )
    observability.record_effect(
        "published",
        effect_kind="notification",
        result_code="completed",
    )

    samples = _samples(observability)
    assert samples
    for sample in samples:
        labels = sample["labels"]
        assert set(labels) <= _ALLOWED_LABELS
        assert not set(labels).intersection(_FORBIDDEN_LABELS)
        serialized = repr(labels).lower()
        for forbidden_text in (
            "user_id",
            "chat text",
            "instruction",
            "payload",
            "bearer ",
            "credential",
            "target_chat",
        ):
            assert forbidden_text not in serialized
