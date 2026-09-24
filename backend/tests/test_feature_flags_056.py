"""Tests confirming chained-agent delegation reuses the existing recursive_delegation
and scheduler_execution flags (shared/feature_flags.py), both defaulting off, with no
new flag registered.
"""

from shared.feature_flags import FeatureFlags


def _fresh_flags(monkeypatch, **env):
    for var in ("FF_RECURSIVE_DELEGATION", "FF_SCHEDULER_EXECUTION"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    return FeatureFlags()


def test_recursive_delegation_defaults_off(monkeypatch):
    flags = _fresh_flags(monkeypatch)
    assert flags.is_enabled("recursive_delegation") is False


def test_scheduler_execution_defaults_off(monkeypatch):
    flags = _fresh_flags(monkeypatch)
    assert flags.is_enabled("scheduler_execution") is False


def test_flags_enable_via_env(monkeypatch):
    flags = _fresh_flags(
        monkeypatch,
        FF_RECURSIVE_DELEGATION="true",
        FF_SCHEDULER_EXECUTION="1",
    )
    assert flags.is_enabled("recursive_delegation") is True
    assert flags.is_enabled("scheduler_execution") is True


def test_no_new_056_flag_registered(monkeypatch):
    flags = _fresh_flags(monkeypatch)
    names = set(flags._flags)
    assert "recursive_delegation" in names
    assert "scheduler_execution" in names
    assert not any("chain" in n or n.startswith("056") for n in names)
