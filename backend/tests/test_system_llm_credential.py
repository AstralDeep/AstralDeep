"""Feature 054 — T040: the admin-managed deployment-wide System LLM credential.

Covers:

* the ``llm_system`` surface registration + server-side admin gating
  (non-admin handler invocation refused + ``settings.admin_denied`` audit);
* the probe-gated system save (``scope:"system"`` audit, persisted row);
* the web-only menu carve-out (``include_admin=False`` channels never see it);
* scheduler honesty (FR-020): ``run_scheduled_turn`` raises ``LLMUnavailable``
  with no system row (audited ``feature:"scheduled_job"``), proceeds with one;
  ``JobRunner.run_job`` records ``outcome="failure"`` with an
  ``llm_unavailable`` summary and an error notification;
* the mid-clear race (cleared between enqueue and run ⇒ honest failure);
* FR-019 in both directions (user sockets never resolve the system record;
  system contexts never resolve a user record).

References: specs/054-byo-llm-setup/spec.md FR-018..FR-021.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator import chrome_events  # noqa: E402

SECRET = "sk-supersecret-system-key-1234567890123456"
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
    """Snapshot + remove any pre-existing system row; restore afterwards."""
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


# ---------------------------------------------------------------------------
# (a) surface registration + admin gating
# ---------------------------------------------------------------------------

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
    ws = _register(orch, uid, roles=("user",))  # no admin role
    await _seed_user(orch, uid)  # configured, so the llm gate is not in play
    try:
        handled = await chrome_events.handle_chrome_event(
            orch, ws, "chrome_llm_sys_save",
            {"fields": {"provider": "openai", "api_key": SECRET,
                        "model": "gpt-4o-mini"}},
            uid)

        assert handled is True
        # Refusal notice pushed (web modal), never the handler's own output.
        renders = _frames(orch, "chrome_render")
        assert renders and "admin role" in renders[-1]["html"]
        # settings.admin_denied audit fired.
        assert any(e.action_type == "settings.admin_denied" for e in denied.events)
        # Nothing was persisted.
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


# ---------------------------------------------------------------------------
# (b) admin save persists the system row with scope:"system" audit
# ---------------------------------------------------------------------------

async def test_admin_save_persists_system_row_with_system_scope_audit(
        orch, clean_system_row, monkeypatch):
    from orchestrator.projection_surfaces import llm_system

    probed = {}

    async def fake_probe(*, api_key, base_url, model, **kw):
        probed.update(api_key=api_key, base_url=base_url, model=model)
        return True, None, None

    # llm_system imports probe_chat_completion directly — patch ITS binding.
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
    # Probe ran against the exact server-derived triple.
    assert probed == {"api_key": SECRET,
                      "base_url": "https://api.openai.com/v1",
                      "model": "gpt-4o-mini"}
    # Persisted (encrypted at rest, decrypts back to the submitted key).
    cfg = await orch._llm_store.get_system()
    assert cfg is not None
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.api_key == SECRET
    # scope:"system" audit trail: tested then created.
    actions = [e.action_type for e in orch.audit_recorder.events]
    assert actions == ["llm_config.tested", "llm_config.created"]
    for e in orch.audit_recorder.events:
        assert e.inputs_meta["scope"] == "system"
        assert SECRET not in json.dumps(e.inputs_meta)

    # Clear round-trip: audited scope:"system" and honest degradation copy.
    surface, _params, notice = await llm_system._handle_clear(
        orch, ws, "admin1", ["admin"], {})
    assert surface == "llm_system" and "cleared" in notice
    assert await orch._llm_store.get_system() is None
    cleared = orch.audit_recorder.events[-1]
    assert cleared.action_type == "llm_config.cleared"
    assert cleared.inputs_meta["scope"] == "system"


# ---------------------------------------------------------------------------
# (c) menu model — web-only admin carve-out
# ---------------------------------------------------------------------------

def test_menu_model_web_admin_sees_system_llm_natives_never_do():
    from webrender.chrome.menu_model import build_menu_model

    def _surfaces(model):
        return [i.surface for g in model.menu for i in g.items]

    web_admin = build_menu_model(["admin"], include_admin=True)
    assert "llm_system" in _surfaces(web_admin)
    item = next(i for g in web_admin.menu for i in g.items if i.surface == "llm_system")
    assert item.admin_only is True and item.label == "System LLM"

    # Native channels (include_admin=False) omit it even for admins.
    native_admin = build_menu_model(["admin"], include_admin=False)
    assert "llm_system" not in _surfaces(native_admin)

    # Non-admins never see it on any channel.
    web_user = build_menu_model(["user"], include_admin=True)
    assert "llm_system" not in _surfaces(web_user)


# ---------------------------------------------------------------------------
# (d) scheduler honesty (FR-020)
# ---------------------------------------------------------------------------

async def test_run_scheduled_turn_raises_llm_unavailable_without_system_row(
        orch, clean_system_row):
    with pytest.raises(orch._LLMUnavailable):
        await orch.run_scheduled_turn(
            user_id=_uid(), chat_id=None, instruction="do the thing",
            agent_id=None, access_token="tok", allowed_scopes=[],
            correlation_id="corr-1")

    assert orch._record_llm_unconfigured.await_count == 1
    assert orch._record_llm_unconfigured.call_args.kwargs["feature"] == "scheduled_job"


async def test_run_scheduled_turn_proceeds_with_system_row(
        orch, clean_system_row, monkeypatch):
    await _seed_system(orch)
    ran = {}

    async def fake_handle_chat_message(websocket, message, chat_id, **kwargs):
        ran.update(message=message, chat_id=chat_id, user_id=kwargs.get("user_id"))

    monkeypatch.setattr(orch, "handle_chat_message", fake_handle_chat_message)
    uid = _uid()

    summary = await orch.run_scheduled_turn(
        user_id=uid, chat_id="sched-chat-1", instruction="summarize inbox",
        agent_id=None, access_token="tok", allowed_scopes=[],
        correlation_id="corr-2")

    assert ran == {"message": "summarize inbox", "chat_id": "sched-chat-1",
                   "user_id": uid}
    assert summary == "Your scheduled task finished."
    assert orch._record_llm_unconfigured.await_count == 0


class _RunnerStore:
    def __init__(self):
        self.finished = []
        self.statuses = []
        self.updated = []

    def start_run(self, job_id, user_id, correlation_id):
        return "run-1"

    def finish_run(self, run_id, *, outcome, summary=None, auth_ref=None):
        self.finished.append({"run_id": run_id, "outcome": outcome,
                              "summary": summary})

    def set_status(self, user_id, job_id, status):
        self.statuses.append((job_id, status))

    def update_after_run(self, job_id, *, last_run_at, next_run_at, completed):
        self.updated.append((job_id, completed))


class _RunnerGrants:
    def is_valid(self, grant_id, *, user_id):
        return True

    async def mint_access_token(self, grant_id, *, user_id):
        return "minted-token"


async def test_job_runner_records_honest_failure_and_error_notification():
    from llm_config import LLMUnavailable
    from scheduler.runner import JobRunner

    notifications = []

    async def notify_user(user_id, payload):
        notifications.append((user_id, payload))

    async def run_scheduled_turn(**kwargs):
        raise LLMUnavailable("no system LLM credential configured")

    fake_orch = SimpleNamespace(
        run_scheduled_turn=run_scheduled_turn,
        notify_user=notify_user,
        tool_permissions=SimpleNamespace(get_agent_scopes=lambda u, a: {}),
    )
    store = _RunnerStore()
    runner = JobRunner(fake_orch, store, _RunnerGrants())
    job = {
        "id": "job-1", "user_id": "u1", "name": "Morning brief",
        "instruction": "brief me", "schedule_kind": "one_shot",
        "schedule_expr": "2020-01-01T00:00:00Z", "timezone": "UTC",
        "consented_scopes": [], "agent_id": None, "target_chat_id": None,
        "offline_grant_id": "grant-1",
    }

    outcome = await runner.run_job(job)

    # Never a silent success (US4-AS1 / FR-020).
    assert outcome == "failure"
    assert store.finished == [{"run_id": "run-1", "outcome": "failure",
                               "summary": "llm_unavailable: no system AI "
                                          "credential configured"}]
    assert "llm_unavailable" in store.finished[0]["summary"]
    # The owner is told the AI was unavailable.
    assert len(notifications) == 1
    user_id, payload = notifications[0]
    assert user_id == "u1"
    assert payload["level"] == "error"
    assert "AI was unavailable" in payload["body"]
    assert "failed" in payload["title"]


async def test_mid_clear_race_is_honest_failure_not_success(
        orch, clean_system_row):
    # Enqueue-time check would have passed...
    await _seed_system(orch)
    assert await orch._llm_store.get_system() is not None

    # ...but an admin clears the credential before the run executes.
    assert await orch._llm_store.clear_system() is True

    with pytest.raises(orch._LLMUnavailable):
        await orch.run_scheduled_turn(
            user_id=_uid(), chat_id=None, instruction="x", agent_id=None,
            access_token="tok", allowed_scopes=[], correlation_id="corr-3")
    assert orch._record_llm_unconfigured.call_args.kwargs["feature"] == "scheduled_job"


# ---------------------------------------------------------------------------
# (f) FR-019 — no fallback in either direction
# ---------------------------------------------------------------------------

async def test_user_socket_never_resolves_system_record(
        orch, clean_system_row):
    await _seed_system(orch)  # a system row exists...
    uid = _uid()              # ...but this user is unconfigured
    ws = _register(orch, uid)

    assert await orch.llm_configured_for(uid) is False
    with pytest.raises(orch._LLMUnavailable):
        await orch._resolve_llm_client_for(ws)

    # Chat pre-flight refuses server-side (the gate, not the system record).
    called = {"n": 0}

    async def fake_call_llm(*args, **kwargs):
        called["n"] += 1
        return None, None

    orch._call_llm = fake_call_llm
    await orch.handle_chat_message(
        ws, "hello there", f"sysgate-{uuid.uuid4().hex[:8]}", user_id=uid)
    assert called["n"] == 0, "no LLM call may run for a gated user"
    assert orch._record_llm_unconfigured.call_args.kwargs["feature"] == "chat_dispatch"
    alerts = [c for call in orch.send_ui_render.call_args_list
              for c in (call.args[1] if len(call.args) > 1 else [])
              if isinstance(c, dict) and c.get("type") == "alert"]
    assert alerts and "Set up your AI provider" in alerts[-1]["message"]


async def test_system_context_never_resolves_a_user_record(
        orch, clean_system_row):
    uid = _uid()
    await _seed_user(orch, uid)  # only user rows exist; no system row
    try:
        # websocket=None (background/system context) must not borrow it.
        with pytest.raises(orch._LLMUnavailable):
            await orch._resolve_llm_client_for(None)

        # A scheduled-turn VirtualWebSocket is a system context too.
        from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
        vws = VirtualWebSocket(BackgroundTask(
            task_id="t1", chat_id="c1", user_id=uid))
        with pytest.raises(orch._LLMUnavailable):
            await orch._resolve_llm_client_for(vws)
    finally:
        await orch._llm_store.clear(uid)


# ---------------------------------------------------------------------------
# (g) surface render + models/test handlers (coverage for the web-only UI)
# ---------------------------------------------------------------------------

async def test_render_unconfigured_custom_vs_preset(orch, clean_system_row):
    from orchestrator.projection_surfaces import llm_system

    # Preset provider: endpoint caption shown; the (always-present) custom
    # base_url input is hidden and toggled client-side.
    html = await llm_system.render(orch, "admin1", ["admin"], {"provider": "openai"})
    assert "set automatically" in html.lower()
    assert "astral-llm-provider" in html and "data-llm-endpoints" in html
    assert 'astral-llm-endpoint-custom' in html and 'style="display:none"' in html
    assert "not configured" in html
    assert "chrome_llm_sys_save" in html
    assert "Clear system credential" not in html

    # Custom provider: free-form endpoint field visible with its value.
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
    assert SECRET not in html  # write-only key, never echoed
    # models param upgrades the model input to a select
    html = await llm_system.render(orch, "admin1", ["admin"],
                                   {"models": ["m1", "m2"]})
    assert "<select" in html and "m1" in html and "m2" in html


async def test_sys_models_validation_and_success(orch, clean_system_row, monkeypatch):
    from orchestrator.projection_surfaces import llm_system

    # custom without a URL -> field error, no upstream call
    surface, keep, notice = await llm_system._handle_models(
        orch, None, "admin1", ["admin"],
        {"fields": {"provider": "custom", "api_key": SECRET}})
    assert surface == "llm_system" and "endpoint address" in notice

    # key-required preset without a key (and nothing saved) -> error
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
    assert "load models" in notice and "transport_error" in notice  # esc()'d apostrophe

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

    # missing model -> error before any upstream call
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
    """Write-only semantics: a blank key submission keeps the saved system key
    (the probe must receive the SAVED key)."""
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
                    "model": "sys-model-2"}})  # no api_key field
    assert probed["api_key"] == SECRET
    assert "kept the previously saved API key" in notice
    cfg = await orch._llm_store.get_system()
    assert cfg.model == "sys-model-2" and cfg.api_key == SECRET
