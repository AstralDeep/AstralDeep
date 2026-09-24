"""Tests for ws_handlers.py's llm_config_set/clear handlers: field validation,
server-derived base_url for catalog presets, probe-gated save, audit emission, and
the retired register_ui accept-and-ignore path.
"""

from __future__ import annotations

import json

import pytest

from llm_config.ws_handlers import (
    handle_llm_config_clear,
    handle_llm_config_set,
    populate_from_register_ui,
    validate_config_submission,
)

USER = "user-alice"
KEY = "sk-realkey-abcdef1234567890abcd"


@pytest.fixture
def probe_calls(monkeypatch):
    calls = []

    async def _probe(**kwargs):
        calls.append(kwargs)
        return (True, None, None)

    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", _probe)
    return calls


@pytest.fixture
def probe_fails(monkeypatch):
    calls = []

    async def _probe(**kwargs):
        calls.append(kwargs)
        return (False, "auth_failed", "Incorrect API key provided (HTTP 401)")

    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", _probe)
    return calls


def _events(rec):
    return [c.args[0] for c in rec.record.await_args_list]


def _sent(safe_send):
    return [json.loads(c.args[1]) for c in safe_send.await_args_list]


async def _set(store, recorder, safe_send, config, user=USER):
    return await handle_llm_config_set(
        safe_send=safe_send,
        websocket=object(),
        config=config,
        actor_user_id=user,
        auth_principal=user,
        store=store,
        recorder=recorder,
    )


@pytest.mark.parametrize("config,expected_field", [
    ({"provider": "openai", "api_key": KEY, "model": ""}, "model"),
    ({"provider": "openai", "api_key": "", "model": "gpt-4o-mini"}, "api_key"),
    ({"provider": "martian-ai", "api_key": KEY, "model": "m"}, "provider"),
    ({"provider": "custom", "api_key": KEY, "model": "m", "base_url": ""},
     "base_url"),
    ({"provider": "custom", "api_key": KEY, "model": "m",
      "base_url": "ftp://x/v1"}, "base_url"),
    ("not-a-dict", "config"),
])
async def test_validation_error_stores_nothing(
        store, fake_db, fake_recorder, safe_send, probe_calls,
        config, expected_field):
    result = await _set(store, fake_recorder, safe_send, config)
    assert result is False
    assert store.get_sync(USER) is None
    assert fake_db.users == {}
    assert probe_calls == []
    assert _events(fake_recorder) == []
    sent = _sent(safe_send)
    assert len(sent) == 1
    assert sent[0]["type"] == "error"
    assert sent[0]["code"] == "llm_config_invalid"
    assert expected_field in sent[0]["fields"]


def test_validate_config_submission_reports_all_missing_fields():
    fields, errors = validate_config_submission(
        {"provider": "custom", "api_key": "", "model": "", "base_url": ""})
    assert set(errors) == {"base_url", "model"}


def test_validate_config_submission_still_requires_key_for_keyed_provider():
    fields, errors = validate_config_submission(
        {"provider": "openai", "api_key": "", "model": "", "base_url": ""})
    assert "api_key" in errors


def test_validate_unknown_provider_short_circuits():
    fields, errors = validate_config_submission(
        {"provider": "nope", "api_key": "k", "model": "m"})
    assert list(errors) == ["provider"]
    assert fields == {}


def test_validate_keyless_preset_permits_empty_key():
    fields, errors = validate_config_submission(
        {"provider": "ollama", "api_key": "", "model": "llama3"})
    assert errors == {}
    assert fields["base_url"] == "http://localhost:11434/v1"


async def test_preset_base_url_ignores_submitted_url(
        store, fake_db, fake_recorder, safe_send, probe_calls):
    result = await _set(store, fake_recorder, safe_send, {
        "provider": "openai",
        "api_key": KEY,
        "model": "gpt-4o-mini",
        "base_url": "https://evil.example/v1",
    })
    assert result is True
    assert probe_calls[0]["base_url"] == "https://api.openai.com/v1"
    got = store.get_sync(USER)
    assert got.base_url == "https://api.openai.com/v1"
    assert fake_db.users[USER]["base_url"] == "https://api.openai.com/v1"


