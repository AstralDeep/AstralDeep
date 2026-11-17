"""Runs InProcessDriver.probe_delegation under the development profile the verification harness requires, without a configured signing key."""

from __future__ import annotations

import tempfile

import pytest

from verification.config import RunConfig
from verification.drivers.fixture_identity import FixtureIdentity
from verification.drivers.in_process import InProcessDriver


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.delenv("DELEGATION_CHILD_SIGNING_KEY", raising=False)
    monkeypatch.delenv("MEMORY_HMAC_KEY", raising=False)
    monkeypatch.delenv("ASTRAL_NODE_PROFILE", raising=False)
    run_id = "__verif__deleg0001"
    drv = InProcessDriver(RunConfig(mode="in_process", run_id=run_id, out_dir=tempfile.mkdtemp(prefix="verif_deleg_")))
    drv._fixture_identity = FixtureIdentity(run_id)
    yield drv
    drv._fixture_identity.close()


def test_probe_delegation_mints_and_reports_the_actor_chain_without_a_configured_key(driver):
    evidence = driver.probe_delegation(driver.config.run_id)
    assert evidence.extra["act_sub"] == f"agent:{driver.agent_id}"
    assert "__verif__deleg0001" in evidence.extra["sub"]
