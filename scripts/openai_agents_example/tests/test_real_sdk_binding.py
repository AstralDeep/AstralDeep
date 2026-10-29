"""Real-SDK qualification for the example's binding: the pinned openai-agents package
drives the Work tools through Runner with a deterministic ScriptedModel while a probe
trace processor proves no trace or span is ever recorded. These tests run in the
isolated environment from requirements.lock.txt (PYTHONPATH must not include backend/)
and are fully offline and deterministic; repo CI exercises the same boundaries with a
stub SDK in scripts/tests/test_openai_agents_example.py.
"""

from __future__ import annotations

import json

import pytest
from agents import Runner
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tracing import add_trace_processor

from scripts.openai_agents_example.agent import AGENT_TOOL_NAMES, AstralAgentSession
from scripts.openai_agents_example.client import AstralWorkClient
from scripts.openai_agents_example.tests.mcp_fakes import (
    BEARER, OPERATION_ID, FakeAstral,
)


class RecordingProcessor:
    def __init__(self):
        self.traces = []
        self.spans = []

    def on_trace_start(self, trace):
        self.traces.append(trace)

    def on_trace_end(self, trace):
        self.traces.append(trace)

    def on_span_start(self, span):
        self.spans.append(span)

    def on_span_end(self, span):
        self.spans.append(span)

    def shutdown(self):
        return None

    def force_flush(self):
        return True


def _tool_calls(fake):
    return [
        request for request in fake.requests
        if request.headers.get("mcp-method") == "tools/call"
    ]


@pytest.mark.asyncio
async def test_scripted_agent_submits_and_reports():
    fake = FakeAstral()
    async with AstralWorkClient(
        base_url="http://testserver", bearer=BEARER, transport=fake.transport,
    ) as client:
        model = ScriptedModel([
            [function_call("astral_submit_operation", {
                "idempotency_key": "sdk-qual-1", "name": "SDK qualification",
                "instructions": "Reply with acknowledged.",
            }, call_id="call-1")],
            [assistant_message("Submitted the qualification operation.")],
        ])
        session = AstralAgentSession(client=client, model=model)
        agent = session.build_agent()
        result = await Runner.run(agent, "Submit the qualification probe.")
    assert result.final_output == "Submitted the qualification operation."
    assert session.tracing_before_first_request is True
    submitted = fake.submissions["sdk-qual-1"]
    assert submitted["id"] == OPERATION_ID and submitted["created"] is True
    tool_call = _tool_calls(fake)[0]
    assert tool_call.headers["Mcp-Name"] == "astral_submit_operation"
    assert json.loads(tool_call.content)["params"]["_meta"][
        "io.modelcontextprotocol/protocolVersion"
    ] == "2026-07-28"


@pytest.mark.asyncio
async def test_no_trace_or_span_reaches_a_registered_sink():
    preexisting = RecordingProcessor()
    add_trace_processor(preexisting)
    fake = FakeAstral()
    async with AstralWorkClient(
        base_url="http://testserver", bearer=BEARER, transport=fake.transport,
    ) as client:
        AstralAgentSession(client=client)
        probe = RecordingProcessor()
        add_trace_processor(probe)
        model = ScriptedModel([
            [function_call("astral_get_operation", {"operation_id": OPERATION_ID}, call_id="call-1")],
            [assistant_message("Operation is queued.")],
        ])
        agent = AstralAgentSession(client=client, model=model).build_agent()
        await Runner.run(agent, "Check the operation.")
    assert preexisting.traces == [] and preexisting.spans == []
    assert probe.traces == [] and probe.spans == []


@pytest.mark.asyncio
async def test_polling_and_terminal_disposition_through_the_agent():
    fake = FakeAstral()
    async with AstralWorkClient(
        base_url="http://testserver", bearer=BEARER, transport=fake.transport,
    ) as client:
        model = ScriptedModel([
            [function_call("astral_submit_operation", {
                "idempotency_key": "sdk-qual-2", "name": "SDK qualification",
                "instructions": "Reply with acknowledged.",
            }, call_id="call-1")],
            [function_call("astral_get_operation_events", {
                "operation_id": OPERATION_ID, "after_revision": 1,
            }, call_id="call-2")],
            [function_call("astral_get_operation_events", {
                "operation_id": OPERATION_ID, "after_revision": 2,
            }, call_id="call-3")],
            [assistant_message("The operation completed.")],
        ])
        agent = AstralAgentSession(client=client, model=model).build_agent()
        result = await Runner.run(agent, "Run and poll the probe.")
    assert result.final_output == "The operation completed."
    names = [
        json.loads(request.content)["params"]["name"] for request in _tool_calls(fake)
    ]
    assert names == [
        "astral_submit_operation", "astral_get_operation_events",
        "astral_get_operation_events",
    ]
    second_events = json.loads(_tool_calls(fake)[2].content)["params"]["arguments"]
    assert second_events == {"operation_id": OPERATION_ID, "after_revision": 2}


@pytest.mark.asyncio
async def test_server_refusal_feeds_back_to_the_model():
    fake = FakeAstral(behaviors={
        "astral_get_operation": ("tool_error", "work_not_found"),
    })
    async with AstralWorkClient(
        base_url="http://testserver", bearer=BEARER, transport=fake.transport,
    ) as client:
        model = ScriptedModel([
            [function_call("astral_get_operation", {"operation_id": OPERATION_ID}, call_id="call-1")],
            [assistant_message("The server refused the read.")],
        ])
        agent = AstralAgentSession(client=client, model=model).build_agent()
        result = await Runner.run(agent, "Check the missing operation.")
    assert result.final_output == "The server refused the read."
    assert len(_tool_calls(fake)) == 1


@pytest.mark.asyncio
async def test_session_tool_set_stays_within_the_documented_boundary():
    fake = FakeAstral()
    async with AstralWorkClient(
        base_url="http://testserver", bearer=BEARER, transport=fake.transport,
    ) as client:
        session = AstralAgentSession(client=client)
        agent = session.build_agent()
        assert tuple(tool.name for tool in agent.tools) == AGENT_TOOL_NAMES
        assert agent.handoffs == []
