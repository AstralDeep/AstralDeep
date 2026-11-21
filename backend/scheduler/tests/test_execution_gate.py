"""Tests that scheduler execution defaults off via FF_SCHEDULER_EXECUTION while
memory_chat defaults on, guarding shared/feature_flags.py's fail-closed default.
"""

import importlib
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture
def fresh_flags():
    import shared.feature_flags as ff

    original = ff.flags
    try:
        yield ff
    finally:
        ff.flags = original


def test_scheduler_execution_defaults_off(monkeypatch, fresh_flags):
    monkeypatch.delenv("FF_SCHEDULER_EXECUTION", raising=False)
    importlib.reload(fresh_flags)
    assert fresh_flags.flags.is_enabled("scheduler_execution") is False


def test_scheduler_execution_opt_in(monkeypatch, fresh_flags):
    monkeypatch.setenv("FF_SCHEDULER_EXECUTION", "true")
    importlib.reload(fresh_flags)
    assert fresh_flags.flags.is_enabled("scheduler_execution") is True
    monkeypatch.delenv("FF_SCHEDULER_EXECUTION", raising=False)
    importlib.reload(fresh_flags)


def test_memory_chat_flag_defaults_on(monkeypatch, fresh_flags):
    monkeypatch.delenv("FF_MEMORY_CHAT", raising=False)
    importlib.reload(fresh_flags)
    assert fresh_flags.flags.is_enabled("memory_chat") is True
