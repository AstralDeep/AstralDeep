"""Fixtures mapping every A2A task state to an explicit MCPResponse outcome, where only a
completed task reports success, refusals carry the peer's task identity, and a strict local
peer drives the working-to-completed lifecycle over real HTTP without duplicate effects."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from a2a.types import Artifact, Message, Part, Role, Task, TaskState, TaskStatus
from shared.a2a_bridge import a2a_response_to_mcp_response

_NON_COMPLETED_REASONS = {
    "TASK_STATE_UNSPECIFIED": "Task state is unspecified",
    "TASK_STATE_SUBMITTED": "Task is pending",
    "TASK_STATE_WORKING": "Task is still working",
    "TASK_STATE_INPUT_REQUIRED": "Task requires additional input",
    "TASK_STATE_AUTH_REQUIRED": "Task requires authorization",
    "TASK_STATE_CANCELED": "Task was canceled",
    "TASK_STATE_FAILED": "Task failed",
    "TASK_STATE_REJECTED": "Task was rejected",
}


def _task(state, *, status_text=None, artifact_text=None,
          task_id="task-1", context_id="context-1"):
    task = Task(id=task_id, context_id=context_id, status=TaskStatus(state=state))
    if status_text is not None:
        task.status.message.CopyFrom(
            Message(message_id="msg-1", role=Role.ROLE_AGENT, parts=[Part(text=status_text)])
        )
    if artifact_text is not None:
        task.artifacts.append(Artifact(artifact_id="artifact-1", parts=[Part(text=artifact_text)]))
    return task


@pytest.mark.parametrize(
    ("state_name", "reason"),
    [*_NON_COMPLETED_REASONS.items(), ("TASK_STATE_COMPLETED", None)],
)
def test_every_task_state_maps_to_an_explicit_outcome(state_name, reason):
    task = _task(TaskState.Value(state_name), artifact_text="payload")
    response = a2a_response_to_mcp_response(task, "req-1")
    if reason is None:
        assert response.error is None
        assert response.result == "payload"
        assert response.result_type == "complete"
        return
    assert response.result is None
    assert response.ui_components is None
    assert response.error["code"] == -32603
    assert response.error["message"] == reason
    assert response.error["retryable"] is False
    assert response.error["task_state"] == state_name
    assert response.error["task_id"] == "task-1"
    assert response.error["context_id"] == "context-1"


@pytest.mark.parametrize(
    "state_name",
    ["TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED",
     "TASK_STATE_AUTH_REQUIRED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED",
     "TASK_STATE_REJECTED"],
)
def test_peer_status_text_becomes_the_bounded_reason(state_name):
    task = _task(TaskState.Value(state_name), status_text="please provide a patient id")
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.error["message"] == "please provide a patient id"
    assert response.result is None


@pytest.mark.parametrize("state_name", ["TASK_STATE_CANCELED", "TASK_STATE_REJECTED", "TASK_STATE_WORKING"])
def test_refused_tasks_never_surface_partial_artifacts(state_name):
    task = _task(TaskState.Value(state_name), artifact_text="partial-side-effect")
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.result is None
    assert "partial-side-effect" not in json.dumps(response.error)


def test_completed_task_still_merges_its_status_message():
    task = _task(TaskState.TASK_STATE_COMPLETED, status_text="final answer")
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.error is None
    assert response.result == "final answer"


def test_failed_task_keeps_its_default_reason_without_status_text():
    task = _task(TaskState.TASK_STATE_FAILED)
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.error["message"] == "Task failed"
    assert response.error["task_state"] == "TASK_STATE_FAILED"


def test_task_without_status_fails_closed_before_completion():
    task = Task(id="task-1", context_id="context-1")
    task.artifacts.append(Artifact(artifact_id="artifact-1", parts=[Part(text="payload")]))
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.result is None
    assert response.error["task_state"] == "TASK_STATE_UNSPECIFIED"
    assert response.error["retryable"] is False


def test_unknown_numeric_state_fails_closed_with_the_raw_value():
    task = _task(99)
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.result is None
    assert response.error["task_state"] == "99"
    assert response.error["message"] == "Task did not complete"
    assert response.error["retryable"] is False


def test_refusals_omit_absent_identity_fields():
    task = Task(status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    response = a2a_response_to_mcp_response(task, "req-1")
    assert "task_id" not in response.error
    assert "context_id" not in response.error
    assert response.error["task_state"] == "TASK_STATE_WORKING"


def test_message_response_still_maps_to_a_result():
    message = Message(message_id="msg-1", role=Role.ROLE_AGENT, parts=[Part(text="hello")])
    response = a2a_response_to_mcp_response(message, "req-1")
    assert response.error is None
    assert response.result == "hello"


class _StrictTaskPeer:
    def __init__(self):
        self.executions = 0
        self.requests = []
        self.output = None
        self.task_id = "peer-task-1"
        self.context_id = "peer-context-1"
        self._executed_keys = set()
        peer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.loads(raw)
                peer.requests.append(payload)
                body = json.dumps(peer._task_response(payload)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"

    def _task_response(self, payload):
        assert payload["method"] == "message/send"
        message = payload["params"]["message"]
        data = next(part["data"] for part in message["parts"] if "data" in part)
        assert data["method"] == "tools/call"
        key = data["arguments"]["idempotency_key"]
        if key not in self._executed_keys:
            self._executed_keys.add(key)
            self.executions += 1
        if self.output is None:
            result = {
                "id": self.task_id,
                "context_id": self.context_id,
                "status": {"state": "TASK_STATE_WORKING"},
                "artifacts": [{"artifact_id": "partial-1", "parts": [{"text": "partial output"}]}],
            }
        else:
            result = {
                "id": self.task_id,
                "context_id": self.context_id,
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"artifact_id": "final-1", "parts": [{"text": self.output}]}],
            }
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def test_strict_peer_working_then_completed_drives_one_physical_effect(monkeypatch):
    from orchestrator.orchestrator import Orchestrator

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    peer = _StrictTaskPeer()
    try:
        orchestrator = SimpleNamespace(a2a_clients={"remote-1": peer.base_url}, agent_urls={})
        orchestrator._a2a_wire_arguments = Orchestrator._a2a_wire_arguments
        args = {"idempotency_key": "key-1", "name": "draft_report", "instructions": "write"}

        first = asyncio.run(Orchestrator._execute_via_a2a(orchestrator, "remote-1", "draft_report", args))
        assert first.result is None
        assert first.ui_components is None
        assert first.error["retryable"] is False
        assert first.error["task_state"] == "TASK_STATE_WORKING"
        assert first.error["message"] == "Task is still working"
        assert first.error["task_id"] == peer.task_id
        assert first.error["context_id"] == peer.context_id
        assert peer.executions == 1
        assert len(peer.requests) == 1

        peer.output = "final report"
        second = asyncio.run(Orchestrator._execute_via_a2a(orchestrator, "remote-1", "draft_report", args))
        assert second.error is None
        assert second.result == "final report"
        assert peer.executions == 1
        assert len(peer.requests) == 2
    finally:
        peer.close()
