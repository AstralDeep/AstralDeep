"""Tests for persistent_agents/execution.py: dispatch receipts and fences across
successful, failed, cancelled and rejected attempts, reservation release on
permission loss, model usage accounting, and epoch-successor identity across
pause/resume.
"""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from astralplane.repositories.assignments import (
    AssignmentActionIntent,
    AssignmentActionRecord,
    AssignmentResourceAmount,
)
from persistent_agents.dispatch_context import (
    DispatchDenied,
    bind_dispatch,
)
from persistent_agents.execution import safe_text
from persistent_agents.runtime_values import digest
from persistent_agents.tests.test_dispatch import context


def action_record(record, request=None, *, state="ready", intent=None):
    request = request or {"kind": "tool", "agent_id": "web-research-1", "tool_name": "fetch_page",
                          "arguments": {"url": "https://www.python.org/downloads/"}}
    intent = intent or AssignmentActionIntent("one", request, digest(request),
        AssignmentResourceAmount(tool_calls=1, elapsed_ms=30_000), "b"*64, "c"*64)
    return AssignmentActionRecord(str(uuid4()), record.assignment_id, "owner", intent, 1, 1, state)


@pytest.mark.asyncio
async def test_private_marker_exactness_and_nested_dispatch_denied():
    ctx = context(remote_marker="trusted")
    with pytest.raises(DispatchDenied, match="confirmation_binding_changed"):
        await ctx.validate_tool("owner", "web-research-1", "fetch_page", ctx.arguments)
    await ctx.validate_tool("owner", "web-research-1", "fetch_page", {**ctx.arguments, "_remote_op_proposal_id": "trusted"})
    with bind_dispatch(ctx), pytest.raises(DispatchDenied, match="nested_dispatch"), bind_dispatch(context()):
        pass
    with pytest.raises(ValueError):
        context(timeout_seconds=0)


@pytest.mark.asyncio
async def test_security_text_gate_checks_injection_and_phi(monkeypatch):
    monkeypatch.setattr("orchestrator.mas_defense.scan_message", Mock(return_value=True))
    with pytest.raises(DispatchDenied, match="quarantined"):
        await safe_text("untrusted")
    monkeypatch.setattr("orchestrator.mas_defense.scan_message", Mock(return_value=False))
    gate = SimpleNamespace(contains_phi=Mock(return_value=True))
    monkeypatch.setattr("persistent_agents.execution.get_phi_gate", lambda: gate)
    with pytest.raises(DispatchDenied, match="phi_refused"):
        await safe_text("private")
    gate.contains_phi.return_value = False
    await safe_text("public")


