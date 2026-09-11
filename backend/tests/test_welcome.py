"""Server-driven welcome canvas (orchestrator/welcome.py).

The initial-load examples are ordinary astralprims components delivered over
the normal ui_render path — renderable by the registry, adaptable by ROTE,
actionable through the standard ``chat_message`` ui_event. No shell HTML, no
client-specific code (Constitution II).
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
    assert comps[0]["type"] == "hero"
    assert comps[0]["title"] == "How can I help?"
    assert not comps[0].get("eyebrow") and not comps[0].get("subtitle")
    grid = comps[1]
    assert grid["type"] == "grid"
    assert len(grid["children"]) == 3
    assert all(child["type"] == "button" for child in grid["children"])
    more = comps[2]
    assert more["type"] == "collapsible"
    assert more["title"] == "More examples" and more["default_open"] is False
    assert len([n for n in _walk(more["content"]) if n["type"] == "button"]) == 3
    assert not any(n["type"] in {"text", "card"} for n in _walk(comps))
    assert json.dumps(comps), "wire-serializable"


def test_buttons_dispatch_standard_chat_message_action():
    buttons = [n for n in _walk(welcome_components()) if n["type"] == "button"]
    assert len(buttons) == len(WELCOME_EXAMPLES)
    queries = {b["payload"]["message"] for b in buttons}
    assert all(b["action"] == "chat_message" for b in buttons)
    assert queries == {q for _, _, q in WELCOME_EXAMPLES}
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
    consent = comps[1]
    assert consent["type"] == "card" and "Agents are off" in consent["title"]
    actions = [n for n in _walk([consent]) if n["type"] == "button"]
    assert [n["action"] for n in actions] == ["enable_recommended_agents", "chrome_open"]
    assert actions[1]["payload"] == {"surface": "agents"}
    assert "never write access" in str(consent)
    assert len([n for n in _walk(comps) if n.get("action") == "chat_message"]) == 6


def test_welcome_components_carry_no_workspace_identity():
    # 055 US1: welcome components now carry EPHEMERAL wel_ identities (so
    # clients can purge them at turn start) — the ephemerality invariant is
    # that any identity present is wel_-namespaced, which the workspace layer
    # structurally refuses to persist (see test_workspace_wel_guard.py).
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
    assert "How can I help?" in text
    for title, _, _ in WELCOME_EXAMPLES:
        assert title.split(" ", 1)[1] in text, f"example {title!r} unreadable on voice"
