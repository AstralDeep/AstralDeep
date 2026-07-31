"""Bounded, payload-free runtime reliability telemetry.

Feature 060 deliberately keeps this collector independent of a monitoring
vendor.  Runtime paths record only low-cardinality, reviewed labels; callers
may expose :meth:`RuntimeObservability.snapshot` through the deployment's
chosen metrics bridge without ever placing user, chat, prompt, credential, or
target identities in labels.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Mapping

from orchestrator.work_admission import AdmissionClassStatus


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
    }
)


@dataclass(frozen=True)
class RuntimeMetricSample:
    """One immutable-by-convention metric projection.

    ``labels`` is copied both when recorded and when projected, so callers
    cannot mutate the collector's internal key space.
    """

    name: str
    value: int | float
    labels: Mapping[str, str]


class RuntimeObservability:
    """Thread-safe low-cardinality gauges and counters for runtime work.

    The class accepts no arbitrary high-cardinality dimensions.  Label names
    are an explicit allow-list and values are bounded snake-case tokens; this
    rejects identifiers, prose, URLs, bearer material, serialized payloads,
    and other accidental sensitive content before it reaches an exporter.
    """

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
        """Increment one counter after enforcing the telemetry boundary."""

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
        """Publish effective limits/counts and queue/running ages."""

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
        """Record the latest bounded operation duration without identity labels."""

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

    def observe_retention(
        self,
        *,
        purged_count: int,
        lag_seconds: float,
    ) -> None:
        """Record bounded retention throughput and current cleanup lag.

        The purge count is cumulative while lag is a last-observed gauge.  No
        operation or submission identity crosses this telemetry boundary.
        """

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
        """Publish the duration and unfinished remainder of the latest drain."""

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

    def snapshot(self) -> tuple[RuntimeMetricSample, ...]:
        """Return a deterministic copy suitable for an exporter or status API."""

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
