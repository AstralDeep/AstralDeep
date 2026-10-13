"""Exercises the context-only provider adapter with real SDK serialization and controlled HTTP responses.
Endpoint, egress, keyless authorization and response lifetime checks precede every physical request.
"""

import importlib
import json
import ssl

from openai import DefaultHttpxClient, OpenAI
import pytest

from llm_config import evidence_transport
from llm_config.client_factory import KEYLESS_API_KEY_SENTINEL
from shared import external_http

sdk_httpx = importlib.import_module(DefaultHttpxClient.__mro__[1].__module__.split(".")[0])


@pytest.fixture
def wire(monkeypatch):
    observed, transports, addresses = [], [], ["8.8.8.8"]
    reply = {"status": 200, "headers": {}, "body": {
        "id": "chatcmpl-evidence", "object": "chat.completion", "created": 1, "model": "model",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "answer"}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
    }}
    original = evidence_transport.DefaultHttpxClient

    def receive(request):
        observed.append(request)
        if callable(reply.get("on_response")):
            reply["on_response"](request)
        return sdk_httpx.Response(reply["status"], headers=reply["headers"],
            content=reply.get("content"), json=None if "content" in reply else reply["body"], request=request)

    class ControlledClient(original):
        def __init__(self, **kwargs):
            self.supplied_options = dict(kwargs)
            kwargs["transport"] = sdk_httpx.MockTransport(receive)
            super().__init__(**kwargs)
            transports.append(self)

    monkeypatch.setattr(evidence_transport, "DefaultHttpxClient", ControlledClient)
    monkeypatch.setattr(external_http, "_resolve_host_addresses", lambda _host: tuple(addresses))
    monkeypatch.delenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", raising=False)
    return observed, transports, addresses, reply


def bind(*, base_url="https://model.invalid/v1", api_key="synthetic-owner-key"):
    original = OpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=11)
    return original, evidence_transport.guard_model_client(original, base_url=base_url)


def test_real_sdk_preserves_complete_request_and_closes_only_finished_attempts(wire):
    observed, transports, _addresses, _reply = wire
    original, client = bind()
    messages = [{"role": "system", "content": "Verbatim governing instruction."},
                {"role": "user", "content": "x" * 70_000}]
    tools = [{"type": "function", "function": {"name": "read_source", "description": "Read exact source.",
             "parameters": {"type": "object", "properties": {"offset": {"type": "integer"}}}}}]
    assert original.is_closed()
    assert not client.is_closed() and client.api_key == "synthetic-owner-key"
    response = client.chat.completions.create(model="model", messages=messages, tools=tools,
                                              tool_choice="auto", max_tokens=512, temperature=0)
    assert response.choices[0].message.content == "answer"
    assert len(observed) == len(transports) == 1
    payload = json.loads(observed[0].content)
    assert payload == {"model": "model", "messages": messages, "tools": tools,
                       "tool_choice": "auto", "max_tokens": 512, "temperature": 0}
    assert str(observed[0].url) == "https://model.invalid/v1/chat/completions"
    assert observed[0].headers["authorization"] == "Bearer synthetic-owner-key"
    assert transports[0].is_closed
    assert transports[0].supplied_options["verify"] is True
    assert transports[0].supplied_options["trust_env"] is False
    assert transports[0].supplied_options["follow_redirects"] is False
    assert client.chat.completions.create(model="model", messages=messages).usage.total_tokens == 10
    assert len(transports) == 2 and all(value.is_closed for value in transports)
    client.close()
    assert client.is_closed()
    with pytest.raises(evidence_transport.EvidenceTransportError, match="evidence_provider_unavailable"):
        client.chat.completions.create(model="model", messages=messages)


@pytest.mark.parametrize("key", ["", KEYLESS_API_KEY_SENTINEL])
def test_replacement_transport_retains_keyless_authorization_stripping(wire, key):
    observed, transports, _addresses, _reply = wire
    _original, client = bind(api_key=key or KEYLESS_API_KEY_SENTINEL)
    client.chat.completions.create(model="model", messages=[], extra_headers={"Authorization": "Bearer injected"})
    assert "authorization" not in observed[0].headers
    assert transports[0].is_closed


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "::ffff:127.0.0.1"])
def test_actual_request_rechecks_shared_egress_before_transport_receives_body(wire, address):
    observed, transports, addresses, _reply = wire
    _original, client = bind()
    addresses[:] = [address]
    with pytest.raises(Exception) as refused:
        client.chat.completions.create(model="model", messages=[{"role": "user", "content": "owned synthetic content"}])
    assert observed == [] and transports[0].is_closed
    assert "owned synthetic content" not in str(refused.value)
    assert "model.invalid" not in str(refused.value)


