"""Exercises source-bound review and physical-effect refusal through host dispatch.
The fixture records actual invocation rather than trusting an agent response.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import hitl_confirmation as hc
from orchestrator import source_bound
from orchestrator.source_bound import declaration as strict_declaration
from shared.protocol import MCPResponse
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401


@pytest.fixture
def runtime(gate_orchestrator, monkeypatch):  # noqa: F811
    orch, ws = gate_orchestrator
    monkeypatch.setattr(source_bound, "declaration", strict_declaration)
    orch.tool_permissions.get_tool_scope_map = MagicMock(return_value={
        "send_email": "tools:write", "read_note": "tools:read"})
    orch.audit_recorder = SimpleNamespace(record=AsyncMock(return_value=object()))
    orch._execute_with_retry = AsyncMock(return_value=MCPResponse(result={"ok": True}))
    monkeypatch.setattr("personalization.phi_gate.get_phi_gate",
                        lambda: SimpleNamespace(contains_phi=lambda _text: False))
    return orch, ws


def _call(tool, args):
    return SimpleNamespace(id="source-bound-test", function=SimpleNamespace(
        name=tool, arguments=json.dumps(args)))


@pytest.mark.asyncio
async def test_read_continues_and_effect_requests_exact_review(runtime):
    orch, ws = runtime
    read = await orch.execute_single_tool(
        ws, _call("read_note", {"id": "safe"}), {"read_note": "agent-a"},
        "chat-a", user_id="owner-a")
    assert read.error is None
    effect = await orch.execute_single_tool(
        ws, _call("send_email", {"to": "a@example.org", "body": "note"}),
        {"send_email": "agent-a"}, "chat-a", user_id="owner-a")
    assert effect.result["_data"]["status"] == "confirmation_required"
    assert effect.ui_components
    orch._execute_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_declaration_refuses_before_dispatch(runtime):
    orch, ws = runtime
    orch.tool_permissions.get_tool_scope_map.return_value = {}
    response = await orch.execute_single_tool(
        ws, _call("send_email", {"to": "a@example.org"}),
        {"send_email": "agent-a"}, "chat-a", user_id="owner-a")
    assert response.error["code"] == "effect_declaration_unavailable"
    orch._execute_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_receipt_failure_prevents_review(runtime):
    orch, ws = runtime
    orch.audit_recorder.record.return_value = None
    response = await orch.execute_single_tool(
        ws, _call("send_email", {"to": "a@example.org"}),
        {"send_email": "agent-a"}, "chat-a", user_id="owner-a")
    assert "audit" in response.error["message"].lower()
    orch._execute_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_policy_refuses_effect(runtime, monkeypatch):
    from orchestrator import policy

    orch, ws = runtime
    monkeypatch.setattr(policy, "policy_enabled", lambda: True)
    monkeypatch.setenv("POLICY_RULES", "{malformed")
    response = await orch.execute_single_tool(
        ws, _call("send_email", {"to": "a@example.org"}),
        {"send_email": "agent-a"}, "chat-a", user_id="owner-a")
    assert response.error["code"] == "effect_policy_unavailable"
    orch._execute_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_physical_effect_requires_one_current_exact_approval(runtime):
    orch, ws = runtime
    effects = []

    class Adapter:
        mode = "off"

        async def execute(self, **kwargs):
            return await kwargs["invoke"](None)

    orch.governed_final_dispatch = Adapter()

    async def invoke(_capabilities):
        effects.append("sent")
        return MCPResponse(result={"ok": True})

    async def attempt(args):
        return await orch._execute_governed_attempt(
            ws, "agent-a", "send_email", args,
            user_id="owner-a", channel="web", audit_correlation_id="test",
            actor_user_id="owner-a", auth_principal="owner-a",
            conversation_id="chat-a", invoke=invoke)

    args = {"to": "a@example.org", "body": "note"}
    assert (await attempt(args)).error["code"] == "source_bound_approval_required"
    assert not effects
    call = hc.PendingCall("id", "owner-a", "chat-a", "agent-a", "send_email",
                          json.dumps(args, sort_keys=True, separators=(",", ":")),
                          source_bound.review_risks(strict_declaration(
                              {"send_email": "tools:write"}, "agent-a", "send_email")),
                          time.monotonic() + 60, websocket=ws,
                          cancellation=hc.cancellation_witness(orch, ws, "owner-a", "chat-a", create=True))
    approval = hc.Approval(orch, call, consumed=True)
    token = hc._APPROVAL.set(approval)
    try:
        assert (await attempt({**args, "to": "b@example.org"})).error["code"] == "source_bound_approval_required"
        assert (await attempt(args)).error is None
        assert effects == ["sent"]
        assert (await attempt(args)).error["code"] == "source_bound_approval_required"
    finally:
        hc._APPROVAL.reset(token)


@pytest.mark.asyncio
async def test_pre_effect_audit_failure_has_zero_effect(runtime):
    orch, ws = runtime
    orch.audit_recorder.record.return_value = None
    args = {"to": "a@example.org"}
    call = hc.PendingCall("id", "owner-a", "chat-a", "agent-a", "send_email",
                          json.dumps(args, sort_keys=True, separators=(",", ":")),
                          ("source_bound",), time.monotonic() + 60, websocket=ws,
                          cancellation=hc.cancellation_witness(orch, ws, "owner-a", "chat-a", create=True))
    token = hc._APPROVAL.set(hc.Approval(orch, call, consumed=True))
    effects = []

    class Adapter:
        mode = "off"

        async def execute(self, **kwargs):
            return await kwargs["invoke"](None)

    orch.governed_final_dispatch = Adapter()
    try:
        response = await orch._execute_governed_attempt(
            ws, "agent-a", "send_email", args,
            user_id="owner-a", channel="web", audit_correlation_id="test",
            actor_user_id="owner-a", auth_principal="owner-a",
            conversation_id="chat-a", invoke=lambda _cap: effects.append("sent"))
        assert response.error["code"] == "effect_audit_unavailable"
        assert not effects
    finally:
        hc._APPROVAL.reset(token)


def test_expired_or_revoked_approval_cannot_reach_physical_effect(runtime):
    orch, ws = runtime
    args = {"to": "a@example.org"}
    call = hc.PendingCall("id", "owner-a", "chat-a", "agent-a", "send_email",
                          json.dumps(args, sort_keys=True, separators=(",", ":")),
                          ("source_bound",), time.monotonic() - 1, websocket=ws,
                          cancellation=hc.cancellation_witness(orch, ws, "owner-a", "chat-a", create=True))
    token = hc._APPROVAL.set(hc.Approval(orch, call, consumed=True))
    try:
        assert hc.current_approval(
            orch, "owner-a", "chat-a", "agent-a", "send_email", args) is None
    finally:
        hc._APPROVAL.reset(token)


@pytest.mark.asyncio
async def test_missing_completion_audit_reports_uncertain_outcome(runtime):
    orch, ws = runtime
    orch.audit_recorder.record.side_effect = [object(), None]
    args = {"to": "a@example.org"}
    call = hc.PendingCall("id", "owner-a", "chat-a", "agent-a", "send_email",
                          json.dumps(args, sort_keys=True, separators=(",", ":")),
                          ("source_bound",), time.monotonic() + 60, websocket=ws,
                          cancellation=hc.cancellation_witness(orch, ws, "owner-a", "chat-a", create=True))
    token = hc._APPROVAL.set(hc.Approval(orch, call, consumed=True))
    effects = []

    class Adapter:
        mode = "off"

        async def execute(self, **kwargs):
            return await kwargs["invoke"](None)

    async def invoke(_capabilities):
        effects.append("sent")
        return MCPResponse(result={"ok": True})

    orch.governed_final_dispatch = Adapter()
    try:
        response = await orch._execute_governed_attempt(
            ws, "agent-a", "send_email", args,
            user_id="owner-a", channel="web", audit_correlation_id="test",
            actor_user_id="owner-a", auth_principal="owner-a",
            conversation_id="chat-a", invoke=invoke)
        assert effects == ["sent"]
        assert response.error["code"] == "outcome_uncertain"
        assert hc._APPROVAL.get().effect_started is True
    finally:
        hc._APPROVAL.reset(token)
