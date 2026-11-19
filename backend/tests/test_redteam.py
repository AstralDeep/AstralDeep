"""Tests for backend/orchestrator/redteam.py: the FF_REDTEAM_SELFTEST flag and the stable
code agentic_creation.py refuses approval with while the retired gate is requested.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import redteam  # noqa: E402


def test_redteam_default_off(monkeypatch):
    monkeypatch.delenv("FF_REDTEAM_SELFTEST", raising=False)
    assert redteam.redteam_enabled() is False


@pytest.mark.parametrize("v", ["true", "1", "yes", "on"])
def test_redteam_on(monkeypatch, v):
    monkeypatch.setenv("FF_REDTEAM_SELFTEST", v)
    assert redteam.redteam_enabled() is True


def test_unavailable_code_is_stable():
    assert redteam.UNAVAILABLE_CODE == "redteam_unavailable"
