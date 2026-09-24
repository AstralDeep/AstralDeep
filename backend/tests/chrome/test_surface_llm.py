"""Tests for orchestrator/projection_surfaces/llm.py: web render() and native
components() over the persisted per-user llm_config store and provider catalog,
including save/clear/probe handlers and audit.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from llm_config.api import ListModelsResponse, TestConnectionResponse
from llm_config.providers import CUSTOM_PROVIDER_KEY, all_presets
from llm_config.user_store import PersistedLLMConfig
from orchestrator.projection_surfaces import get_surface
from orchestrator.projection_surfaces import llm as llm_surface

SECRET = "sk-supersecret-test-key-123456789012345"

PRESET_KEYS = [p.key for p in all_presets()]


class FakeRecorder:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


class FakeWS:
    pass


class FakeStore:
    def __init__(self):
        self._users = {}

    async def get(self, user_id):
        return self._users.get(user_id)

    async def set(self, user_id, *, provider, base_url, model, api_key):
        provider = (provider or "").strip() or "custom"
        base_url = (base_url or "").strip().rstrip("/")
        model = (model or "").strip()
        api_key = (api_key or "").strip()
        if not base_url or not model:
            raise ValueError("base_url and model must be non-empty")
        cfg = PersistedLLMConfig(provider=provider, base_url=base_url,
                                 model=model, api_key=api_key,
                                 updated_at=time.time())
        self._users[user_id] = cfg
        return cfg

    async def clear(self, user_id):
        return self._users.pop(user_id, None) is not None

    def seed(self, user_id, *, provider="custom",
             base_url="https://api.example.com/v1", model="gpt-x",
             api_key=SECRET):
        self._users[user_id] = PersistedLLMConfig(
            provider=provider, base_url=base_url, model=model,
            api_key=api_key, updated_at=time.time())

    def get_sync(self, user_id):
        return self._users.get(user_id)


def make_orch():
    sent = []

    async def safe_send(websocket, text):
        sent.append((websocket, text))

    orch = SimpleNamespace(
        ui_sessions={},
        _llm_store=FakeStore(),
        _ws_llm_gated={},
        audit_recorder=FakeRecorder(),
        _safe_send=safe_send,
    )
    orch.sent = sent
    return orch


def register(orch, ws, sub="u1"):
    orch.ui_sessions[ws] = {"sub": sub, "preferred_username": f"{sub}@example"}


def run(coro):
    return asyncio.run(coro)


def render(orch, user_id="u1", roles=None, params=None):
    return run(llm_surface.render(orch, user_id, roles or ["user"], params or {}))


def components(orch, user_id="u1", roles=None, params=None):
    return run(llm_surface.components(orch, user_id, roles or ["user"], params or {}))


def _probe_ok(monkeypatch, calls=None):
    async def fake_probe(*, api_key, base_url, model, **kw):
        if calls is not None:
            calls.update(api_key=api_key, base_url=base_url, model=model)
        return True, None, None

    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", fake_probe)


def test_registry_resolves_llm_surface():
    mod = get_surface("llm")
    assert mod is llm_surface
    assert mod.TITLE == "LLM settings"
    assert mod.FIRST_RUN_TITLE == "Set up your AI provider"
    assert not getattr(mod, "ADMIN_ONLY", False)


def test_handlers_cover_contract_actions():
    assert set(llm_surface.HANDLERS) == {
        "chrome_llm_models", "chrome_llm_test", "chrome_llm_save", "chrome_llm_clear",
        "chrome_typesafe_save", "chrome_typesafe_clear",
    }
    for fn in llm_surface.HANDLERS.values():
        assert asyncio.iscoroutinefunction(fn)


def test_render_empty_state_form_structure():
    html = render(make_orch())
    footer = llm_surface.footer_html()
    assert "data-ui-form" in html
    assert '<select name="provider"' in html
    assert 'type="password"' in html and 'name="api_key"' in html
    assert 'name="model"' in html and '<select name="model"' not in html
    for action in ("chrome_llm_models", "chrome_llm_test", "chrome_llm_save"):
        assert f'data-ui-action="{action}"' in footer
        assert 'data-ui-collect="true"' in footer
    assert "chrome_llm_clear" not in html
    assert "sk-..." in html
    assert "not configured" in html


def test_render_provider_dropdown_offers_all_presets():
    html = render(make_orch())
    assert len(PRESET_KEYS) == 11
    for key in PRESET_KEYS:
        assert f'<option value="{key}"' in html
    assert html.rindex('<option value="custom"') > html.rindex('<option value="openai"')


def test_render_endpoint_toggle_preset_vs_custom():
    html = render(make_orch(), params={"provider": "openai"})
    assert 'name="base_url"' in html
    assert "https://api.openai.com/v1" in html
    assert "set automatically" in html
    assert "astral-llm-provider" in html
    assert "data-llm-endpoints" in html
    assert 'astral-llm-endpoint-custom' in html and 'style="display:none"' in html

    html_custom = render(make_orch(), params={"provider": CUSTOM_PROVIDER_KEY})
    assert 'name="base_url"' in html_custom
    assert 'astral-llm-endpoint-preset text-xs text-astral-muted" style="display:none"' in html_custom


def test_render_keyless_preset_marks_key_optional():
    html = render(make_orch(), params={"provider": "ollama"})
    assert "optional for local runtimes" in html
    assert "http://localhost:11434/v1" in html
    assert "optional for local runtimes" not in render(
        make_orch(), params={"provider": "openai"})


def test_render_first_run_copy_and_local_runtime_note():
    html = render(make_orch(), params={"first_run": True})
    assert "nothing is built in" in html
    assert "reachable FROM THE SERVER" in html


def test_render_saved_state_shows_placeholder_never_echoes_key():
    orch = make_orch()
    orch._llm_store.seed("u1")
    html = render(orch, user_id="u1")
    assert "leave blank to keep" in html
    assert SECRET not in html
    assert 'value="https://api.example.com/v1"' in html
    assert 'value="gpt-x"' in html
    assert 'data-ui-action="chrome_llm_clear"' in html
    assert ">configured<" in html


def test_render_saved_state_is_per_user():
    orch = make_orch()
    orch._llm_store.seed("someone-else")
    html = render(orch, user_id="u1")
    assert "chrome_llm_clear" not in html
    assert "https://api.example.com/v1" not in html
    assert "not configured" in html


def test_render_models_param_builds_escaped_select():
    html = render(make_orch(), params={"models": ["m-one", "<bad>"], "model": "m-one"})
    assert '<select name="model"' in html
    assert '<option value="m-one" selected>' in html
    assert "&lt;bad&gt;" in html and "<bad>" not in html


def test_render_preserves_submitted_values_from_params():
    html = render(make_orch(), params={"provider": "custom",
                                       "base_url": "https://x.test/v1",
                                       "model": "my-model"})
    assert 'value="https://x.test/v1"' in html
    assert 'value="my-model"' in html


def _param_picker(comps):
    pickers = [c for c in comps if isinstance(c, dict) and c.get("type") == "param_picker"]
    assert len(pickers) == 1, f"expected one form, got {pickers!r}"
    return pickers[0]


def _field(picker, name):
    for f in picker["fields"]:
        if f.get("name") == name:
            return f
    return None


def test_components_include_provider_select_with_full_catalog():
    comps = components(make_orch())
    picker = _param_picker(comps)
    provider = _field(picker, "provider")
    assert provider is not None
    assert provider["kind"] == "select"
    assert provider["options"] == PRESET_KEYS
    actions = {a["action"] for a in picker["actions"]}
    assert {"chrome_llm_models", "chrome_llm_test", "chrome_llm_save"} <= actions


def test_components_first_run_carries_local_runtime_note():
    comps = components(make_orch(), params={"first_run": True})
    texts = [c.get("content", "") for c in comps
             if isinstance(c, dict) and c.get("type") == "text"]
    assert any("reachable FROM THE SERVER" in t for t in texts)
    assert any("nothing is built in" in t for t in texts)


def test_components_always_include_base_url_field():
    picker = _param_picker(components(make_orch(), params={"provider": "openai"}))
    f = _field(picker, "base_url")
    assert f is not None and f.get("default") == "https://api.openai.com/v1"

    picker = _param_picker(components(make_orch(), params={"provider": "custom"}))
    assert _field(picker, "base_url") is not None


def test_components_never_echo_saved_key():
    orch = make_orch()
    orch._llm_store.seed("u1")
    comps = components(orch, user_id="u1")
    assert SECRET not in json.dumps(comps)
    picker = _param_picker(comps)
    key_field = _field(picker, "api_key")
    assert key_field["kind"] == "password"
    assert "leave blank" in key_field["help"]
    badges = [c for c in comps if isinstance(c, dict) and c.get("type") == "badge"]
    assert badges and badges[0]["label"] == "configured"


def _payload(**fields):
    return {"fields": fields}


def test_save_probes_persists_audits_and_acks(monkeypatch):
    calls = {}
    _probe_ok(monkeypatch, calls)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    result = run(llm_surface.HANDLERS["chrome_llm_save"](
        orch, ws, "u1", ["user"],
        _payload(provider="custom", base_url="https://api.example.com/v1/",
                 api_key=SECRET, model="gpt-x"),
    ))
    surface, params, notice = result
    assert surface == "llm"
    assert calls == {"api_key": SECRET,
                     "base_url": "https://api.example.com/v1",
                     "model": "gpt-x"}
    cfg = orch._llm_store.get_sync("u1")
    assert cfg is not None and cfg.api_key == SECRET
    assert cfg.base_url == "https://api.example.com/v1"
    assert [e.action_type for e in orch.audit_recorder.events] == [
        "llm_config.tested", "llm_config.created"]
    assert orch.audit_recorder.events[-1].auth_principal == "u1@example"
    assert any("llm_config_ack" in text for sock, text in orch.sent if sock is ws)
    assert SECRET not in notice and SECRET not in str(params)
    assert "saved" in notice


def test_save_preset_derives_base_url_server_side(monkeypatch):
    calls = {}
    _probe_ok(monkeypatch, calls)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    run(llm_surface.HANDLERS["chrome_llm_save"](
        orch, ws, "u1", ["user"],
        _payload(provider="groq", base_url="https://evil.example.com/v1",
                 api_key=SECRET, model="llama-3.1-8b-instant"),
    ))
    assert calls["base_url"] == "https://api.groq.com/openai/v1"
    assert orch._llm_store.get_sync("u1").base_url == "https://api.groq.com/openai/v1"


def test_save_missing_fields_is_error_without_mutation(monkeypatch):
    async def probe_must_not_run(**kwargs):
        raise AssertionError("probe must not run on an invalid submission")

    monkeypatch.setattr(
        "llm_config.ws_handlers.probe_chat_completion", probe_must_not_run)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    surface, params, notice = run(llm_surface.HANDLERS["chrome_llm_save"](
        orch, ws, "u1", ["user"],
        _payload(provider="custom", base_url="https://x.test/v1",
                 api_key="", model=""),
    ))
    assert surface == "llm"
    assert orch._llm_store.get_sync("u1") is None
    assert orch.audit_recorder.events == []
    assert "astral-chrome-notice" in notice and "Save rejected" in notice
    assert params["base_url"] == "https://x.test/v1"


def test_save_failed_probe_refuses_and_stores_nothing(monkeypatch):
    async def failing_probe(*, api_key, base_url, model, **kw):
        return False, "auth_failed", "401 unauthorized"

    monkeypatch.setattr(
        "llm_config.ws_handlers.probe_chat_completion", failing_probe)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    _surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_save"](
        orch, ws, "u1", ["user"],
        _payload(provider="custom", base_url="https://x.test/v1",
                 api_key=SECRET, model="gpt-x"),
    ))
    assert orch._llm_store.get_sync("u1") is None
    assert "Save rejected" in notice
    assert [e.action_type for e in orch.audit_recorder.events] == ["llm_config.tested"]
    assert orch.audit_recorder.events[0].outcome == "failure"


def test_save_blank_key_keeps_saved_key_at_same_endpoint(monkeypatch):
    calls = {}
    _probe_ok(monkeypatch, calls)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    orch._llm_store.seed("u1", base_url="https://old.test/v1", model="old-model")
    surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_save"](
        orch, ws, "u1", ["user"],
        _payload(provider="custom", base_url="https://old.test/v1/",
                 api_key="", model="new-model"),
    ))
    assert surface == "llm"
    cfg = orch._llm_store.get_sync("u1")
    assert cfg.api_key == SECRET and cfg.model == "new-model"
    assert calls["api_key"] == SECRET
    assert [e.action_type for e in orch.audit_recorder.events] == [
        "llm_config.tested", "llm_config.updated"]
    assert "kept" in notice


@pytest.mark.parametrize("action", ["chrome_llm_models", "chrome_llm_test", "chrome_llm_save"])
@pytest.mark.parametrize("destination", [
    "https://new.test/v1", "https://old.test/v2", "http://old.test/v1",
])
def test_saved_key_never_reaches_a_changed_endpoint(monkeypatch, action, destination):
    async def forbidden_probe(**kwargs):
        raise AssertionError("a saved key must not reach a changed endpoint")

    monkeypatch.setattr("llm_config.api.list_models", forbidden_probe)
    monkeypatch.setattr("llm_config.api.test_connection", forbidden_probe)
    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", forbidden_probe)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    orch._llm_store.seed("u1", base_url="https://old.test/v1", model="old-model")
    before = orch._llm_store.get_sync("u1")

    surface, params, notice = run(llm_surface.HANDLERS[action](
        orch, ws, "u1", ["user"],
        _payload(provider="custom", base_url=destination, api_key="", model="new-model"),
    ))

    assert surface == "llm"
    assert "enter the API key again" in notice
    assert SECRET not in notice and "api_key" not in params
    assert orch._llm_store.get_sync("u1") is before
    assert not orch.audit_recorder.events


def test_explicit_key_can_replace_saved_key_for_new_endpoint(monkeypatch):
    calls = {}
    _probe_ok(monkeypatch, calls)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    orch._llm_store.seed("u1", base_url="https://old.test/v1", model="old-model")

    run(llm_surface.HANDLERS["chrome_llm_save"](
        orch, ws, "u1", ["user"],
        _payload(provider="custom", base_url="https://new.test/v1",
                 api_key="explicit-replacement-key", model="new-model"),
    ))

    assert calls["api_key"] == "explicit-replacement-key"
    assert calls["base_url"] == "https://new.test/v1"
    assert orch._llm_store.get_sync("u1").api_key == "explicit-replacement-key"


def test_saved_key_destination_uses_server_derived_preset(monkeypatch):
    seen = {}

    async def fake_list_models(*, body, **kwargs):
        seen.update(base_url=body.base_url, api_key=body.api_key)
        return ListModelsResponse(ok=True, models=["m"], probed_at="t")

    monkeypatch.setattr("llm_config.api.list_models", fake_list_models)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    orch._llm_store.seed("u1", provider="openai", base_url="https://api.openai.com/v1")
    run(llm_surface.HANDLERS["chrome_llm_models"](
        orch, ws, "u1", ["user"],
        _payload(provider="openai", base_url="https://untrusted.test/v1", api_key=""),
    ))
    assert seen == {"base_url": "https://api.openai.com/v1", "api_key": SECRET}


def test_clear_drops_record_audits_and_regates():
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    orch._llm_store.seed("u1", base_url="https://x.test/v1", model="m")
    result = run(llm_surface.HANDLERS["chrome_llm_clear"](
        orch, ws, "u1", ["user"], {},
    ))
    assert result is None
    assert orch._llm_store.get_sync("u1") is None
    assert [e.action_type for e in orch.audit_recorder.events] == ["llm_config.cleared"]
    mandatory = [json.loads(text) for sock, text in orch.sent
                 if sock is ws and '"chrome_render"' in text]
    assert mandatory and 'data-mandatory="1"' in mandatory[-1]["html"]
    assert orch._ws_llm_gated.get(id(ws)) is True


def test_clear_when_empty_is_quiet_noop():
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    _surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_clear"](
        orch, ws, "u1", ["user"], {},
    ))
    assert orch.audit_recorder.events == []
    assert "No stored AI provider configuration" in notice


def test_models_success_rerenders_with_select(monkeypatch):
    calls = {}

    async def fake_list_models(*, body, request, user_id, user_payload):
        calls["base_url"] = body.base_url
        calls["api_key"] = body.api_key
        return ListModelsResponse(ok=True, models=["m-a", "m-b"], probed_at="t", latency_ms=5)

    monkeypatch.setattr("llm_config.api.list_models", fake_list_models)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    surface, params, notice = run(llm_surface.HANDLERS["chrome_llm_models"](
        orch, ws, "u1", ["user"],
        _payload(base_url="https://x.test/v1", api_key=SECRET, model="m-b"),
    ))
    assert surface == "llm"
    assert calls == {"base_url": "https://x.test/v1", "api_key": SECRET}
    assert params["models"] == ["m-a", "m-b"] and params["model"] == "m-b"
    assert "Loaded 2 models" in notice
    html = render(orch, params=params)
    assert '<option value="m-b" selected>' in html


def test_models_failure_renders_error_class(monkeypatch):
    async def fake_list_models(**kwargs):
        return ListModelsResponse(
            ok=False, models=[], probed_at="t",
            error_class="transport_error", upstream_message="dns <fail>",
        )

    monkeypatch.setattr("llm_config.api.list_models", fake_list_models)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    _surface, params, notice = run(llm_surface.HANDLERS["chrome_llm_models"](
        orch, ws, "u1", ["user"], _payload(base_url="https://x.test/v1", api_key=SECRET),
    ))
    assert "transport_error" in notice
    assert "reached" in notice
    assert "dns" in notice and "<fail>" not in notice
    assert "models" not in params


def test_models_invalid_base_url_skips_probe(monkeypatch):
    async def boom(**kwargs):
        raise AssertionError("probe must not run on invalid input")

    monkeypatch.setattr("llm_config.api.list_models", boom)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    _surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_models"](
        orch, ws, "u1", ["user"], _payload(base_url="ftp://x.test", api_key=SECRET),
    ))
    assert "astral-chrome-notice" in notice and "base_url" in notice


def test_models_requires_key_for_key_required_providers():
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    _surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_models"](
        orch, ws, "u1", ["user"],
        _payload(provider="openai", base_url="", api_key=""),
    ))
    assert "required" in notice


def test_models_allows_keyless_custom_endpoint():
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    _surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_models"](
        orch, ws, "u1", ["user"],
        _payload(provider="custom", base_url="https://x.test/v1", api_key=""),
    ))
    assert "An API key is required" not in notice


def test_models_blank_key_uses_saved_persisted_key(monkeypatch):
    seen = {}

    async def fake_list_models(*, body, request, user_id, user_payload):
        seen["api_key"] = body.api_key
        return ListModelsResponse(ok=True, models=["m"], probed_at="t")

    monkeypatch.setattr("llm_config.api.list_models", fake_list_models)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    orch._llm_store.seed("u1", base_url="https://x.test/v1", model="m")
    run(llm_surface.HANDLERS["chrome_llm_models"](
        orch, ws, "u1", ["user"], _payload(base_url="https://x.test/v1", api_key=""),
    ))
    assert seen["api_key"] == SECRET


def test_test_success_renders_latency_verdict(monkeypatch):
    async def fake_test(*, body, request, user_id, user_payload):
        assert request.app.state.orchestrator is orch
        return TestConnectionResponse(ok=True, model=body.model, probed_at="t", latency_ms=123)

    monkeypatch.setattr("llm_config.api.test_connection", fake_test)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    _surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_test"](
        orch, ws, "u1", ["user"],
        _payload(base_url="https://x.test/v1", api_key=SECRET, model="gpt-x"),
    ))
    assert "Connection OK" in notice and "gpt-x" in notice and "123 ms" in notice


def test_test_failure_renders_error_class_and_message(monkeypatch):
    async def fake_test(**kwargs):
        return TestConnectionResponse(
            ok=False, model="gpt-x", probed_at="t",
            error_class="auth_failed", upstream_message="401 <unauthorized>",
        )

    monkeypatch.setattr("llm_config.api.test_connection", fake_test)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    _surface, params, notice = run(llm_surface.HANDLERS["chrome_llm_test"](
        orch, ws, "u1", ["user"],
        _payload(base_url="https://x.test/v1", api_key=SECRET, model="gpt-x"),
    ))
    assert "auth_failed" in notice
    assert "rejected the API key" in notice
    assert "401" in notice and "<unauthorized>" not in notice
    assert params == {"provider": "custom", "base_url": "https://x.test/v1",
                      "model": "gpt-x"}


def test_test_failure_html_error_page_is_never_dumped(monkeypatch):
    page = ('<!doctype html><html lang="en"><head><title>Example Domain</title>'
            "<style>body{background:#eee}</style></head>"
            "<body><h1>Example Domain</h1></body></html>")

    async def fake_test(**kwargs):
        return TestConnectionResponse(
            ok=False, model="ddddd", probed_at="t",
            error_class="other", upstream_message=page,
        )

    monkeypatch.setattr("llm_config.api.test_connection", fake_test)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    _surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_test"](
        orch, ws, "u1", ["user"],
        _payload(base_url="http://example.com", api_key=SECRET, model="ddddd"),
    ))
    assert "doctype" not in notice.lower() and "Example Domain" not in notice
    assert "Double-check the provider" in notice


def test_clean_upstream_sanitizes_and_bounds():
    assert llm_surface._clean_upstream("dns <fail>") == "dns"
    assert llm_surface._clean_upstream("401 <unauthorized>") == "401"
    assert llm_surface._clean_upstream("<!DOCTYPE html><html><body>x</body></html>") == ""
    assert llm_surface._clean_upstream("  Error   code: 404 - model missing ") == "Error code: 404 - model missing"
    assert len(llm_surface._clean_upstream("y" * 5000)) == llm_surface._UPSTREAM_SNIPPET_LEN
    assert llm_surface._clean_upstream("") == ""


def test_test_requires_all_fields():
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    _surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_test"](
        orch, ws, "u1", ["user"], _payload(base_url="https://x.test/v1", api_key=SECRET, model=""),
    ))
    assert "required" in notice


def _native_orch(device):
    from rote.rote import ROTE

    orch = make_orch()
    orch.rote = ROTE()
    ws = FakeWS()
    register(orch, ws)
    orch.rote.register_device(ws, {"device_type": device})
    return orch, ws


def _close_frames(orch):
    out = []
    for _ws, text in orch.sent:
        try:
            frame = json.loads(text)
        except (TypeError, ValueError):
            continue
        if (frame.get("type") == "chrome_surface"
                and frame.get("surface_key") == ""
                and not (frame.get("components") or [])):
            out.append(frame)
    return out


@pytest.mark.parametrize("device", ["macos", "ios", "windows", "android"])
def test_settings_path_save_closes_the_surface_on_native_clients(monkeypatch, device):
    _probe_ok(monkeypatch)
    orch, ws = _native_orch(device)
    orch._llm_store.seed("u1", base_url="https://old.test/v1", model="old-model")

    result = run(llm_surface.HANDLERS["chrome_llm_save"](
        orch, ws, "u1", ["user"],
        _payload(provider="custom", base_url="https://new.test/v1",
                 api_key=SECRET, model="new-model"),
    ))

    assert result is None
    assert len(_close_frames(orch)) == 1
    assert orch._llm_store.get_sync("u1").model == "new-model"


@pytest.mark.parametrize("device", ["macos", "android"])
def test_rejected_save_keeps_the_native_surface_open_to_show_the_error(monkeypatch, device):
    async def failing_probe(*, api_key, base_url, model, **kw):
        return False, "auth_failed", "401 unauthorized"

    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", failing_probe)
    orch, ws = _native_orch(device)

    surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_save"](
        orch, ws, "u1", ["user"],
        _payload(provider="custom", base_url="https://x.test/v1",
                 api_key=SECRET, model="gpt-x"),
    ))

    assert surface == "llm"
    assert "Save rejected" in notice
    assert _close_frames(orch) == []
    assert orch._llm_store.get_sync("u1") is None


def test_settings_path_save_keeps_the_web_success_notice(monkeypatch):
    _probe_ok(monkeypatch)
    orch, ws = _native_orch("browser")
    orch._llm_store.seed("u1", base_url="https://old.test/v1", model="old-model")

    surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_save"](
        orch, ws, "u1", ["user"],
        _payload(provider="custom", base_url="https://new.test/v1",
                 api_key=SECRET, model="new-model"),
    ))

    assert surface == "llm"
    assert "AI provider saved for your account" in notice
    assert _close_frames(orch) == []
