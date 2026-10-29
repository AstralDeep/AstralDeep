"""Private selected-runner validation keeps request, usage, delivery and agent budgets bounded.
Synthetic helper values never register deployment facts or authorize provider execution.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from llm_config import research_profile as profile
from orchestrator.harness.execution_binding import ModelHarnessBinding
from orchestrator.harness.production_profiles import HarnessSelectionError, public_profiles


def binding(**changes):
    value = ModelHarnessBinding(None, None, None, None, "owner", None,
        SimpleNamespace(profile=profile.LOCAL_PROFILE.bind(model="synthetic", base_url="http://127.0.0.1:11434/v1")),
        None, None, None)
    return replace(value, **changes)


def response(prompt=31744, completion=1024, total=32768, *, reason="stop", body='{}'):
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
        total_tokens=total), choices=[SimpleNamespace(finish_reason=reason)], model_dump_json=lambda: body)


def test_exact_local_limits_and_request_fields_leave_production_unavailable():
    selected = binding()
    request = {"model": "synthetic", "messages": [{"role": "user", "content": "bounded"}]}
    selected.request(request)
    assert {name: request[name] for name in ["stream", "store", "n", "max_completion_tokens"]} == {
        "stream": False, "store": False, "n": 1, "max_completion_tokens": 1024}
    selected.response(response())
    assert public_profiles()[0]["available"] is False


@pytest.mark.parametrize("body", [{"model": "other"}, {"model": "synthetic", "messages": ["x" * 65536]},
    {"model": "synthetic", "temperature": float("nan")}, {"model": "synthetic", "extra": object()}])
def test_request_model_size_and_json_fail_closed(body):
    with pytest.raises(HarnessSelectionError):
        binding().request(body)


@pytest.mark.parametrize("changes", [{"prompt": True}, {"completion": 1025, "total": 32769},
    {"prompt": 32768, "completion": 1, "total": 32769}, {"total": 0},
    {"prompt": -1}, {"reason": "length"}, {"body": "x" * (1024 * 1024 + 1)}])
def test_usage_output_and_oversize_fail_closed(changes):
    with pytest.raises(HarnessSelectionError):
        binding().response(response(**changes))


def test_selected_agent_step_budget_uses_original_profile(monkeypatch):
    from orchestrator.work_submit import WorkSubmitService
    from orchestrator.projection_surfaces.authoring import DeclarativeAgentService
    from persistent_agents.models import AssignmentError
    limits = {"model_calls": 2, "tool_calls": 2, "tokens": 50000, "elapsed_ms": 200000,
              "max_retries": 0}
    agent_limits = {"daily": {**limits, "spend_micro_units": None},
                    "lifetime": {**limits, "spend_micro_units": None},
                    "max_retries": 0, "step_timeout_ms": 65000}
    parsed = SimpleNamespace(fixed_research_shape=True, to_dict=lambda: {"limits": agent_limits})
    monkeypatch.setattr(DeclarativeAgentService, "_stored_definition", lambda _: parsed)
    monkeypatch.setattr("orchestrator.agent_authoring.byo_enabled", lambda: True)
    service = WorkSubmitService.__new__(WorkSubmitService)
    service.assignments = SimpleNamespace(tool_bound=lambda _: {"elapsed_ms": 30000})
    definition = SimpleNamespace(limits=limits)
    with pytest.raises(AssignmentError, match="work_selected_agent_budget_refused"):
        service._agent_selection_policy(object(), definition, profile.LOCAL_PROFILE)
    agent_limits["step_timeout_ms"] = 120000
    service._agent_selection_policy(object(), definition, profile.LOCAL_PROFILE)
    agent_limits["step_timeout_ms"] = 65000
    service._agent_selection_policy(object(), definition)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "during", "unchanged"])
async def test_binding_checks_current_owner_before_and_after_repository_wait(stage):
    socket = object()
    claims = {"sub": "owner"}
    orch = SimpleNamespace(ui_sessions={socket: claims})
    orch._llm_context_user_id = lambda ws: orch.ui_sessions.get(ws, {}).get("sub")
    if stage == "before":
        orch.ui_sessions[socket] = {"sub": "other"}
    def current(**kwargs):
        if stage == "during":
            orch.ui_sessions[socket] = {"sub": "owner"}
    value = binding(service=SimpleNamespace(orch=orch), coordinator=SimpleNamespace(harness_capture=current),
                    websocket=socket, claims=claims)
    if stage == "unchanged":
        await value.assert_current()
    else:
        with pytest.raises(HarnessSelectionError, match="harness_owner_invalid"):
            await value.assert_current()


@pytest.mark.asyncio
async def test_unbound_or_invalid_runtime_context_does_not_invent_authority():
    from orchestrator.harness.execution_binding import prepare_model_binding
    assert await prepare_model_binding(SimpleNamespace(), object(), None) is None
    with pytest.raises(HarnessSelectionError, match="harness_owner_invalid"):
        await prepare_model_binding(SimpleNamespace(_llm_context_user_id=lambda _: "owner"), object(),
                                    {"owner": {}, "execution_fence": object()})


@pytest.mark.asyncio
async def test_selected_physical_failure_is_one_attempt_and_audited_without_probe_fallback(monkeypatch):
    from tests.test_call_llm_wave0 import _bare_orch, _FakeCompletions, _FakeClient, _StatusError
    from llm_config.types import CredentialSource
    from unittest.mock import AsyncMock
    from orchestrator.harness import execution_binding
    comp = _FakeCompletions(fail_on=lambda _: _StatusError(400, "max_completion_tokens rejected"))
    orch = _bare_orch(comp)
    captured = object()
    async def physical(body):
        return comp.create(**body)
    from orchestrator.harness.standard_llm_adapter import StandardLLMHarness
    selected = SimpleNamespace(capture=captured, owner_id="owner",
        adapter_identity=StandardLLMHarness().identity, assert_current=AsyncMock(), request=lambda _: None,
        invoke=physical, response=lambda _: None, client=lambda **kw: (_FakeClient(comp), CredentialSource.USER,
            SimpleNamespace(model="m1", base_url="https://ep/v1")))
    monkeypatch.setattr(execution_binding, "prepare_model_binding", AsyncMock(return_value=selected))
    with pytest.raises(_StatusError):
        await orch._call_llm(None, [{"role": "user", "content": "bounded"}])
    assert len(comp.calls) == 1
    assert len(orch._audits) == 1 and orch._audits[0]["outcome"] == "failure"
    assert orch._audits[0]["harness_capture"] is captured


@pytest.mark.asyncio
async def test_selected_lifecycle_rechecks_owner_before_materializing_captured_client(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from tests.test_call_llm_wave0 import _bare_orch, _FakeCompletions
    from orchestrator.harness.standard_llm_adapter import StandardLLMHarness
    from orchestrator.harness import execution_binding
    orch = _bare_orch(_FakeCompletions())
    selected = SimpleNamespace(owner_id="owner", adapter_identity=StandardLLMHarness().identity,
        assert_current=AsyncMock(side_effect=HarnessSelectionError("harness_owner_invalid", 403)),
        client=Mock(side_effect=AssertionError("captured client materialized before owner check")))
    monkeypatch.setattr(execution_binding, "prepare_model_binding", AsyncMock(return_value=selected))
    with pytest.raises(HarnessSelectionError, match="harness_owner_invalid"):
        await orch._call_llm(None, [{"role": "user", "content": "bounded"}])
    selected.client.assert_not_called()
