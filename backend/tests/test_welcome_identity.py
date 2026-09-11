"""Feature 055 US1 — wel_ identities on the welcome canvas (welcome.py).

Every welcome component carries a stable ephemeral identity (BOTH ``id`` and
``component_id``, same ``wel_`` value): the web identity wrapper keys on
component_id, natives read ``component_id ?? id``, and clients purge
``wel_``-prefixed components from canvas state at turn start. With
FF_FIRST_TURN_CONTRACT off the same welcome tree is emitted without identities.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from shared.feature_flags import flags  # noqa: E402
from orchestrator.welcome import (  # noqa: E402
    WELCOME_EXAMPLES, _slug, welcome_components,
)


def _top_ids(comps):
    return [(c.get("type"), c.get("id"), c.get("component_id")) for c in comps]


def _walk(nodes):
    for node in nodes:
        yield node
        for key in ("children", "content"):
            if isinstance(node.get(key), list):
                yield from _walk(node[key])


def test_top_level_components_carry_matching_wel_ids(monkeypatch):
    monkeypatch.setitem(flags._flags, "first_turn_contract", True)
    comps = welcome_components()
    for comp in comps:
        assert comp["id"] == comp["component_id"], _top_ids(comps)
        assert comp["id"].startswith("wel_"), _top_ids(comps)
    assert comps[0]["id"] == "wel_hero"
    assert comps[1]["id"] == "wel_examples"
    assert comps[-1]["id"] == "wel_more"


def test_enable_agents_card_gets_wel_enable(monkeypatch):
    monkeypatch.setitem(flags._flags, "first_turn_contract", True)
    comps = welcome_components(tools_available=False)
    enables = [c for c in comps if c.get("id") == "wel_enable"]
    assert len(enables) == 1 and enables[0]["type"] == "card"


def test_examples_keep_unique_slug_ids_inside_both_groups(monkeypatch):
    monkeypatch.setitem(flags._flags, "first_turn_contract", True)
    buttons = [c for c in _walk(welcome_components()) if c.get("action") == "chat_message"]
    ids = [c["id"] for c in buttons]
    assert len(ids) == len(WELCOME_EXAMPLES)
    assert len(set(ids)) == len(ids), "example ids must be unique"
    assert set(ids) == {f"wel_ex_{_slug(title)}" for title, _, _ in WELCOME_EXAMPLES}
    assert all(c["id"] == c["component_id"] for c in buttons)


@pytest.mark.parametrize("device_type", [
    "browser", "windows", "android", "ios", "macos", "mobile", "watch", "voice",
])
def test_adapted_welcome_groups_remain_retirable(device_type, monkeypatch):
    from rote.adapter import ComponentAdapter
    from rote.capabilities import DeviceProfile

    monkeypatch.setitem(flags._flags, "first_turn_contract", True)
    adapted = ComponentAdapter.adapt(
        welcome_components(tools_available=False),
        DeviceProfile.from_dict({"device_type": device_type}),
    )
    assert adapted
    ids = [comp.get("component_id") for comp in adapted]
    assert len(set(ids)) == len(ids)
    assert all(cid and cid.startswith("wel_") for cid in ids)
    assert not [comp for comp in adapted if not comp["component_id"].startswith("wel_")]


def test_slug_is_deterministic_ascii():
    assert _slug("📊 Business dashboard") == "business_dashboard"
    assert _slug("⛅ Weather outlook") == "weather_outlook"
    assert _slug("🎲🎲🎲") == "example"  # emoji-only degenerates to a stable token


def test_flag_off_restores_idless_tree(monkeypatch):
    monkeypatch.setitem(flags._flags, "first_turn_contract", False)
    for comp in _walk(welcome_components(tools_available=False)):
        assert "id" not in comp and "component_id" not in comp
