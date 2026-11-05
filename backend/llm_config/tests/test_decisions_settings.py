"""Exercises decisions-provider validation, safe saves, and shared conditional settings fields.
The tests use the real encrypted credential store and replace only external probes.
"""

import asyncio
from types import SimpleNamespace

import pytest
from orchestrator.projection_surfaces import llm

from llm_config import typesafe_handlers as handlers

USER = "decision-settings-owner"
KEY = "decision-CANARY-key"


@pytest.fixture(autouse=True)
def reset_rate():
    handlers.reset_probe_rate()
    yield
    handlers.reset_probe_rate()


def orch(store):
    return SimpleNamespace(_typesafe_store=store, _llm_store=None, ui_sessions={})


def test_native_decision_fields_follow_provider_and_never_echo_keys(typesafe_store):
    typesafe_store.save_config_sync(USER, provider="custom", base_url="https://models.example/v1",
                                   model="bespokelabs/Bespoke-Nimble-9B", api_key=KEY)
    components = asyncio.run(llm.components(orch(typesafe_store), USER, [], {}))
    fields = {f["name"]: f for c in components if c["type"] == "param_picker" for f in c["fields"]}
    assert fields["decisions_provider"]["default"] == "custom"
    assert fields["decisions_provider"]["options"] == [
        {"value": "typesafe", "label": "TypeSafe AI"}, {"value": "custom", "label": "Custom"}]
    for name in ("decisions_base_url", "decisions_model"):
        assert fields[name]["visible_when"] == {"decisions_provider": "custom"}
    assert fields["decisions_api_key"]["kind"] == "password"
    assert fields["decisions_api_key"].get("help") == "A key is saved."
    assert not fields["typesafe_api_key"].get("help")
    assert KEY not in repr(components)
    web = asyncio.run(llm.render(orch(typesafe_store), USER, [], {}))
    assert 'name="decisions_provider"' in web
    assert 'data-chrome-visible-when=' in web
    assert KEY not in web


def test_hosted_key_hint_does_not_claim_a_custom_key_is_saved(typesafe_store):
    typesafe_store.save_sync(USER, KEY)
    components = asyncio.run(llm.components(orch(typesafe_store), USER, [], {}))
    fields = {f["name"]: f for c in components if c["type"] == "param_picker" for f in c["fields"]}
    assert fields["typesafe_api_key"].get("help") == "A key is saved."
    assert not fields["decisions_api_key"].get("help")


def test_custom_save_probes_before_persisting_and_preserves_on_failure(typesafe_store, monkeypatch):
    async def probe(**kwargs):
        assert kwargs["base_url"] == "https://models.example/v1"
        assert kwargs["api_key"] == ""
    monkeypatch.setattr(handlers, "probe_custom", probe)
    fields = {"decisions_provider": "custom", "decisions_base_url": "https://models.example/v1",
              "decisions_model": "nimble", "decisions_api_key": ""}
    asyncio.run(handlers.save_decisions(typesafe_store, USER, fields))
    assert typesafe_store.get_key_sync(USER).api_key == ""

    async def fail(**kwargs):
        raise ValueError("unavailable")
    monkeypatch.setattr(handlers, "probe_custom", fail)
    fields["decisions_model"] = "other"
    with pytest.raises(handlers.TypeSafeSaveError):
        asyncio.run(handlers.save_decisions(typesafe_store, USER, fields))
    assert typesafe_store.get_key_sync(USER).model == "nimble"


def test_saved_decision_key_cannot_follow_a_changed_endpoint(typesafe_store, monkeypatch):
    typesafe_store.save_config_sync(USER, provider="custom", base_url="https://one.example/v1",
                                   model="nimble", api_key=KEY)
    async def forbidden(**kwargs):
        pytest.fail("changed destination must be rejected before probing")
    monkeypatch.setattr(handlers, "probe_custom", forbidden)
    with pytest.raises(handlers.TypeSafeSaveError, match="endpoint changed"):
        asyncio.run(handlers.save_decisions(typesafe_store, USER, {
            "decisions_provider": "custom", "decisions_base_url": "https://two.example/v1",
            "decisions_model": "nimble"}))


