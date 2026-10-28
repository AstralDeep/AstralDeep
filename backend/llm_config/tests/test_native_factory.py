"""Checks native selection from encrypted owner records and revalidation at physical delivery boundaries. The provider wire is synthetic and remains offline."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from llm_config.tests.native_fixtures import native_offline as native_offline

from llm_config.client_factory import build_llm_client, create_completion
from llm_config.types import CredentialSource, LLMUnavailable
from llm_config.tests.test_native_wire import MODEL, response


@pytest.fixture
def enabled(monkeypatch, credential_plane):
    from shared.feature_flags import flags
    credential_plane.repositories.history = SimpleNamespace(sessions=SimpleNamespace(
        bound_request_execution_waits=lambda _: None))
    monkeypatch.setitem(flags._flags, "anthropic_native", True)

    def refuse(*_, **__):
        raise RuntimeError("unexpected_compat_transport")

    monkeypatch.setattr("httpx.Client.send", refuse)


async def selected(store, owner="alice"):
    await store.set(owner, provider="anthropic-native", base_url="https://api.anthropic.com/v1", model=MODEL, api_key="fixture-" + owner)
    return await store.get(owner)


def test_default_off_does_not_reinterpret_native_looking_compat_config(monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "anthropic_native", False)
    config = SimpleNamespace(provider="anthropic", base_url="https://api.anthropic.com/v1", model=MODEL, api_key="compat-owner")
    with patch("llm_config.client_factory.OpenAI") as factory:
        _, _, resolved = build_llm_client(config, CredentialSource.USER)
    assert factory.call_args.kwargs == {"base_url": config.base_url, "max_retries": 0, "timeout": 60.0, "api_key": "compat-owner"}
    assert resolved.base_url == config.base_url
    config.provider = "anthropic-native"
    with pytest.raises(LLMUnavailable):
        build_llm_client(config, CredentialSource.USER)


async def test_native_factory_requires_encrypted_record_binding(store, enabled):
    config = await selected(store)
    client, source, resolved = build_llm_client(config, CredentialSource.USER)
    assert source == CredentialSource.USER and resolved.owner_id == "alice"
    assert resolved.provider == "anthropic-native" and resolved.profile == "anthropic-messages-text-tools/v1"
    assert len(resolved.config_revision) == 64
    assert "fixture-alice" not in repr(client) + repr(config) + repr(resolved)
    unbound = SimpleNamespace(provider=config.provider, base_url=config.base_url, model=config.model, api_key="ambient")
    with pytest.raises(LLMUnavailable):
        build_llm_client(unbound, CredentialSource.USER)
    with pytest.raises(LLMUnavailable):
        build_llm_client(config, CredentialSource.SYSTEM)


async def test_owner_selected_keys_and_usage_flow_through_one_factory_wire(store, enabled, monkeypatch):
    from shared import external_http
    captured = []

    async def wire(url, *, body, headers, **_):
        captured.append((url, json.loads(body), headers))
        yield json.dumps(response(content=[{"type": "text", "text": "bounded response"}], stop_reason="end_turn")).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://private")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-private: ambient")
    for owner in ("alice", "bob"):
        client, _, _ = build_llm_client(await selected(store, owner), CredentialSource.USER)
        result = await create_completion(client, model=MODEL, messages=[{"role": "user", "content": "question"}], max_tokens=80)
        assert result.usage.total_tokens == 63
    assert [value[2]["x-api-key"] for value in captured] == ["fixture-alice", "fixture-bob"]
    assert all(value[0] == "https://api.anthropic.com/v1/messages" for value in captured)
    assert all(set(value[2]) == {"x-api-key", "anthropic-version", "content-type"} for value in captured)


async def test_native_configuration_change_before_send_prevents_physical_request(store, credential_plane, enabled, monkeypatch):
    from shared import external_http
    calls = []

    async def wire(*args, **kwargs):
        calls.append(1)
        yield b"{}"

    monkeypatch.setattr(external_http, "async_post", wire)
    client, _, _ = build_llm_client(await selected(store), CredentialSource.USER)
    credential_plane.users["alice"]["api_key_enc"] = "changed-encrypted-row"
    with pytest.raises(LLMUnavailable):
        await create_completion(client, model=MODEL, messages=[{"role": "user", "content": "question"}])
    assert calls == []


async def test_native_revocation_while_provider_runs_denies_result(store, credential_plane, enabled, monkeypatch):
    from shared import external_http

    async def wire(*args, **kwargs):
        credential_plane.users.pop("alice")
        yield json.dumps(response(content=[{"type": "text", "text": "must not deliver"}], stop_reason="end_turn")).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    client, _, _ = build_llm_client(await selected(store), CredentialSource.USER)
    with pytest.raises(LLMUnavailable):
        await create_completion(client, model=MODEL, messages=[{"role": "user", "content": "question"}])


async def test_native_catalog_is_explicit_and_rolls_back_without_changing_compat(monkeypatch, enabled):
    from llm_config.providers import all_presets, get_preset, resolve_base_url
    from shared.feature_flags import flags
    assert get_preset("anthropic-native").base_url == "https://api.anthropic.com/v1"
    assert resolve_base_url("anthropic-native", "http://private") == "https://api.anthropic.com/v1"
    assert any(p.key == "anthropic-native" for p in all_presets())
    monkeypatch.setitem(flags._flags, "anthropic_native", False)
    assert get_preset("anthropic-native") is None
    assert all(p.key != "anthropic-native" for p in all_presets())
    assert get_preset("anthropic").key == "anthropic"


async def test_native_saved_check_uses_encrypted_configuration_without_compat_probe(store, enabled, fake_recorder, safe_send, monkeypatch):
    from llm_config import ws_handlers
    from shared import external_http
    calls = []

    async def wire(*_, **kwargs):
        calls.append(json.loads(kwargs["body"]))
        yield json.dumps(response(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn",
            usage={"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    monkeypatch.setattr(ws_handlers, "probe_chat_completion", AsyncMock(return_value=(False, "other", None)))
    assert await ws_handlers.handle_llm_config_set(safe_send=safe_send, websocket=object(),
        config={"provider": "anthropic-native", "model": MODEL, "api_key": "fixture-alice"},
        actor_user_id="alice", auth_principal="alice", store=store, recorder=fake_recorder)
    assert len(calls) == 1 and calls[0]["max_tokens"] == 1
    assert [call.args[0].inputs_meta["action"] for call in fake_recorder.record.await_args_list] == ["created", "tested"]
    assert fake_recorder.record.await_args_list[-1].args[0].outcome == "success"


async def test_native_sync_facade_refuses_unported_callers_explicitly(store, enabled):
    from llm_config.native_wire import NativeContractError
    client, _, _ = build_llm_client(await selected(store), CredentialSource.USER)
    for kwargs in ({}, {"stream": True}):
        with pytest.raises(NativeContractError, match="native_async"):
            client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "question"}], **kwargs)
    with pytest.raises(NativeContractError):
        await client.create_async(model=MODEL, messages=[{"role": "user", "content": "question"}], stream=True)


@pytest.mark.parametrize("mutate", [{"provider": "other"}, {"base_url": "http://private"}, {"model": "other"}, {"api_key": "other"}])
async def test_mutating_configuration_fields_cannot_rebind_the_sealed_credential(store, enabled, mutate):
    config = await selected(store)
    for key, value in mutate.items():
        setattr(config, key, value)
    with pytest.raises(LLMUnavailable):
        build_llm_client(config, CredentialSource.USER)


async def test_user_record_cannot_be_attached_to_another_owner(store, enabled):
    from llm_config.native_provider import bind_record
    config = await selected(store)
    record = store._repository.repository.get_user(None, owner_id="alice")
    with pytest.raises(LLMUnavailable):
        bind_record(store, record, scope="user", owner_id="bob", api_key=config.api_key)


async def test_concurrent_owner_revocation_does_not_affect_other_owner(store, credential_plane, enabled, monkeypatch):
    import asyncio
    from shared import external_http
    arrived, count = asyncio.Event(), []

    async def wire(*_, **kwargs):
        count.append(kwargs["headers"]["x-api-key"])
        if len(count) == 2:
            arrived.set()
        await arrived.wait()
        if kwargs["headers"]["x-api-key"] == "fixture-bob":
            credential_plane.users.pop("bob")
        yield json.dumps(response(content=[{"type": "text", "text": "answer"}], stop_reason="end_turn")).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    alice, _, _ = build_llm_client(await selected(store, "alice"), CredentialSource.USER)
    bob, _, _ = build_llm_client(await selected(store, "bob"), CredentialSource.USER)
    values = await asyncio.gather(*(create_completion(client, model=MODEL, messages=[{"role": "user", "content": "question"}])
                                   for client in (alice, bob)), return_exceptions=True)
    assert values[0].choices[0].message.content == "answer"
    assert isinstance(values[1], LLMUnavailable) and values[1].usage.total_tokens == 63
    assert sorted(count) == ["fixture-alice", "fixture-bob"]


async def test_intentionally_one_token_saved_probe_accepts_valid_truncation_only_as_connectivity(store, enabled, monkeypatch):
    from llm_config.probe import probe_native_saved
    from shared import external_http
    calls = []

    async def wire(*_, **kwargs):
        calls.append(json.loads(kwargs["body"]))
        yield json.dumps(response(content=[{"type": "text", "text": "Hello"}], stop_reason="max_tokens",
            usage={"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    assert await probe_native_saved(await selected(store)) == (True, None, None)
    assert len(calls) == 1 and calls[0]["max_tokens"] == 1


async def test_saved_probe_cannot_accept_truncation_after_configuration_revocation(store, credential_plane, enabled, monkeypatch):
    from llm_config.probe import probe_native_saved
    from shared import external_http

    async def wire(*_, **__):
        credential_plane.users.pop("alice")
        yield json.dumps(response(content=[{"type": "text", "text": "Hello"}], stop_reason="max_tokens",
            usage={"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    result = await probe_native_saved(await selected(store))
    assert result[0] is False


async def test_native_system_record_is_explicit_and_revalidated_separately(store, enabled, monkeypatch):
    from shared import external_http

    async def wire(*_, **kwargs):
        assert kwargs["headers"]["x-api-key"] == "fixture-system"
        yield json.dumps(response(content=[{"type": "text", "text": "system answer"}], stop_reason="end_turn")).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    await store.set_system(provider="anthropic-native", base_url="https://api.anthropic.com/v1", model=MODEL,
                           api_key="fixture-system", updated_by="admin")
    config = await store.get_system()
    client, source, resolved = build_llm_client(config, CredentialSource.SYSTEM)
    assert resolved.owner_id is None and source == CredentialSource.SYSTEM
    result = await create_completion(client, model=MODEL, messages=[{"role": "user", "content": "system task"}])
    assert result.choices[0].message.content == "system answer"
    with pytest.raises(LLMUnavailable):
        build_llm_client(config, CredentialSource.USER)


async def test_fenced_native_save_produces_a_current_encrypted_binding(store, enabled):
    import time
    from datetime import UTC, datetime, timedelta
    from llm_config.tests.test_operation_status_060 import _accepted_claim, _coordinator

    coordinator = _coordinator()
    owner, _, claim = _accepted_claim(coordinator)
    committed = store.set_fenced_sync(owner.owner_user_id, provider="anthropic-native",
        base_url="https://api.anthropic.com/v1", model=MODEL, api_key="fixture-fenced",
        coordinator=coordinator, fence=claim.fence, deadline_at_monotonic=time.monotonic() + 10,
        deadline_at_utc=datetime.now(UTC) + timedelta(seconds=10))
    client, _, resolved = build_llm_client(committed.config, CredentialSource.USER)
    assert resolved.owner_id == owner.owner_user_id
    await client.binding.validate()


@pytest.mark.parametrize("status,category", [(401, "auth_failed"), (403, "auth_failed"), (404, "model_not_found"),
    (429, "provider_unavailable"), (500, "provider_unavailable"), (400, "contract_violation")])
async def test_native_saved_check_reports_typed_failure_without_private_upstream_text(store, enabled, monkeypatch, status, category):
    from llm_config.probe import probe_native_saved
    from shared import external_http

    async def wire(*_, **__):
        error = external_http.ExternalHttpError("safe_status")
        error.status_code = status
        raise error
        yield b""

    monkeypatch.setattr(external_http, "async_post", wire)
    ok, error, summary = await probe_native_saved(await selected(store))
    assert ok is False and error == category
    assert "fixture" not in summary


async def test_native_prospective_probe_refuses_plain_credentials_until_saved(fake_recorder):
    from llm_config import api
    body = api.TestConnectionRequest(provider="anthropic-native", base_url="https://api.anthropic.com/v1",
                                    model=MODEL, api_key="fixture-unsaved")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(orchestrator=SimpleNamespace(audit_recorder=fake_recorder))))
    result = await api.test_connection(body, request, user_id="native-prospective-owner", user_payload={"sub": "native-prospective-owner"})
    assert result.ok is False and result.upstream_message == "native_saved_configuration_required"
    assert fake_recorder.record.await_args.args[0].outcome == "failure"


@pytest.mark.parametrize("changes", [{"base_url": "http://private"}, {"api_key": ""}, {"api_key": "invalid\nheader"}, {"model": "m" * 257}])
async def test_invalid_native_selection_cannot_mutate_encrypted_configuration(store, credential_plane, enabled, changes):
    config = await selected(store)
    before = dict(credential_plane.users["alice"])
    values = dict(provider="anthropic-native", base_url=config.base_url, model=MODEL, api_key="fixture-alice")
    values.update(changes)
    with pytest.raises(ValueError):
        await store.set("alice", **values)
    assert credential_plane.users["alice"] == before


async def test_native_user_settings_test_retains_selected_profile_and_saved_record(store, enabled, fake_recorder, monkeypatch):
    from orchestrator.projection_surfaces import llm
    from shared import external_http
    calls = []
    await selected(store)

    async def wire(*_, **kwargs):
        calls.append(kwargs)
        yield json.dumps(response(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn",
            usage={"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    websocket = object()
    orch = SimpleNamespace(_llm_store=store, audit_recorder=fake_recorder, ui_sessions={websocket: {"sub": "alice"}})
    _, _, notice = await llm._handle_test(orch, websocket, "alice", ["user"],
        {"fields": {"provider": "anthropic-native", "model": MODEL, "api_key": ""}})
    assert "Connection OK" in notice and len(calls) == 1


@pytest.mark.parametrize("failure", [False, True])
async def test_native_system_settings_encrypt_before_any_probe_and_report_the_committed_state(store, enabled, fake_recorder, monkeypatch, failure):
    from orchestrator.projection_surfaces import llm_system
    from shared import external_http
    calls = []

    async def wire(*_, **kwargs):
        saved = await store.get_system()
        assert saved is not None and saved.provider == "anthropic-native"
        calls.append(kwargs)
        if failure:
            error = external_http.ExternalHttpError("safe_unavailable")
            error.status_code = 500
            raise error
        yield json.dumps(response(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn",
            usage={"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    monkeypatch.setattr(llm_system, "probe_chat_completion", AsyncMock(side_effect=AssertionError("unexpected_compat_probe")))
    websocket = object()
    orch = SimpleNamespace(_llm_store=store, audit_recorder=fake_recorder, ui_sessions={websocket: {"sub": "admin"}})
    _, _, notice = await llm_system._handle_save(orch, websocket, "admin", ["admin"],
        {"fields": {"provider": "anthropic-native", "model": MODEL, "api_key": "fixture-system"}})
    assert len(calls) == 1 and "saved" in notice.lower() and "fixture-system" not in notice
    assert (await store.get_system()).provider == "anthropic-native"
    if failure:
        assert "connection check failed" in notice and "nothing saved" not in notice


@pytest.mark.parametrize("system", [False, True])
async def test_native_model_discovery_is_explicitly_unsupported_without_compatibility_probe(store, enabled, fake_recorder, monkeypatch, system):
    from llm_config import api
    from orchestrator.projection_surfaces import llm, llm_system
    monkeypatch.setattr(api, "list_models", AsyncMock(side_effect=AssertionError("unexpected_compat_models")))
    websocket = object()
    orch = SimpleNamespace(_llm_store=store, audit_recorder=fake_recorder, ui_sessions={websocket: {"sub": "alice"}})
    surface = llm_system if system else llm
    _, _, notice = await surface._handle_models(orch, websocket, "alice", ["admin" if system else "user"],
        {"fields": {"provider": "anthropic-native", "model": MODEL, "api_key": "fixture-alice"}})
    assert "model id" in notice.lower() and "not supported" in notice.lower()


@pytest.mark.parametrize("saved", [False, True])
async def test_native_system_test_never_sends_unsaved_plaintext_and_uses_saved_scope(store, enabled, fake_recorder, monkeypatch, saved):
    from orchestrator.projection_surfaces import llm_system
    from shared import external_http
    calls = []
    if saved:
        await store.set_system(provider="anthropic-native", base_url="https://api.anthropic.com/v1", model=MODEL,
                               api_key="fixture-system", updated_by="admin")

    async def wire(*_, **kwargs):
        calls.append(kwargs)
        yield json.dumps(response(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn",
            usage={"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    websocket = object()
    orch = SimpleNamespace(_llm_store=store, audit_recorder=fake_recorder, ui_sessions={websocket: {"sub": "admin"}})
    _, _, notice = await llm_system._handle_test(orch, websocket, "admin", ["admin"],
        {"fields": {"provider": "anthropic-native", "model": MODEL, "api_key": "fixture-system"}})
    if saved:
        assert len(calls) == 1 and "Connection OK" in notice
    else:
        assert not calls and "Save the native system configuration" in notice


async def test_native_profile_rollback_invalidates_an_already_created_client(store, enabled, monkeypatch):
    from shared import external_http
    from shared.feature_flags import flags
    calls = []

    async def wire(*_, **__):
        calls.append(1)
        yield json.dumps(response(content=[{"type": "text", "text": "not authorized"}], stop_reason="end_turn")).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    client, _, _ = build_llm_client(await selected(store), CredentialSource.USER)
    monkeypatch.setitem(flags._flags, "anthropic_native", False)
    with pytest.raises(LLMUnavailable):
        await create_completion(client, model=MODEL, messages=[{"role": "user", "content": "question"}])
    assert calls == []


def test_native_profile_flag_defaults_off_without_configuration(monkeypatch):
    from shared.feature_flags import FeatureFlags
    monkeypatch.delenv("FF_ANTHROPIC_NATIVE", raising=False)
    assert not FeatureFlags().is_enabled("anthropic_native")
