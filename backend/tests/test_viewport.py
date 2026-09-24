"""Tests for orchestrator/viewport.py's targeted re-adaptation diff: the feature flag,
pushing only components whose rendered fragment changed between device profiles, and
skipping components without an id or with a render error.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import viewport  # noqa: E402


def test_viewport_default_off(monkeypatch):
    monkeypatch.delenv("FF_LIVE_VIEWPORT", raising=False)
    assert viewport.viewport_enabled() is False


@pytest.mark.parametrize("v", ["true", "1", "yes", "on"])
def test_viewport_on_values(monkeypatch, v):
    monkeypatch.setenv("FF_LIVE_VIEWPORT", v)
    assert viewport.viewport_enabled() is True


def _comps():
    return [{"type": "grid", "component_id": "g1"},
            {"type": "text", "component_id": "t1"},
            {"type": "grid", "component_id": "g2"}]


def test_only_changed_components_are_pushed():
    def render_old(c):
        return c, f"<{c['type']} cols=1>"

    def render_new(c):
        html = f"<{c['type']} cols=3>" if c["type"] == "grid" else f"<{c['type']} cols=1>"
        return c, html

    ops = viewport.targeted_ops(_comps(), render_old, render_new)
    assert [o["component_id"] for o in ops] == ["g1", "g2"]
    assert all(o["op"] == "upsert" and "html" in o for o in ops)


def test_no_change_yields_no_ops():
    same = lambda c: (c, "<x>")  # noqa: E731
    assert viewport.targeted_ops(_comps(), same, same) == []


def test_components_without_id_are_skipped():
    comps = [{"type": "grid"}, {"type": "grid", "component_id": "g1"}]
    ops = viewport.targeted_ops(comps, lambda c: (c, "a"), lambda c: (c, "b"))
    assert [o["component_id"] for o in ops] == ["g1"]


def test_render_error_skips_that_component_not_the_batch():
    comps = _comps()

    def render_old(c):
        if c["component_id"] == "g1":
            raise RuntimeError("boom")
        return c, "old"

    ops = viewport.targeted_ops(comps, render_old, lambda c: (c, "new"))
    assert [o["component_id"] for o in ops] == ["t1", "g2"]


def test_ops_carry_the_new_adapted_component_and_html():
    def render_old(c):
        return {"adapted": "old", "component_id": c["component_id"]}, "old-html"

    def render_new(c):
        return {"adapted": "new", "component_id": c["component_id"]}, "new-html"

    ops = viewport.targeted_ops([{"type": "grid", "component_id": "g1"}],
                                render_old, render_new)
    assert ops == [{"op": "upsert", "component_id": "g1",
                    "component": {"adapted": "new", "component_id": "g1"},
                    "html": "new-html"}]


def test_empty_input():
    assert viewport.targeted_ops([], lambda c: (c, "a"), lambda c: (c, "b")) == []
    assert viewport.targeted_ops(None, lambda c: (c, "a"), lambda c: (c, "b")) == []
