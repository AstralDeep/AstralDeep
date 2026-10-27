"""Verifies the My computers native components keep controls and consent while dropping introductions.
Host inventory and the local-computer descriptor are replaced with deterministic doubles.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator.projection_surfaces import my_computers


def run(coro):
    return asyncio.run(coro)


def _host(online=True):
    return {"host_id": "h1", "name": "Studio", "platform": "macos", "online": online,
            "last_seen": 0, "screens": [{"width": 100, "height": 50}], "session": None}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(my_computers, "_enabled", lambda: True)


def _components(monkeypatch, rows, local):
    monkeypatch.setattr(my_computers, "_rows", lambda orch, user: rows)
    monkeypatch.setattr(my_computers, "_this_computer", lambda orch, user, params: local)
    return run(my_computers.components(object(), "u1", ["user"], {}))


def _texts(components):
    return [node["content"] for node in components if node.get("type") == "text"]


def test_disabled_keeps_the_state_message(monkeypatch):
    monkeypatch.setattr(my_computers, "_enabled", lambda: False)
    components = run(my_computers.components(object(), "u1", ["user"], {}))
    assert _texts(components) == [my_computers._DISABLED_MSG]


def test_empty_inventory_and_introduction_are_not_drawn(enabled, monkeypatch):
    assert _components(monkeypatch, [], None) == []


def test_remote_control_on_card_keeps_badge_and_stop_without_explanation(enabled, monkeypatch):
    components = _components(monkeypatch, [], {"enabled": True, "name": "Studio", "host_id": "h1"})
    card, = components
    assert card["title"] == "This computer"
    kinds = [child["type"] for child in card["content"]]
    assert kinds == ["badge", "button"]
    assert card["content"][1]["payload"] == {"enabled": False}


def test_remote_control_off_card_keeps_only_the_safety_line_beside_allow(enabled, monkeypatch):
    components = _components(monkeypatch, [], {"enabled": False, "name": None, "host_id": None})
    card, = components
    texts = [child["content"] for child in card["content"] if child["type"] == "text"]
    assert len(texts) == 1 and texts[0].startswith("You stay in control")
    assert "Switch this on" not in texts[0]
    button, = [child for child in card["content"] if child["type"] == "button"]
    assert button["payload"] == {"enabled": True}


def test_listed_computers_keep_controls(enabled, monkeypatch):
    components = _components(monkeypatch, [_host()], None)
    blob = json.dumps(components)
    assert "Control this computer" in blob
    assert "No computers yet" not in blob and "Computers that run" not in blob


class _Session:
    host_name = "Studio"


class _LocallyPausedSessions:
    async def resume(self, session):
        from orchestrator.computer_hosts import ComputerHostError
        raise ComputerHostError("local_pause_requires_local_resume", "paused locally")


class _Orch:
    computer_sessions = _LocallyPausedSessions()


def test_remote_resume_of_a_local_pause_names_the_mac(enabled, monkeypatch):
    monkeypatch.setattr(my_computers, "_owned_session", lambda orch, user, payload: _Session())
    _, _, notice = run(my_computers._h_session_resume(_Orch(), None, "u1", ["user"], {"session_id": "s1"}))
    assert "text-red-400" in notice
    assert "Studio was paused on that Mac. Resume it there." in notice


class _InputPausedSessions:
    async def resume(self, session):
        from orchestrator.computer_hosts import ComputerHostError
        raise ComputerHostError("local_resume_required", "paused by local input")


class _InputPausedOrch:
    computer_sessions = _InputPausedSessions()


def test_remote_resume_of_a_local_input_pause_is_refused_with_a_notice(enabled, monkeypatch):
    monkeypatch.setattr(my_computers, "_owned_session", lambda orch, user, payload: _Session())
    _, _, notice = run(my_computers._h_session_resume(_InputPausedOrch(), None, "u1", ["user"], {"session_id": "s1"}))
    assert "text-red-400" in notice
    assert "Studio was paused at that computer. Resume it there." in notice


class _BrokenSessions:
    async def resume(self, session):
        from orchestrator.computer_hosts import ComputerHostError
        raise ComputerHostError("failed", "other")


class _BrokenOrch:
    computer_sessions = _BrokenSessions()


def test_other_resume_failures_still_propagate(enabled, monkeypatch):
    from orchestrator.computer_hosts import ComputerHostError
    monkeypatch.setattr(my_computers, "_owned_session", lambda orch, user, payload: _Session())
    with pytest.raises(ComputerHostError):
        run(my_computers._h_session_resume(_BrokenOrch(), None, "u1", ["user"], {"session_id": "s1"}))


def _paused(reason):
    return {**_host(), "session": {"session_id": "s1", "state": "paused", "pause_reason": reason,
                                   "controller_label": "Mac", "verbs_run": 0}}


@pytest.mark.parametrize("reason,offered", [("local_input", False), ("local_pause", False),
                                            ("user_pause", True), (None, True)])
def test_resume_is_offered_only_for_remotely_resumable_pauses(enabled, monkeypatch, reason, offered):
    blob = json.dumps(_components(monkeypatch, [_paused(reason)], None))
    assert ("chrome_computer_session_resume" in blob) is offered
    assert "chrome_computer_session_stop" in blob
    monkeypatch.setattr(my_computers, "_this_computer", lambda orch, user, params: None)
    html = run(my_computers.render(object(), "u1", ["user"], {}))
    assert ("chrome_computer_session_resume" in html) is offered
    assert "chrome_computer_session_stop" in html


class _RevokingSessions:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    async def revoke_grant(self, *args):
        self.calls.append(args)
        return self.result


_REVOKE = {"grant_id": "g1", "session_id": "s1", "host_id": "h1", "chat_id": "c1"}


def _revoke(payload, result=True):
    orch = SimpleNamespace(computer_sessions=_RevokingSessions(result))
    key, params, notice = run(my_computers.HANDLERS["chrome_computer_grant_revoke"](
        orch, None, "u1", ["user"], payload))
    return orch.computer_sessions.calls, key, notice


def test_grant_revoke_names_owner_session_host_chat_and_grant(enabled):
    calls, key, notice = _revoke({**_REVOKE, "surface": "my_computers"})
    assert calls == [("u1", "s1", "h1", "c1", "g1")] and key == "my_computers"
    assert "text-green-400" in notice and "Grant revoked." in notice


@pytest.mark.parametrize("payload", [{}, {**_REVOKE, "grant_id": ""}, {**_REVOKE, "chat_id": None},
                                     {**_REVOKE, "host_id": "h" * 257}, {**_REVOKE, "extra": 1},
                                     {k: v for k, v in _REVOKE.items() if k != "session_id"}, "not-a-dict"])
def test_grant_revoke_refuses_malformed_payloads_without_touching_grants(enabled, payload):
    calls, _key, notice = _revoke(payload)
    assert calls == [] and "That grant is not active." in notice


def test_grant_revoke_of_an_unknown_or_foreign_grant_is_a_bounded_error(enabled):
    calls, _key, notice = _revoke(dict(_REVOKE), result=False)
    assert len(calls) == 1 and "text-red-400" in notice and "That grant is not active." in notice


def test_grant_revoke_is_refused_while_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(my_computers, "_enabled", lambda: False)
    calls, _key, notice = _revoke(dict(_REVOKE))
    assert calls == [] and my_computers._DISABLED_MSG in notice


def test_a_reregistered_mac_shows_once(enabled, monkeypatch):
    from tests.test_computer_use_076 import OWNER, _FakeOrch, _host_named

    orch = _FakeOrch()
    _host_named(orch, "RyzenRoll Mobile", online=False, age_s=60)
    _host_named(orch, "RyzenRoll Mobile")
    monkeypatch.setattr(my_computers, "_this_computer", lambda orch, user, params: None)
    cards = run(my_computers.components(orch, OWNER, ["user"], {}))
    assert [card["title"] for card in cards] == ["RyzenRoll Mobile"]
    assert "offline" not in json.dumps(cards[0]["content"][0])
    html = run(my_computers.render(orch, OWNER, ["user"], {}))
    assert html.count("RyzenRoll Mobile") == 1 and "1 online · 1 total" in html
