"""Tests that scheduler execution defaults off via FF_SCHEDULER_EXECUTION while
memory_chat defaults on, guarding shared/feature_flags.py's fail-closed default.
"""

import importlib
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def test_scheduler_execution_defaults_off(monkeypatch):
    monkeypatch.delenv("FF_SCHEDULER_EXECUTION", raising=False)
    import shared.feature_flags as ff
    importlib.reload(ff)
    assert ff.flags.is_enabled("scheduler_execution") is False


def test_scheduler_execution_opt_in(monkeypatch):
    monkeypatch.setenv("FF_SCHEDULER_EXECUTION", "true")
    import shared.feature_flags as ff
    importlib.reload(ff)
    assert ff.flags.is_enabled("scheduler_execution") is True
    monkeypatch.delenv("FF_SCHEDULER_EXECUTION", raising=False)
    importlib.reload(ff)


def test_memory_chat_flag_defaults_on(monkeypatch):
    monkeypatch.delenv("FF_MEMORY_CHAT", raising=False)
    import shared.feature_flags as ff
    importlib.reload(ff)
    assert ff.flags.is_enabled("memory_chat") is True
