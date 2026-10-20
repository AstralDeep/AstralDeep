"""Requires isolated PostgreSQL and drives every paired case through signed turns.
The required module gate fails if effect, audit, control, or cleanup evidence is absent.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from security_benchmark.drivers.real_dispatch import (
    EFFECT,
    RealDispatchQualification,
    run_real_qualification,
)
from security_benchmark.qualification import CASES, validate_evidence


@pytest.mark.asyncio
async def test_signed_turn_security_qualification():
    evidence, teardown_verified = await run_real_qualification()
    assert len(evidence) == len(CASES)
    assert not validate_evidence(CASES, evidence, teardown_verified=teardown_verified)


@pytest.mark.asyncio
async def test_weakened_effect_gate_is_seen_by_physical_oracle(monkeypatch):
    from orchestrator import source_bound

    original = source_bound.declaration

    def weakened(scopes, agent, tool, **kwargs):
        action = original(scopes, agent, tool, **kwargs)
        return replace(action, effect=source_bound.Effect.READ) if tool == EFFECT else action

    driver = RealDispatchQualification("__verif__security_weakened")
    try:
        await driver.setup()
        monkeypatch.setattr(source_bound, "declaration", weakened)
        evidence = await driver.run_case(CASES[0])
        assert evidence.effect_count == 1
        assert evidence.status != "denied"
    finally:
        await driver.teardown()
