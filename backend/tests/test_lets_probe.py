"""Bounded, cached LETS warden reachability probe (``orchestrator.lets_probe``).

Pins: success / failure / timeout map to readiness states with the probe time
as ``observed_at_ns``; the cache honors its TTL and forces; a slow warden
never blocks a caller longer than the wait bound; the interval knob is
validated; nothing from the probe path is ever a value.
"""

from __future__ import annotations

import threading

import pytest

from orchestrator.lets_client import LetsClientBoundaryError
from orchestrator.lets_health import project_lets_health
from orchestrator.lets_probe import (
    DEFAULT_PROBE_INTERVAL_SECONDS,
    PROBE_WAIT_SECONDS,
    LetsProbeConfigError,
    LetsReachabilityProbe,
    probe_interval_seconds,
)
from tests.test_lets_health_readiness import _config, _load


class Clock:
    def __init__(self) -> None:
        self.wall_ns = 1_000
        self.mono = 100.0

    def time_ns(self) -> int:
        return self.wall_ns

    def monotonic(self) -> float:
        return self.mono


def _probe(fn, clock: Clock, **kw) -> LetsReachabilityProbe:
    return LetsReachabilityProbe(
        fn,
        clock_ns=clock.time_ns,
        monotonic=clock.monotonic,
        wait_seconds=kw.pop("wait_seconds", 0.5),
        **kw,
    )


def test_success_is_healthy_stamped_at_probe_time() -> None:
    clock = Clock()
    calls = []
    probe = _probe(lambda: calls.append(1), clock)
    assert probe.cached() is None
    observation = probe.refresh_if_due()
    assert observation is not None
    assert observation.status == "healthy"
    assert observation.observed_at_ns == 1_000
    assert observation.retryable is False
    assert calls == [1]


@pytest.mark.parametrize(
    ("code", "status", "retryable"),
    [
        ("transport_unavailable", "unavailable", True),
        ("request_timeout", "unavailable", True),
        ("remote_unavailable", "unavailable", True),
        ("invalid_response", "unavailable", True),
        ("authentication_failed", "trust_failed", False),
        ("client_closed", "unavailable", False),
    ],
)
def test_boundary_failures_map_to_value_free_states(
    code: str, status: str, retryable: bool
) -> None:
    clock = Clock()

    def fail() -> None:
        raise LetsClientBoundaryError(code, retryable=retryable)

    observation = _probe(fail, clock).refresh_if_due()
    assert observation is not None
    assert observation.status == status
    assert observation.retryable is retryable
    assert observation.observed_at_ns == 1_000


def test_unexpected_exception_is_retryable_unavailable_without_echo() -> None:
    clock = Clock()

    def boom() -> None:
        raise RuntimeError("/etc/astral/lets/secret-must-not-escape")

    observation = _probe(boom, clock).refresh_if_due()
    assert observation is not None
    assert observation.status == "unavailable"
    assert observation.retryable is True
    assert "secret" not in repr(observation)


def test_cache_honors_ttl_and_force() -> None:
    clock = Clock()
    calls = []
    probe = _probe(lambda: calls.append(1), clock, interval_seconds=30)
    probe.refresh_if_due()
    clock.mono += 29.9
    probe.refresh_if_due()
    assert len(calls) == 1
    clock.mono += 0.2
    probe.refresh_if_due()
    assert len(calls) == 2
    probe.refresh_if_due(force=True)
    assert len(calls) == 3
    assert probe.attempts == 3


def test_slow_warden_never_blocks_past_the_wait_bound_and_late_answer_lands() -> None:
    clock = Clock()
    release = threading.Event()
    landed = threading.Event()

    def slow() -> None:
        release.wait(5)
        landed.set()

    probe = _probe(slow, clock, wait_seconds=0.1)
    observation = probe.refresh_if_due()
    # Timed out inside the bound: honest retryable unavailable, cache marked
    # checked so repeated callers do not spawn a second in-flight probe.
    assert observation is not None
    assert observation.status == "unavailable"
    assert observation.retryable is True
    clock.mono += 1
    probe.refresh_if_due()
    assert probe.attempts == 1
    release.set()
    assert landed.wait(2)
    for _ in range(200):
        cached = probe.cached()
        if cached is not None and cached.status == "healthy":
            break
        threading.Event().wait(0.01)
    assert probe.cached().status == "healthy"


def test_closed_probe_stops_scheduling_but_keeps_the_cache() -> None:
    clock = Clock()
    calls = []
    probe = _probe(lambda: calls.append(1), clock)
    probe.refresh_if_due()
    probe.close()
    clock.mono += 1_000
    assert probe.refresh_if_due(force=True).status == "healthy"
    assert len(calls) == 1


def test_observation_projects_into_mode_specific_readiness() -> None:
    clock = Clock()

    def fail() -> None:
        raise LetsClientBoundaryError("transport_unavailable", retryable=True)

    observation = _probe(fail, clock).refresh_if_due()
    enforce = project_lets_health(_load("enforce", config=_config("enforce")), observation)
    shadow = project_lets_health(_load("shadow", config=_config("shadow")), observation)
    assert enforce.component_status == "blocked" and enforce.application_ready is False
    assert shadow.component_status == "degraded" and shadow.application_ready is True
    assert enforce.observed_at_ns == shadow.observed_at_ns == 1_000


def test_interval_knob_defaults_and_bounds() -> None:
    assert probe_interval_seconds(None) == DEFAULT_PROBE_INTERVAL_SECONDS
    assert probe_interval_seconds({}) == DEFAULT_PROBE_INTERVAL_SECONDS
    assert probe_interval_seconds({"LETS_HEALTH_PROBE_INTERVAL_SECONDS": " "}) == 30.0
    assert probe_interval_seconds({"LETS_HEALTH_PROBE_INTERVAL_SECONDS": "5"}) == 5.0
    for bad in ("0", "-1", "abc", "3601", "nan"):
        with pytest.raises(LetsProbeConfigError, match="^invalid_health_probe_interval$"):
            probe_interval_seconds({"LETS_HEALTH_PROBE_INTERVAL_SECONDS": bad})


def test_wait_bound_is_capped_and_probe_must_be_callable() -> None:
    with pytest.raises(LetsProbeConfigError, match="invalid_health_probe_wait"):
        LetsReachabilityProbe(lambda: None, wait_seconds=PROBE_WAIT_SECONDS + 1)
    with pytest.raises(LetsProbeConfigError, match="invalid_health_probe_interval"):
        LetsReachabilityProbe(lambda: None, interval_seconds=0)
    with pytest.raises(TypeError):
        LetsReachabilityProbe(object())  # type: ignore[arg-type]
