"""FR-019 SDK-wire compatibility, without vendor traffic or billing claims.

The real encrypted store, context resolver, factory and OpenAI SDK execute.
Only the final httpx transport is replaced; request hooks and serialization
remain real. A synthetic response proves local interoperability, not that a
vendor/model is live or has a qualified task reservation/accounting profile.
"""

from copy import deepcopy
import importlib
import importlib.util
import inspect
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from openai import APIStatusError
import pytest

from audit.pii import private_binding_key
from llm_config.client_factory import build_llm_client, local_inference_frame
from llm_config.providers import all_presets, get_preset, resolve_base_url
from llm_config import research_profile as profile
from llm_config.research_profile import ResearchProfileUnavailable, select_config
from llm_config.tests.test_call_llm_credential_resolution import (
    _FakeWS,
    _make_stub,
    _register,
)
from llm_config.types import CredentialSource, LLMUnavailable


@pytest.fixture
def wire(request, monkeypatch):
    """Capture the actual SDK request before any network connection is made."""
    state = SimpleNamespace(requests=[], status=200, usage=True)

    def refuse_network(*_args, **_kwargs):
        raise AssertionError("provider test attempted unmocked network access")

    if inspect.iscoroutinefunction(getattr(request.node, "obj", None)):
        # Materialize the test's event loop BEFORE the socket refusal is
        # installed: on Windows the ProactorEventLoop builds its self-pipe with
        # a loopback ``socketpair`` connect, which must stay refused for the
        # test body itself (``test_transport_fixture_blocks_unintercepted_network``
        # pins that 127.0.0.1 connects are refused, so the guard is not
        # loosened). pytest-asyncio 1.x names the loop runner fixture by scope;
        # a missing name is not an error on hosts that never needed it.
        try:
            request.getfixturevalue("_function_scoped_runner")
        except pytest.FixtureLookupError:
            pass

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


def _bind_audit(store, monkeypatch):
    """Private binding key + session stub the exact-row capture path needs."""
    store._repository.plane_runtime.repositories.history = SimpleNamespace(
        sessions=SimpleNamespace(bound_request_execution_waits=Mock(return_value=None)),
    )
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "provider_breadth_test")
    monkeypatch.setenv(
        "AUDIT_HMAC_SECRET", "synthetic-provider-binding-" + "0123456789abcdef" * 2
    )
    monkeypatch.delenv("AUDIT_HMAC_SECRET_PROVIDER_BREADTH_TEST", raising=False)
    return private_binding_key()


DOCUMENTED_PRESET_KEYS = (
    "openai",
    "anthropic",
    "gemini",
    "xai",
    "openrouter",
    "groq",
    "together",
    "mistral",
    "ollama",
    "lmstudio",
    "custom",
)


def test_preset_catalog_is_exactly_the_documented_breadth():
    """FR-019: a removed, renamed or reordered provider fails here, not in prod."""
    presets = all_presets()
    assert tuple(preset.key for preset in presets) == DOCUMENTED_PRESET_KEYS
    assert presets[-1].key == "custom" and presets[-1].base_url is None
    key_optional = tuple(preset.key for preset in presets if not preset.key_required)
    assert key_optional == profile.LOCAL_PROVIDERS == ("ollama", "lmstudio", "custom")
    assert all(get_preset(key) is not None for key in DOCUMENTED_PRESET_KEYS)
    for preset in presets:
        if preset.key not in profile.LOCAL_PROVIDERS:
            assert preset.key_required and preset.base_url.startswith("https://")
    # The Work profiles select from this catalog and never widen it.
    assert profile.OPENAI_PROFILE.providers == ("openai",)
    assert set(profile.LOCAL_PROFILE.providers) < set(DOCUMENTED_PRESET_KEYS)
    assert profile.PROFILES == (profile.OPENAI_PROFILE, profile.LOCAL_PROFILE)
    assert profile.DEFAULT_PROFILE is profile.OPENAI_PROFILE


