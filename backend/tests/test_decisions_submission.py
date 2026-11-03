"""Keeps optional decisions providers on the selected endpoint for chat routing and Work submission screening."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.typesafe_routing import runner
from orchestrator.typesafe_routing.budget import Outcome, UserCircuit
from orchestrator.typesafe_routing.custom import CustomDecisionClient
from orchestrator.typesafe_routing.decision import Tier
from orchestrator.typesafe_routing.questions import AgentOption, RoutingRequest, ToolOption
from orchestrator.work_submit import WorkSubmitService
from shared import external_http
from tests.fakes.typesafe_fake import AnswerSet, FakeTypeSafeClient


MODEL = "bespokelabs/Bespoke-Nimble-9B"
BASE = "https://factory.example/v1"
USER = "decisions-owner"


class Enabled:
    @staticmethod
    def is_enabled(name):
        return True


@pytest.fixture
def endpoint(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        answers = {}
        for name, question in kwargs["json_body"]["questions"].items():
            kind = question["type"]
            if kind == "noul":
                answer = {"noul": 0.0}
            elif kind == "score":
                answer = {"score": 0.0}
            else:
                criteria = list(question["criteria"])
                selected = criteria[0]
                probabilities = {key: 0.02 / (len(criteria) - 1) for key in criteria}
                probabilities[selected] = 0.98
                answer = {"choice": selected, "confidence": 0.98, "probabilities": probabilities}
            answers[name] = {"type": kind, **answer}
        return SimpleNamespace(status_code=200, content=json.dumps({"model": MODEL, "answers": answers}).encode())

    monkeypatch.setattr(external_http, "request", request)
    return calls


def _never_hosted():
    raise AssertionError("A custom decision must not use the TypeSafe endpoint")


@pytest.mark.asyncio
async def test_keyless_custom_routing_reaches_configured_endpoint_and_parses_decision(monkeypatch, endpoint):
    monkeypatch.setattr(runner, "default_adapter_client", _never_hosted)
    request = RoutingRequest.build(current_request="What is the weather?",
        agents=[AgentOption("weather", "Weather")],
        tools_by_agent={"weather": [ToolOption("weather__current")]})
    task = await runner.start_routing(user_id=USER, request=request, api_key="", fingerprint="custom",
        client=CustomDecisionClient(BASE, MODEL), circuit=UserCircuit(), flags=Enabled)
    outcome = await runner.await_decision(task)
    assert outcome.outcome is Outcome.SUCCESS
    assert outcome.decision.tier is Tier.HIGH
    assert outcome.decision.tool_name == "weather__current"
    assert len(endpoint) == 1
    assert endpoint[0][1] == BASE + "/systemone"
    assert endpoint[0][2]["api_key"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", ["", "custom-key-never-for-typesafe"])
async def test_work_screen_uses_selected_custom_endpoint_and_key(monkeypatch, endpoint, api_key):
    monkeypatch.setattr(runner, "default_adapter_client", _never_hosted)
    monkeypatch.setattr(runner, "routing_enabled", lambda flags=None: True)
    stored = SimpleNamespace(provider="custom", base_url=BASE, model=MODEL,
        api_key=api_key, fingerprint="custom")
    store = SimpleNamespace(get_key=AsyncMock(return_value=stored))
    assignments = SimpleNamespace(store=None, orch=SimpleNamespace(_typesafe_store=store))
    service = WorkSubmitService(assignments, None, None)
    await service._typesafe_screen_chat(USER, "Summarise the research digest.")
    assert len(endpoint) == 1
    assert endpoint[0][1] == BASE + "/systemone"
    assert endpoint[0][2]["api_key"] == api_key
    assert endpoint[0][2]["json_body"]["state"] == {"current_request": "Summarise the research digest."}
    assert set(endpoint[0][2]["json_body"]["questions"]) == {"is_jailbreak", "harm_score", "threat_category"}
    assert api_key == "" or api_key not in json.dumps(endpoint[0][2]["json_body"])
    store.get_key.assert_awaited_once_with(USER)


@pytest.mark.asyncio
async def test_work_screen_retains_hosted_default_for_legacy_credential(monkeypatch, endpoint):
    fake = FakeTypeSafeClient(default=AnswerSet(threat_category="none"))
    monkeypatch.setattr(runner, "default_adapter_client", lambda: fake)
    monkeypatch.setattr(runner, "routing_enabled", lambda flags=None: True)
    stored = SimpleNamespace(api_key="hosted-key", fingerprint="hosted")
    store = SimpleNamespace(get_key=AsyncMock(return_value=stored))
    assignments = SimpleNamespace(store=None, orch=SimpleNamespace(_typesafe_store=store))
    service = WorkSubmitService(assignments, None, None)
    await service._typesafe_screen_chat(USER, "Summarise the research digest.")
    assert fake.call_count == 1
    assert fake.calls[0].api_key == "hosted-key"
    assert endpoint == []


@pytest.mark.asyncio
async def test_keyless_security_screen_reaches_custom_provider(monkeypatch, endpoint):
    monkeypatch.setattr(runner, "default_adapter_client", _never_hosted)
    await runner.screen_instruction(user_id=USER, text="Hello.", api_key="", fingerprint="custom",
        client=CustomDecisionClient(BASE, MODEL), circuit=UserCircuit(), flags=Enabled)
    assert len(endpoint) == 1
    assert endpoint[0][2]["api_key"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", ["", "custom-key-never-for-typesafe"])
async def test_orchestrator_selects_custom_provider_before_real_routing(monkeypatch, endpoint, api_key):
    from orchestrator.harness import execution_binding
    from orchestrator.orchestrator import Orchestrator

    monkeypatch.setattr(execution_binding, "prepare_model_binding", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "default_adapter_client", _never_hosted)
    monkeypatch.setattr(runner, "routing_enabled", lambda flags=None: True)
    stored = SimpleNamespace(provider="custom", base_url=BASE, model=MODEL,
        api_key=api_key, fingerprint="custom-orchestrator")
    orch = object.__new__(Orchestrator)
    orch._typesafe_store = SimpleNamespace(get_key=AsyncMock(return_value=stored))
    orch.agent_cards = {"weather": SimpleNamespace(name="Weather", description="Weather forecasts")}
    orch._tool_to_agent_lookup = lambda name, eligible: "weather"
    tools = [{"type": "function", "function": {"name": "weather__current", "description": "Current weather"}}]
    task, fingerprint = await orch._typesafe_start_routing(websocket=None, user_id=USER,
        chat_id="decisions-chat", message="What is the weather?", eligible=["weather"],
        tools_desc=tools, selected_tools=[])
    outcome = await runner.await_decision(task)
    assert fingerprint == "custom-orchestrator"
    assert outcome.outcome is Outcome.SUCCESS
    assert outcome.decision.tier is Tier.HIGH
    assert outcome.decision.tool_name == "weather__current"
    assert len(endpoint) == 1
    assert endpoint[0][1] == BASE + "/systemone"
    assert endpoint[0][2]["api_key"] == api_key