def test_environment_proxy_and_tls_settings_cannot_replace_guarded_transport(wire, monkeypatch):
    observed, transports, _addresses, _reply = wire
    _original, client = bind()
    monkeypatch.setenv("HTTPS_PROXY", "http://unapproved.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://unapproved.invalid:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/missing/environment-certificate.pem")
    client.chat.completions.create(model="model", messages=[])
    assert len(observed) == 1 and transports[0].supplied_options["trust_env"] is False
    assert transports[0].supplied_options["verify"] is True


@pytest.mark.parametrize("status", [301, 302, 307, 308])
@pytest.mark.parametrize("stream", [False, True])
def test_redirect_is_refused_without_second_request_or_disclosing_body(wire, status, stream):
    observed, transports, _addresses, reply = wire
    reply.update(status=status, headers={"location": "https://different.invalid/collect"})
    _original, client = bind()
    with pytest.raises(Exception):
        client.chat.completions.create(model="model", messages=[{"role": "user", "content": "protected synthetic body"}], stream=stream)
    assert len(observed) == 1 and transports[0].is_closed


@pytest.mark.parametrize("url", [
    "", "ftp://model.invalid/v1", "https://name:pass@model.invalid/v1", "https://model.invalid/v1?version=1",
    "https://model.invalid/v1#fragment", "https://model.invalid/v1/../other", "https://model.invalid/v1/%2fother",
    "https://model.invalid/v1//other", "http://model.invalid/v1", "https://model.invalid/v1\\other",
    "https://model.invalid:0/v1",
])
def test_unsupported_endpoint_fails_without_eager_dns_or_physical_request(wire, monkeypatch, url):
    observed, transports, _addresses, _reply = wire
    resolved = []
    monkeypatch.setattr(external_http, "_resolve_host_addresses", lambda host: resolved.append(host) or ("8.8.8.8",))
    original = OpenAI(api_key="synthetic-owner-key", base_url="https://model.invalid/v1", max_retries=0)
    try:
        with pytest.raises(evidence_transport.EvidenceTransportError):
            evidence_transport.guard_model_client(original, base_url=url)
        assert observed == transports == resolved == []
    finally:
        original.close()


@pytest.mark.parametrize("mutation", ["origin", "path", "query", "method", "host_header"])
def test_actual_request_cannot_escape_configured_endpoint(wire, mutation):
    observed, transports, _addresses, _reply = wire
    _original, client = bind()
    transport = evidence_transport.DefaultHttpxClient(verify=True, trust_env=False, follow_redirects=False)
    try:
        target = "https://different.invalid/v1/chat/completions" if mutation == "origin" else "https://model.invalid/v1/chat/completions"
        if mutation == "path":
            target = "https://model.invalid/v1/models"
        elif mutation == "query":
            target += "?route=other"
        request = sdk_httpx.Request("GET" if mutation == "method" else "POST", target,
                                   headers={"Host": "different.invalid"} if mutation == "host_header" else {})
        with pytest.raises(evidence_transport.EvidenceTransportError):
            client._guard(request)
        assert observed == []
    finally:
        transport.close()
    assert all(value.is_closed for value in transports)


@pytest.mark.parametrize("host,address", [("localhost", "127.0.0.1"), ("10.1.2.3", "10.1.2.3"),
    ("local-model.invalid", "192.168.1.2"), ("local-model.invalid", "::ffff:127.0.0.1")])
def test_private_plaintext_needs_positive_operator_host_admission(wire, monkeypatch, host, address):
    observed, transports, addresses, _reply = wire
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", host)
    addresses[:] = [address]
    original, client = bind(base_url=f"http://{host}:11434/v1", api_key=KEYLESS_API_KEY_SENTINEL)
    assert original.is_closed()
    client.chat.completions.create(model="model", messages=[])
    assert len(observed) == 1 and "authorization" not in observed[0].headers
    assert transports[0].is_closed
    addresses[:] = ["8.8.8.8"]
    with pytest.raises(Exception):
        client.chat.completions.create(model="model", messages=[])
    assert len(observed) == 1 and all(value.is_closed for value in transports)


