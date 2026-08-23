"""Feature 033 (capability C-D3) — declarative device-fit objectives folded
into the adaptive UI designer's candidate ranking.

When ``FF_ADAPTIVE_OBJECTIVES`` is on, :func:`design_round` adds a per-candidate
device-fit bias (mean ``rote.objectives.score_adaptation`` over the component
types the arrangement renders, against the connecting device) to the ranking.
On a constrained surface a device-friendly arrangement (a glanceable hero +
grouped grid) out-ranks a flat stack of wide data views; with the flag OFF the
ranking is exactly the legacy last-wins behaviour.

These tests drive the REAL ``design_round`` / ``objectives_bias`` — the LLM is a
stub and there is no DB or network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import ui_designer  # noqa: E402
from orchestrator.ui_designer import design_round, objectives_bias  # noqa: E402

ALLOWED = {
    "container", "text", "card", "table", "list", "alert", "metric", "grid",
    "tabs", "divider", "collapsible", "bar_chart", "line_chart", "pie_chart",
    "plotly_chart", "hero", "badge", "rating", "keyvalue", "timeline",
}

# A small/constrained surface and a roomy browser, in the plain device model
# the rote.objectives module reads.
SMALL_DEVICE = {"device_type": "mobile", "is_small": True, "max_grid_columns": 1}
BROWSER_DEVICE = {"device_type": "browser", "is_small": False, "max_grid_columns": 12}

# Two wide data views (table + chart): they fit a roomy browser but not a phone.
_COMPS = [
    {"type": "table", "component_id": "A", "title": "Tbl", "_source_agent": "a", "_source_tool": "t"},
    {"type": "line_chart", "component_id": "B", "title": "Chart", "_source_agent": "a", "_source_tool": "t"},
]
# Draft: glanceable hero anchor + a grouped grid of the two data views.
_DRAFT = json.dumps({"layout": [
    {"type": "hero", "title": "Overview"},
    {"type": "grid", "columns": 2, "children": [
        {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]},
]})
# Flat refinement: just the two wide data views, no device-friendly garnish.
_FLAT = json.dumps({"layout": [
    {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]})

# component_id -> real type, as design_round builds it.
RT = {"A": "table", "B": "line_chart"}


def _stub_llm(replies):
    it = iter(replies)

    async def _call(_messages):
        try:
            return next(it)
        except StopIteration:
            return "DONE"
    return _call


# ───────────────────────── flag ──────────────────────────────────────────────

def test_objectives_bias_flag_default_off(monkeypatch):
    monkeypatch.delenv("FF_ADAPTIVE_OBJECTIVES", raising=False)
    assert ui_designer.objectives_bias_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "  TRUE  "])
def test_objectives_bias_flag_on(monkeypatch, value):
    monkeypatch.setenv("FF_ADAPTIVE_OBJECTIVES", value)
    assert ui_designer.objectives_bias_enabled() is True


# ───────────────────────── pure helper ranking ───────────────────────────────

def test_objectives_bias_prefers_device_friendly_arrangement_on_small():
    """On a phone, the hero+grid arrangement of wide data views beats the flat
    stack: the glanceable hero lifts the mean device-fit score."""
    draft = json.loads(_DRAFT)["layout"]
    flat = json.loads(_FLAT)["layout"]
    assert objectives_bias(draft, SMALL_DEVICE, RT) > objectives_bias(flat, SMALL_DEVICE, RT)


def test_objectives_bias_zero_for_empty_layout():
    assert objectives_bias([], SMALL_DEVICE, RT) == 0.0
    assert objectives_bias([{}], SMALL_DEVICE, RT) >= 0.0  # never raises


def test_objectives_bias_resolves_ref_to_real_type():
    """A bare ref scores by the REAL placed type, not 'ref': a table ref on a
    phone scores worse than a text ref."""
    table_ref = [{"type": "ref", "component_id": "A"}]
    text_ref = [{"type": "ref", "component_id": "B"}]
    rt = {"A": "table", "B": "text"}
    assert objectives_bias(text_ref, SMALL_DEVICE, rt) > objectives_bias(table_ref, SMALL_DEVICE, rt)


# ───────────────────────── driver integration ────────────────────────────────

async def test_driver_objectives_on_picks_device_best(monkeypatch):
    """Objectives ON, structural scorer OFF, small device: a good draft then a
    WORSE flat refinement then DONE → the objectives bias keeps the draft."""
    monkeypatch.setenv("FF_ADAPTIVE_OBJECTIVES", "true")
    monkeypatch.setenv("FF_UI_DESIGNER_SCORER", "false")  # isolate the objectives signal
    out = await design_round(
        user_request="show me", round_components=_COMPS, canvas_rows=[],
        chat_id="o1", layout_key="lk1", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT, _FLAT, "DONE"]),
        timeout_s=5, max_rounds=3, device=SMALL_DEVICE,
    )
    assert out is not None
    assert any(n.get("type") == "hero" for n in out)  # kept the device-best arrangement


async def test_driver_objectives_off_is_legacy_last_wins(monkeypatch):
    """Both ranking flags OFF, same sequence → legacy last-wins returns the
    flat arrangement (proves OFF is unchanged)."""
    monkeypatch.setenv("FF_ADAPTIVE_OBJECTIVES", "false")
    monkeypatch.setenv("FF_UI_DESIGNER_SCORER", "false")
    out = await design_round(
        user_request="show me", round_components=_COMPS, canvas_rows=[],
        chat_id="o2", layout_key="lk2", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT, _FLAT, "DONE"]),
        timeout_s=5, max_rounds=3, device=SMALL_DEVICE,
    )
    assert out is not None
    assert not any(n.get("type") == "hero" for n in out)
    assert [n.get("component_id") for n in out if n.get("type") == "ref"] == ["A", "B"]


async def test_driver_objectives_failure_is_fail_open(monkeypatch):
    """An objectives_bias that raises must never break the designer — it reverts
    to legacy last-wins selection (the fail-open guarantee)."""
    monkeypatch.setenv("FF_ADAPTIVE_OBJECTIVES", "true")
    monkeypatch.setenv("FF_UI_DESIGNER_SCORER", "false")

    def _boom(*_a, **_k):
        raise RuntimeError("objectives exploded")

    monkeypatch.setattr(ui_designer, "objectives_bias", _boom)
    out = await design_round(
        user_request="show me", round_components=_COMPS, canvas_rows=[],
        chat_id="o3", layout_key="lk3", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT, _FLAT, "DONE"]),
        timeout_s=5, max_rounds=3, device=SMALL_DEVICE,
    )
    assert out is not None  # never crashes
    assert not any(n.get("type") == "hero" for n in out)  # fell back to last-wins (flat)


async def test_driver_objectives_default_off_does_not_select(monkeypatch):
    """Default env (objectives flag absent) + scorer OFF → no selection, last
    wins. Guards the default-OFF posture explicitly."""
    monkeypatch.delenv("FF_ADAPTIVE_OBJECTIVES", raising=False)
    monkeypatch.setenv("FF_UI_DESIGNER_SCORER", "false")
    out = await design_round(
        user_request="show me", round_components=_COMPS, canvas_rows=[],
        chat_id="o4", layout_key="lk4", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT, _FLAT, "DONE"]),
        timeout_s=5, max_rounds=3, device=SMALL_DEVICE,
    )
    assert out is not None
    assert not any(n.get("type") == "hero" for n in out)


# ───────────────────────── orchestrator wiring ──────────────────────────────

async def test_run_designer_passes_connecting_device(monkeypatch):
    """``Orchestrator._run_designer`` hands the connecting socket's ROTE
    profile to ``design_round`` as the plain device model the objectives
    read — without it every candidate scores against a desktop browser."""
    from types import SimpleNamespace
    from orchestrator import ui_designer
    from orchestrator.orchestrator import Orchestrator
    from rote.capabilities import DeviceProfile

    seen = {}

    async def _fake_design(**kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(ui_designer, "design_round", _fake_design)

    ws = object()
    profile = DeviceProfile.from_dict({"device_type": "mobile"})
    fake = SimpleNamespace(
        rote=SimpleNamespace(get_profile=lambda s: profile if s is ws else None),
        workspace=SimpleNamespace(live_layouts=lambda c, u: [],
                                  live_rows=lambda c, u: []),
    )
    out = await Orchestrator._run_designer(
        fake, ws, _COMPS, "chat", "user", "show me", "lk", progress=False)
    assert out is None
    assert seen["device"] == {
        "device_type": "mobile", "is_small": True, "is_voice": False,
        "max_grid_columns": profile.max_grid_columns,
    }
    assert seen["device"]["max_grid_columns"] == 1


def test_designer_device_fail_open():
    from types import SimpleNamespace
    from orchestrator.orchestrator import _designer_device

    def _boom(_s):
        raise RuntimeError("no profile")

    rote = SimpleNamespace(get_profile=_boom)
    assert _designer_device(rote, object()) is None
    assert _designer_device(rote, None) is None
    assert _designer_device(None, object()) is None
