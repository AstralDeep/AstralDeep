"""Requires isolated PostgreSQL and drives every paired case through signed turns.
The required module gate fails if effect, audit, control, or cleanup evidence is absent.
"""

from __future__ import annotations

import pytest

from security_benchmark.drivers.real_dispatch import run_real_qualification
from security_benchmark.qualification import CASES, validate_evidence


@pytest.mark.asyncio
async def test_signed_turn_security_qualification():
    evidence, teardown_verified = await run_real_qualification()
    assert len(evidence) == len(CASES)
    assert not validate_evidence(CASES, evidence, teardown_verified=teardown_verified)
