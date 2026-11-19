"""Tests for the admin-managed system LLM credential in
orchestrator/projection_surfaces/llm_system.py: admin-only save/clear with scoped
audit rows, the web-only menu carve-out, and the system and user credentials never
resolving each other.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator import chrome_events  # noqa: E402

SECRET = ""


@pytest.fixture(autouse=True)
def _credential_values(monkeypatch):
    monkeypatch.setitem(globals(), "SECRET", secrets.token_urlsafe(32))
class FakeRecorder:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


def _uid() -> str:
    return f"sys054-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def orch_module(orchestrator_module_factory):
    return orchestrator_module_factory()


@pytest.fixture
def orch(orch_module):
    o = orch_module
    o.ui_sessions = {}
    o._ws_llm_gated = {}
    o._ws_active_chat = {}
    o._ws_welcome = {}
    o._ff_llm_first_run = True
    sent = []

    async def _capture(ws, data):
        sent.append((ws, data))
        return True

    o._safe_send = _capture
    o.sent = sent
    o.send_ui_render = AsyncMock()
    o._record_llm_unconfigured = AsyncMock()
    o.audit_recorder = FakeRecorder()
    return o


@pytest.fixture
def clean_system_row(orch):
    store = orch._llm_store
    saved = store.get_system_sync()
    store.clear_system_sync()
    yield
    store.clear_system_sync()
    if saved:
        store.set_system_sync(
            provider=saved.provider,
            base_url=saved.base_url,
            model=saved.model,
            api_key=saved.api_key,
            updated_by="pytest-restore",
        )


def _register(orch, uid, device="browser", roles=("user",)):
    ws = MagicMock()
    orch.ui_sessions[ws] = {
        "sub": uid,
        "preferred_username": f"{uid}@example",
        "realm_access": {"roles": list(roles)},
    }
    orch.rote.register_device(ws, {"device_type": device})
    return ws


async def _seed_user(orch, uid):
    await orch._llm_store.set(
        uid, provider="custom", base_url="https://user.example.com/v1",
        model="user-model", api_key=SECRET)


async def _seed_system(orch):
    await orch._llm_store.set_system(
        provider="custom", base_url="https://system.example.com/v1",
        model="sys-model", api_key=SECRET, updated_by="admin1")


def _frames(orch, ftype):
    out = []
    for ws, data in orch.sent:
        try:
            f = json.loads(data)
        except (TypeError, ValueError):
            continue
        if f.get("type") == ftype:
            out.append(f)
    return out


def test_llm_system_surface_registered_and_admin_only():
    from orchestrator.projection_surfaces import SURFACE_MODULES, get_surface
    from orchestrator.projection_surfaces import llm_system

    assert SURFACE_MODULES["llm_system"] == "orchestrator.projection_surfaces.llm_system"
    mod = get_surface("llm_system")
    assert mod is llm_system
    assert mod.ADMIN_ONLY is True
    assert mod.TITLE == "System LLM"
    assert set(mod.HANDLERS) == {
        "chrome_llm_sys_models", "chrome_llm_sys_test",
        "chrome_llm_sys_save", "chrome_llm_sys_clear",
    }


async def test_non_admin_sys_save_refused_server_side(
        orch, clean_system_row, monkeypatch):
    denied = FakeRecorder()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: denied)
    uid = _uid()
    ws = _register(orch, uid, roles=("user",))
    await _seed_user(orch, uid)
    try:
        handled = await chrome_events.handle_chrome_event(
            orch, ws, "chrome_llm_sys_save",
            {"fields": {"provider": "openai", "api_key": SECRET,
                        "model": "gpt-4o-mini"}},
            uid)

        assert handled is True
        renders = _frames(orch, "chrome_render")
        assert renders and "admin role" in renders[-1]["html"]
        assert any(e.action_type == "settings.admin_denied" for e in denied.events)
        assert await orch._llm_store.get_system() is None
    finally:
        await orch._llm_store.clear(uid)


async def test_non_admin_chrome_open_llm_system_refused(orch, monkeypatch):
    denied = FakeRecorder()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: denied)
    uid = _uid()
    ws = _register(orch, uid, roles=("user",))
    await _seed_user(orch, uid)
    try:
        handled = await chrome_events.handle_chrome_event(
            orch, ws, "chrome_open", {"surface": "llm_system"}, uid)
        assert handled is True
        renders = _frames(orch, "chrome_render")
        assert renders and "Not authorized" in renders[-1]["html"]
        assert any(e.action_type == "settings.admin_denied" for e in denied.events)
    finally:
        await orch._llm_store.clear(uid)


async def test_admin_save_persists_system_row_with_system_scope_audit(
        orch, clean_system_row, monkeypatch):
    from orchestrator.projection_surfaces import llm_system

    probed = {}

    async def fake_probe(*, api_key, base_url, model, **kw):
        probed.update(api_key=api_key, base_url=base_url, model=model)
        return True, None, None

    # Patches the name llm_system imported, not the source
    monkeypatch.setattr(
        "orchestrator.projection_surfaces.llm_system.probe_chat_completion", fake_probe)
    ws = _register(orch, "admin1", roles=("admin", "user"))

    result = await llm_system._handle_save(
        orch, ws, "admin1", ["admin"],
        {"fields": {"provider": "openai", "api_key": SECRET,
                    "model": "gpt-4o-mini"}})

    surface, _params, notice = result
    assert surface == "llm_system"
    assert "System LLM credential saved" in notice
    assert probed == {"api_key": SECRET,
                      "base_url": "https://api.openai.com/v1",
                      "model": "gpt-4o-mini"}
    cfg = await orch._llm_store.get_system()
    assert cfg is not None
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.api_key == SECRET
    actions = [e.action_type for e in orch.audit_recorder.events]
    assert actions == ["llm_config.tested", "llm_config.created"]
    for e in orch.audit_recorder.events:
        assert e.inputs_meta["scope"] == "system"
        assert SECRET not in json.dumps(e.inputs_meta)

    surface, _params, notice = await llm_system._handle_clear(
        orch, ws, "admin1", ["admin"], {})
    assert surface == "llm_system" and "cleared" in notice
    assert await orch._llm_store.get_system() is None
    cleared = orch.audit_recorder.events[-1]
    assert cleared.action_type == "llm_config.cleared"
    assert cleared.inputs_meta["scope"] == "system"


def test_menu_model_web_admin_sees_system_llm_natives_never_do():
    from webrender.chrome.menu_model import build_menu_model

    def _surfaces(model):
        return [i.surface for g in model.menu for i in g.items]

    web_admin = build_menu_model(["admin"], include_admin=True)
    assert "llm_system" in _surfaces(web_admin)
    item = next(i for g in web_admin.menu for i in g.items if i.surface == "llm_system")
    assert item.admin_only is True and item.label == "System LLM"

    native_admin = build_menu_model(["admin"], include_admin=False)
    assert "llm_system" not in _surfaces(native_admin)

    web_user = build_menu_model(["user"], include_admin=True)
    assert "llm_system" not in _surfaces(web_user)


async def test_user_socket_never_resolves_system_record(
        orch, clean_system_row, user_skills_disabled):
    await _seed_system(orch)
    uid = _uid()
    ws = _register(orch, uid)

    assert await orch.llm_configured_for(uid) is False
    with pytest.raises(orch._LLMUnavailable):
        await orch._resolve_llm_client_for(ws)


async def test_system_context_never_resolves_a_user_record(
        orch, clean_system_row):
    uid = _uid()
    await _seed_user(orch, uid)
    try:
        with pytest.raises(orch._LLMUnavailable):
            await orch._resolve_llm_client_for(None)

        from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
        vws = VirtualWebSocket(BackgroundTask(
            task_id="t1", chat_id="c1", user_id=uid))
        with pytest.raises(orch._LLMUnavailable):
            await orch._resolve_llm_client_for(vws)
    finally:
        await orch._llm_store.clear(uid)


async def test_render_unconfigured_custom_vs_preset(orch, clean_system_row):
    from orchestrator.projection_surfaces import llm_system

    html = await llm_system.render(orch, "admin1", ["admin"], {"provider": "openai"})
    assert "set automatically" in html.lower()
    assert "astral-llm-provider" in html and "data-llm-endpoints" in html
    assert 'astral-llm-endpoint-custom' in html and 'style="display:none"' in html
    assert "not configured" in html
    assert "chrome_llm_sys_save" in html
    assert "Clear system credential" not in html

    html = await llm_system.render(orch, "admin1", ["admin"],
                                   {"provider": "custom", "base_url": "https://x.example/v1"})
    assert 'name="base_url"' in html and "https://x.example/v1" in html
    assert 'astral-llm-endpoint-preset text-xs text-astral-muted" style="display:none"' in html


async def test_render_configured_shows_badge_and_never_the_key(
        orch, clean_system_row):
    from orchestrator.projection_surfaces import llm_system

    await _seed_system(orch)
    html = await llm_system.render(orch, "admin1", ["admin"], {})
    assert "configured" in html
    assert "Clear system credential" in html
    assert "leave blank to keep" in html.lower()
    assert SECRET not in html
    html = await llm_system.render(orch, "admin1", ["admin"],
                                   {"models": ["m1", "m2"]})
    assert "<select" in html and "m1" in html and "m2" in html


async def test_sys_models_validation_and_success(orch, clean_system_row, monkeypatch):
    from orchestrator.projection_surfaces import llm_system

    surface, keep, notice = await llm_system._handle_models(
        orch, None, "admin1", ["admin"],
        {"fields": {"provider": "custom", "api_key": SECRET}})
    assert surface == "llm_system" and "endpoint address" in notice

    surface, keep, notice = await llm_system._handle_models(
        orch, None, "admin1", ["admin"], {"fields": {"provider": "openai"}})
    assert "API key is required" in notice

    async def fake_list(*, body, request, user_id, user_payload):
        return SimpleNamespace(ok=True, models=["b-model", "a-model"],
                               error_class=None, upstream_message=None)

    monkeypatch.setattr("llm_config.api.list_models", fake_list)
    surface, keep, notice = await llm_system._handle_models(
        orch, None, "admin1", ["admin"],
        {"fields": {"provider": "openai", "api_key": SECRET}})
    assert keep["models"] == ["b-model", "a-model"]
    assert "Loaded 2 models" in notice


async def test_sys_models_failure_and_empty(orch, clean_system_row, monkeypatch):
    from orchestrator.projection_surfaces import llm_system

    async def fail_list(*, body, request, user_id, user_payload):
        return SimpleNamespace(ok=False, models=[], error_class="transport_error",
                               upstream_message="boom")

    monkeypatch.setattr("llm_config.api.list_models", fail_list)
    _s, _k, notice = await llm_system._handle_models(
        orch, None, "admin1", ["admin"],
        {"fields": {"provider": "openai", "api_key": SECRET}})
    assert "load models" in notice and "transport_error" in notice

    async def empty_list(*, body, request, user_id, user_payload):
        return SimpleNamespace(ok=True, models=[], error_class=None,
                               upstream_message=None)

    monkeypatch.setattr("llm_config.api.list_models", empty_list)
    _s, _k, notice = await llm_system._handle_models(
        orch, None, "admin1", ["admin"],
        {"fields": {"provider": "openai", "api_key": SECRET}})
    assert "advertises no models" in notice


async def test_sys_test_validation_success_and_failure(
        orch, clean_system_row, monkeypatch):
    from orchestrator.projection_surfaces import llm_system

    _s, _k, notice = await llm_system._handle_test(
        orch, None, "admin1", ["admin"],
        {"fields": {"provider": "openai", "api_key": SECRET}})
    assert "required" in notice

    async def ok_test(*, body, request, user_id, user_payload):
        return SimpleNamespace(ok=True, model=body.model, latency_ms=42,
                               error_class=None, upstream_message=None)

    monkeypatch.setattr("llm_config.api.test_connection", ok_test)
    _s, _k, notice = await llm_system._handle_test(
        orch, None, "admin1", ["admin"],
        {"fields": {"provider": "openai", "api_key": SECRET,
                    "model": "gpt-4o-mini"}})
    assert "Connection OK" in notice and "42 ms" in notice

    async def bad_test(*, body, request, user_id, user_payload):
        return SimpleNamespace(ok=False, model=body.model, latency_ms=None,
                               error_class="auth_failed", upstream_message="401")

    monkeypatch.setattr("llm_config.api.test_connection", bad_test)
    _s, _k, notice = await llm_system._handle_test(
        orch, None, "admin1", ["admin"],
        {"fields": {"provider": "openai", "api_key": SECRET,
                    "model": "gpt-4o-mini"}})
    assert "Connection test failed" in notice and "auth_failed" in notice


async def test_sys_saved_key_reused_on_blank_submission(
        orch, clean_system_row, monkeypatch):
    from orchestrator.projection_surfaces import llm_system

    await _seed_system(orch)
    probed = {}

    async def fake_probe(*, api_key, base_url, model, **kw):
        probed.update(api_key=api_key, base_url=base_url, model=model)
        return True, None, None

    monkeypatch.setattr(
        "orchestrator.projection_surfaces.llm_system.probe_chat_completion", fake_probe)
    _s, _k, notice = await llm_system._handle_save(
        orch, None, "admin1", ["admin"],
        {"fields": {"provider": "custom",
                    "base_url": "https://system.example.com/v1",
                    "model": "sys-model-2"}})
    assert probed["api_key"] == SECRET
    assert "kept the previously saved API key" in notice
    cfg = await orch._llm_store.get_system()
    assert cfg.model == "sys-model-2" and cfg.api_key == SECRET