@pytest.mark.parametrize("fields", [
    {"decisions_provider": "unknown"},
    {"decisions_provider": "custom", "decisions_base_url": "https://name:secret@models.example",
     "decisions_model": "nimble"},
    {"decisions_provider": "custom", "decisions_base_url": "https://models.example/v1?key=secret",
     "decisions_model": "nimble"},
    {"decisions_provider": "custom", "decisions_base_url": "https://models.example/v1",
     "decisions_model": ""},
])
def test_invalid_decision_config_never_persists(typesafe_store, fields):
    with pytest.raises(handlers.TypeSafeSaveError):
        asyncio.run(handlers.save_decisions(typesafe_store, USER, fields))
    assert typesafe_store.get_key_sync(USER) is None


def test_typesafe_save_uses_its_key_and_retains_hosted_defaults(typesafe_store, monkeypatch):
    seen = []
    async def probe(key):
        seen.append(key)
    monkeypatch.setattr(handlers, "probe_key", probe)
    asyncio.run(handlers.save_decisions(typesafe_store, USER, {
        "decisions_provider": "typesafe", "typesafe_api_key": KEY,
        "decisions_api_key": "unused-custom-key", "decisions_base_url": "https://other.example"}))
    asyncio.run(handlers.save_decisions(typesafe_store, USER, {"decisions_provider": "typesafe"}))
    assert seen == [KEY, KEY]
    assert typesafe_store.get_key_sync(USER).base_url == "https://api.typesafe.ai"


def test_custom_same_endpoint_reuses_saved_key(typesafe_store, monkeypatch):
    typesafe_store.save_config_sync(USER, provider="custom", base_url="https://one.example/v1",
                                   model="nimble", api_key=KEY)
    seen = []
    async def probe(**kwargs):
        seen.append(kwargs["api_key"])
    monkeypatch.setattr(handlers, "probe_custom", probe)
    asyncio.run(handlers.save_decisions(typesafe_store, USER, {
        "decisions_provider": "custom", "decisions_base_url": "https://one.example/v1/",
        "decisions_model": "new-model"}))
    assert seen == [KEY]
    assert typesafe_store.get_key_sync(USER).model == "new-model"


def test_decisions_handler_saves_for_current_owner_and_reports_probe_failure(typesafe_store, monkeypatch):
    async def probe(**kwargs):
        return None
    monkeypatch.setattr(handlers, "probe_custom", probe)
    payload = {"fields": {"decisions_provider": "custom", "decisions_base_url": "https://models.example/v1",
                          "decisions_model": "nimble"}}
    result = asyncio.run(llm._handle_typesafe_save(orch(typesafe_store), object(), USER, [], payload))
    assert "Decisions model saved" in result[2]
    assert typesafe_store.get_key_sync(USER).model == "nimble"
    payload["fields"]["decisions_model"] = ""
    result = asyncio.run(llm._handle_typesafe_save(orch(typesafe_store), object(), USER, [], payload))
    assert "valid base URL" in result[2]
    assert typesafe_store.get_key_sync(USER).model == "nimble"
    cleared = asyncio.run(llm._handle_typesafe_clear(orch(typesafe_store), object(), USER, [], {}))
    assert "Decisions model removed" in cleared[2]
    assert typesafe_store.get_key_sync(USER) is None


@pytest.mark.parametrize("config", [
    {"provider": "invalid", "base_url": "https://models.example", "model": "nimble", "api_key": ""},
    {"provider": "custom", "base_url": "https://models.example", "model": "nimble", "api_key": "bad key"},
])
def test_invalid_custom_values_are_refused_at_storage_boundary(typesafe_store, config):
    with pytest.raises(ValueError):
        typesafe_store.save_config_sync(USER, **config)
    assert typesafe_store.get_key_sync(USER) is None
