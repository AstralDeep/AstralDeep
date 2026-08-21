"""Real tests for the three companion wirings:
  * voice/aom render-target dispatch (C-D4/C-D5) via target_for_profile,
  * transaction_token mint↔verify round-trip (C-S8) via mint_action_token,
  * model_router on-device lane (C-D6) surfaced as _last_route_ondevice.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _voice_profile():
    from rote.capabilities import DeviceCapabilities, DeviceProfile
    return DeviceProfile._derive(DeviceCapabilities(device_type="voice",
                                                    viewport_width=0, viewport_height=0))


# --------------------------------------------------------------------------- #
# voice / aom render-target dispatch (C-D4 / C-D5)
# --------------------------------------------------------------------------- #

def test_target_for_profile_gating(monkeypatch):
    from webrender import target_for_profile
    vp = _voice_profile()
    monkeypatch.setenv("FF_NATIVE_TARGETS", "false")
    assert target_for_profile(vp) == "web"           # default ⇒ web, unchanged
    monkeypatch.setenv("FF_NATIVE_TARGETS", "true")
    assert target_for_profile(vp) == "voice"          # voice device ⇒ SSML target


def test_target_for_profile_explicit_aom(monkeypatch):
    from webrender import target_for_profile
    monkeypatch.setenv("FF_NATIVE_TARGETS", "true")
    prof = MagicMock()
    prof.render_target = "aom"
    prof.device_type = "browser"
    assert target_for_profile(prof) == "aom"          # explicit AOM target honored


def test_voice_target_renders_ssml():
    from webrender import render_for_target
    out = render_for_target("voice", [{"type": "text", "content": "Hello there"}], _voice_profile())
    assert isinstance(out, str) and out                # voice renderer reachable, emits text
    aom = render_for_target("aom", [{"type": "text", "content": "Hi"}], None)
    assert aom is not None                             # aom renderer reachable


# --------------------------------------------------------------------------- #
# transaction_token mint ↔ verify round-trip (C-S8)
# --------------------------------------------------------------------------- #

def test_mint_action_token_round_trip(monkeypatch):
    monkeypatch.setenv("TXN_TOKEN_KEY", "test-signing-key-123")
    from orchestrator.orchestrator import Orchestrator
    from orchestrator import transaction_token as txn

    o = Orchestrator.__new__(Orchestrator)
    agent, user, tool = "a-1", "u-1", "send_email"
    args = {"to": "bob@example.com", "body": "hi"}

    token = o.mint_action_token(agent, user, tool, args)
    assert token, "mint must issue a token when a signing key is configured"

    store = txn.default_store()
    ok, _ = txn.verify_and_consume(store, token, agent, user, tool, args)
    assert ok is True                                  # the gate accepts the minted token
    ok2, why = txn.verify_and_consume(store, token, agent, user, tool, args)
    assert ok2 is False                                # single-use: replay rejected


# --------------------------------------------------------------------------- #
# model_router on-device lane (C-D6) — _last_route_ondevice is set
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_model_router_ondevice_surfaced(monkeypatch, orchestrator_factory):
    monkeypatch.setenv("FF_MODEL_ROUTER", "true")
    from orchestrator import model_router

    o = await asyncio.to_thread(orchestrator_factory)
    user_id = f"companions-user-{uuid.uuid4().hex}"

    def fake_route(feature, *, default_model, device_type=None, device_caps=None):
        return model_router.RouteDecision(model=default_model, tier=1, ondevice=True)

    monkeypatch.setattr("orchestrator.model_router.route", fake_route)

    try:
        o._record_llm_call = AsyncMock()
        o._record_llm_unconfigured = AsyncMock()
        o._emit_llm_usage_report = AsyncMock()
        await o._llm_store.set(
            user_id,
            provider="custom",
            base_url="http://test.invalid/v1",
            model="test-model",
            api_key="test-key",
        )

        completions = MagicMock()
        completions.create.side_effect = ValueError("no network in test")
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions),
        )

        def failing_client(config, source):
            assert config is not None
            assert config.model == "test-model"
            assert source is o._CredentialSource.USER
            return (
                client,
                source,
                o._ResolvedConfig(
                    base_url=config.base_url,
                    model=config.model,
                ),
            )

        o._build_llm_client = failing_client
        ws = MagicMock()
        o.ui_sessions[ws] = {
            "sub": user_id,
            "preferred_username": user_id,
        }

        result = await o._call_llm(
            ws,
            [{"role": "user", "content": "hi"}],
        )

        assert result == (None, None)
        assert getattr(o, "_last_route_ondevice", None) is True
        completions.create.assert_called_once()
        assert completions.create.call_args.kwargs["model"] == "test-model"
    finally:
        try:
            await o._llm_store.clear(user_id)
        finally:
            await asyncio.wait_for(o._close_started_services(), timeout=15.0)
