"""Tests that orchestrator.py's register_ui sends the server default theme (Graphite preset and
colors from projection_surfaces/theme.py) when nothing is stored, and a stored theme unchanged.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.external_identity_links import PREFERENCES_KEY
from orchestrator.projection_surfaces import theme
from tests.test_register_ui_auth_required import (
    _FakeWS,
    _make_fake,
    _register_msg,
    _run_and_drain,
    auth_audit as auth_audit,
)


def _preferences_frames(fake):
    return [payload for _ws, payload in fake._sent if payload["type"] == "user_preferences"]


def _register(fake):
    ws = _FakeWS("theme")
    fake._registered_events[id(ws)] = asyncio.Event()
    _run_and_drain(fake.handle_ui_message(ws, _register_msg(token="dev-token")))


@pytest.fixture
def mock_auth(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")


def test_registration_without_a_stored_theme_sends_the_graphite_preset_and_colors(auth_audit, mock_auth):
    fake = _make_fake()
    _register(fake)
    frame, = _preferences_frames(fake)
    assert frame["preferences"] == theme.default_theme_preferences()
    assert frame["preferences"]["theme"]["preset"] == "graphite"
    assert frame["preferences"]["theme"]["colors"] == theme.PRESETS["graphite"]


def test_registration_with_a_stored_midnight_theme_sends_it_unchanged(auth_audit, mock_auth):
    fake = _make_fake()
    stored = {"theme": {"preset": "midnight"}, PREFERENCES_KEY: {"orcid": {"subject": "x"}}}
    fake._load_user_preferences = lambda _uid: stored
    _register(fake)
    frame, = _preferences_frames(fake)
    assert frame["preferences"] == {"theme": {"preset": "midnight"}}


def test_registration_with_only_other_preferences_still_gets_the_default_theme(auth_audit, mock_auth):
    fake = _make_fake()
    fake._load_user_preferences = lambda _uid: {PREFERENCES_KEY: {"orcid": {"subject": "x"}}}
    _register(fake)
    frame, = _preferences_frames(fake)
    assert frame["preferences"] == theme.default_theme_preferences()


def test_registration_sends_no_theme_when_preferences_cannot_be_read(auth_audit, mock_auth):
    fake = _make_fake()

    def unavailable(_uid):
        raise RuntimeError("synthetic preference store outage")

    fake._load_user_preferences = unavailable
    _register(fake)
    assert _preferences_frames(fake) == []
