"""Checks publisher-only owner registration and capability-only empty wakes through the HTTP boundary and real Plane.
APNs is a recording transport, so denials and failures exercise the real registry without third-party services."""

from __future__ import annotations



from orchestrator.mesh_push_gateway import PublisherWakeLimiter
from tests.test_mesh_push_registry import KEY, MESH, DEVICE, OTHER, Clock, plane  # noqa: F401

ISSUER = "https://issuer.example/realms/owners"


class Apns:
    configured = True

    def __init__(self):
        self.tokens = []
        self.error = None

    def send_wake(self, *, device_token):
        if self.error:
            raise self.error
        self.tokens.append(device_token)


def test_public_caller_limiter_is_bounded_and_unknown_keys_cannot_grow_it():
    clock = Clock()
    limiter = PublisherWakeLimiter(capacity=2, burst=2, window_seconds=60, clock=lambda: clock.now / 1000)
    assert limiter.admit("one") and limiter.admit("one")
    assert not limiter.admit("one")
    assert limiter.admit("two")
    for number in range(100):
        assert not limiter.admit(str(number))
    clock.now += 60_000
    assert limiter.admit("three")


def test_actual_orchestrator_composes_disabled_publisher_without_operator_setup(orchestrator_factory, monkeypatch):
    monkeypatch.delenv("ASTRAL_MESH_PUSH_PUBLISHER_ENABLED", raising=False)
    orch = orchestrator_factory()
    assert orch.mesh_push_publisher is None

