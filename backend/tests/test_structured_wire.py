"""Tests for the astralprims structured component tree as the canonical intermediate:
ROTE adapts it before webrender renders it, and the tree stays a plain,
JSON-serializable dict for non-web consumers.
"""

import astralprims as ap
import webrender
from rote.rote import ROTE
from rote.capabilities import DeviceType


def test_adapt_then_render_ordering_on_a_watch_profile():
    rote = ROTE()

    class _WS:
        pass
    ws = _WS()
    rote.register_device(ws, {"device_type": "watch", "viewport_width": 180})
    profile = rote.get_profile(ws)
    assert profile.device_type == DeviceType.WATCH and profile.supports_charts is False

    components = [ap.BarChart(title="B", labels=["a", "b"], datasets=[{"data": [1, 2]}]).to_dict()]
    adapted = rote.adapt(ws, components)
    assert all(c.get("type") != "bar_chart" for c in adapted)
    html = webrender.render_for_target("web", adapted, profile)
    assert isinstance(html, str)
    assert 'data-chart-type="bar"' not in html


def test_browser_profile_is_passthrough_and_renders_chart():
    rote = ROTE()

    class _WS:
        pass
    ws = _WS()
    rote.register_device(ws, {"device_type": "browser", "viewport_width": 1920})
    components = [ap.BarChart(labels=["a"], datasets=[{"data": [1]}]).to_dict()]
    adapted = rote.adapt(ws, components)
    assert adapted == components
    html = webrender.render_for_target("web", adapted, rote.get_profile(ws))
    assert 'data-chart-type="bar"' in html


def test_structured_components_are_plain_serializable_dicts():
    tree = ap.Card(title="t", content=[ap.Text(content="x"), ap.Table(headers=["A"], rows=[["1"]])]).to_dict()
    import json
    assert json.loads(json.dumps(tree)) == tree
    assert tree["type"] == "card" and tree["content"][1]["type"] == "table"
