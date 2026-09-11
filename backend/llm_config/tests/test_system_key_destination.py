"""System-key destination and admin refusals through the real chrome dispatcher.

The existing typed encrypted-store fixture is isolated and all provider calls
are replaced at their network boundary; no application database or IAM is used.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import chrome_events
from orchestrator.projection_surfaces import llm_system

SECRET = "synthetic-system-destination-key"
OLD_URL = "https://old.example/v1"
NEW_URL = "https://new.example/v1"
EXPECTED = (
    "The endpoint changed; enter the API key again. "
    "For a keyless endpoint, clear the saved configuration first."
)
ACTIONS = ("chrome_llm_sys_models", "chrome_llm_sys_test", "chrome_llm_sys_save")


@pytest.fixture
def system_ui(store, fake_recorder):
    ws = object()
    return SimpleNamespace(
        ws=ws, _llm_store=store, audit_recorder=fake_recorder,
        _safe_send=AsyncMock(return_value=True), _ws_llm_gated={},
        _ff_llm_first_run=False,
        ui_sessions={ws: {"sub": "admin", "realm_access": {"roles": ["admin", "user"]}}},
    )


async def seed(store, *, provider="custom", base_url=OLD_URL, api_key=SECRET):
    await store.set_system(
        provider=provider, base_url=base_url, model="old-model",
        api_key=api_key, updated_by="admin",
    )


async def dispatch(orch, action, *, provider="custom", base_url=NEW_URL, api_key=""):
    assert await chrome_events.handle_chrome_event(
        orch, orch.ws, action,
        {"fields": {"provider": provider, "base_url": base_url,
                    "model": "new-model", "api_key": api_key}}, "admin",
    )
    frames = [json.loads(call.args[1]) for call in orch._safe_send.await_args_list]
    return frames[-1]["html"]


def forbid_probes(monkeypatch):
    probe = AsyncMock(side_effect=AssertionError("provider must not be contacted"))
    monkeypatch.setattr("llm_config.api.list_models", probe)
    monkeypatch.setattr("llm_config.api.test_connection", probe)
    monkeypatch.setattr(llm_system, "probe_chat_completion", probe)
    return probe


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("destination", [NEW_URL, "https://old.example/v2", "http://old.example/v1"])
async def test_admin_changed_endpoint_refuses_before_probe_or_write(
    system_ui, store, fake_db, monkeypatch, action, destination,
):
    await seed(store)
    before = dict(fake_db.system)
    probe = forbid_probes(monkeypatch)

    html = await dispatch(system_ui, action, base_url=destination)

    assert EXPECTED in html
    assert SECRET not in html
    assert fake_db.system == before
    probe.assert_not_awaited()
    system_ui.audit_recorder.record.assert_not_awaited()


@pytest.mark.parametrize("action", ACTIONS)
async def test_non_admin_cannot_reuse_or_probe_system_key(
    system_ui, store, fake_db, fake_recorder, monkeypatch, action,
):
    await seed(store)
    before = dict(fake_db.system)
    probe = forbid_probes(monkeypatch)
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: fake_recorder)
    system_ui.ui_sessions[system_ui.ws]["realm_access"]["roles"] = ["user"]

    html = await dispatch(system_ui, action, base_url=OLD_URL)

    assert "admin role" in html and SECRET not in html
    assert fake_db.system == before
    probe.assert_not_awaited()
    event = fake_recorder.record.await_args.args[0]
    assert event.action_type == "settings.admin_denied"


@pytest.mark.parametrize("action", ACTIONS)
async def test_admin_same_endpoint_reuses_saved_key(
    system_ui, store, monkeypatch, action,
):
    await seed(store)
    calls = []

    async def api_probe(*, body, **kwargs):
        calls.append((body.base_url, body.api_key))
        return SimpleNamespace(ok=True, models=["new-model"], model="new-model", latency_ms=1)

    async def save_probe(*, api_key, base_url, **kwargs):
        calls.append((base_url, api_key))
        return True, None, None

    monkeypatch.setattr("llm_config.api.list_models", api_probe)
    monkeypatch.setattr("llm_config.api.test_connection", api_probe)
    monkeypatch.setattr(llm_system, "probe_chat_completion", save_probe)

    html = await dispatch(system_ui, action, base_url="  " + OLD_URL + "/  ")

    assert calls == [(OLD_URL, SECRET)]
    assert EXPECTED not in html and SECRET not in html
    assert (await store.get_system()).api_key == SECRET


async def test_admin_preset_ignores_submitted_destination(system_ui, store, monkeypatch):
    await seed(store, provider="openai", base_url="https://api.openai.com/v1")
    probe = AsyncMock(return_value=SimpleNamespace(ok=True, models=["m"]))
    monkeypatch.setattr("llm_config.api.list_models", probe)

    await dispatch(system_ui, "chrome_llm_sys_models", provider="openai", base_url=NEW_URL)

    body = probe.await_args.kwargs["body"]
    assert body.base_url == "https://api.openai.com/v1" and body.api_key == SECRET


async def test_admin_explicit_key_replaces_destination_and_preserves_audit(
    system_ui, store, fake_recorder, monkeypatch,
):
    await seed(store)
    replacement = "synthetic-replacement-key"
    probe = AsyncMock(return_value=(True, None, None))
    monkeypatch.setattr(llm_system, "probe_chat_completion", probe)

    html = await dispatch(system_ui, "chrome_llm_sys_save", api_key=replacement)

    saved = await store.get_system()
    assert (saved.base_url, saved.api_key) == (NEW_URL, replacement)
    assert probe.await_args.kwargs["api_key"] == replacement
    assert "System LLM credential saved" in html
    assert replacement not in html and SECRET not in html
    events = [call.args[0] for call in fake_recorder.record.await_args_list]
    assert [event.action_type for event in events] == ["llm_config.tested", "llm_config.updated"]
    assert all(event.inputs_meta["scope"] == "system" for event in events)
    assert replacement not in json.dumps([event.inputs_meta for event in events])


@pytest.mark.parametrize("clear_saved_key", [False, True])
async def test_admin_keyless_destination_after_clear_or_prior_keyless_config(
    system_ui, store, monkeypatch, clear_saved_key,
):
    await seed(store, api_key=SECRET if clear_saved_key else "")
    if clear_saved_key:
        await dispatch(system_ui, "chrome_llm_sys_clear")
        assert await store.get_system() is None
    probe = AsyncMock(return_value=(True, None, None))
    monkeypatch.setattr(llm_system, "probe_chat_completion", probe)

    html = await dispatch(system_ui, "chrome_llm_sys_save")

    saved = await store.get_system()
    assert (saved.base_url, saved.api_key) == (NEW_URL, "")
    assert probe.await_args.kwargs["api_key"] == ""
    assert "System LLM credential saved" in html and SECRET not in html
