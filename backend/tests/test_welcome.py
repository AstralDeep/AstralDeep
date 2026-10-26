"""Tests for orchestrator/welcome.py's server-driven welcome canvas: every example
renders through the normal astralprims/ROTE/ui_event path, the first screen discloses
three choices, and unavailable tools stay separate from example buttons.
"""

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.welcome import WELCOME_EXAMPLES, welcome_components  # noqa: E402


def _walk(nodes):
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node
        for key in ("children", "content"):
            nested = node.get(key)
            if isinstance(nested, list):
                yield from _walk(nested)


def test_every_type_is_renderable():
    import webrender

    allowed = webrender.allowed_primitive_types()
    comps = welcome_components()
    types = {n["type"] for n in _walk(comps)}
    assert types <= allowed, f"non-renderable welcome types: {types - allowed}"


def test_first_screen_has_three_choices_and_discloses_the_rest():
    comps = welcome_components()
    assert [node["type"] for node in comps] == ["grid", "collapsible"]
    grid, more = comps
    assert len(grid["children"]) == 3
    assert all(child["type"] == "button" for child in grid["children"])
    assert more["title"] == "" and more["default_open"] is False
    assert more["aria-label"] == "More examples"
    assert (len([n for n in _walk(more["content"]) if n["type"] == "button"])
            == len(WELCOME_EXAMPLES) - 3)
    assert not any(n["type"] in {"text", "card", "hero"} for n in _walk(comps))
    assert "How can I help?" not in json.dumps(comps)
    assert json.dumps(comps), "wire-serializable"


def test_buttons_dispatch_standard_chat_message_action():
    buttons = [n for n in _walk(welcome_components()) if n["type"] == "button"]
    assert len(buttons) == len(WELCOME_EXAMPLES)
    queries = {b["payload"]["message"] for b in buttons}
    assert all(b["action"] == "chat_message" for b in buttons)
    assert queries == {q for _, q in WELCOME_EXAMPLES}
    assert all(q.strip() for q in queries)


def test_example_names_are_unique_visible_and_accessible():
    import webrender

    buttons = [n for n in _walk(welcome_components()) if n["type"] == "button"]
    labels = [button["label"] for button in buttons]
    assert len(set(labels)) == len(WELCOME_EXAMPLES)
    for button in buttons:
        assert button["aria-label"] == button["label"]
        assert "Run example" not in button["label"]
        html = webrender.render_one(button)
        assert f'aria-label="{button["label"]}"' in html
        assert f'>{button["label"]}</button>' in html


def test_unavailable_tools_keep_explicit_consent_separate_from_examples():
    comps = welcome_components(tools_available=False)
    consent = comps[0]
    assert consent["type"] == "card" and "Agents are off" in consent["title"]
    actions = [n for n in _walk([consent]) if n["type"] == "button"]
    assert [n["action"] for n in actions] == ["enable_recommended_agents", "chrome_open"]
    assert actions[1]["payload"] == {"surface": "agents"}
    assert "never write access" in str(consent)
    assert (len([n for n in _walk(comps) if n.get("action") == "chat_message"])
            == len(WELCOME_EXAMPLES))


def test_welcome_components_carry_no_workspace_identity():
    for node in _walk(welcome_components()):
        cid = node.get("component_id")
        if cid is not None:
            assert str(cid).startswith("wel_"), \
                f"non-ephemeral welcome identity: {cid!r}"


def test_voice_profile_gets_readable_text():
    from rote.adapter import ComponentAdapter

    text = " ".join(
        ComponentAdapter._extract_text(c) for c in welcome_components()
    )
    for title, _ in WELCOME_EXAMPLES:
        assert title in text, f"example {title!r} unreadable on voice"


def test_landing_scenarios_send_prompts_without_captions():
    from orchestrator import web_landing

    items = web_landing.scenarios()
    assert [(item["title"], item["prompt"]) for item in items] == list(WELCOME_EXAMPLES)
    assert all(item["description"] == "" for item in items)
    assert all(item["category"] in web_landing.categories(items) for item in items)
