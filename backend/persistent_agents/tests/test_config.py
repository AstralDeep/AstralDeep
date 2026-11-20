"""Tests for persistent_agents/config.py: disabled by default, operator bounds are
honored, and invalid polling intervals are refused.
"""

import pytest
from persistent_agents.config import RunnerConfig
from shared.feature_flags import FeatureFlags


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FF_PERSISTENT_AGENTS", raising=False)
    assert not FeatureFlags().is_enabled("persistent_agents")


def test_operator_bounds(monkeypatch):
    monkeypatch.delenv("PERSISTENT_AGENTS_TICK_SECONDS", raising=False)
    assert RunnerConfig.from_environment() == RunnerConfig()
    monkeypatch.setenv("PERSISTENT_AGENTS_TICK_SECONDS", "25")
    assert RunnerConfig.from_environment().tick_seconds == 25


@pytest.mark.parametrize("value", ["", "no", "0", "31", "1.5"])
def test_invalid_poll_interval_refused(monkeypatch, value):
    monkeypatch.setenv("PERSISTENT_AGENTS_TICK_SECONDS", value)
    with pytest.raises(ValueError, match="PERSISTENT_AGENTS_TICK_SECONDS"):
        RunnerConfig.from_environment()
