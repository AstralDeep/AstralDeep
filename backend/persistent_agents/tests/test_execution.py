"""Action receipts, actual-dispatch fences and shared usage bounds."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from astralplane.repositories.assignments import (
    AssignmentActionIntent,
    AssignmentActionRecord,
    AssignmentResourceAmount,
)
from orchestrator.chain_authority import AuthoritySkip
from persistent_agents.dispatch_context import (
    DispatchDenied,
    bind_dispatch,
    current_dispatch,
)
from persistent_agents.execution import ActionExecutor, ApprovalPending, safe_text
from persistent_agents.models import CreateAssignmentRequest
from persistent_agents.runtime_values import digest, thaw
from persistent_agents.tests.test_dispatch import context
from persistent_agents.tests.test_models import create_payload
from persistent_agents.tests.test_service import service as shared_service
from shared.protocol import MCPResponse

service = shared_service


@pytest.fixture
async def executor(service, monkeypatch):
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()))
    monkeypatch.setattr("persistent_agents.execution.safe_text", AsyncMock())
    operation = SimpleNamespace(operation_id=uuid4(), execution_generation=1, execution_lease_token=uuid4())
    claim = SimpleNamespace(assignment=record, fence=object())
    socket = object()
    service.orch.ui_sessions[socket] = {"sub": "owner"}
    service.orch.work_admission = SimpleNamespace(assert_current_execution=Mock())
    service.orch.derive_machine_authority = AsyncMock(return_value=object())
    service.orch._bind_machine_turn = Mock()
    service.validate_execution = AsyncMock(return_value={"permission_digest": "b"*64, "precondition_digest": "c"*64})
    store = service.store
    store.repository = SimpleNamespace(start_action=Mock(return_value=SimpleNamespace(dispatch_token="private-permit")))
    outcomes = []
    calls = []
    async def call(method, **kwargs):
        calls.append((method, kwargs))
        if method == "assert_current_claim":
            return record
        if method == "reserve_action":
            return SimpleNamespace(created=True)
        if method == "record_action_outcome":
            outcomes.append(kwargs["outcome"])
            return SimpleNamespace(result={"result": kwargs["outcome"].result})
        if method in ("release_unstarted_action", "get_action_by_key"):
            return None
        if method == "put_action":
            return action_record(record, kwargs["intent"].request, intent=kwargs["intent"])
        raise AssertionError(method)
    store.call = AsyncMock(side_effect=call)
    async def transaction(callback):
        return callback("transaction", store.repository)
    store.transaction = AsyncMock(side_effect=transaction)
    runner = SimpleNamespace(orch=service.orch, service=service)
    engine = ActionExecutor(runner, claim, operation, socket)
    engine.test_outcomes = outcomes
    engine.test_calls = calls
    async def execute_tool(ws, tool_call, mapping, **kwargs):
        ctx = current_dispatch()
        arguments = json.loads(tool_call.function.arguments)
        await ctx.validate_tool("owner", mapping[tool_call.function.name], tool_call.function.name, arguments)
        response = MCPResponse(result={"text": "Public release changed"})
        return await ctx.invoke_tool(AsyncMock(return_value=response))
    service.orch.execute_single_tool = AsyncMock(side_effect=execute_tool)
    return engine


def action_record(record, request=None, *, state="ready", intent=None):
    request = request or {"kind": "tool", "agent_id": "web-research-1", "tool_name": "fetch_page",
                          "arguments": {"url": "https://www.python.org/downloads/"}}
    intent = intent or AssignmentActionIntent("one", request, digest(request),
        AssignmentResourceAmount(tool_calls=1, elapsed_ms=30_000), "b"*64, "c"*64)
    return AssignmentActionRecord(str(uuid4()), record.assignment_id, "owner", intent, 1, 1, state)


@pytest.mark.asyncio
async def test_actual_tool_dispatch_records_receipt_and_never_releases_begun_capacity(executor):
    action = action_record(executor.record)
    result = await executor.execute(action)
    assert "Public release changed" in result["text"]
    assert executor.test_outcomes[0].outcome == "succeeded"
    assert executor.test_outcomes[0].actual.tool_calls == 1
    assert executor.store.repository.start_action.call_args.kwargs["current_permission_digest"] == "b"*64
    assert not any(method == "release_unstarted_action" for method, _ in executor.test_calls)
    executor.orch._bind_machine_turn.assert_called()


@pytest.mark.asyncio
async def test_completed_receipt_is_reused_without_dispatch_and_ambiguity_never_retries(executor):
    action = replace(action_record(executor.record), state="succeeded", result={"result": {"text": "retained"}})
    assert await executor.execute(action) == {"text": "retained"}
    for state in ("proposed", "approved", "uncertain", "started", "reconciliation", "declined", "invalidated", "expired"):
        with pytest.raises((ApprovalPending, DispatchDenied)):
            await executor.execute(replace(action, state=state))
    executor.orch.execute_single_tool.assert_not_called()
    executor.store.call.assert_not_called()


@pytest.mark.asyncio
async def test_permission_loss_before_dispatch_releases_only_unstarted_reservation(executor):
    executor.service.validate_execution.return_value["permission_digest"] = "d"*64
    with pytest.raises(DispatchDenied, match="precondition_changed"):
        await executor.execute(action_record(executor.record))
    executor.store.repository.start_action.assert_not_called()
    assert executor.test_calls[-1][0] == "release_unstarted_action"
    assert not executor.test_outcomes


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["read_only", "unreplayable"])
async def test_physical_failure_is_charged_and_only_known_reads_can_retry(executor, boundary):
    async def failure(*args, **kwargs):
        return await current_dispatch().invoke_tool(AsyncMock(side_effect=ConnectionError("private error")))
    executor.orch.execute_single_tool.side_effect = failure
    action = action_record(executor.record)
    action = replace(action, intent=replace(action.intent, boundary=boundary))
    with pytest.raises(ConnectionError if boundary == "read_only" else DispatchDenied):
        await executor.execute(action)
    assert executor.test_outcomes[0].outcome == ("failed" if boundary == "read_only" else "uncertain")
    assert not any(method == "release_unstarted_action" for method, _ in executor.test_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["null_response", "gate_error", "no_physical_call", "duplicate_reservation"])
async def test_gate_refusal_or_missing_physical_dispatch_never_records_success(executor, failure):
    if failure == "duplicate_reservation":
        old = executor.store.call.side_effect
        async def call(method, **kwargs):
            return SimpleNamespace(created=False) if method == "reserve_action" else await old(method, **kwargs)
        executor.store.call.side_effect = call
    else:
        executor.orch.execute_single_tool.side_effect = None
        executor.orch.execute_single_tool.return_value = None if failure == "null_response" else (
            MCPResponse(error={"message": "denied"}) if failure == "gate_error" else MCPResponse(result={"text": "fake"}))
    with pytest.raises(DispatchDenied):
        await executor.execute(action_record(executor.record))
    assert not executor.test_outcomes
    executor.store.repository.start_action.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("content,usage", [("Result", 42), ("Result", None), ("", None), ("x"*9000, None)])
async def test_model_usage_and_invalid_output_are_durably_accounted(executor, content, usage):
    request = {"kind": "model", "messages": [{"role": "user", "content": "Analyze"}], "max_output_tokens": 1024}
    action = action_record(executor.record, request, intent=AssignmentActionIntent(
        "model", request, digest(request), AssignmentResourceAmount(model_calls=1, tokens=2048, elapsed_ms=30_000),
        "b"*64, "c"*64))
    async def model(*args, **kwargs):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                                   usage=SimpleNamespace(total_tokens=usage))
        await current_dispatch().invoke_model(AsyncMock(return_value=response), {"messages": request["messages"]})
        return SimpleNamespace(content=content), None
    executor.orch._call_llm = AsyncMock(side_effect=model)
    if not content or len(content) > 8192:
        with pytest.raises(DispatchDenied, match="result_refused"):
            await executor.execute(action)
        assert executor.test_outcomes[0].outcome == "failed"
    else:
        assert (await executor.execute(action))["text"] == content
        amount = executor.test_outcomes[0].actual
        assert amount.tokens == usage if usage is not None else amount is None


@pytest.mark.asyncio
async def test_action_intent_charges_declared_tool_bound_and_requires_sensitive_review(executor):
    executor.orch.tool_permissions.get_tool_scope.return_value = "tools:write"
    executor.service.tool_bound = Mock(return_value={"model_calls": 2, "tool_calls": 3, "tokens": 4000, "elapsed_ms": 20_000})
    executor.execute = AsyncMock(return_value="pending")
    request = {"kind": "tool", "agent_id": "reader", "tool_name": "write", "arguments": {}}
    assert await executor.action("key", request) == "pending"
    action = executor.execute.call_args.args[0]
    assert action.intent.interactive_only and action.intent.sensitivity == "sensitive"
    assert action.intent.maximum.tokens == 4000 and action.intent.maximum.model_calls == 2
    executor.service.tool_bound.return_value["elapsed_ms"] = 120_000
    with pytest.raises(DispatchDenied, match="time_bound"):
        await executor.action("another", request)


@pytest.mark.asyncio
async def test_action_replay_checks_original_request_and_machine_mint_failure(executor):
    original = action_record(executor.record)
    old = executor.store.call.side_effect
    async def call(method, **kwargs):
        return original if method == "get_action_by_key" else await old(method, **kwargs)
    executor.store.call.side_effect = call
    executor.execute = AsyncMock(return_value="retained")
    assert await executor.action("key", thaw(original.intent.request)) == "retained"
    with pytest.raises(DispatchDenied, match="binding_changed"):
        await executor.action("key", {"kind": "model"})
    executor.orch.derive_machine_authority.return_value = AuthoritySkip("revoked_or_expired")
    with pytest.raises(DispatchDenied, match="authorization_required"):
        await executor.refresh()


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


@pytest.mark.asyncio
async def test_paused_unstarted_action_gets_new_epoch_identity_without_losing_old_receipt(executor):
    action = replace(action_record(executor.record), state="invalidated", control_epoch=0, ever_started=False)
    previous = executor.store.call.side_effect
    lookups = []
    async def call(method, **kwargs):
        if method == "get_action_by_key":
            lookups.append(kwargs["action_key"])
            return action if len(lookups) == 1 else None
        return await previous(method, **kwargs)
    executor.store.call.side_effect = call
    executor.execute = AsyncMock(return_value="new attempt")
    assert await executor.action("original-key", thaw(action.intent.request)) == "new attempt"
    assert lookups == ["original-key", digest(["original-key", "successor", 0])]
    replacement = executor.execute.call_args.args[0]
    assert replacement.intent.action_key == lookups[1]
    assert action.state == "invalidated"


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [{"dispatch_token": "begun"}, {"outcome": "uncertain"}, {"outcome": "succeeded"}])
async def test_started_history_never_gets_replacement_identity_after_pause(executor, attempt):
    action = replace(action_record(executor.record), state="invalidated", control_epoch=0,
                     ever_started=True, attempts=(attempt,))
    original = executor.store.call.side_effect
    async def call(method, **kwargs):
        return action if method == "get_action_by_key" else await original(method, **kwargs)
    executor.store.call.side_effect = call
    with pytest.raises(DispatchDenied, match="approval_invalid"):
        await executor.action("original", thaw(action.intent.request))
    assert [method for method, _ in executor.test_calls] == []
    assert executor.store.call.await_count == 1


@pytest.mark.asyncio
async def test_actual_usage_preserves_reserved_currency_quote(executor):
    action = action_record(executor.record)
    amount = replace(action.intent.maximum, spend_micro_units=17, currency="USD")
    action = replace(action, intent=replace(action.intent, maximum=amount))
    await executor.execute(action)
    actual = executor.test_outcomes[0].actual
    assert actual.currency == "USD" and actual.spend_micro_units == 17
