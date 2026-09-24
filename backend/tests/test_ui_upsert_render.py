"""Tests for webrender's component-fragment rendering (data-component-id wrapping,
escaping, legacy parity) and shared/protocol.py's UIUpsert/AuthRequired round-trip
plus legacy UIRender/UIUpdate parsing.
"""

import json

from shared.protocol import (
    AuthRequired,
    Message,
    UIRender,
    UIUpdate,
    UIUpsert,
)
from webrender import render, render_component_fragment, render_one, render_workspace


def _card(title: str, body: str, **extra):
    comp = {
        "type": "card",
        "title": title,
        "content": [{"type": "text", "variant": "body", "content": body}],
    }
    comp.update(extra)
    return comp


def test_fragment_with_component_id_wraps_card_markup(monkeypatch):
    monkeypatch.setenv("FF_PROVENANCE_SURFACING", "false")
    monkeypatch.setenv("FF_A11Y", "false")
    comp = _card("Vitals", "all good", component_id="wc_x")
    out = render_component_fragment(comp)
    assert out.startswith('<div class="astral-component" data-component-id="wc_x">')
    assert out.endswith("</div>")
    inner = render_one(comp)
    assert inner
    assert out == f'<div class="astral-component" data-component-id="wc_x">{inner}</div>'


def test_fragment_without_component_id_is_legacy_parity(monkeypatch):
    monkeypatch.setenv("FF_PROVENANCE_SURFACING", "false")
    comp = _card("Vitals", "all good")
    out = render_component_fragment(comp)
    assert out == render_one(comp)
    assert "astral-component" not in out
    assert "data-component-id" not in out


def test_fragment_non_dict_renders_empty():
    assert render_component_fragment(None) == ""
    assert render_component_fragment("text") == ""


def test_component_id_attribute_is_escaped():
    hostile = '"x"><script>alert(1)</script>'
    comp = _card("T", "b", component_id=hostile)
    out = render_component_fragment(comp)
    assert "<script" not in out
    assert "</script>" not in out
    assert 'data-component-id="&quot;x&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in out
    assert '"x">' not in out


def test_unknown_type_in_fragment_emits_placeholder_never_raises():
    comp = {"type": "flux_capacitor", "component_id": "wc_u"}
    out = render_component_fragment(comp)
    assert 'data-component-id="wc_u"' in out
    assert "astral-unsupported" in out
    assert "flux_capacitor" in out


def test_render_workspace_wraps_each_component_in_order():
    comps = [
        _card("First", "one", component_id="wc_a"),
        _card("Second", "two", component_id="wc_b"),
    ]
    out = render_workspace(comps)
    assert out.startswith('<div class="dynamic-renderer space-y-3">')
    assert out.endswith("</div>")
    pos_a = out.find('data-component-id="wc_a"')
    pos_b = out.find('data-component-id="wc_b"')
    assert pos_a != -1 and pos_b != -1
    assert pos_a < pos_b
    plain = render(comps)
    for comp in comps:
        inner = render_one(comp)
        assert inner in out
        assert inner in plain


def test_render_workspace_without_ids_equals_render(monkeypatch):
    monkeypatch.setenv("FF_PROVENANCE_SURFACING", "false")
    comps = [_card("Plain", "no id"), {"type": "text", "content": "hello"}]
    assert render_workspace(comps) == render(comps)


def test_render_workspace_empty_and_none():
    assert render_workspace([]) == '<div class="dynamic-renderer space-y-3"></div>'
    assert render_workspace(None) == '<div class="dynamic-renderer space-y-3"></div>'


def test_ui_upsert_round_trips_through_message_from_json():
    ops = [
        {
            "op": "upsert",
            "component_id": "wc_1",
            "component": _card("Live", "v2", component_id="wc_1"),
            "html": render_component_fragment(_card("Live", "v2", component_id="wc_1")),
        },
        {"op": "remove", "component_id": "wc_2"},
    ]
    msg = UIUpsert(chat_id="c", ops=ops)
    wire = msg.to_json()
    data = json.loads(wire)
    assert data["type"] == "ui_upsert"
    assert data["chat_id"] == "c"
    assert data["ops"] == ops
    parsed = Message.from_json(wire)
    assert isinstance(parsed, UIUpsert)
    assert parsed.chat_id == "c"
    assert parsed.ops == ops


def test_auth_required_round_trips():
    wire = AuthRequired(reason="expired").to_json()
    parsed = Message.from_json(wire)
    assert isinstance(parsed, AuthRequired)
    assert parsed.type == "auth_required"
    assert parsed.reason == "expired"
    parsed_default = Message.from_json(AuthRequired().to_json())
    assert isinstance(parsed_default, AuthRequired)
    assert parsed_default.reason == "invalid"


def test_legacy_ui_render_and_ui_update_shapes_still_parse():
    comps = [{"type": "text", "content": "hi"}]
    old_render = json.dumps({"type": "ui_render", "components": comps, "target": "canvas"})
    parsed = Message.from_json(old_render)
    assert isinstance(parsed, UIRender)
    assert parsed.components == comps and parsed.target == "canvas" and parsed.html is None

    old_update = json.dumps({"type": "ui_update", "components": comps})
    parsed_u = Message.from_json(old_update)
    assert isinstance(parsed_u, UIUpdate)
    assert parsed_u.components == comps and parsed_u.html is None

    cur = UIRender(components=comps, target="chat", html="<div></div>")
    back = Message.from_json(cur.to_json())
    assert isinstance(back, UIRender)
    assert back.html == "<div></div>" and back.target == "chat" and back.components == comps
