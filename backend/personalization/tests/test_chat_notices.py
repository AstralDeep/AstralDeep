"""Tests for personalization/chat_notices.py: public-location queries never trigger a
PHI notice while storage stays gated, notice-once-per-chat behavior, preference
scoping, and a real Plane round-trip for stored notice choices.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from personalization import chat_notices as notices
from personalization.phi_gate import PHIGate


class Preferences:
    def __init__(self):
        self.choices = {}

    def get_chat_phi_notice_enabled(self, *, owner_id):
        return self.choices.get(owner_id, True)

    def set_chat_phi_notice_enabled(self, *, owner_id, enabled):
        self.choices[owner_id] = enabled


def make_orch():
    repo = Preferences()
    return SimpleNamespace(
        chat_notice_preference_context=SimpleNamespace(
            repository=repo, call=lambda fn, **kw: fn(**kw)),
        send_ui_render=AsyncMock(),
    )


class Analyzer:
    def __init__(self, entities):
        self.entities = entities
        self.requests = []

    def analyze(self, **kwargs):
        self.requests.append(kwargs)
        return [SimpleNamespace(entity_type=entity) for entity in self.entities]


def test_public_weather_location_is_not_a_notice_but_storage_gate_stays_closed():
    analyzer = Analyzer(["LOCATION"])
    gate = PHIGate(analyzer=analyzer)
    message = "What's the weather forecast for Lexington, KY this week? Show it with charts"
    assert not gate.detect_for_notice(message)
    assert "LOCATION" not in analyzer.requests[-1]["entities"]
    assert gate.contains_phi(message)
    assert "LOCATION" in analyzer.requests[-1]["entities"]


@pytest.mark.parametrize("entities", [["PERSON"], ["LOCATION", "PERSON"], ["US_SSN"]])
def test_identifying_notice_signals_remain(entities):
    assert PHIGate(analyzer=Analyzer(entities)).detect_for_notice("identifying narrative")


def test_preferences_are_owner_scoped_and_survive_new_call():
    orch = make_orch()
    assert notices.notices_enabled(orch, "alice")
    notices.set_notices_enabled(orch, "alice", False)
    assert not notices.notices_enabled(orch, "alice")
    assert notices.notices_enabled(orch, "bob")
    notices.set_notices_enabled(orch, "alice", True)
    assert notices.notices_enabled(orch, "alice")


def test_real_composition_context_binds_existing_catalog():
    repo, runtime = object(), object()
    orch = SimpleNamespace(plane_repository_source=SimpleNamespace(
        plane_runtime=runtime, plane_repositories=SimpleNamespace(preferences=repo)))
    context = notices._context(orch)
    assert context.repository is repo and context.plane_runtime is runtime


def test_enabled_notice_once_per_chat_and_no_content_audit(monkeypatch):
    from orchestrator.orchestrator import Orchestrator

    orch, socket = make_orch(), object()
    monkeypatch.setattr(notices, "get_phi_gate", lambda: PHIGate(analyzer=Analyzer(["PERSON"])))
    for _ in range(2):
        asyncio.run(Orchestrator._notify_phi_if_detected(orch, socket, "chat", "alice", "Jane Doe"))
    orch.send_ui_render.assert_awaited_once()
    args = orch.send_ui_render.await_args
    assert args.kwargs == {"target": "chat"}
    assert args.args[1][0]["title"] == "Possible PHI in your message"
    assert "Jane Doe" not in str(args)


@pytest.mark.parametrize("chat,user,message", [("", "alice", "x"), ("c", "", "x"), ("c", "a", "")])
def test_missing_identity_or_text_never_notifies(chat, user, message):
    orch = make_orch()
    asyncio.run(notices.notify_if_detected(orch, object(), chat, user, message))
    orch.send_ui_render.assert_not_called()


def test_disabled_preference_skips_detector(monkeypatch):
    orch = make_orch()
    notices.set_notices_enabled(orch, "alice", False)
    def unexpected():
        raise AssertionError("detector must not run")
    monkeypatch.setattr(notices, "get_phi_gate", unexpected)
    asyncio.run(notices.notify_if_detected(orch, object(), "chat", "alice", "123-45-6789"))
    orch.send_ui_render.assert_not_called()


def test_clean_message_and_storage_failure_are_nonblocking(monkeypatch):
    orch = make_orch()
    monkeypatch.setattr(notices, "get_phi_gate", lambda: PHIGate(analyzer=Analyzer([])))
    asyncio.run(notices.notify_if_detected(orch, object(), "chat", "alice", "hello"))
    orch.send_ui_render.assert_not_called()
    del orch.chat_notice_preference_context
    asyncio.run(notices.notify_if_detected(orch, object(), "chat", "alice", "hello"))
    orch.send_ui_render.assert_not_called()


def test_opt_out_during_analysis_is_respected(monkeypatch):
    orch = make_orch()
    def detect(_):
        notices.set_notices_enabled(orch, "alice", False)
        return True
    monkeypatch.setattr(notices, "get_phi_gate", lambda: SimpleNamespace(detect_for_notice=detect))
    asyncio.run(notices.notify_if_detected(orch, object(), "chat", "alice", "identifier"))
    orch.send_ui_render.assert_not_called()


def test_notice_choice_persists_through_plane_and_preserves_other_preferences():
    from tests.helpers.voice_plane_runtime import isolated_plane_runtime

    with isolated_plane_runtime("notice_review") as runtime:
        runtime.execute(
            "INSERT INTO user_preferences (user_id, preferences, updated_at) VALUES (%s, %s, %s)",
            ("alice", '{"unrelated":{"keep":true}}', 1),
        )
        def owner_context():
            return SimpleNamespace(plane_repository_source=SimpleNamespace(
                plane_runtime=runtime, plane_repositories=runtime.repositories,
            ))
        assert notices.notices_enabled(owner_context(), "alice")
        notices.set_notices_enabled(owner_context(), "alice", False)
        assert not notices.notices_enabled(owner_context(), "alice")
        assert notices.notices_enabled(owner_context(), "bob")
        notices.set_notices_enabled(owner_context(), "bob", False)
        notices.set_notices_enabled(owner_context(), "alice", True)
        assert notices.notices_enabled(owner_context(), "alice")
        assert not notices.notices_enabled(owner_context(), "bob")
        import json
        row = runtime.fetch_one("SELECT preferences FROM user_preferences WHERE user_id = %s", ("alice",))
        assert json.loads(row["preferences"])["unrelated"] == {"keep": True}
