"""Bounded, cached LIVE reachability observation of the LETS warden.

Composition binds the warden client exactly once and sends nothing, so a
composed graph proves only that configuration and credentials were loadable:
a down, unresolvable, or mis-certified warden would otherwise read "healthy"
forever.  This module owns the one place the host actually contacts the warden
for posture: a cheap idempotent ``GET /health/ready`` executed on a worker
thread, waited on for a short bounded window, and cached for a configurable
interval.  It never runs on the tool-dispatch path; ``/readyz`` and the admin
health route are its only callers besides the first probe at composition.

Every value it emits is a ``LetsRuntimeObservation`` status code plus the
probe time: no exception text, response body, or configuration value escapes.
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
# Hard ceiling on how long any caller waits for a probe to answer.
PROBE_WAIT_SECONDS = 2.0


class LetsProbeConfigError(ValueError):
    """Stable refusal for an unusable probe interval (no value echoed)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def probe_interval_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Parse ``LETS_HEALTH_PROBE_INTERVAL_SECONDS`` (default 30, 1..3600)."""

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
    # Transport, timeout, TLS, DNS, 5xx, invalid body: all retryable
    # "unavailable" — the warden may come back without operator action.
    return LetsRuntimeObservation("unavailable", at_ns, retryable=True)


class LetsReachabilityProbe:
    """One cached, single-flight reachability observation per composition.

    ``probe`` is a blocking callable that returns on success and raises
    ``LetsClientBoundaryError`` on failure (``LetsWardenClient.probe``).  It
    runs on a daemon thread; callers of :meth:`refresh_if_due` wait at most
    ``wait_seconds`` and otherwise record a retryable ``unavailable``
    observation so the cache is never empty after the first attempt.  A late
    answer from that same thread still lands (newer evidence wins), and only
    one probe is ever in flight.
    """

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
        """The last observation without touching the network (None = never)."""

        with self._lock:
            return self._observation

    def close(self) -> None:
        """Stop scheduling new probes; the cached observation stays readable."""

        with self._lock:
            self._closed = True

    def refresh_if_due(self, *, force: bool = False) -> LetsRuntimeObservation | None:
        """Return the cached observation, refreshing it first when stale.

        Blocks the calling thread for at most ``wait_seconds``; never call it
        from the event loop directly (``asyncio.to_thread`` it).
        """

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
                # Still unanswered inside the bound: record a retryable
                # unavailable stamp so readiness is honest now, and mark the
                # cache checked so callers do not pile up behind one slow
                # warden. The in-flight thread overwrites this when it lands.
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
