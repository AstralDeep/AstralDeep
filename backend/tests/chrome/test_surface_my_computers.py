"""Verifies the My computers native components keep controls and consent while dropping introductions.
Host inventory and the local-computer descriptor are replaced with deterministic doubles.
"""

import asyncio
import json

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
