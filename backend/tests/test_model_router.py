"""Feature 033 (capability C-D6) — device-capability-aware model router.

Covers the cheap-first task→tier mapping, device caps, on-device eligibility,
the escalation decision, the confidence heuristic, tier→model resolution, and
the top-level route().
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import model_router as mr  # noqa: E402
from orchestrator.model_router import (  # noqa: E402
    LARGE, MEDIUM, ONDEVICE, SMALL,
)


# ───────────────────────── flag ──────────────────────────────────────────────

def test_router_default_off(monkeypatch):
    monkeypatch.delenv("FF_MODEL_ROUTER", raising=False)
    assert mr.router_enabled() is False


@pytest.mark.parametrize("v", ["true", "1", "yes", "on"])
def test_router_on_values(monkeypatch, v):
    monkeypatch.setenv("FF_MODEL_ROUTER", v)
    assert mr.router_enabled() is True


# ───────────────────────── task → tier (cheap-first) ─────────────────────────

@pytest.mark.parametrize("feature,tier", [
    ("chat_title", SMALL), ("narrative", SMALL), ("summarize", SMALL),
    ("tool_dispatch", MEDIUM), ("ui_designer", LARGE), ("code_generation", LARGE),
    ("something_unlisted", MEDIUM), (None, MEDIUM),
])
def test_tier_for_task(feature, tier):
    assert mr.tier_for_task(feature) == tier


def test_tier_for_task_hint_overrides():
    assert mr.tier_for_task("ui_designer", hint=SMALL) == SMALL
    assert mr.tier_for_task("chat_title", hint=99) == SMALL  # invalid hint ignored


# ───────────────────────── device cap ────────────────────────────────────────

@pytest.mark.parametrize("dtype,cap", [
    ("watch", SMALL), ("voice", SMALL), ("mobile", MEDIUM),
    ("tablet", LARGE), ("browser", LARGE), (None, LARGE), ("weird", LARGE),
])
def test_device_cap_tier(dtype, cap):
    assert mr.device_cap_tier(dtype) == cap


# ───────────────────────── on-device lane ────────────────────────────────────

def test_can_use_ondevice_requires_capability_and_simple_task():
    caps = {"has_browser_ai": True}
    assert mr.can_use_ondevice(caps, "chat_title") is True      # simple + capable
    assert mr.can_use_ondevice(caps, "ui_designer") is False    # too heavy
    assert mr.can_use_ondevice({"has_browser_ai": False}, "chat_title") is False
    assert mr.can_use_ondevice(None, "chat_title") is False


def test_can_use_ondevice_reads_object_attr():
    class Caps:
        has_browser_ai = True
    assert mr.can_use_ondevice(Caps(), "summarize") is True


# ───────────────────────── escalation + confidence ───────────────────────────

def test_escalate_climbs_then_stops():
    assert mr.escalate(SMALL) == MEDIUM
    assert mr.escalate(MEDIUM) == LARGE
    assert mr.escalate(LARGE) is None
    assert mr.escalate(None) is None


@pytest.mark.parametrize("text,ok", [
    ("Here is a clear, complete answer.", True),
    ("x", True),
    ("", False),
    ("   ", False),
    ("I'm not sure, but maybe?", False),
    ("As an AI, I cannot help with that.", False),
    ("There is insufficient information to answer.", False),
])
def test_confidence_ok(text, ok):
    assert mr.confidence_ok(text) is ok


def test_confidence_min_chars():
    assert mr.confidence_ok("short", min_chars=10) is False


# ───────────────────────── tier → model resolution ───────────────────────────

def test_resolve_model_falls_back_to_default():
    assert mr.resolve_model(MEDIUM, "default-model", tier_map={}) == "default-model"


def test_resolve_model_uses_tier_map():
    tm = {SMALL: "tiny", LARGE: "huge"}
    assert mr.resolve_model(SMALL, "d", tier_map=tm) == "tiny"
    assert mr.resolve_model(MEDIUM, "d", tier_map=tm) == "d"  # unmapped → default


def test_env_tier_map(monkeypatch):
    monkeypatch.setenv("MODEL_TIERS", '{"small":"s-8b","medium":"m-70b","large":"l-405b"}')
    assert mr.resolve_model(SMALL, "d") == "s-8b"
    assert mr.resolve_model(LARGE, "d") == "l-405b"


def test_env_tier_map_bad_json_falls_back(monkeypatch):
    monkeypatch.setenv("MODEL_TIERS", "{not json")
    assert mr.resolve_model(SMALL, "default") == "default"


# ───────────────────────── route() ───────────────────────────────────────────

def test_route_cheap_first_and_device_cap():
    tm = {SMALL: "s", MEDIUM: "m", LARGE: "l"}
    # a heavy task on a browser → LARGE
    d = mr.route("ui_designer", default_model="def", device_type="browser", tier_map=tm)
    assert d.tier == LARGE and d.model == "l"
    # the SAME heavy task on a watch is capped to SMALL
    d2 = mr.route("ui_designer", default_model="def", device_type="watch", tier_map=tm)
    assert d2.tier == SMALL and d2.model == "s"


def test_route_floors_at_small_and_defaults_model():
    d = mr.route("chat_title", default_model="def", device_type="browser", tier_map={})
    assert d.tier == SMALL and d.model == "def"   # ONDEVICE never selected as a server tier
    assert d.tier >= SMALL


def test_route_sets_ondevice_hint():
    d = mr.route("chat_title", default_model="def", device_type="mobile",
                 device_caps={"has_browser_ai": True})
    assert d.ondevice is True
    assert ONDEVICE < d.tier  # server still resolves a real tier as fallback


def test_env_tier_map_bad_json_warns_once(monkeypatch, caplog):
    """A malformed MODEL_TIERS must not be silently ignored — one WARNING per
    process (not per call), and the raw value never appears in the record."""
    import logging
    monkeypatch.setattr(mr, "_BAD_TIER_MAP_WARNED", False)
    monkeypatch.setenv("MODEL_TIERS", "{not json secret-looking}")
    with caplog.at_level(logging.WARNING, logger="orchestrator.model_router"):
        assert mr.resolve_model(SMALL, "default") == "default"
        assert mr.resolve_model(LARGE, "default") == "default"
    warns = [r for r in caplog.records if r.levelno == logging.WARNING
             and "MODEL_TIERS" in r.getMessage()]
    assert len(warns) == 1
    assert "secret-looking" not in warns[0].getMessage()
