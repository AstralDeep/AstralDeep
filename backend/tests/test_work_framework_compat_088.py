"""Tests for orchestrator/work_operations.py's FrameworkWorkOperations against real
Plane: credential-scoped submit/read/cancel through the same repository the REST
router uses, idempotent replay, and refusal of not-yet-wired or human-only commands.
"""

from __future__ import annotations


import pytest
from cryptography.fernet import Fernet



_ALL_SCOPES = ("operations.submit", "operations.read", "operations.control", "artifacts.read")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-work-framework-audit-key")


def test_not_yet_wired_names_are_absent_from_the_dispatchable_and_projected_sets():
    from orchestrator.mcp_projection import _WORK_TOOL_SPECS, project_tools
    from orchestrator.work_operations import DISPATCHABLE_TOOL_NAMES

    for absent in ("astral_resume_operation", "astral_wait_operation", "astral_wake_operation",
                  "astral_list_artifacts", "astral_list_agents"):
        assert absent not in DISPATCHABLE_TOOL_NAMES
        assert absent not in _WORK_TOOL_SPECS
    names = {t.name for t in project_tools(None, "u1", {"sub": "u1", "_framework_scopes": _ALL_SCOPES})}
    assert names == set(DISPATCHABLE_TOOL_NAMES)