@pytest.mark.parametrize("provider", ["ollama", "lmstudio", "custom"])
@pytest.mark.parametrize("key", ["", "not-needed", "synthetic-local-key"])
def test_local_profile_reserves_bounded_accounting_and_charges_unknown_usage_at_reserved_max(
    store, fake_db, wire, monkeypatch, provider, key
):
    base_url = {
        "ollama": "http://localhost:11434/v1",
        "lmstudio": "http://localhost:1234/v1",
        "custom": "http://192.168.10.20:8000/v1",
    }[provider]
    store.set_sync("alice", provider=provider, base_url=base_url,
                   model="local-selector-q4", api_key=key)
    binding = _bind_audit(store, monkeypatch)
    encrypted_before = deepcopy((fake_db.users, fake_db.system))
    capture = store.capture_user_sync("alice")
    # The default (OpenAI) profile still refuses this row: no silent widening.
    with pytest.raises(ResearchProfileUnavailable):
        select_config(capture, store=store, binding_key=binding)
    selection = select_config(
        capture, store=store, binding_key=binding, profile=profile.LOCAL_PROFILE
    )
    assert selection.local and selection.profile.name == "local-page-selection/v1"
    assert selection.profile.model == "local-selector-q4"
    assert selection.profile.base_url == base_url
    assert selection.profile.endpoint == base_url + "/chat/completions"
    assert selection._api_key == ("" if key in ("", "not-needed") else key)
    # Same shape as the OpenAI reservation the execution adapter charges.
    reservation = selection.reservation
    assert set(reservation) == set(profile.OPENAI_PROFILE.reservation()) == {
        "model_calls", "tokens", "elapsed_ms"}
    assert reservation == {"model_calls": 1, "tokens": 32768 + 1024, "elapsed_ms": 120000}
    assert reservation == profile.LOCAL_PROFILE.reservation()
    assert 0 < reservation["tokens"] and 0 < reservation["elapsed_ms"] <= 120_000
    # The admitted body can never exceed the declared context bound.
    assert profile.MAX_REQUEST_BYTES // 2 <= profile.LOCAL_CONTEXT_TOKENS
    observation = _observation()
    request = profile.build_request("Select release notes.", observation,
                                    profile=selection.profile)
    body = json.loads(request.body)
    assert body["model"] == "local-selector-q4"
    assert body["max_completion_tokens"] == profile.LOCAL_OUTPUT_TOKENS
    assert "p001" in request.passage_ids
    # Unknown usage is 'usage_unknown' -- never a synthetic zero -- so the
    # adapter rule (tokens = maximum.tokens when total is None) charges the
    # full reserved maximum for this profile.
    document = _reply("local-selector-q4", usage=None)
    unknown = profile.parse_response(document, status_code=200,
                                     passage_ids=request.passage_ids,
                                     profile=selection.profile)
    assert unknown == profile.ResearchResponse(None, None, "usage_unknown")
    assert unknown.usage is None
    known = profile.parse_response(_reply("local-selector-q4", usage=(30, 7, 37)),
                                   status_code=200, passage_ids=request.passage_ids,
                                   profile=selection.profile)
    assert known.disposition == "evidence" and known.passage_ids == ("p001",)
    assert (known.usage.total_tokens, known.usage.prompt_tokens,
            known.usage.completion_tokens) == (37, 30, 7)
    assert not known.usage.exceeds_profile
    # Overrun is observed against the LOCAL bound, not the OpenAI 128k one.
    over = profile.parse_response(_reply("local-selector-q4", usage=(32769, 1, 32770)),
                                  status_code=200, passage_ids=request.passage_ids,
                                  profile=selection.profile)
    assert over.disposition == "profile_exceeded" and over.usage.total_tokens == 32770
    # An OpenAI-model answer on the local profile is an invalid answer, never a
    # cross-profile fallback; usage stays factual.
    foreign = profile.parse_response(_reply(profile.MODEL, usage=(1, 1, 2)), status_code=200,
                                     passage_ids=request.passage_ids, profile=selection.profile)
    assert foreign.disposition == "answer_invalid" and foreign.usage.total_tokens == 2
    assert wire.requests == []
    assert (fake_db.users, fake_db.system) == encrypted_before
    if key == "synthetic-local-key":
        assert key not in repr(selection) and key not in repr(encrypted_before)


