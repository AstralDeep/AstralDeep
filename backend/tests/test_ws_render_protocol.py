"""Tests for the UI render protocol (backend/shared/protocol.py, AstralPrimitives,
AstralProjection webrender): server-rendered html carried alongside structured
components, round-tripping over the wire.
"""

import json

import astralprims as ap
import webrender
from shared.protocol import Message, UIRender, UIUpdate


def test_uirender_carries_html_and_components():
    comps = [ap.Text(content="hi").to_dict(), ap.Alert(message="m").to_dict()]
    html = webrender.render_for_target("web", comps, None)
    msg = UIRender(components=comps, target="canvas", html=html)
    wire = msg.to_json()
    data = json.loads(wire)
    assert data["type"] == "ui_render"
    assert data["target"] == "canvas"
    assert data["components"] == comps
    assert data["html"].startswith("<div class=\"dynamic-renderer")
    parsed = Message.from_json(wire)
    assert isinstance(parsed, UIRender) and parsed.html == html


def test_uiupdate_carries_html():
    comps = [ap.MetricCard(title="t", value="1").to_dict()]
    msg = UIUpdate(components=comps, html=webrender.render_for_target("web", comps, None))
    data = json.loads(msg.to_json())
    assert data["type"] == "ui_update" and data["html"] and data["components"] == comps


def test_stream_chunk_wire_shape():
    comps = [ap.Text(content="chunk").to_dict()]
    wire = {
        "type": "ui_stream_data", "stream_id": "s1", "session_id": "c1", "seq": 3,
        "components": comps, "html": webrender.render_for_target("web", comps, None),
        "raw": None, "terminal": False, "error": None,
    }
    blob = json.dumps(wire)
    back = json.loads(blob)
    assert back["html"] and back["components"] == comps and back["seq"] == 3


def test_html_absent_consumer_still_has_components():
    comps = [ap.Table(headers=["A"], rows=[["1"]]).to_dict()]
    msg = UIRender(components=comps)
    data = json.loads(msg.to_json())
    assert data["html"] is None and data["components"] == comps
