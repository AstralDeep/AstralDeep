"""FR-019 SDK-wire compatibility, without vendor traffic or billing claims.

The real encrypted store, context resolver, factory and OpenAI SDK execute.
Only the final httpx transport is replaced; request hooks and serialization
remain real. A synthetic response proves local interoperability, not that a
vendor/model is live or has a qualified task reservation/accounting profile.
"""

from copy import deepcopy
import importlib
import importlib.util
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from openai import APIStatusError
import pytest

from audit.pii import private_binding_key
from llm_config.client_factory import build_llm_client
from llm_config.providers import all_presets, get_preset, resolve_base_url
from llm_config.research_profile import ResearchProfileUnavailable, select_config
from llm_config.tests.test_call_llm_credential_resolution import (
    _FakeWS,
    _make_stub,
    _register,
)
from llm_config.types import CredentialSource, LLMUnavailable


@pytest.fixture
def wire(monkeypatch):
    """Capture the actual SDK request before any network connection is made."""
    state = SimpleNamespace(requests=[], status=200, usage=True)

    def refuse_network(*_args, **_kwargs):
        raise AssertionError("provider test attempted unmocked network access")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    monkeypatch.setattr(socket, "getaddrinfo", refuse_network)

    def handle_request(request, response_type):
        body = json.loads(request.content)
        state.requests.append((request, body))
        if state.status != 200:
            return response_type(
                state.status, json={"error": {"message": "fixture refusal"}}
            )
        response = {
            "id": "chatcmpl-fixture",
            "object": "chat.completion",
            "created": 1,
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Fixture answer"},
                    "finish_reason": "stop",
                }
            ],
        }
        if state.usage:
            response["usage"] = {
                "prompt_tokens": 3,
                "completion_tokens": 5,
                "total_tokens": 8,
            }
        return response_type(200, json=response)

    # The allowed OpenAI SDK range includes legacy httpx and newer httpx2
    # defaults. Keyless production clients explicitly use legacy httpx. Patch
    # each installed transport while retaining its own request/response types.
    transports = [httpx]
    if importlib.util.find_spec("httpx2") is not None:
        transports.append(importlib.import_module("httpx2"))
    for module in transports:

        def intercept(_transport, request, response_type=module.Response):
            return handle_request(request, response_type)

        monkeypatch.setattr(module.HTTPTransport, "handle_request", intercept)
    return state


def test_transport_fixture_blocks_unintercepted_network(wire):
    """A future SDK transport change must fail before DNS or any connection."""
    with socket.socket() as connection:
        for action in (
            lambda: connection.connect(("127.0.0.1", 1)),
            lambda: connection.connect_ex(("127.0.0.1", 1)),
            lambda: socket.create_connection(("127.0.0.1", 1)),
            lambda: socket.getaddrinfo("provider.example.test", 443),
        ):
            with pytest.raises(AssertionError, match="unmocked network"):
                action()
    assert wire.requests == []


def _complete(client, resolved):
    """Use ordinary compatible chat framing with the selected model unchanged."""
    return client.chat.completions.create(
        model=resolved.model,
        messages=[
            {"role": "system", "content": "Synthetic instruction"},
            {"role": "user", "content": "Synthetic question"},
        ],
        max_tokens=17,
        stream=False,
    )


def _seed(store, provider, source):
    """Keep alternate user and system records populated to detect borrowing."""
    selected = dict(
        provider=provider,
        base_url=resolve_base_url(provider, "https://custom.example.test/gateway/v1"),
        model=f"selected-{provider}-{source.value}",
        api_key=f"synthetic-{source.value}-key"
        if get_preset(provider).key_required
        else "",
    )
    alternate = dict(
        provider="custom",
        base_url="https://other.example.test/v1",
        model="other-context-model",
        api_key="synthetic-other-context-key",
    )
    store.set_sync(
        "alice", **(selected if source is CredentialSource.USER else alternate)
    )
    store.set_sync("bob", **alternate)
    store.set_system_sync(
        **(selected if source is CredentialSource.SYSTEM else alternate),
        updated_by="fixture-admin",
    )
    return selected


