"""Tests for shared/a2a_bridge.py's A2A task-state mapping: every A2A 1.0 task
state gets an explicit MCP outcome, only COMPLETED reports success, and a strict
local peer driving WORKING then COMPLETED never triggers a second physical send.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest
from a2a.types import (
    Artifact,
    Message as A2AMessage,
    Role,
    SendMessageResponse,
    Task,
    TaskState,
    TaskStatus,
)
from google.protobuf.json_format import MessageToDict

from orchestrator.orchestrator import Orchestrator
from shared.a2a_bridge import (
    a2a_response_to_mcp_response,
    make_data_part,
    make_text_part,
)
from shared.protocol import MCPResponse

ALL_STATES = [
    TaskState.TASK_STATE_UNSPECIFIED,
    TaskState.TASK_STATE_SUBMITTED,
    TaskState.TASK_STATE_WORKING,
    TaskState.TASK_STATE_INPUT_REQUIRED,
    TaskState.TASK_STATE_AUTH_REQUIRED,
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
]

TERMINAL_FAILURES = [
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
]

UNRESOLVED_STATES = [
    TaskState.TASK_STATE_UNSPECIFIED,
    TaskState.TASK_STATE_SUBMITTED,
    TaskState.TASK_STATE_WORKING,
    TaskState.TASK_STATE_INPUT_REQUIRED,
    TaskState.TASK_STATE_AUTH_REQUIRED,
]


def _task(
    state: TaskState,
    *,
    task_id: str = "task-abc",
    context_id: str = "ctx-123",
    status_text: str | None = None,
    artifacts: List[Artifact] | None = None,
) -> Task:
    status = TaskStatus(state=state)
    if status_text is not None:
        status.message.CopyFrom(
            A2AMessage(
                message_id="m-1",
                role=Role.ROLE_AGENT,
                parts=[make_text_part(status_text)],
            )
        )
    return Task(
        id=task_id,
        context_id=context_id,
        status=status,
        artifacts=artifacts or [],
    )


def _completed_artifact(payload: Dict[str, Any]) -> Artifact:
    return Artifact(
        artifact_id="a-1",
        name="result",
        parts=[make_data_part(payload)],
    )


def _state_name(state: TaskState) -> str:
    return TaskState.Name(state)


@pytest.mark.parametrize("state", ALL_STATES, ids=_state_name)
def test_every_state_has_explicit_outcome(state: TaskState) -> None:
    response = a2a_response_to_mcp_response(_task(state), "req-1")
    assert isinstance(response, MCPResponse)
    assert response.request_id == "req-1"
    if state == TaskState.TASK_STATE_COMPLETED:
        assert response.error is None
        assert response.result_type == "complete"
    else:
        assert response.error is not None
        assert response.result is None
        assert response.ui_components is None
        assert response.error.get("retryable") is False
        assert response.error.get("message")
    assert response.correlation_id == "task-abc"


@pytest.mark.parametrize("state", UNRESOLVED_STATES, ids=_state_name)
def test_unresolved_states_refuse_continuation_and_preserve_identity(
    state: TaskState,
) -> None:
    response = a2a_response_to_mcp_response(_task(state), "req-1")
    error = response.error
    assert error is not None
    assert error["a2a_task_id"] == "task-abc"
    assert error["a2a_context_id"] == "ctx-123"
    assert error["a2a_state"] == _state_name(state)
    assert error["retryable"] is False


def test_working_task_refusal_does_not_leak_artifact_result() -> None:
    task = _task(
        TaskState.TASK_STATE_WORKING,
        artifacts=[_completed_artifact({"side_effect": "already done"})],
    )
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.error is not None
    assert response.result is None
    assert response.ui_components is None


@pytest.mark.parametrize("state", TERMINAL_FAILURES, ids=_state_name)
def test_terminal_failures_carry_safe_reason(state: TaskState) -> None:
    response = a2a_response_to_mcp_response(_task(state), "req-1")
    error = response.error
    assert error is not None
    assert error["retryable"] is False
    assert error["message"]
    assert len(error["message"]) <= 600


def test_failed_task_prefers_status_message_as_reason() -> None:
    task = _task(
        TaskState.TASK_STATE_FAILED,
        status_text="scheduler rejected the job",
    )
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.error is not None
    assert "scheduler rejected the job" in response.error["message"]
    assert response.error["code"] == -32603


def test_failed_task_without_message_keeps_default_reason() -> None:
    response = a2a_response_to_mcp_response(
        _task(TaskState.TASK_STATE_FAILED), "req-1"
    )
    assert response.error is not None
    assert response.error["message"] == "Task failed"


def test_failed_task_with_non_text_status_parts_keeps_default_reason() -> None:
    status = TaskStatus(state=TaskState.TASK_STATE_FAILED)
    status.message.CopyFrom(
        A2AMessage(
            message_id="m-3",
            role=Role.ROLE_AGENT,
            parts=[make_data_part({"detail": "structured only"})],
        )
    )
    task = Task(id="task-abc", context_id="ctx-123", status=status)
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.error is not None
    assert response.error["message"] == "Task failed"


def test_unknown_task_state_gets_explicit_refusal() -> None:
    task = _task(99)
    response = a2a_response_to_mcp_response(task, "req-1")
    error = response.error
    assert error is not None
    assert error["retryable"] is False
    assert error["a2a_state"] == "unknown-99"
    assert "has not completed" in error["message"]
    assert error["a2a_task_id"] == "task-abc"


def test_completed_task_reports_success_with_identity() -> None:
    task = _task(
        TaskState.TASK_STATE_COMPLETED,
        artifacts=[_completed_artifact({"answer": 42})],
    )
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.error is None
    assert response.result == {"answer": 42}
    assert response.result_type == "complete"
    assert response.correlation_id == "task-abc"


def test_completed_task_without_artifacts_still_succeeds() -> None:
    response = a2a_response_to_mcp_response(
        _task(TaskState.TASK_STATE_COMPLETED), "req-1"
    )
    assert response.error is None
    assert response.result is None


def test_message_input_still_maps_to_message_response() -> None:
    msg = A2AMessage(
        message_id="m-2",
        role=Role.ROLE_AGENT,
        parts=[make_text_part("hello")],
    )
    response = a2a_response_to_mcp_response(msg, "req-1")
    assert response.error is None
    assert response.result == "hello"


@pytest.mark.asyncio
async def test_strict_peer_working_then_completed_drives_lifecycle_without_second_effect(
    monkeypatch,
) -> None:
    sends: List[Dict[str, Any]] = []
    working = {"pending": True}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sends.append(payload)
        assert payload["method"] == "SendMessage"
        assert request.headers["A2A-Version"] == "1.0"
        if working["pending"]:
            working["pending"] = False
            task = _task(TaskState.TASK_STATE_WORKING)
        else:
            task = _task(
                TaskState.TASK_STATE_COMPLETED,
                artifacts=[_completed_artifact({"done": True})],
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": MessageToDict(SendMessageResponse(task=task)),
            },
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    class _PatchedAsyncClient(real_async_client):
        def __init__(self, **kwargs: Any) -> None:
            kwargs.pop("timeout", None)
            super().__init__(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _PatchedAsyncClient)
    monkeypatch.setattr("shared.external_http._resolve_host_addresses", lambda host: ["93.184.216.34"])

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.a2a_clients = {"agent-1": "http://peer.test"}
    orchestrator.agent_urls = {}
    physical_sends = {"count": 0}
    execute_via_a2a = orchestrator._execute_via_a2a

    async def execute_tool_and_wait(agent_id, tool_name, args, **kwargs):
        physical_sends["count"] += 1
        return await execute_via_a2a(agent_id, tool_name, args, timeout=5.0)

    orchestrator.execute_tool_and_wait = execute_tool_and_wait

    first = await orchestrator._execute_with_retry(
        None,
        "agent-1",
        "heavy_effect",
        {},
        user_id="owner-1",
        channel="rest",
        audit_correlation_id="corr-1",
        timeout=5.0,
    )
    assert first.error is not None
    assert first.error["retryable"] is False
    assert first.error["a2a_state"] == "TASK_STATE_WORKING"
    assert first.result is None
    assert physical_sends["count"] == 1
    assert len(sends) == 1

    second = await orchestrator._execute_with_retry(
        None,
        "agent-1",
        "heavy_effect",
        {},
        user_id="owner-1",
        channel="rest",
        audit_correlation_id="corr-1",
        timeout=5.0,
    )
    assert second.error is None
    assert second.result == {"done": True}
    assert second.correlation_id == "task-abc"
    assert physical_sends["count"] == 2
    assert len(sends) == 2