def test_private_host_permission_revocation_is_current_at_actual_request(wire, monkeypatch):
    observed, transports, addresses, _reply = wire
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "localhost")
    addresses[:] = ["127.0.0.1"]
    _original, client = bind(base_url="http://localhost:11434/v1")
    monkeypatch.delenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS")
    with pytest.raises(Exception):
        client.chat.completions.create(model="model", messages=[])
    assert observed == [] and transports[0].is_closed


def test_research_classification_cannot_authorize_public_plaintext(wire, monkeypatch):
    observed, transports, addresses, _reply = wire
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "local-model.invalid")
    monkeypatch.setenv("RESEARCH_LOCAL_ENDPOINT_ALLOWLIST", "http://8.8.8.8")
    addresses[:] = ["8.8.8.8"]
    _original, client = bind(base_url="http://local-model.invalid:11434/v1")
    with pytest.raises(Exception):
        client.chat.completions.create(model="model", messages=[])
    assert observed == [] and transports[0].is_closed


def test_private_host_admission_lost_during_shared_validation_refuses_plaintext(wire, monkeypatch):
    observed, transports, addresses, _reply = wire
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "localhost")
    addresses[:] = ["127.0.0.1"]
    _original, client = bind(base_url="http://localhost:11434/v1")
    validate = external_http.validate_egress_url

    def revoke_after_validation(url):
        validate(url)
        monkeypatch.delenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS")

    monkeypatch.setattr(external_http, "validate_egress_url", revoke_after_validation)
    with pytest.raises(Exception):
        client.chat.completions.create(model="model", messages=[])
    assert observed == [] and transports[0].is_closed


def test_client_copy_failure_closes_transport_without_physical_request(wire, monkeypatch):
    observed, transports, _addresses, _reply = wire
    original, client = bind()

    def unavailable_copy(*_args, **_kwargs):
        raise RuntimeError("synthetic SDK copy failure")

    monkeypatch.setattr(OpenAI, "with_options", unavailable_copy)
    with pytest.raises(RuntimeError, match="synthetic SDK copy failure"):
        client.chat.completions.create(model="model", messages=[])
    assert original.is_closed() and observed == [] and transports[0].is_closed


def test_non_sdk_or_mismatched_template_cannot_bind_transport(wire, monkeypatch):
    observed, transports, _addresses, _reply = wire
    resolved = []
    monkeypatch.setattr(external_http, "_resolve_host_addresses", lambda host: resolved.append(host) or ("8.8.8.8",))
    with pytest.raises(evidence_transport.EvidenceTransportError):
        evidence_transport.guard_model_client(object(), base_url="https://model.invalid/v1")
    original = OpenAI(api_key="synthetic-owner-key", base_url="https://different.invalid/v1", max_retries=0)
    with pytest.raises(evidence_transport.EvidenceTransportError):
        evidence_transport.guard_model_client(original, base_url="https://model.invalid/v1")
    assert original.is_closed() and observed == transports == resolved == []


def test_stream_owns_derived_client_until_consumed_or_closed(wire):
    observed, transports, _addresses, reply = wire
    chunk = {"id": "chatcmpl-stream", "object": "chat.completion.chunk", "created": 1, "model": "model",
             "choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": None}]}
    reply.update(headers={"content-type": "text/event-stream"}, content=("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
    _original, client = bind()
    stream = client.chat.completions.create(model="model", messages=[], stream=True)
    assert len(observed) == 1 and not transports[0].is_closed
    assert [value.choices[0].delta.content for value in stream] == ["answer"]
    assert transports[0].is_closed
    stream.close()
    pending = client.chat.completions.create(model="model", messages=[], stream=True)
    assert not transports[-1].is_closed
    with pending:
        assert not transports[-1].is_closed
    assert transports[-1].is_closed
    final = client.chat.completions.create(model="model", messages=[], stream=True)
    assert final.response.status_code == 200
    client.close()
    assert client.is_closed() and not transports[-1].is_closed
    assert next(final).choices[0].delta.content == "answer"
    with pytest.raises(StopIteration):
        next(final)
    assert transports[-1].is_closed


def test_tls_failure_does_not_leave_a_derived_client_open(wire, monkeypatch):
    observed, transports, _addresses, _reply = wire
    def refuse(_request):
        raise ssl.SSLCertVerificationError("owned synthetic certificate rejection")
    monkeypatch.setattr(evidence_transport.DefaultHttpxClient, "_send_single_request", lambda _self, request: refuse(request))
    _original, client = bind()
    with pytest.raises(Exception):
        client.chat.completions.create(model="model", messages=[])
    assert observed == [] and transports[0].is_closed
