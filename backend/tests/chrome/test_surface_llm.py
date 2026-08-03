"""Features 027/043/054 — LLM settings surface (key ``llm``).

Feature 054 rebuilt this surface around the PERSISTED per-user store
(``user_llm_config``) and the server-owned provider catalog
(:mod:`llm_config.providers`). These tests run structurally on a minimal
fake orchestrator — no Postgres, no network: the store is an in-memory fake
implementing exactly the async surface the handlers use; the save-path
connection probe is monkeypatched at ``llm_config.ws_handlers`` (spec FR-008)
and the list-models / test probes at their ``llm_config.api`` definitions.
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from llm_config.api import ListModelsResponse, TestConnectionResponse
from llm_config.providers import CUSTOM_PROVIDER_KEY, all_presets
from llm_config.user_store import PersistedLLMConfig
from webrender.chrome.surfaces import get_surface
from webrender.chrome.surfaces import llm as llm_surface

SECRET = "sk-supersecret-test-key-123456789012345"

PRESET_KEYS = [p.key for p in all_presets()]


class FakeRecorder:
    """Collects audit events (stand-in for ``orch.audit_recorder``)."""

    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


class FakeWS:
    """Identity-only websocket stand-in."""


class FakeStore:
    """In-memory twin of ``UserLLMConfigStore``'s per-user async surface."""

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

    # test conveniences
    def seed(self, user_id, *, provider="custom",
             base_url="https://api.example.com/v1", model="gpt-x",
             api_key=SECRET):
        self._users[user_id] = PersistedLLMConfig(
            provider=provider, base_url=base_url, model=model,
            api_key=api_key, updated_at=time.time())

    def get_sync(self, user_id):
        return self._users.get(user_id)


def make_orch():
    """Minimal orch exposing exactly what the surface touches."""
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


# ---------------------------------------------------------------------------
# Registry / module contract
# ---------------------------------------------------------------------------

def test_registry_resolves_llm_surface():
    mod = get_surface("llm")
    assert mod is llm_surface
    assert mod.TITLE == "LLM settings"
    assert mod.FIRST_RUN_TITLE == "Set up your AI provider"
    assert not getattr(mod, "ADMIN_ONLY", False)


def test_handlers_cover_contract_actions():
    assert set(llm_surface.HANDLERS) == {
        "chrome_llm_models", "chrome_llm_test", "chrome_llm_save", "chrome_llm_clear",
    }
    for fn in llm_surface.HANDLERS.values():
        assert asyncio.iscoroutinefunction(fn)


# ---------------------------------------------------------------------------
# Render (web HTML)
# ---------------------------------------------------------------------------

def test_render_empty_state_form_structure():
    html = render(make_orch())
    assert "data-ui-form" in html
    assert '<select name="provider"' in html
    assert 'type="password"' in html and 'name="api_key"' in html
    assert 'name="model"' in html and '<select name="model"' not in html
    for action in ("chrome_llm_models", "chrome_llm_test", "chrome_llm_save"):
        assert f'data-ui-action="{action}"' in html
        assert 'data-ui-collect="true"' in html
    # No saved config -> no clear affordance, generic key placeholder.
    assert "chrome_llm_clear" not in html
    assert "sk-..." in html
    assert "not configured" in html


def test_render_provider_dropdown_offers_all_presets():
    html = render(make_orch())
    assert len(PRESET_KEYS) == 11  # FR-011: ten presets + custom
    for key in PRESET_KEYS:
        assert f'<option value="{key}"' in html
    # Custom is the last option (escape hatch ordering).
    assert html.rindex('<option value="custom"') > html.rindex('<option value="openai"')


