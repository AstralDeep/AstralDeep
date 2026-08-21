"""T029 (056-delegated-agent-chaining): the US2 machinery ships DARK (FR-016).

Machine-turn authority inherits the pending offline-grant security review (025
T057 / 030 FR-004/FR-005): with ``FF_SCHEDULER_EXECUTION`` off — the default —
the scheduler execution loop never starts, so no scheduled run derives
authority or dispatches anything. The review gate is inherited, not bypassed.
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.feature_flags import FeatureFlags  # noqa: E402


def test_scheduler_execution_defaults_off(monkeypatch):
    monkeypatch.delenv("FF_SCHEDULER_EXECUTION", raising=False)
    assert FeatureFlags().is_enabled("scheduler_execution") is False


def test_execution_loop_is_flag_gated():
    """The loop that would call JobRunner.run_job only starts behind the flag."""
    from orchestrator.orchestrator import Orchestrator

    src = inspect.getsource(Orchestrator)
    assert 'flags.is_enabled("scheduler_execution")' in src


def test_flag_off_starts_no_scheduler_loop(orchestrator_factory):
    o = orchestrator_factory()
    # Nothing constructs a scheduler loop at init; it is created only inside
    # the flag-gated start path.
    assert getattr(o, "_scheduler_loop", None) is None


def test_consent_capture_is_independent_of_the_flag():
    """Capture + threading may ship dark: the code exists and is wired, it
    simply never executes a run while the review gate is closed."""
    from orchestrator import scheduling_chat

    src = inspect.getsource(scheduling_chat)
    assert "_capture_consent" in src
    assert "scheduler_execution" not in src  # capture is not flag-gated


def test_machine_authority_module_has_no_flag_bypass():
    """chain_authority never reads the flag — it cannot re-open the gate."""
    from orchestrator import chain_authority

    src = inspect.getsource(chain_authority)
    assert "scheduler_execution" not in src
    assert "FF_SCHEDULER_EXECUTION" not in src
