"""Fixtures mapping every A2A task state to an explicit MCPResponse outcome, where only a
completed task reports success, refusals carry a bounded reason with the peer's task identity,
and a protocol-shaped local peer drives the working-to-completed lifecycle without duplicate
effects."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from a2a.types import Artifact, Message, Part, Role, Task, TaskState, TaskStatus
from shared.a2a_bridge import (
    _A2A_MAX_REASON_CHARS,
    _A2A_REASON_INPUT_CHARS,
    a2a_response_to_mcp_response,
    make_data_part,
)

_NON_COMPLETED_STATES = {
    "TASK_STATE_UNSPECIFIED": ("Task state is unspecified", False, "unsupported"),
    "TASK_STATE_SUBMITTED": ("Task is pending", False, "unsupported"),
    "TASK_STATE_WORKING": ("Task is still working", False, "unsupported"),
    "TASK_STATE_INPUT_REQUIRED": ("Task requires additional input", False, "unsupported"),
    "TASK_STATE_AUTH_REQUIRED": ("Task requires authorization", False, "unsupported"),
    "TASK_STATE_CANCELED": ("Task was canceled", True, "not_applicable"),
    "TASK_STATE_FAILED": ("Task failed", True, "not_applicable"),
    "TASK_STATE_REJECTED": ("Task was rejected", True, "not_applicable"),
}

_A2A_STATE_NAMES = {
    value.name for value in TaskState.DESCRIPTOR.values
}
assert _A2A_STATE_NAMES == set(_NON_COMPLETED_STATES) | {"TASK_STATE_COMPLETED"}, \
    "the A2A TaskState vocabulary grew; extend the mapping table"


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
    ("state_name", "expected"),
    [*_NON_COMPLETED_STATES.items(), ("TASK_STATE_COMPLETED", None)],
)
def test_every_task_state_maps_to_an_explicit_outcome(state_name, expected):
    task = _task(TaskState.Value(state_name), artifact_text="payload")
    response = a2a_response_to_mcp_response(task, "req-1")
    if expected is None:
        assert response.error is None
        assert response.result == "payload"
        assert response.result_type == "complete"
        return
    reason, terminal, continuation = expected
    assert response.result is None
    assert response.ui_components is None
    assert response.error["code"] == -32603
    assert response.error["message"] == reason
    assert response.error["retryable"] is False
    assert response.error["task_state"] == state_name
    assert response.error["task_terminal"] is terminal
    assert response.error["continuation"] == continuation
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


def test_oversized_peer_status_text_is_truncated_to_the_reason_cap():
    task = _task(TaskState.TASK_STATE_REJECTED, status_text="untrusted " + "A" * 100_000)
    message = a2a_response_to_mcp_response(task, "req-1").error["message"]
    assert len(message) <= _A2A_MAX_REASON_CHARS
    assert message.startswith("untrusted A")
    assert message.endswith("...")


def test_reason_input_is_bounded_before_normalisation(monkeypatch):
    import shared.a2a_bridge as bridge

    tail = "\r\n" + "\u202e" + "B" * (_A2A_REASON_INPUT_CHARS * 4)
    task = _task(TaskState.TASK_STATE_REJECTED, status_text="head" + tail)
    seen = []
    original = bridge._A2A_CONTROL_CHARACTERS

    class _Spy:
        def sub(self, replacement, text):
            seen.append(len(text))
            return original.sub(replacement, text)

    monkeypatch.setattr(bridge, "_A2A_CONTROL_CHARACTERS", _Spy())
    message = a2a_response_to_mcp_response(task, "req-1").error["message"]
    assert seen, "the sanitiser never ran"
    assert max(seen) <= _A2A_REASON_INPUT_CHARS
    assert len(message) <= _A2A_MAX_REASON_CHARS
    assert "\u202e" not in message
    assert message.endswith("...")


@pytest.mark.parametrize("length", [_A2A_MAX_REASON_CHARS - 1, _A2A_MAX_REASON_CHARS,
                                    _A2A_MAX_REASON_CHARS + 1])
def test_reason_length_boundary_is_exact(length):
    task = _task(TaskState.TASK_STATE_WORKING, status_text="x" * length)
    message = a2a_response_to_mcp_response(task, "req-1").error["message"]
    if length <= _A2A_MAX_REASON_CHARS:
        assert message == "x" * length
        assert not message.endswith("...")
        return
    assert len(message) <= _A2A_MAX_REASON_CHARS
    assert message.endswith("...")


def test_control_characters_and_line_breaks_are_flattened_out_of_the_reason():
    task = _task(TaskState.TASK_STATE_FAILED, status_text="header\r\nX-Injected: 1\x00\x1b[31m")
    message = a2a_response_to_mcp_response(task, "req-1").error["message"]
    assert message == "header X-Injected: 1 [31m"
    assert not any(character in message for character in ("\r", "\n", "\x00", "\x1b"))


@pytest.mark.parametrize("direction", ["\u202e", "\u200f", "\u2066", "\u061c"])
def test_directional_control_characters_are_stripped_from_the_reason(direction):
    task = _task(TaskState.TASK_STATE_REJECTED, status_text=f"sec{direction}ret value")
    message = a2a_response_to_mcp_response(task, "req-1").error["message"]
    assert message == "secret value"
    assert direction not in message


@pytest.mark.parametrize("status_text", ["", "   ", "\r\n\t", "\x00\x1b"])
def test_blank_peer_status_text_falls_back_to_the_state_default(status_text):
    task = _task(TaskState.TASK_STATE_INPUT_REQUIRED, status_text=status_text)
    message = a2a_response_to_mcp_response(task, "req-1").error["message"]
    assert message == "Task requires additional input"


@pytest.mark.parametrize("state_name", ["TASK_STATE_CANCELED", "TASK_STATE_REJECTED",
                                        "TASK_STATE_WORKING"])
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


def test_completed_task_collects_data_artifacts_and_ui_components():
    task = _task(TaskState.TASK_STATE_COMPLETED)
    task.artifacts.append(Artifact(artifact_id="a-1", parts=[make_data_part({"first": 1})]))
    task.artifacts.append(Artifact(artifact_id="a-2", parts=[
        make_data_part({"_ui_components": [{"type": "card"}]})]))
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.error is None
    assert response.result == {"first": 1}
    assert response.ui_components == [{"type": "card"}]


def test_completed_task_keeps_the_last_data_artifact_as_the_result():
    task = _task(TaskState.TASK_STATE_COMPLETED)
    task.artifacts.append(Artifact(artifact_id="a-1", parts=[make_data_part({"first": 1})]))
    task.artifacts.append(Artifact(artifact_id="a-2", parts=[make_data_part({"second": 2})]))
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.result == {"second": 2}


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
    assert response.error["task_terminal"] is False
    assert response.error["continuation"] == "unsupported"


def test_unknown_numeric_state_fails_closed_with_the_raw_value():
    task = _task(99)
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.result is None
    assert response.error["task_state"] == "99"
    assert response.error["message"] == "Task did not complete"
    assert response.error["retryable"] is False
    assert response.error["task_terminal"] is False
    assert response.error["continuation"] == "unsupported"


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
    def __init__(self, mode="complete", dedupe=True):
        self.mode = mode
        self.dedupe = dedupe
        self.executions = 0
        self.message_sends = 0
        self.task_gets = 0
        self.errors = []
        self.completed = False
        self.output = None
        self.task_id = "peer-task-1"
        self.context_id = "peer-context-1"
        self._executed_keys = set()
        self.v1_sends = 0
        self.v1_gets = 0
        peer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                try:
                    payload = json.loads(raw)
                    if self.headers.get("A2A-Version") == "1.0":
                        assert payload["method"] in ("SendMessage", "GetTask")
                    body = peer._dispatch(payload)
                except Exception as exc:
                    peer.errors.append(repr(exc))
                    body = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "invalid request"}}
                    self.send_response(400)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                encoded = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"

    def _task_payload(self, state, artifact_text=None):
        payload = {"id": self.task_id, "contextId": self.context_id,
                   "status": {"state": state}}
        if artifact_text is not None:
            payload["artifacts"] = [{"artifactId": "final-1",
                                     "parts": [{"text": artifact_text}]}]
        return payload

    def _dispatch(self, payload):
        method = payload.get("method")
        params = payload.get("params") or {}
        if method in ("SendMessage", "message/send"):
            message = params["message"]
            if method == "SendMessage":
                assert message.get("messageId"), "Message.messageId is required"
                assert message.get("role"), "Message.role is required"
            else:
                assert message.get("message_id"), "Message.message_id is required"
                assert message.get("role"), "Message.role is required"
            assert isinstance(message.get("parts"), list) and message["parts"], \
                "Message.parts is required"
            self.message_sends += 1
            if method == "SendMessage":
                self.v1_sends += 1
            data = next(part["data"] for part in message["parts"] if "data" in part)
            assert data["method"] == "tools/call"
            key = data["arguments"]["idempotency_key"]
            if not self.dedupe or key not in self._executed_keys:
                self._executed_keys.add(key)
                self.executions += 1
            configuration = params.get("configuration") or {}
            if configuration.get("returnImmediately") is True:
                result = self._task_payload("TASK_STATE_WORKING")
            elif self.mode == "complete":
                self.completed = True
                self.output = "final output"
                result = self._task_payload("TASK_STATE_COMPLETED", self.output)
            else:
                result = self._task_payload("TASK_STATE_INPUT_REQUIRED")
                result["status"]["message"] = {
                    "messageId": "peer-msg-1", "role": "ROLE_AGENT",
                    "parts": [{"text": "provide a patient id"}]}
            if method == "SendMessage":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": {"task": result}}
            return {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        if method in ("GetTask", "tasks/get"):
            self.task_gets += 1
            if method == "GetTask":
                self.v1_gets += 1
            queried = params["id"]
            assert queried == self.task_id
            if self.completed:
                state, artifact = "TASK_STATE_COMPLETED", self.output
            elif self.mode == "interrupted":
                state, artifact = "TASK_STATE_INPUT_REQUIRED", None
            else:
                state, artifact = "TASK_STATE_WORKING", None
            body = self._task_payload(state, artifact)
            if method == "GetTask":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": body}
            return {"jsonrpc": "2.0", "id": payload["id"], "result": body}
        raise AssertionError(f"unsupported method: {method}")

    def call(self, method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": "client-1", "method": method,
                           "params": params}).encode()
        request = urllib.request.Request(
            f"{self.base_url}/a2a", data=body,
            headers={"Content-Type": "application/json", "A2A-Version": "1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)["result"]

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _orchestrator_toward(peer):
    from orchestrator.orchestrator import Orchestrator

    orchestrator = SimpleNamespace(a2a_clients={"remote-1": peer.base_url}, agent_urls={})
    orchestrator._a2a_wire_arguments = Orchestrator._a2a_wire_arguments
    return orchestrator


def _tool_args():
    return {"idempotency_key": "key-1", "name": "draft_report", "instructions": "write"}


def test_strict_peer_completes_a_blocking_request_without_duplicate_effect(monkeypatch):
    from orchestrator.orchestrator import Orchestrator

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    peer = _StrictTaskPeer(mode="complete")
    try:
        orchestrator = _orchestrator_toward(peer)
        response = asyncio.run(Orchestrator._execute_via_a2a(
            orchestrator, "remote-1", "draft_report", _tool_args()))
        assert response.error is None
        assert response.result == "final output"
        assert peer.executions == 1
        assert peer.message_sends == 1
        assert peer.errors == []
    finally:
        peer.close()


def test_strict_peer_interrupted_request_is_refused_as_unsupported_continuation(monkeypatch):
    from orchestrator.orchestrator import Orchestrator

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    peer = _StrictTaskPeer(mode="interrupted")
    try:
        orchestrator = _orchestrator_toward(peer)
        response = asyncio.run(Orchestrator._execute_via_a2a(
            orchestrator, "remote-1", "draft_report", _tool_args()))
        assert response.result is None
        assert response.ui_components is None
        assert response.error["task_state"] == "TASK_STATE_INPUT_REQUIRED"
        assert response.error["task_terminal"] is False
        assert response.error["continuation"] == "unsupported"
        assert response.error["retryable"] is False
        assert response.error["message"] == "provide a patient id"
        assert response.error["task_id"] == peer.task_id
        assert response.error["context_id"] == peer.context_id
        assert peer.executions == 1
        assert peer.message_sends == 1
        assert peer.errors == []
    finally:
        peer.close()


def test_new_dispatch_after_peer_completion_returns_the_completed_output(monkeypatch):
    from orchestrator.orchestrator import Orchestrator

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    peer = _StrictTaskPeer(mode="interrupted", dedupe=False)
    try:
        orchestrator = _orchestrator_toward(peer)
        refusal = asyncio.run(Orchestrator._execute_via_a2a(
            orchestrator, "remote-1", "draft_report", _tool_args()))
        assert refusal.result is None
        assert refusal.error["task_state"] == "TASK_STATE_INPUT_REQUIRED"
        assert refusal.error["task_terminal"] is False
        assert refusal.error["continuation"] == "unsupported"
        assert refusal.error["retryable"] is False
        assert refusal.error["message"] == "provide a patient id"
        assert refusal.error["task_id"] == peer.task_id
        assert refusal.error["context_id"] == peer.context_id
        assert peer.message_sends == 1
        assert peer.executions == 1

        client_view = peer.call("GetTask", {"id": peer.task_id})
        assert client_view["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        assert client_view["id"] == peer.task_id

        peer.mode = "complete"
        peer.completed = True
        peer.output = "final output"
        resolved = asyncio.run(Orchestrator._execute_via_a2a(
            orchestrator, "remote-1", "draft_report", _tool_args()))
        assert resolved.error is None
        assert resolved.result == "final output"
        assert peer.message_sends == 2
        assert peer.executions == 2
        assert peer.task_gets == 1
        assert peer.errors == []
    finally:
        peer.close()


def test_repeated_dispatch_of_an_unfinished_task_does_not_resend(monkeypatch):
    from orchestrator import hitl_confirmation
    from orchestrator.orchestrator import Orchestrator

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(hitl_confirmation, "approved_call", lambda *args, **kwargs: False)
    peer = _StrictTaskPeer(mode="interrupted", dedupe=False)
    try:
        orchestrator = _orchestrator_toward(peer)
        attempts = []
        refusal = asyncio.run(Orchestrator._execute_via_a2a(
            orchestrator, "remote-1", "draft_report", _tool_args()))
        assert refusal.error["continuation"] == "unsupported"
        assert peer.executions == 1
        assert peer.message_sends == 1

        async def _execute_tool_and_wait(agent_id, tool_name, args, **kwargs):
            attempts.append((agent_id, tool_name))
            return refusal

        orchestrator.MAX_RETRIES = 3
        orchestrator.RETRY_BACKOFF = (0.01, 0.01)
        orchestrator.execute_tool_and_wait = _execute_tool_and_wait
        orchestrator._protected_dispatch_channel = (
            lambda websocket, explicit=None: "dispatch")
        result = asyncio.run(Orchestrator._execute_with_retry(
            orchestrator, None, "remote-1", "draft_report", _tool_args(),
            max_retries=3, user_id="owner"))
        assert attempts == [("remote-1", "draft_report")]
        assert result is refusal
        assert peer.message_sends == 1
        assert peer.executions == 1
        assert peer.errors == []
    finally:
        peer.close()


def test_strict_peer_rejects_messages_missing_required_fields():
    peer = _StrictTaskPeer(mode="complete")
    try:
        for broken in (
                {"role": "ROLE_USER", "parts": [{"text": "x"}]},
                {"messageId": "m-1", "parts": [{"text": "x"}]},
                {"messageId": "m-1", "role": "ROLE_USER"}):
            with pytest.raises(Exception):
                peer.call("SendMessage", {"message": broken})
        assert peer.message_sends == 0
        assert len(peer.errors) == 3
        assert peer.executions == 0
    finally:
        peer.close()


def test_strict_peer_rejects_v03_messages_missing_required_fields():
    peer = _StrictTaskPeer(mode="complete")
    try:
        for broken in (
                {"role": "ROLE_USER", "parts": [{"text": "x"}]},
                {"message_id": "m-1", "parts": [{"text": "x"}]},
                {"message_id": "m-1", "role": "ROLE_USER"}):
            with pytest.raises(Exception):
                peer.call("message/send", {"message": broken})
        assert peer.message_sends == 0
        assert len(peer.errors) == 3
        assert peer.executions == 0
    finally:
        peer.close()


def test_explicit_return_immediately_false_uses_blocking_semantics():
    peer = _StrictTaskPeer(mode="complete")
    try:
        response = peer.call("SendMessage", {
            "message": {"messageId": "m-1", "role": "ROLE_USER",
                        "parts": [{"data": {"method": "tools/call", "name": "draft_report",
                                            "arguments": _tool_args()}}]},
            "configuration": {"returnImmediately": False}})
        assert response["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        assert peer.executions == 1
        assert peer.v1_sends == 1
        assert peer.errors == []
    finally:
        peer.close()


def test_strict_peer_serves_a2a_1_0_clients_with_the_v1_wire_shapes(monkeypatch):
    peer = _StrictTaskPeer(mode="complete")
    try:
        started = peer.call("SendMessage", {
            "message": {"messageId": "m-1", "role": "ROLE_USER",
                        "parts": [{"data": {"method": "tools/call", "name": "draft_report",
                                            "arguments": _tool_args()}}]},
            "configuration": {"returnImmediately": True}})
        assert started["task"]["status"]["state"] == "TASK_STATE_WORKING"
        assert started["task"]["id"] == peer.task_id
        assert started["task"]["contextId"] == peer.context_id
        assert peer.executions == 1
        assert peer.v1_sends == 1

        peer.completed = True
        peer.output = "final output"
        observed = peer.call("GetTask", {"id": peer.task_id})
        assert observed["status"]["state"] == "TASK_STATE_COMPLETED"
        assert observed["artifacts"][0]["parts"][0]["text"] == "final output"
        assert peer.executions == 1
        assert peer.v1_sends == 1
        assert peer.v1_gets == 1
        assert peer.errors == []
    finally:
        peer.close()


def test_working_refusal_is_not_retried_by_the_dispatch_layer(monkeypatch):
    from orchestrator import hitl_confirmation
    from orchestrator.orchestrator import Orchestrator

    monkeypatch.setattr(hitl_confirmation, "approved_call", lambda *args, **kwargs: False)
    working = _task(TaskState.TASK_STATE_WORKING, artifact_text="partial output",
                    task_id="peer-task-1", context_id="peer-context-1")
    refusal = a2a_response_to_mcp_response(working, "req-1")
    attempts = []

    async def _execute_tool_and_wait(agent_id, tool_name, args, **kwargs):
        attempts.append((agent_id, tool_name))
        return refusal

    orchestrator = SimpleNamespace(
        MAX_RETRIES=3,
        RETRY_BACKOFF=(0.01, 0.01),
        execute_tool_and_wait=_execute_tool_and_wait,
        _protected_dispatch_channel=lambda websocket, explicit=None: "dispatch",
    )
    result = asyncio.run(Orchestrator._execute_with_retry(
        orchestrator, None, "remote-1", "draft_report", _tool_args(),
        max_retries=3, user_id="owner"))
    assert attempts == [("remote-1", "draft_report")]
    assert result is refusal
    assert result.error["retryable"] is False
    assert result.error["continuation"] == "unsupported"
    assert result.error["task_id"] == "peer-task-1"
    assert result.error["context_id"] == "peer-context-1"