def test_render_endpoint_toggle_preset_vs_custom():
    """Both endpoint halves are always in the DOM (the static modal toggles
    them client-side); the preset caption is shown + the custom input hidden
    for a preset, and vice versa for custom. The provider <select> carries
    the client-side toggle hook and the form embeds the endpoints map."""
    html = render(make_orch(), params={"provider": "openai"})
    assert 'name="base_url"' in html               # always present (hidden for presets)
    assert "https://api.openai.com/v1" in html
    assert "set automatically" in html
    assert "astral-llm-provider" in html           # client-side change hook
    assert "data-llm-endpoints" in html            # embedded provider->url map
    # For a preset the custom input is hidden and the preset caption is shown.
    assert 'astral-llm-endpoint-custom' in html and 'style="display:none"' in html

    html_custom = render(make_orch(), params={"provider": CUSTOM_PROVIDER_KEY})
    assert 'name="base_url"' in html_custom
    # For custom the preset caption is hidden.
    assert 'astral-llm-endpoint-preset text-xs text-astral-muted" style="display:none"' in html_custom


def test_render_keyless_preset_marks_key_optional():
    html = render(make_orch(), params={"provider": "ollama"})
    assert "optional for local runtimes" in html
    assert "http://localhost:11434/v1" in html
    # Key-required presets never carry the optional copy.
    assert "optional for local runtimes" not in render(
        make_orch(), params={"provider": "openai"})


def test_render_first_run_copy_and_local_runtime_note():
    html = render(make_orch(), params={"first_run": True})
    assert "nothing is built in" in html
    assert "reachable FROM THE SERVER" in html


def test_render_saved_state_shows_placeholder_never_echoes_key():
    orch = make_orch()
    orch._llm_store.seed("u1")  # provider=custom, base_url, model, SECRET key
    html = render(orch, user_id="u1")
    assert "leave blank to keep" in html
    assert SECRET not in html  # write-only display — the key is NEVER echoed
    assert 'value="https://api.example.com/v1"' in html  # custom → editable field
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


# ---------------------------------------------------------------------------
# SDUI components() (feature 043 twin of the web render)
# ---------------------------------------------------------------------------

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
    # Same multi-action wiring as the web buttons.
    actions = {a["action"] for a in picker["actions"]}
    assert {"chrome_llm_models", "chrome_llm_test", "chrome_llm_save"} <= actions


def test_components_first_run_carries_local_runtime_note():
    comps = components(make_orch(), params={"first_run": True})
    texts = [c.get("content", "") for c in comps
             if isinstance(c, dict) and c.get("type") == "text"]
    assert any("reachable FROM THE SERVER" in t for t in texts)
    assert any("nothing is built in" in t for t in texts)


def test_components_always_include_base_url_field():
    """Native forms can't re-render on provider change, so the base_url field
    is ALWAYS present — prefilled with the preset endpoint for presets
    (server ignores it) and editable for custom (source of truth)."""
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


# ---------------------------------------------------------------------------
# chrome_llm_save / chrome_llm_clear (persisted store + probe gate + audit)
# ---------------------------------------------------------------------------

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
    # The probe ran against the exact triple being saved (FR-008).
    assert calls == {"api_key": SECRET,
                     "base_url": "https://api.example.com/v1",
                     "model": "gpt-x"}
    cfg = orch._llm_store.get_sync("u1")
    assert cfg is not None and cfg.api_key == SECRET
    assert cfg.base_url == "https://api.example.com/v1"  # store rstrips '/'
    # Audit preserved (same path as WS llm_config_set): probe then persist.
    assert [e.action_type for e in orch.audit_recorder.events] == [
        "llm_config.tested", "llm_config.created"]
    assert orch.audit_recorder.events[-1].auth_principal == "u1@example"
    # llm_config_ack still sent over the live websocket.
    assert any("llm_config_ack" in text for sock, text in orch.sent if sock is ws)
    # Key never leaks into the re-render inputs.
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
        # Submitted base_url is IGNORED for presets — server derives it.
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
    assert params["base_url"] == "https://x.test/v1"  # submitted values preserved


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
    assert orch._llm_store.get_sync("u1") is None  # nothing persisted
    assert "Save rejected" in notice
    # The failed probe itself is audited; no created/updated follows.
    assert [e.action_type for e in orch.audit_recorder.events] == ["llm_config.tested"]
    assert orch.audit_recorder.events[0].outcome == "failure"


