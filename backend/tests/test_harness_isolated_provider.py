"""Selected transport uses the real supervised HTTP child with bounded operator-approved egress.
A synthetic local responder exercises transport only and never qualifies or activates a model deployment.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from llm_config import research_profile
from llm_config.provider_egress import capture_private_policy, ProviderEgressUnavailable
from orchestrator.harness.execution_binding import ModelHarnessBinding
from orchestrator.harness.production_profiles import public_profiles


@pytest.fixture
def responder():
    state = SimpleNamespace(calls=0, response_size=0, status=200, delay=False,
                            arrived=threading.Event(), release=threading.Event())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            state.calls += 1
            state.request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"id": "synthetic", "object": "chat.completion", "created": 1,
                "model": "synthetic", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "bounded"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}).encode()
            state.arrived.set()
            if state.delay:
                state.release.wait(2)
            if state.response_size:
                body = b"x" * state.response_size
            self.send_response(state.status)
            if state.status == 302:
                self.send_header("Location", state.base_url + "/second")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def selected(base_url):
    config = SimpleNamespace(profile=research_profile.LOCAL_PROFILE.bind(model="synthetic", base_url=base_url),
                             _api_key="")
    return ModelHarnessBinding(None, None, None, None, "owner", None, config, None, None, None,
                               capture_private_policy())


@pytest.mark.asyncio
async def test_real_child_obeys_captured_existing_policy_and_sends_once(responder, monkeypatch):
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    value = selected(responder.base_url)
    body = {"model": "synthetic", "messages": [{"role": "user", "content": "bounded"}],
            "stream": False, "store": False, "n": 1, "max_completion_tokens": 1024}
    value.request(body)
    response = await value.invoke(body)
    value.response(response)
    assert responder.calls == 1 and responder.request == body
    monkeypatch.delenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS")
    with pytest.raises(ProviderEgressUnavailable):
        await value.invoke(body)
    assert responder.calls == 1 and public_profiles()[0]["available"] is False


@pytest.mark.asyncio
async def test_private_denial_and_oversize_never_retry(responder, monkeypatch):
    from shared.isolated_http import IsolatedHttpError
    monkeypatch.delenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", raising=False)
    body = {"model": "synthetic", "messages": [{"role": "user", "content": "bounded"}],
            "stream": False, "store": False, "n": 1, "max_completion_tokens": 1024}
    with pytest.raises(IsolatedHttpError):
        await selected(responder.base_url).invoke(body)
    assert responder.calls == 0
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    responder.response_size = 1024 * 1024 + 1
    with pytest.raises(IsolatedHttpError):
        await selected(responder.base_url).invoke(body)
    assert responder.calls == 1


@pytest.mark.asyncio
async def test_redirect_does_not_send_a_second_request(responder, monkeypatch):
    from shared.isolated_http import IsolatedHttpError
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    responder.status = 302
    with pytest.raises(IsolatedHttpError):
        await selected(responder.base_url).invoke({"model": "synthetic", "messages": [], "stream": False,
            "store": False, "n": 1, "max_completion_tokens": 1024})
    assert responder.calls == 1


@pytest.mark.asyncio
async def test_private_attempt_timeout_never_retries(responder, monkeypatch):
    from shared.isolated_http import IsolatedHttpError
    from orchestrator.harness import execution_binding
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    monkeypatch.setattr(execution_binding, "SELECTED_ATTEMPT_SECONDS", 0.2, raising=False)
    responder.delay = True
    with pytest.raises(IsolatedHttpError):
        await selected(responder.base_url).invoke({"model": "synthetic", "messages": [], "stream": False,
            "store": False, "n": 1, "max_completion_tokens": 1024})
    assert responder.calls <= 1


@pytest.mark.asyncio
async def test_private_attempt_cancel_settles_child_without_second_send(responder, monkeypatch):
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    responder.delay = True
    task = asyncio.create_task(selected(responder.base_url).invoke({"model": "synthetic", "messages": [], "stream": False,
            "store": False, "n": 1, "max_completion_tokens": 1024}))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(responder.arrived.wait, 10), 12)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert responder.calls == 1
    finally:
        responder.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_physical_transport_preserves_context_guard_lower_output_bound(responder, monkeypatch):
    from copy import deepcopy
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    value = selected(responder.base_url)
    body = {"model": "synthetic", "messages": [{"role": "user", "content": "bounded"}],
            "stream": False, "store": False, "n": 1, "max_completion_tokens": 1024}
    value.request(body)
    body.pop("max_completion_tokens")
    body["max_tokens"] = 16
    guarded = deepcopy(body)
    await value.invoke(body)
    assert responder.request == guarded and body == guarded


@pytest.mark.asyncio
async def test_existing_sdk_assistant_tool_message_keeps_normal_wire_fields(responder, monkeypatch):
    from openai.types.chat import ChatCompletionMessage
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    value = selected(responder.base_url)
    assistant = ChatCompletionMessage(role="assistant", content=None,
        tool_calls=[{"id": "synthetic-call", "type": "function",
                     "function": {"name": "trusted_tool", "arguments": "{}"}}])
    body = {"model": "synthetic", "messages": [{"role": "user", "content": "bounded"}, assistant,
        {"role": "tool", "tool_call_id": "synthetic-call", "content": "bounded result"}]}
    value.request(body)
    await value.invoke(body)
    assert responder.request["messages"][1] == assistant.model_dump(mode="json")
    assert responder.calls == 1