async def test_probe_failure_refuses_save(
        store, fake_db, fake_recorder, safe_send, probe_fails):
    result = await _set(store, fake_recorder, safe_send, {
        "provider": "openai", "api_key": KEY, "model": "gpt-4o-mini",
    })
    assert result is False
    assert store.get_sync(USER) is None
    assert fake_db.users == {}
    events = _events(fake_recorder)
    assert len(events) == 1
    assert events[0].inputs_meta["action"] == "tested"
    assert events[0].outputs_meta == {
        "result": "failure", "error_class": "auth_failed"}
    sent = _sent(safe_send)
    assert sent[-1]["type"] == "error"
    assert sent[-1]["code"] == "llm_config_invalid"
    assert sent[-1]["error_class"] == "auth_failed"
    assert all(m["type"] != "llm_config_ack" for m in sent)


async def test_probe_success_persists_audits_and_acks(
        store, fake_db, fake_recorder, safe_send, probe_calls):
    result = await _set(store, fake_recorder, safe_send, {
        "provider": "openai", "api_key": KEY, "model": "gpt-4o-mini",
    })
    assert result is True
    assert probe_calls == [{
        "api_key": KEY,
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    }]
    got = store.get_sync(USER)
    assert got.api_key == KEY
    assert got.model == "gpt-4o-mini"
    enc = fake_db.users[USER]["api_key_enc"]
    assert enc and KEY not in enc
    events = _events(fake_recorder)
    assert [e.inputs_meta["action"] for e in events] == ["tested", "created"]
    assert events[0].outputs_meta["result"] == "success"
    for ev in events:
        assert KEY not in ev.model_dump_json()
    assert _sent(safe_send)[-1] == {"type": "llm_config_ack", "ok": True}


async def test_second_save_audits_updated(
        store, fake_recorder, safe_send, probe_calls):
    assert await _set(store, fake_recorder, safe_send, {
        "provider": "openai", "api_key": KEY, "model": "gpt-4o-mini"})
    assert await _set(store, fake_recorder, safe_send, {
        "provider": "groq", "api_key": "gsk_newkey1234567890abcdef",
        "model": "llama-3.1-70b"})
    actions = [e.inputs_meta["action"] for e in _events(fake_recorder)]
    assert actions == ["tested", "created", "tested", "updated"]
    got = store.get_sync(USER)
    assert got.provider == "groq"
    assert got.base_url == "https://api.groq.com/openai/v1"


async def test_keyless_preset_saves_with_empty_key(
        store, fake_db, fake_recorder, safe_send, probe_calls):
    result = await _set(store, fake_recorder, safe_send, {
        "provider": "ollama", "api_key": "", "model": "llama3",
    })
    assert result is True
    assert fake_db.users[USER]["api_key_enc"] is None
    assert store.get_sync(USER).api_key == ""


async def test_clear_with_row_returns_true_and_audits(
        store, fake_recorder, safe_send):
    store.set_sync(USER, provider="openai",
                   base_url="https://api.openai.com/v1",
                   model="m", api_key=KEY)
    removed = await handle_llm_config_clear(
        safe_send=safe_send, websocket=object(),
        actor_user_id=USER, auth_principal=USER,
        store=store, recorder=fake_recorder)
    assert removed is True
    assert store.get_sync(USER) is None
    events = _events(fake_recorder)
    assert len(events) == 1
    assert events[0].inputs_meta["action"] == "cleared"
    assert "base_url" not in events[0].inputs_meta
    assert _sent(safe_send)[-1] == {"type": "llm_config_ack", "ok": True}


async def test_clear_without_row_returns_false_no_audit_still_acks(
        store, fake_recorder, safe_send):
    removed = await handle_llm_config_clear(
        safe_send=safe_send, websocket=object(),
        actor_user_id=USER, auth_principal=USER,
        store=store, recorder=fake_recorder)
    assert removed is False
    assert _events(fake_recorder) == []
    assert _sent(safe_send)[-1] == {"type": "llm_config_ack", "ok": True}


async def test_register_ui_seeding_is_accepted_and_ignored(
        store, fake_db, fake_recorder):
    result = await populate_from_register_ui(
        websocket=object(),
        llm_config={
            "api_key": KEY,
            "base_url": "https://x.example/v1",
            "model": "model-a",
        },
        actor_user_id=USER,
        auth_principal=USER,
        recorder=fake_recorder,
        store=store,
    )
    assert result is None
    assert store.get_sync(USER) is None
    assert fake_db.users == {}
    assert _events(fake_recorder) == []


async def test_register_ui_none_payload_is_noop(store, fake_recorder):
    await populate_from_register_ui(
        websocket=object(), llm_config=None,
        actor_user_id=USER, auth_principal=USER,
        recorder=fake_recorder, store=store)
    assert store.get_sync(USER) is None
    assert _events(fake_recorder) == []
