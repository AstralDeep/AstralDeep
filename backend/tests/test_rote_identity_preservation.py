"""Feature 055 US1 — identity survives ROTE degrade/collapse rebuilds.

Degraded components must stay addressable: clients key canvases (and purge
wel_ welcome components) by ``component_id ?? id``, and ui_upsert morphs
target the same identity — a hero degraded to text on the watch, or a grid
collapsed to a container, previously lost both fields and keyed as anon-N.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from rote.adapter import ComponentAdapter  # noqa: E402
from rote.capabilities import DeviceProfile  # noqa: E402


#: the 10-type native set real watch clients advertise in register_ui
#: (Projection Apple AstralCore Dispositions.swift watchNativeComponentTypes)
_WATCH_TYPES = ["alert", "badge", "card", "container", "divider",
                "keyvalue", "list", "metric", "progress", "text"]


def _watch(advertise: bool = True) -> DeviceProfile:
    payload = {"device_type": "watch"}
    if advertise:
        payload["supported_types"] = _WATCH_TYPES
    return DeviceProfile.from_dict(payload)


def _identified(comp_type: str, ident: str, **kw):
    return {"type": comp_type, "id": ident, "component_id": ident, **kw}


def test_hero_degraded_on_watch_keeps_identity():
    out = ComponentAdapter.adapt(
        [_identified("hero", "wel_hero", title="Welcome", subtitle="hi")], _watch())
    assert out[0]["type"] != "hero"
    assert out[0]["id"] == "wel_hero"
    assert out[0]["component_id"] == "wel_hero"


def test_grid_collapse_on_watch_keeps_identity():
    grid = _identified("grid", "wel_examples", columns=2, children=[
        {"type": "card", "title": "A", "content": []},
    ])
    out = ComponentAdapter.adapt([grid], _watch())
    assert out[0]["type"] == "container"
    assert out[0]["id"] == "wel_examples"
    assert out[0]["component_id"] == "wel_examples"


def test_chart_substitution_keeps_workspace_identity():
    chart = _identified("line_chart", "wc_abc123def4567890",
                        labels=["a"], datasets=[{"label": "s", "data": [1]}])
    out = ComponentAdapter.adapt([chart], _watch())
    assert out[0]["type"] != "line_chart"
    assert out[0]["component_id"] == "wc_abc123def4567890"


def test_chart_to_metric_on_bare_profile_keeps_identity():
    # No advertised supported_types: the per-type _adapt_chart rebuild
    # (chart -> metric when supports_charts is false) must also carry identity.
    chart = _identified("line_chart", "wc_bare000000000000",
                        labels=["a"], datasets=[{"label": "s", "data": [1]}])
    out = ComponentAdapter.adapt([chart], _watch(advertise=False))
    assert out[0]["type"] == "metric"
    assert out[0]["component_id"] == "wc_bare000000000000"


def test_unidentified_components_gain_no_identity():
    out = ComponentAdapter.adapt(
        [{"type": "hero", "title": "T", "subtitle": "s"}], _watch())
    assert "id" not in out[0] and "component_id" not in out[0]