@pytest.mark.parametrize(
    "provider,base_url",
    [
        ("custom", "https://custom.example.test/gateway/v1"),
        ("custom", "http://ollama.internal:11434/v1"),  # unresolved hostname
        ("custom", "http://169.254.1.1:8000/v1"),  # link-local is not RFC1918
        ("custom", "http://100.64.0.1:8000/v1"),  # CGNAT is not RFC1918
        ("custom", "http://172.32.0.1:8000/v1"),  # one past 172.16/12
        ("custom", "http://user:secret@127.0.0.1:8000/v1"),
        ("ollama", "https://api.openai.com/v1"),
        ("openai", "https://api.openai.com/v1"),
        ("openai", "http://localhost:11434/v1"),
        ("groq", "http://127.0.0.1:8000/v1"),
    ],
)
def test_local_profile_never_selected_for_remote_base_url(
    store, fake_db, wire, monkeypatch, provider, base_url
):
    fake_db.users["alice"] = {
        "provider": provider, "base_url": base_url, "model": "some-model",
        "api_key_enc": store._encrypt_key("synthetic-remote-key"),
    }
    binding = _bind_audit(store, monkeypatch)
    capture = store.capture_user_sync("alice")
    with pytest.raises(ResearchProfileUnavailable, match="^research_profile_unavailable$"):
        select_config(capture, store=store, binding_key=binding,
                      profile=profile.LOCAL_PROFILE)
    # Allowlisting a DIFFERENT origin (other port) does not admit this one.
    monkeypatch.setenv("RESEARCH_LOCAL_ENDPOINT_ALLOWLIST", "http://ollama.internal:11435")
    with pytest.raises(ResearchProfileUnavailable):
        select_config(capture, store=store, binding_key=binding,
                      profile=profile.LOCAL_PROFILE,
                      local_allowlist=("http://ollama.internal:11436",))
    if provider != "openai" or base_url != profile.BASE_URL:
        with pytest.raises(ResearchProfileUnavailable):
            select_config(capture, store=store, binding_key=binding)
    assert wire.requests == []
    frame = local_inference_frame(store.get_sync("alice"))
    if provider in profile.LOCAL_PROVIDERS:
        # A local-runtime provider on a remote endpoint is remote everywhere.
        assert not frame.local and frame.audit_base_url == base_url
    else:
        # A hosted-vendor provider is refused by the profile even when its
        # endpoint is lexically local; the frame is lexical only.
        assert frame.model == "some-model" and not frame.keyless


def test_local_profile_admits_exactly_allowlisted_origin(store, fake_db, wire, monkeypatch):
    store.set_sync("alice", provider="custom", base_url="http://ollama.internal:11434/v1",
                   model="local-selector-q4", api_key="")
    binding = _bind_audit(store, monkeypatch)
    capture = store.capture_user_sync("alice")
    with pytest.raises(ResearchProfileUnavailable):
        select_config(capture, store=store, binding_key=binding, profile=profile.LOCAL_PROFILE)
    selection = select_config(capture, store=store, binding_key=binding,
                              profile=profile.LOCAL_PROFILE,
                              local_allowlist=("http://ollama.internal:11434",))
    assert selection.local
    monkeypatch.setenv("RESEARCH_LOCAL_ENDPOINT_ALLOWLIST", " http://ollama.internal:11434 ,")
    assert select_config(capture, store=store, binding_key=binding,
                         profile=profile.LOCAL_PROFILE).revision == selection.revision
    frame = local_inference_frame(store.get_sync("alice"))
    assert frame.local and frame.endpoint_class == "allowlisted" and frame.keyless
    assert frame.audit_base_url == "http://<allowlisted>"
    assert "ollama.internal" not in frame.audit_base_url
    assert wire.requests == []


def _observation():
    from persistent_agents.research_result import retain_page_observation

    return retain_page_observation(
        {
            "version": 1,
            "requested_url": "https://example.org/releases",
            "final_url": "https://example.org/releases",
            "retrieved_at": "2026-09-12T12:00:00+00:00",
            "media_type": "text/plain",
            "extraction_profile": "plain_text_v1",
            "title": "Releases",
            "text": "Version 088 adds bounded background research. " * 30,
            "body_complete": True,
            "extraction_complete": True,
            "excerpt_complete": True,
            "redacted": False,
        },
        source_action_id="d2f39b41-b686-4b8f-92e0-70d0d743d6e0",
        redacted=False,
    )


def _reply(model, *, usage):
    document = {
        "id": "chatcmpl-local",
        "object": "chat.completion",
        "created": 1789214400,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "logprobs": None,
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"version": 1, "passage_ids": ["p001"]}),
                },
            }
        ],
        "usage": None
        if usage is None
        else dict(zip(("prompt_tokens", "completion_tokens", "total_tokens"), usage)),
    }
    return json.dumps(document).encode("utf-8")
