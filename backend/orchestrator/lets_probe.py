"""Bounded, cached live reachability probe for the LETS warden: runs a cheap GET
/health/ready on a worker thread, waits a short bounded window, and caches the result
for a configurable interval. Backs lets_composition.py and lets_health_api.py.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

from orchestrator.lets_client import LetsClientBoundaryError
from orchestrator.lets_health import LetsRuntimeObservation

PROBE_INTERVAL_ENV = "LETS_HEALTH_PROBE_INTERVAL_SECONDS"
DEFAULT_PROBE_INTERVAL_SECONDS = 30.0
MAX_PROBE_INTERVAL_SECONDS = 3_600.0
PROBE_WAIT_SECONDS = 2.0


class LetsProbeConfigError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def probe_interval_seconds(environ: Mapping[str, str] | None = None) -> float:
    raw = None if environ is None else environ.get(PROBE_INTERVAL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_PROBE_INTERVAL_SECONDS
    try:
        value = float(raw.strip())
    except ValueError:
        raise LetsProbeConfigError("invalid_health_probe_interval") from None
    if not (1.0 <= value <= MAX_PROBE_INTERVAL_SECONDS):
        raise LetsProbeConfigError("invalid_health_probe_interval")
    return value


def _observation_for_failure(code: object, at_ns: int) -> LetsRuntimeObservation:
    if code == "authentication_failed":
        return LetsRuntimeObservation("trust_failed", at_ns)
    if code in {"client_closed", "client_not_configured"}:
        return LetsRuntimeObservation("unavailable", at_ns, retryable=False)
    return LetsRuntimeObservation("unavailable", at_ns, retryable=True)


class LetsReachabilityProbe:
    def __init__(
        self,
        probe: Callable[[], object],
        *,
        interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS,
        wait_seconds: float = PROBE_WAIT_SECONDS,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(probe):
            raise TypeError("probe must be callable")
        if not (1.0 <= float(interval_seconds) <= MAX_PROBE_INTERVAL_SECONDS):
            raise LetsProbeConfigError("invalid_health_probe_interval")
        if not (0.0 < float(wait_seconds) <= PROBE_WAIT_SECONDS):
            raise LetsProbeConfigError("invalid_health_probe_wait")
        self._probe = probe
        self._interval = float(interval_seconds)
        self._wait = float(wait_seconds)
        self._clock_ns = clock_ns
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._observation: LetsRuntimeObservation | None = None
        self._checked_at: float | None = None
        self._inflight: threading.Event | None = None
        self._closed = False
        self.attempts = 0

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def cached(self) -> LetsRuntimeObservation | None:
        with self._lock:
            return self._observation

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def refresh_if_due(self, *, force: bool = False) -> LetsRuntimeObservation | None:
        with self._lock:
            if self._closed:
                return self._observation
            now = self._monotonic()
            fresh = (
                self._checked_at is not None
                and now - self._checked_at < self._interval
            )
            if fresh and not force:
                return self._observation
            if self._inflight is None:
                done = threading.Event()
                self._inflight = done
                self.attempts += 1
                thread = threading.Thread(
                    target=self._run,
                    args=(done,),
                    name="lets-reachability-probe",
                    daemon=True,
                )
                thread.start()
            else:
                done = self._inflight
        if not done.wait(self._wait):
            with self._lock:
                if self._inflight is done:
                    self._observation = LetsRuntimeObservation(
                        "unavailable", self._clock_ns(), retryable=True
                    )
                    self._checked_at = self._monotonic()
        with self._lock:
            return self._observation

    def _run(self, done: threading.Event) -> None:
        try:
            self._probe()
        except LetsClientBoundaryError as exc:
            observation = _observation_for_failure(exc.code, self._clock_ns())
        except BaseException:
            observation = _observation_for_failure(None, self._clock_ns())
        else:
            observation = LetsRuntimeObservation("healthy", self._clock_ns())
        with self._lock:
            self._observation = observation
            self._checked_at = self._monotonic()
            if self._inflight is done:
                self._inflight = None
        done.set()


__all__ = (
    "DEFAULT_PROBE_INTERVAL_SECONDS",
    "PROBE_INTERVAL_ENV",
    "PROBE_WAIT_SECONDS",
    "LetsProbeConfigError",
    "LetsReachabilityProbe",
    "probe_interval_seconds",
)