@pytest.mark.parametrize("provider", [preset.key for preset in all_presets()])
@pytest.mark.parametrize("source", [CredentialSource.USER, CredentialSource.SYSTEM])
async def test_every_current_provider_retains_its_context_model_and_wire_auth(
    store,
    fake_db,
    fake_recorder,
    wire,
    provider,
    source,
):
    selected = _seed(store, provider, source)
    encrypted_before = deepcopy((fake_db.users, fake_db.system))
    resolver = _make_stub(store, fake_recorder)
    websocket = _FakeWS() if source is CredentialSource.USER else None
    if websocket is not None:
        _register(resolver, websocket, "alice")
    client, actual_source, resolved = await resolver._resolve_llm_client_for(websocket)
    with client:
        response = _complete(client, resolved)
    assert actual_source is source
    assert resolved.model == selected["model"]
    assert response.model == selected["model"]
    assert response.choices[0].message.content == "Fixture answer"
    assert response.usage.total_tokens == 8
    assert len(wire.requests) == 1
    request, body = wire.requests[0]
    assert str(request.url) == selected["base_url"] + "/chat/completions"
    expected_auth = f"Bearer {selected['api_key']}" if selected["api_key"] else None
    assert request.headers.get("Authorization") == expected_auth
    assert body == {
        "model": selected["model"],
        "messages": [
            {"role": "system", "content": "Synthetic instruction"},
            {"role": "user", "content": "Synthetic question"},
        ],
        "max_tokens": 17,
        "stream": False,
    }
    assert not hasattr(resolved, "api_key")
    assert (fake_db.users, fake_db.system) == encrypted_before
    if selected["api_key"]:
        assert selected["api_key"] not in repr(encrypted_before)


@pytest.mark.parametrize("provider", ["ollama", "lmstudio", "custom"])
@pytest.mark.parametrize("key", ["", "not-needed"])
def test_keyless_local_calls_keep_fresh_transport_and_unknown_usage(
    store, wire, provider, key
):
    selected = _seed(store, provider, CredentialSource.USER)
    store.set_sync("alice", **{**selected, "api_key": key})
    wire.usage = False
    for _ in range(2):
        client, _, resolved = build_llm_client(
            store.get_sync("alice"), CredentialSource.USER
        )
        with client:
            response = _complete(client, resolved)
            assert (
                response.usage is None
            )  # Missing observations are not synthetic zero counters.
        assert client.is_closed()
    assert len(wire.requests) == 2
    assert all(
        request.headers.get("Authorization") is None for request, _ in wire.requests
    )
    assert all(body["model"] == selected["model"] for _, body in wire.requests)


@pytest.mark.parametrize("status", [401, 429, 503])
@pytest.mark.parametrize("source", [CredentialSource.USER, CredentialSource.SYSTEM])
async def test_provider_refusal_is_one_sdk_attempt_without_credential_fallback(
    store,
    fake_recorder,
    wire,
    status,
    source,
):
    selected = _seed(store, "groq", source)
    resolver = _make_stub(store, fake_recorder)
    websocket = _FakeWS() if source is CredentialSource.USER else None
    if websocket is not None:
        _register(resolver, websocket, "alice")
    client, _, resolved = await resolver._resolve_llm_client_for(websocket)
    wire.status = status
    with client, pytest.raises(APIStatusError) as failure:
        _complete(client, resolved)
    assert failure.value.status_code == status
    assert len(wire.requests) == 1
    assert (
        wire.requests[0][0].headers["Authorization"] == f"Bearer {selected['api_key']}"
    )


@pytest.mark.parametrize("provider", ["ollama", "lmstudio", "custom"])
def test_local_config_remains_usable_after_strict_research_profile_refusal(
    store,
    fake_db,
    wire,
    monkeypatch,
    provider,
):
    selected = _seed(store, provider, CredentialSource.USER)
    store._repository.plane_runtime.repositories.history = SimpleNamespace(
        sessions=SimpleNamespace(bound_request_execution_waits=Mock(return_value=None)),
    )
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "provider_breadth_test")
    monkeypatch.setenv(
        "AUDIT_HMAC_SECRET", "synthetic-provider-binding-" + "0123456789abcdef" * 2
    )
    monkeypatch.delenv("AUDIT_HMAC_SECRET_PROVIDER_BREADTH_TEST", raising=False)
    encrypted_before = deepcopy((fake_db.users, fake_db.system))
    capture = store.capture_user_sync("alice")
    with pytest.raises(
        ResearchProfileUnavailable, match="^research_profile_unavailable$"
    ):
        select_config(capture, store=store, binding_key=private_binding_key())
    assert wire.requests == []
    assert (fake_db.users, fake_db.system) == encrypted_before
    client, source, resolved = build_llm_client(
        store.get_sync("alice"), CredentialSource.USER
    )
    with client:
        assert _complete(client, resolved).model == selected["model"]
    assert source is CredentialSource.USER
    assert len(wire.requests) == 1
    store.clear_sync("alice")
    with pytest.raises(LLMUnavailable):
        build_llm_client(store.get_sync("alice"), CredentialSource.USER)
    assert store.get_sync("bob") is not None and store.get_system_sync() is not None
    assert len(wire.requests) == 1
