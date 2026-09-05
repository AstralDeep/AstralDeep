"""Operator mistakes cannot silently enlarge unattended execution limits."""

import pytest
from persistent_agents.config import RunnerConfig
from shared.feature_flags import FeatureFlags


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FF_PERSISTENT_AGENTS", raising=False)
    assert not FeatureFlags().is_enabled("persistent_agents")


def test_operator_bounds(monkeypatch):
    for name in ("TICK_SECONDS", "CONCURRENCY", "LEASE_SECONDS"):
        monkeypatch.delenv("PERSISTENT_AGENTS_" + name, raising=False)
    assert RunnerConfig.from_environment() == RunnerConfig()
    monkeypatch.setenv("PERSISTENT_AGENTS_CONCURRENCY", "25")
    assert RunnerConfig.from_environment().concurrency == 25


@pytest.mark.parametrize("value", ["", "no", "0", "26", "1.5"])
def test_invalid_concurrency_refused(monkeypatch, value):
    monkeypatch.setenv("PERSISTENT_AGENTS_CONCURRENCY", value)
    with pytest.raises(ValueError, match="PERSISTENT_AGENTS_CONCURRENCY"):
        RunnerConfig.from_environment()
