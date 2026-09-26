"""Verifies console-only guidance metadata and agent-intro actions.
The native surfaces retain existing correlation, visibility and legacy wire behavior.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator.projection_surfaces import agent_intro
from shared.protocol import ChromeSurface, ProtocolValidationError


def selection(**updates):
    return {"version": 1, "agent": None, "skills": [], "notes": [], **updates}


def surface(**updates):
    return ChromeSurface(surface_key="guidance", request_generation=str(uuid4()), **updates)


@pytest.mark.parametrize("chosen", [
    selection(), selection(agent={"agent_id": str(uuid4()), "revision_id": str(uuid4())}),
    selection(skills=[{"skill_id": str(uuid4()), "revision": 3}]),
    selection(notes=[{"note_id": str(uuid4()), "revision": 5}]),
])
def test_selection_confirmation_metadata_is_closed_and_correlated(chosen):
    frame = surface(selection=chosen)
    wire = json.loads(frame.to_json())
    assert wire["selection"] == chosen
    assert wire["request_generation"] == frame.request_generation
    assert "owner_id" not in wire["selection"]


@pytest.mark.parametrize("chosen", [
    {}, [], selection(version=True), selection(version=2), selection(owner_id="foreign"),
    selection(agent={"agent_id": "not-an-id", "revision_id": str(uuid4())}),
    selection(skills=[{"skill_id": str(uuid4()), "revision": True}]),
    selection(notes=[{"note_id": str(uuid4()), "revision": 0}]),
])
def test_malformed_selection_metadata_is_not_serialized(chosen):
    with pytest.raises(ProtocolValidationError):
        surface(selection=chosen).to_json()


@pytest.mark.parametrize("changes", [
    {"surface_key": "work"}, {"surface_key": "agents"}, {"region": "topbar"},
    {"mode": "append"}, {"request_generation": None},
])
def test_other_surface_or_uncorrelated_response_cannot_borrow_selection(changes):
    frame = surface(selection=selection())
    for key, value in changes.items():
        setattr(frame, key, value)
    with pytest.raises(ProtocolValidationError):
        frame.to_json()


def test_legacy_surface_omits_optional_selection_entirely():
    wire = json.loads(surface().to_json())
    assert "selection" not in wire


def nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from nodes(item)


@pytest.fixture
def visible(monkeypatch):
    card = SimpleNamespace(name="Dice", description="Roll dice.", skills=[], metadata={"examples": [
        {"title": "Two dice", "prompt": "Roll two six-sided dice."},
    ]})
    seen = []

    async def lookup(orch, owner, agent):
        seen.append((orch, owner, agent))
        return card, True

    monkeypatch.setattr(agent_intro, "_visible_agent", lookup)
    return card, seen


@pytest.mark.asyncio
async def test_console_agent_intro_offers_visible_example_load_and_normal_run(visible):
    card, seen = visible
    orch = object()
    output = await agent_intro.components(orch, "owner", [], {"agent_id": "dice"},
                                         console_contract="console/v2")
    assert seen == [(orch, "owner", "dice")]
    assert output[0]["console_role"] == "surface_subtitle"
    assert output[0]["content"] == agent_intro.SUBTITLE
    buttons = [node for node in nodes(output) if node.get("type") == "button"]
    load = next(button for button in buttons if button["label"] == "Load")
    run = next(button for button in buttons if button["label"] == "Run")
    assert load["action"] == "compose_prompt"
    assert run["action"] == "chat_message"
    assert load["payload"] == run["payload"] == {"message": card.metadata["examples"][0]["prompt"]}
    assert any(node.get("content") == card.metadata["examples"][0]["prompt"] for node in nodes(output))


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", ["", "console/v1", None])
async def test_legacy_agent_intro_retains_run_only_shape(visible, contract):
    output = await agent_intro.components(object(), "owner", [], {"agent_id": "dice"},
                                         console_contract=contract)
    buttons = [node for node in nodes(output) if node.get("type") == "button"]
    assert [button["action"] for button in buttons] == ["chat_message", "chrome_open"]
    assert buttons[0]["label"] == "Two dice"
    assert not any(node.get("console_role") == "surface_subtitle" for node in nodes(output))


@pytest.mark.asyncio
async def test_native_intro_fixture_matches_current_server_definition(visible):
    card, _ = visible
    fixture = json.loads((Path(__file__).parents[2] / "components/AstralProjection/contracts/fixtures/console/agent-intro.json").read_text())
    card.name = fixture["title"]
    card.description = "Rolls dice and reports every roll and the total — the smallest honest end-to-end test of routing, permissions and rendering."
    card.skills = [SimpleNamespace(id="roll_dice", name="Roll dice")]
    card.metadata["examples"] = [
        {"title": "Six dice", "prompt": "Roll exactly six six-sided dice and show the normalized results."},
        {"title": "Many rolls", "prompt": "Roll 100 six-sided dice and chart how often each face came up"},
    ]
    assert await agent_intro.components(object(), "owner", [], {"agent_id": "dice_roller"},
                                        console_contract="console/v2") == fixture["components"]


@pytest.mark.asyncio
async def test_unavailable_agent_exposes_no_example_actions(monkeypatch):
    async def hidden(*_):
        return None, False

    monkeypatch.setattr(agent_intro, "_visible_agent", hidden)
    output = await agent_intro.components(object(), "foreign", [], {"agent_id": "private"},
                                         console_contract="console/v2")
    assert [node["type"] for node in output] == ["alert"]
    assert not [node for node in nodes(output) if node.get("type") == "button"]
