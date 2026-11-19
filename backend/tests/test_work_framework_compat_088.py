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


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [False, True])
async def test_retired_submission_can_only_read_an_existing_receipt(replay):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from uuid import uuid4

    from orchestrator.framework_credentials import FrameworkCaller
    from orchestrator.work_operations import FrameworkWorkOperations
    from persistent_agents.models import AssignmentError
    from tests.test_work_api_088 import operation

    read = operation()
    read.assignment.safe_error_code = "turn_engine_retired"
    caller = FrameworkCaller("owner", str(uuid4()), frozenset({"operations.submit"}), "a" * 64)
    repo, credentials = Mock(), Mock(assert_execution=Mock())
    repo.get_operation_receipt.return_value = SimpleNamespace(assignment_id=read.assignment.assignment_id) if replay else None
    repo.get_operation.return_value = read
    tx = object()
    async def transaction(callback, **kwargs):
        return callback(tx, repo)
    assignments = SimpleNamespace(store=SimpleNamespace(transaction=transaction),
                                  phi_gate=SimpleNamespace(contains_phi=lambda _: False))
    ops = FrameworkWorkOperations(assignments=assignments, credentials=credentials)
    request = ops.submit(caller, idempotency_key="previous-work", name="Assignment", instructions="Produce a summary")
    if replay:
        result = await request
        assert result["created"] is False and result["id"] == read.assignment.assignment_id
        assert result["safe_error_code"] == "turn_engine_retired"
    else:
        with pytest.raises(AssignmentError, match="turn_engine_retired") as refused:
            await request
        assert refused.value.status_code == 410
    credentials.assert_execution.assert_called_once_with(tx, credentials.fresh_observation.return_value)
    repo.create_operation.assert_not_called()
    credentials.consume_admission.assert_not_called()
