"""Bounded, payload-free runtime reliability telemetry: counters and gauges keyed to a
reviewed low-cardinality label allow-list, covering admission, background-operation
latency, retention, disconnect drain, and voice lifecycle events.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Mapping

from orchestrator.work_admission import AdmissionClassStatus, OperationState


_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_METRIC_NAME = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_ALLOWED_LABELS = frozenset(
    {
        "deployment_instance",
        "effect_kind",
        "job_type",
        "operation_kind",
        "phase",
        "result_code",
        "client_kind",
        "transport",
        "voice_state",
        "voice_reason",
        "cleanup_outcome",
        "latency_bucket",
    }
)

_BACKGROUND_LATENCY_BUCKETS: tuple[tuple[str, float], ...] = (
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
_BACKGROUND_LATENCY_TOKENS = frozenset(token for token, _ in _BACKGROUND_LATENCY_BUCKETS)

_BACKGROUND_OUTCOMES: dict[OperationState, str] = {
    OperationState.COMPLETED: "completed",
    OperationState.FAILED: "failed",
    OperationState.CANCELLED: "cancelled",
    OperationState.RETRYABLE: "retryable",
}

_VOICE_STATES = frozenset(
    {
        "off",
        "starting",
        "greeting",
        "listening",
        "recognizing",
        "thinking",
        "waiting",
        "speaking",
        "muted",
        "suspended",
        "degraded",
        "ending",
        "ended",
    }
)
_VOICE_REASONS = frozenset(
    {
        "none",
        "user_request",
        "takeover",
        "idle_expired",
        "lease_expired",
        "auth_lost",
        "chat_unavailable",
        "capacity",
        "permission_denied",
        "device_unavailable",
        "livekit_unavailable",
        "worker_unavailable",
        "speech_unavailable",
        "upstream_overloaded",
        "protocol_violation",
        "media_overrun",
        "reconnecting",
        "internal_error",
    }
)
_VOICE_CLIENT_KINDS = frozenset({"web", "windows", "android", "ios", "macos", "watchos"})
_VOICE_TRANSPORTS = frozenset({"livekit", "watch_bridge"})
_VOICE_CLEANUP_OUTCOMES = frozenset({"complete", "partial", "timed_out", "not_required"})
_VOICE_EVENTS = frozenset(
    {
        "deduplication",
        "interruption",
        "readiness",
        "reconnect",
        "session",
        "takeover",
        "tts",
        "turn",
    }
)
_VOICE_EVENT_OUTCOMES = frozenset(
    {
        "accepted",
        "cancelled",
        "degraded",
        "ended",
        "expired",
        "failed",
        "interrupted",
        "ready",
        "recognizing",
        "recovered",
        "refused",
        "rejected",
        "replayed",
        "requested",
        "started",
        "submitted",
        "succeeded",
        "unavailable",
    }
)
_VOICE_EVENT_REASONS = frozenset(
    {
        "activation_replay",
        "asr_unavailable",
        "authentication_required",
        "capacity_exhausted",
        "chat_unavailable",
        "feature_disabled",
        "hallucination_suppressed",
        "idle_expired",
        "internal_error",
        "lease_expired",
        "media_grant_replay",
        "media_grant_rotated",
        "media_unconfigured",
        "media_unreachable",
        "none",
        "no_audio_output",
        "no_microphone",
        "output_language_unsupported",
        "permission_denied",
        "permission_restricted",
        "protocol_violation",
        "ready",
        "reconnecting",
        "self_speech_suppressed",
        "speech_unavailable",
        "takeover",
        "transcript_replay",
        "tts_unavailable",
        "unsupported_transport",
        "user_request",
        "voice_unavailable",
        "worker_unavailable",
    }
)
_VOICE_TIMINGS = frozenset(
    {
        "activation",
        "recognition",
        "acknowledgement",
        "cadence_gap",
        "speech_start",
        "interruption",
        "cleanup",
    }
)
_VOICE_COUNTS = frozenset(
    {
        "sessions",
        "turns",
        "reconnects",
        "deduplications",
        "takeovers",
        "queue_depth",
    }
)


@dataclass(frozen=True)
class RuntimeMetricSample:
    name: str
    value: int | float
    labels: Mapping[str, str]


class RuntimeObservability:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        retention_seconds: int = 86_400,
        deployment_instance: str = "local",
    ) -> None:
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._retention_seconds = int(retention_seconds)
        self._deployment_instance = self._validate_label_value(
            deployment_instance,
            "deployment_instance",
        )
        self._values: dict[
            tuple[str, tuple[tuple[str, str], ...]], int | float
        ] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _validate_label_value(value: str, label_name: str) -> str:
        if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
            raise ValueError(
                f"{label_name} label values must be safe bounded snake_case tokens"
            )
        if label_name == "latency_bucket" and value not in _BACKGROUND_LATENCY_TOKENS:
            raise ValueError("latency_bucket must use a fixed reviewed upper bound")
        return value

    @classmethod
    def _key(
        cls,
        name: str,
        labels: Mapping[str, str],
    ) -> tuple[str, tuple[tuple[str, str], ...]]:
        if not isinstance(name, str) or not _METRIC_NAME.fullmatch(name):
            raise ValueError("metric name must be bounded snake_case")
        unknown = set(labels).difference(_ALLOWED_LABELS)
        if unknown:
            raise ValueError(
                "metric label names must come from the reviewed allowed vocabulary"
            )
        normalized = tuple(
            sorted(
                (
                    label_name,
                    cls._validate_label_value(label_value, label_name),
                )
                for label_name, label_value in labels.items()
            )
        )
        return name, normalized

    @staticmethod
    def _validate_value(value: int | float) -> int | float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("metric value must be numeric")
        if not math.isfinite(float(value)) or value < 0:
            raise ValueError("metric value must be finite and non-negative")
        return value

    def _base_labels(self) -> dict[str, str]:
        return {"deployment_instance": self._deployment_instance}

    def _set(
        self,
        name: str,
        value: int | float,
        labels: Mapping[str, str],
    ) -> None:
        key = self._key(name, labels)
        checked = self._validate_value(value)
        with self._lock:
            self._values[key] = checked

    def record(
        self,
        name: str,
        *,
        value: int | float = 1,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        checked = self._validate_value(value)
        key = self._key(name, labels or {})
        with self._lock:
            self._values[key] = self._values.get(key, 0) + checked

    def observe_admission(
        self,
        status: AdmissionClassStatus,
        *,
        operation_kind: str,
    ) -> None:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("observability clock must return an aware datetime")
        labels = self._base_labels()
        labels["operation_kind"] = operation_kind

        gauges: dict[str, int | float] = {
            "operation_active_limit": status.active_limit,
            "operation_queue_limit": status.queue_limit,
            "operation_queue_max_wait_ms": status.max_wait_ms or 0,
            "operation_retention_seconds": self._retention_seconds,
            "operation_active_count": status.active_count,
            "operation_queued_count": status.queued_count,
            "operation_oldest_queued_age_seconds": self._age_seconds(
                now,
                status.oldest_queued_at,
            ),
            "operation_oldest_running_age_seconds": self._age_seconds(
                now,
                status.oldest_running_at,
            ),
        }
        for name, value in gauges.items():
            self._set(name, value, labels)

    @staticmethod
    def _age_seconds(now: datetime, observed_at: datetime | None) -> float:
        if observed_at is None:
            return 0.0
        if observed_at.tzinfo is None:
            raise ValueError("observed admission timestamps must be timezone-aware")
        return max(0.0, (now - observed_at).total_seconds())

    def record_operation(
        self,
        event: str,
        *,
        operation_kind: str,
        result_code: str | None = None,
        phase: str | None = None,
    ) -> None:
        labels = self._base_labels()
        labels["operation_kind"] = operation_kind
        if result_code is not None:
            labels["result_code"] = result_code
        if phase is not None:
            labels["phase"] = phase
        self.record(f"operation_{event}_total", labels=labels)

    def observe_operation_duration(
        self,
        duration_seconds: float,
        *,
        operation_kind: str,
        phase: str,
        result_code: str,
    ) -> None:
        labels = self._base_labels()
        labels.update(
            operation_kind=operation_kind,
            phase=phase,
            result_code=result_code,
        )
        self._set("operation_duration_seconds", duration_seconds, labels)

    def record_scheduler(
        self,
        event: str,
        *,
        job_type: str,
        result_code: str | None = None,
    ) -> None:
        labels = self._base_labels()
        labels["job_type"] = job_type
        if result_code is not None:
            labels["result_code"] = result_code
        self.record(f"scheduler_{event}_total", labels=labels)

    def record_effect(
        self,
        event: str,
        *,
        effect_kind: str,
        result_code: str | None = None,
    ) -> None:
        labels = self._base_labels()
        labels["effect_kind"] = effect_kind
        if result_code is not None:
            labels["result_code"] = result_code
        self.record(f"scheduler_effect_{event}_total", labels=labels)

    def record_typesafe(
        self,
        event: str,
        *,
        result_code: str,
        phase: str | None = None,
    ) -> None:
        labels = self._base_labels()
        labels["result_code"] = result_code
        if phase is not None:
            labels["phase"] = phase
        self.record(f"typesafe_{event}_total", labels=labels)

    def observe_retention(
        self,
        *,
        purged_count: int,
        lag_seconds: float,
    ) -> None:
        labels = self._base_labels()
        self.record(
            "operation_retention_purged_total",
            value=purged_count,
            labels=labels,
        )
        self._set(
            "operation_retention_purge_lag_seconds",
            lag_seconds,
            labels,
        )

    def observe_disconnect_drain(
        self,
        *,
        duration_seconds: float,
        remainder: int,
    ) -> None:
        labels = self._base_labels()
        self._set(
            "operation_disconnect_drain_duration_seconds",
            duration_seconds,
            labels,
        )
        self._set(
            "operation_disconnect_drain_remainder",
            remainder,
            labels,
        )

    @staticmethod
    def _voice_value(value: str, allowed: frozenset[str], label: str) -> str:
        if value not in allowed:
            raise ValueError(f"{label} is outside the reviewed voice vocabulary")
        return value

    def _voice_labels(
        self,
        *,
        client_kind: str,
        transport: str,
    ) -> dict[str, str]:
        labels = self._base_labels()
        labels["client_kind"] = self._voice_value(
            client_kind,
            _VOICE_CLIENT_KINDS,
            "client_kind",
        )
        labels["transport"] = self._voice_value(
            transport,
            _VOICE_TRANSPORTS,
            "transport",
        )
        return labels

    def record_voice_state(
        self,
        *,
        state: str,
        reason: str,
        client_kind: str,
        transport: str,
    ) -> None:
        labels = self._voice_labels(
            client_kind=client_kind,
            transport=transport,
        )
        labels["voice_state"] = self._voice_value(state, _VOICE_STATES, "voice_state")
        labels["voice_reason"] = self._voice_value(
            reason,
            _VOICE_REASONS,
            "voice_reason",
        )
        self.record("voice_state_transition_total", labels=labels)

    def record_voice_event(
        self,
        event: str,
        outcome: str,
        *,
        reason: str = "none",
        client_kind: str | None = None,
        transport: str | None = None,
    ) -> None:
        checked_event = self._voice_value(event, _VOICE_EVENTS, "voice_event")
        checked_outcome = self._voice_value(
            outcome,
            _VOICE_EVENT_OUTCOMES,
            "voice_outcome",
        )
        checked_reason = self._voice_value(
            reason,
            _VOICE_EVENT_REASONS,
            "voice_event_reason",
        )
        if (client_kind is None) != (transport is None):
            raise ValueError("voice event client and transport must be paired")
        labels = (
            self._base_labels()
            if client_kind is None
            else self._voice_labels(
                client_kind=client_kind,
                transport=transport,
            )
        )
        labels["result_code"] = checked_outcome
        labels["voice_reason"] = checked_reason
        self.record(f"voice_{checked_event}_total", labels=labels)

    def observe_voice_timing(
        self,
        timing: str,
        duration_seconds: float,
        *,
        client_kind: str,
        transport: str,
    ) -> None:
        checked_timing = self._voice_value(timing, _VOICE_TIMINGS, "voice_timing")
        self._set(
            f"voice_{checked_timing}_seconds",
            duration_seconds,
            self._voice_labels(client_kind=client_kind, transport=transport),
        )

    def observe_voice_count(
        self,
        count: str,
        value: int,
        *,
        client_kind: str,
        transport: str,
    ) -> None:
        checked_count = self._voice_value(count, _VOICE_COUNTS, "voice_count")
        self._set(
            f"voice_{checked_count}",
            value,
            self._voice_labels(client_kind=client_kind, transport=transport),
        )

    def record_voice_cleanup(
        self,
        outcome: str,
        *,
        client_kind: str,
        transport: str,
    ) -> None:
        labels = self._voice_labels(client_kind=client_kind, transport=transport)
        labels["cleanup_outcome"] = self._voice_value(
            outcome,
            _VOICE_CLEANUP_OUTCOMES,
            "cleanup_outcome",
        )
        self.record("voice_cleanup_total", labels=labels)

    @staticmethod
    def _background_outcome(state: object) -> str | None:
        try:
            return _BACKGROUND_OUTCOMES.get(state)  # type: ignore[arg-type]
        except TypeError:
            return None

    @staticmethod
    def _as_utc_datetime(value: object) -> datetime:
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("background latency requires aware timestamps")
        # Normalize to UTC first, or DST skews the elapsed time
        return value.astimezone(UTC)

    @staticmethod
    def _finite_duration(span: object) -> float:
        seconds = span.total_seconds()
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("background latency span must be numeric seconds")
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(
                "background latency span must be finite and non-negative"
            )
        return seconds

    def _record_background_skip(self, reason: str) -> None:
        labels = self._base_labels()
        labels["result_code"] = reason
        key = self._key("background_operation_latency_skipped_total", labels)
        with self._lock:
            self._values[key] = self._validate_value(self._values.get(key, 0) + 1)

    def observe_background_operation(self, operation: object) -> None:
        state = getattr(operation, "state", None)
        accepted_at = getattr(operation, "accepted_at", None)
        started_at = getattr(operation, "started_at", None)
        terminal_at = getattr(operation, "terminal_at", None)

        outcome = self._background_outcome(state)
        if outcome is None:
            self._record_background_skip("invalid_state")
            return
        if accepted_at is None or terminal_at is None:
            self._record_background_skip("missing_timestamp")
            return
        started = started_at is not None
        try:
            accepted_at = self._as_utc_datetime(accepted_at)
            terminal_at = self._as_utc_datetime(terminal_at)
            if started:
                started_at = self._as_utc_datetime(started_at)
            valid_order = (
                accepted_at <= started_at <= terminal_at
                if started else accepted_at <= terminal_at
            )
            durations = ()
            if valid_order:
                if started:
                    spans = (
                        ("queue_wait", started_at - accepted_at),
                        ("execution", terminal_at - started_at),
                        ("end_to_end", terminal_at - accepted_at),
                    )
                else:
                    spans = (
                        ("queue_wait", terminal_at - accepted_at),
                        ("end_to_end", terminal_at - accepted_at),
                    )
                durations = tuple(
                    (phase, self._finite_duration(span)) for phase, span in spans
                )
        except Exception:
            self._record_background_skip("invalid_timestamp")
            return
        if not valid_order:
            self._record_background_skip("invalid_order")
            return

        with self._lock:
            staging: dict[
                tuple[str, tuple[tuple[str, str], ...]], int | float
            ] = {}
            for phase, duration in durations:
                labels = self._base_labels()
                labels["phase"] = phase
                labels["result_code"] = outcome
                sum_key = self._key(
                    "background_operation_latency_seconds_sum", labels
                )
                staging[sum_key] = self._validate_value(
                    self._values.get(sum_key, 0.0) + duration
                )
                count_key = self._key(
                    "background_operation_latency_seconds_count", labels
                )
                staging[count_key] = self._validate_value(
                    self._values.get(count_key, 0) + 1
                )
                for token, bound in _BACKGROUND_LATENCY_BUCKETS:
                    bucket_labels = dict(labels)
                    bucket_labels["latency_bucket"] = token
                    bucket_key = self._key(
                        "background_operation_latency_seconds_bucket",
                        bucket_labels,
                    )
                    current = self._values.get(bucket_key, 0)
                    staging[bucket_key] = self._validate_value(
                        current + (1 if duration <= bound else 0)
                    )
            self._values.update(staging)

    def snapshot(self) -> tuple[RuntimeMetricSample, ...]:
        with self._lock:
            items = tuple(sorted(self._values.items(), key=lambda item: item[0]))
        return tuple(
            RuntimeMetricSample(
                name=name,
                value=value,
                labels=dict(label_items),
            )
            for (name, label_items), value in items
        )


__all__ = ["RuntimeMetricSample", "RuntimeObservability"]
