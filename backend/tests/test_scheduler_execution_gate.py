"""Tests that scheduled-run authority ships dark behind FF_SCHEDULER_EXECUTION
(orchestrator/scheduling_chat.py, chain_authority.py): the execution loop never
starts while off, and consent capture stays independent of the flag.
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
    from orchestrator.orchestrator import Orchestrator

    src = inspect.getsource(Orchestrator)
    assert 'flags.is_enabled("scheduler_execution")' in src


def test_flag_off_starts_no_scheduler_loop(orchestrator_factory):
    o = orchestrator_factory()
    assert getattr(o, "_scheduler_loop", None) is None


def test_consent_capture_is_independent_of_the_flag():
    from orchestrator import scheduling_chat

    src = inspect.getsource(scheduling_chat)
    assert "_capture_consent" in src
    assert "scheduler_execution" not in src


def test_machine_authority_module_has_no_flag_bypass():
    from orchestrator import chain_authority

    src = inspect.getsource(chain_authority)
    assert "scheduler_execution" not in src
    assert "FF_SCHEDULER_EXECUTION" not in src
