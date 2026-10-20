"""Exercises source-bound approval transitions against real Plane audit storage.
Physical effects remain observable independently of durable success or refusal.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from audit.recorder import Recorder
from audit.repository import AuditRepository
from orchestrator import hitl_confirmation as hc
from shared.protocol import MCPResponse
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401
from tests.test_source_bound_dispatch import runtime  # noqa: F401


@pytest.fixture
async def durable_approval(runtime, monkeypatch, tmp_path):  # noqa: F811
    orch, ws = runtime
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "source-bound-audit-regression-32-characters")
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "source_bound_regression")
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    with isolated_plane_runtime("ad_source_bound_audit") as plane:
        repository = AuditRepository(plane_runtime=plane, plane_repositories=plane.repositories)
        recorder = Recorder(repository, retry_queue=tmp_path / "audit-retry.jsonl")
        orch.audit_recorder = recorder
        orch.history = SimpleNamespace(get_chat=MagicMock(return_value={"id": "chat-a"}))
        orch.workspace = SimpleNamespace(aupsert=AsyncMock(return_value=[]))
        orch.send_ui_render = AsyncMock()

        async def mutation(**kwargs):
            return await kwargs["mutation"]()

        orch.run_detached_conversation_mutation = mutation
        effects = []
        results = []
        outcome = SimpleNamespace(error=None)

        class Adapter:
            mode = "off"

            async def execute(self, **kwargs):
                return await kwargs["invoke"](None)

        orch.governed_final_dispatch = Adapter()

        async def dispatch(websocket, agent, tool, args, **kwargs):
            async def physical(_capabilities):
                effects.append(dict(args))
                return (MCPResponse(error=outcome.error) if outcome.error
                        else MCPResponse(result={"ok": True}))

            response = await orch._execute_governed_attempt(
                websocket, agent, tool, args, user_id="owner-a", channel="web",
                audit_correlation_id=None, actor_user_id="owner-a", auth_principal="owner-a",
                conversation_id="chat-a", invoke=physical)
            results.append(response)
            return response

        orch._execute_with_retry = dispatch
        try:
            yield orch, ws, repository, recorder, effects, results, outcome
        finally:
            await recorder.close()


async def _propose(orch, ws):
    import json

    call = SimpleNamespace(id="propose", function=SimpleNamespace(
        name="send_email", arguments=json.dumps({"to": "test@example.org", "body": "bounded note"})))
    return await orch.execute_single_tool(ws, call, {"send_email": "agent-a"}, "chat-a", user_id="owner-a")


def _decision(response):
    assert response.error is None
    return next(node["payload"] for node in response.ui_components[0]["content"]
                if node.get("type") == "button" and node.get("label") == "Approve")


@pytest.mark.asyncio
async def test_real_audit_approval_sequence_executes_once(durable_approval):
    orch, ws, repository, _, effects, results, _ = durable_approval
    payload = _decision(await _propose(orch, ws))
    request_id = payload["hitl_request_id"]
    assert str(UUID(request_id, version=4)) == request_id
    await hc.handle_decision(orch, ws, "owner-a", payload)
    await hc.handle_decision(orch, ws, "owner-a", payload)
    assert len(effects) == len(results) == 1 and results[0].error is None
    rows, _ = repository.list_for_user("owner-a")
    assert sorted(row.action_type for row in rows) == sorted([
        "hitl.proposed", "hitl.approved", "hitl.consumed", "hitl.effect_started",
        "hitl.effect_response_success"])
    assert {row.correlation_id for row in rows} == {request_id}
    assert len({row.inputs_meta["arguments_sha256"] for row in rows}) == 1
    assert repository.verify_chain("owner-a") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["proposed", "approved", "consumed", "effect_started", "effect_response_success"])
async def test_real_audit_failure_never_authorizes_or_replays(durable_approval, phase):
    orch, ws, repository, recorder, effects, results, _ = durable_approval
    record = recorder.record

    async def refuse(event):
        if event.action_type == "hitl." + phase:
            raise RuntimeError("audit unavailable")
        return await record(event)

    recorder.record = refuse
    response = await _propose(orch, ws)
    if phase == "proposed":
        assert response.error and not orch._hitl_pending_calls
    else:
        payload = _decision(response)
        await hc.handle_decision(orch, ws, "owner-a", payload)
        await hc.handle_decision(orch, ws, "owner-a", payload)
        assert not orch._hitl_pending_calls
    assert len(effects) == (1 if phase == "effect_response_success" else 0)
    if phase == "effect_response_success":
        assert results[0].error["code"] == "outcome_uncertain"
    assert repository.verify_chain("owner-a") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["owner", "chat", "expired", "permission"])
async def test_real_audit_pending_decision_denials_have_no_effect(durable_approval, denial):
    from dataclasses import replace

    orch, ws, repository, _, effects, _, _ = durable_approval
    payload = _decision(await _propose(orch, ws))
    owner = "owner-a"
    if denial == "owner":
        owner = "other-owner"
    elif denial == "chat":
        orch._ws_active_chat[id(ws)] = "other-chat"
    elif denial == "expired":
        key = payload["hitl_request_id"]
        orch._hitl_pending_calls[key] = replace(orch._hitl_pending_calls[key], expires=0)
    else:
        orch.tool_permissions.is_tool_allowed.return_value = False
    await hc.handle_decision(orch, ws, owner, payload)
    assert not effects
    assert repository.verify_chain("owner-a") is None


@pytest.mark.asyncio
async def test_real_audit_records_uncertain_physical_outcome(durable_approval):
    orch, ws, repository, _, effects, results, outcome = durable_approval
    outcome.error = {"message": "effect outcome unavailable", "retryable": False}
    payload = _decision(await _propose(orch, ws))
    await hc.handle_decision(orch, ws, "owner-a", payload)
    await hc.handle_decision(orch, ws, "owner-a", payload)
    assert len(effects) == len(results) == 1 and results[0].error == outcome.error
    rows, _ = repository.list_for_user("owner-a")
    uncertain = [row for row in rows if row.action_type == "hitl.effect_uncertain"]
    assert len(uncertain) == 1 and uncertain[0].outcome == "interrupted"
    assert not any(row.action_type == "hitl.effect_response_success" for row in rows)
    assert repository.verify_chain("owner-a") is None