def test_save_blank_key_keeps_saved_key(monkeypatch):
    calls = {}
    _probe_ok(monkeypatch, calls)
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    orch._llm_store.seed("u1", base_url="https://old.test/v1", model="old-model")
    surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_save"](
        orch, ws, "u1", ["user"],
        _payload(provider="custom", base_url="https://new.test/v1",
                 api_key="", model="new-model"),
    ))
    assert surface == "llm"
    cfg = orch._llm_store.get_sync("u1")
    assert cfg.api_key == SECRET and cfg.model == "new-model"
    assert calls["api_key"] == SECRET  # the kept key was probed too
    assert [e.action_type for e in orch.audit_recorder.events] == [
        "llm_config.tested", "llm_config.updated"]
    assert "kept" in notice


def test_clear_drops_record_audits_and_regates():
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    orch._llm_store.seed("u1", base_url="https://x.test/v1", model="m")
    result = run(llm_surface.HANDLERS["chrome_llm_clear"](
        orch, ws, "u1", ["user"], {},
    ))
    # The re-gate replaced the modal on every socket — no tuple re-render.
    assert result is None
    assert orch._llm_store.get_sync("u1") is None
    assert [e.action_type for e in orch.audit_recorder.events] == ["llm_config.cleared"]
    # The mandatory setup dialog was pushed to the user's socket (FR-009).
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
    assert orch.audit_recorder.events == []  # no audit noise on empty clear
    assert "No stored AI provider configuration" in notice


# ---------------------------------------------------------------------------
# chrome_llm_models / chrome_llm_test (probe-internals reuse)
# ---------------------------------------------------------------------------

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
    assert "reached" in notice  # actionable hint (check URL/network), not a raw dump
    assert "dns" in notice and "<fail>" not in notice  # sanitized upstream snippet
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
    """A hosted provider still refuses an empty key up front.

    Feature 066 made ``custom`` key-OPTIONAL (self-hosted OpenAI-compatible
    endpoints are commonly keyless), so the refusal is now scoped to the
    presets that genuinely require a key; a keyless custom config is allowed
    to probe and the endpoint's own answer is the honesty gate.
    """
    orch = make_orch()
    ws = FakeWS()
    register(orch, ws)
    _surface, _params, notice = run(llm_surface.HANDLERS["chrome_llm_models"](
        orch, ws, "u1", ["user"],
        _payload(provider="openai", base_url="", api_key=""),
    ))
    assert "required" in notice


def test_models_allows_keyless_custom_endpoint():
    """066 FR-025: custom + empty key reaches the probe instead of refusing."""
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
        assert request.app.state.orchestrator is orch  # probe audit reaches the orch
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
    assert "rejected the API key" in notice  # actionable hint, not a raw dump
    assert "401" in notice and "<unauthorized>" not in notice  # sanitized snippet
    assert params == {"provider": "custom", "base_url": "https://x.test/v1",
                      "model": "gpt-x"}


def test_test_failure_html_error_page_is_never_dumped(monkeypatch):
    """A mistyped Base URL typically answers with a WEBSITE — the notice must
    give the user a what-to-do hint, never the raw page markup (the pre-fix
    behavior dumped `<!doctype html><html…` into the modal)."""
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
    # The actionable what-to-do guidance survived (054 catalog-aware copy).
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


# ---------------------------------------------------------------------------
# Settings-path save: dismissal is device-aware
#
# A save by an ALREADY-configured user unlocks no first-run gate, so nothing
# closes the surface for them. Web can answer with a success notice because its
# modal shell carries a ✕; an Apple surface is a full screen with no ✕ and no
# system Back, so a notice re-render strands it on screen with the save already
# committed (the reported macOS symptom). Natives get the close instruction.
# ---------------------------------------------------------------------------

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

    # No re-render tuple: a stranded surface is the bug being guarded against.
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

    # The surface only stays open when it still needs the user.
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

    # Web keeps its ✕, so it keeps the confirmation it always had.
    assert surface == "llm"
    assert "AI provider saved for your account" in notice
    assert _close_frames(orch) == []
