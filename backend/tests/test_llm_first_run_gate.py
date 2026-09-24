"""Tests for llm_config/ws_handlers.py and orchestrator/llm_gate.py: the mandatory
first-run setup gate's device-specific dialogs, refusal of other surfaces while
unconfigured, probe-gated save with multi-socket unlock fan-out, and its kill switch.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator import chrome_events, llm_gate  # noqa: E402

SECRET = "sk-supersecret-gate-key-123456789012345"

NATIVE_DEVICES = ("windows", "android", "ios", "macos")


class FakeRecorder:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


def _uid() -> str:
    return f"gate054-{uuid.uuid4().hex[:10]}"


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


def _register(orch, uid, device="browser", roles=("user",)):
    ws = MagicMock()
    orch.ui_sessions[ws] = {
        "sub": uid,
        "preferred_username": f"{uid}@example",
        "realm_access": {"roles": list(roles)},
    }
    orch.rote.register_device(ws, {"device_type": device})
    return ws


def _frames(orch, ftype):
    out = []
    for ws, data in orch.sent:
        try:
            f = json.loads(data)
        except (TypeError, ValueError):
            continue
        if f.get("type") == ftype:
            out.append((ws, f))
    return out


async def _seed(orch, uid, base_url="https://api.example.com/v1",
                model="gpt-x", api_key=SECRET, provider="custom"):
    await orch._llm_store.set(
        uid, provider=provider, base_url=base_url, model=model, api_key=api_key)


async def test_push_browser_mandatory_modal_no_close_with_signout(orch):
    uid = _uid()
    ws = _register(orch, uid, device="browser")

    await llm_gate.push_setup_dialog(orch, ws, uid)

    frames = _frames(orch, "chrome_render")
    assert len(frames) == 1
    _, frame = frames[0]
    assert frame["region"] == "modal"
    html = frame["html"]
    assert 'data-mandatory="1"' in html
    assert "astral-modal-close" not in html
    assert "/auth/logout" in html
    assert "Set up your AI provider" in html
    assert 'data-ui-action="chrome_llm_save"' in html
    assert orch._ws_llm_gated.get(id(ws)) is True


async def test_push_native_devices_get_mandatory_chrome_surface(orch):
    uid = _uid()
    for device in NATIVE_DEVICES:
        ws = _register(orch, uid, device=device)
        await llm_gate.push_setup_dialog(orch, ws, uid)
        assert orch._ws_llm_gated.get(id(ws)) is True

    surfaces = _frames(orch, "chrome_surface")
    assert len(surfaces) == len(NATIVE_DEVICES)
    for _, frame in surfaces:
        assert frame["mode"] == "mandatory"
        assert frame["surface_key"] == llm_gate.SURFACE_KEY == "llm"
        assert frame["region"] == "modal"
        assert frame["components"], "mandatory surface must carry the setup form"
    assert _frames(orch, "chrome_render") == []


async def test_push_watch_is_skipped(orch):
    uid = _uid()
    ws = _register(orch, uid, device="watch")

    await llm_gate.push_setup_dialog(orch, ws, uid)

    assert orch.sent == []
    assert id(ws) not in orch._ws_llm_gated


async def test_chrome_open_other_surface_refused_while_unconfigured(orch):
    uid = _uid()
    ws = _register(orch, uid)

    handled = await chrome_events.handle_chrome_event(
        orch, ws, "chrome_open", {"surface": "theme"}, uid)

    assert handled is True
    assert orch._record_llm_unconfigured.await_count == 1
    kwargs = orch._record_llm_unconfigured.call_args.kwargs
    assert kwargs["feature"] == "chrome:chrome_open"
    frames = _frames(orch, "chrome_render")
    assert frames and 'data-mandatory="1"' in frames[-1][1]["html"]
    assert "Theme" not in frames[-1][1]["html"]


async def test_chrome_close_refused_while_unconfigured(orch):
    uid = _uid()
    ws = _register(orch, uid)

    handled = await chrome_events.handle_chrome_event(
        orch, ws, "chrome_close", {}, uid)

    assert handled is True
    assert orch._record_llm_unconfigured.call_args.kwargs["feature"] == "chrome:chrome_close"
    frames = _frames(orch, "chrome_render")
    assert all(f["html"] != "" for _, f in frames)
    assert 'data-mandatory="1"' in frames[-1][1]["html"]


async def test_setup_surface_own_actions_pass_the_gate(orch):
    uid = _uid()
    ws = _register(orch, uid)

    for action in ("chrome_llm_models", "chrome_llm_test",
                   "chrome_llm_save", "chrome_llm_clear"):
        refused = await chrome_events._llm_gate_refusal(orch, ws, action, uid)
        assert refused is False, f"{action} must not be gate-refused"

    handled = await chrome_events.handle_chrome_event(
        orch, ws, "chrome_llm_clear", {}, uid)
    assert handled is True
    assert orch._record_llm_unconfigured.await_count == 0
    frames = _frames(orch, "chrome_render")
    assert frames and "No stored AI provider configuration" in frames[-1][1]["html"]


async def test_configured_user_passes_through_untouched(orch):
    uid = _uid()
    ws = _register(orch, uid)
    await _seed(orch, uid)
    try:
        refused = await chrome_events._llm_gate_refusal(orch, ws, "chrome_open", uid)
        assert refused is False

        handled = await chrome_events.handle_chrome_event(
            orch, ws, "chrome_close", {}, uid)
        assert handled is True
        frames = _frames(orch, "chrome_render")
        assert frames[-1][1]["html"] == ""
        assert orch._record_llm_unconfigured.await_count == 0
    finally:
        await orch._llm_store.clear(uid)


async def test_probe_gated_save_persists_and_returns_true(orch, monkeypatch):
    from llm_config.ws_handlers import handle_llm_config_set

    probed = {}

    async def fake_probe(*, api_key, base_url, model, **kw):
        probed.update(api_key=api_key, base_url=base_url, model=model)
        return True, None, None

    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", fake_probe)
    uid = _uid()
    ws = _register(orch, uid)
    try:
        saved = await handle_llm_config_set(
            safe_send=orch._safe_send,
            websocket=ws,
            config={"provider": "openai", "api_key": SECRET, "model": "gpt-4o-mini"},
            actor_user_id=uid,
            auth_principal=f"{uid}@example",
            store=orch._llm_store,
            recorder=orch.audit_recorder,
        )
        assert saved is True
        cfg = await orch._llm_store.get(uid)
        assert cfg is not None
        assert cfg.base_url == "https://api.openai.com/v1"
        assert cfg.model == "gpt-4o-mini"
        assert cfg.api_key == SECRET
        assert probed == {"api_key": SECRET,
                          "base_url": "https://api.openai.com/v1",
                          "model": "gpt-4o-mini"}
        acks = [json.loads(d) for _, d in orch.sent
                if json.loads(d).get("type") == "llm_config_ack"]
        assert acks and acks[-1]["ok"] is True
    finally:
        await orch._llm_store.clear(uid)


async def test_unlock_after_save_closes_all_gated_sockets(orch):
    uid = _uid()
    ws1 = _register(orch, uid)
    ws2 = _register(orch, uid)
    orch._ws_llm_gated = {id(ws1): True, id(ws2): True}

    unlocked = await llm_gate.unlock_after_save(orch, uid)

    assert unlocked is True
    assert orch._ws_llm_gated == {}
    close_frames = [(ws, f) for ws, f in _frames(orch, "chrome_render")
                    if f["html"] == ""]
    assert {id(ws) for ws, _ in close_frames} == {id(ws1), id(ws2)}
    assert orch.send_ui_render.await_count == 2

    assert await llm_gate.unlock_after_save(orch, uid) is False


async def test_kill_switch_disables_push_but_not_refusals(orch):
    uid = _uid()
    ws = _register(orch, uid)
    orch._ff_llm_first_run = False

    count = await llm_gate.regate_after_clear(orch, uid)
    assert count == 0
    assert orch.sent == []

    handled = await chrome_events.handle_chrome_event(
        orch, ws, "chrome_open", {"surface": "theme"}, uid)
    assert handled is True
    assert orch._record_llm_unconfigured.await_count == 1
    assert orch._record_llm_unconfigured.call_args.kwargs["feature"] == "chrome:chrome_open"


async def test_regate_after_clear_pushes_when_flag_on(orch):
    uid = _uid()
    ws = _register(orch, uid)

    count = await llm_gate.regate_after_clear(orch, uid)

    assert count == 1
    frames = _frames(orch, "chrome_render")
    assert frames and 'data-mandatory="1"' in frames[-1][1]["html"]
    assert orch._ws_llm_gated.get(id(ws)) is True


async def test_failing_provider_does_not_regate_configured_user(orch):
    uid = _uid()
    ws = _register(orch, uid)
    await _seed(orch, uid, base_url="https://provider-down.invalid/v1",
                api_key="sk-revoked-key-000000000000000000000")
    try:
        assert await orch.llm_configured_for(uid) is True
        assert await chrome_events._llm_gate_refusal(
            orch, ws, "chrome_open", uid) is False
        client, source, resolved = await orch._resolve_llm_client_for(ws)
        assert source == orch._CredentialSource.USER
        assert resolved.base_url == "https://provider-down.invalid/v1"
        assert _frames(orch, "chrome_render") == []
        assert _frames(orch, "chrome_surface") == []
    finally:
        await orch._llm_store.clear(uid)


def test_render_modal_shell_mandatory_variant():
    from webrender.chrome import render_modal_shell

    html = render_modal_shell("Set up your AI provider", "<p>body</p>",
                              "llm", mandatory=True)
    assert 'data-mandatory="1"' in html
    assert "astral-modal-close" not in html
    assert '<a href="/auth/logout"' in html and "Sign out" in html
    assert 'data-surface="llm"' in html


def test_render_modal_shell_default_variant_keeps_close_button():
    from webrender.chrome import render_modal_shell

    html = render_modal_shell("LLM settings", "<p>body</p>", "llm")
    assert "data-mandatory" not in html
    assert "astral-modal-close" in html
    assert "/auth/logout" not in html
