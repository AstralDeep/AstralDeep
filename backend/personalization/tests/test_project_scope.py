"""Tests for personalization/project_scope.py: flag default, normalization and scope-key
derivation, project/global filtering in both directions, visibility checks, and
instruction layering.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from personalization import project_scope as ps  # noqa: E402


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("FF_PROJECT_MEMORY", raising=False)
    assert ps.project_scope_enabled() is False
    monkeypatch.setenv("FF_PROJECT_MEMORY", "true")
    assert ps.project_scope_enabled() is True


def test_normalize_and_scope_key():
    assert ps.normalize_project(None) == ps.GLOBAL
    assert ps.normalize_project("  ") == ps.GLOBAL
    assert ps.normalize_project("proj-1") == "proj-1"
    assert ps.scope_key("u", "proj-1") == "u\x1fproj-1"
    assert ps.scope_key("u", None) == f"u\x1f{ps.GLOBAL}"


def test_filter_to_project_includes_own_and_global():
    items = [
        {"id": 1, "project_id": "alpha"},
        {"id": 2, "project_id": "beta"},
        {"id": 3},
    ]
    ids = [i["id"] for i in ps.filter_to_project(items, "alpha")]
    assert ids == [1, 3]


def test_filter_global_view_excludes_project_private():
    items = [{"id": 1, "project_id": "alpha"}, {"id": 2}]
    ids = [i["id"] for i in ps.filter_to_project(items, None)]
    assert ids == [2]


def test_filter_can_exclude_global():
    items = [{"id": 1, "project_id": "alpha"}, {"id": 2}]
    ids = [i["id"] for i in ps.filter_to_project(items, "alpha", include_global=False)]
    assert ids == [1]


def test_filter_skips_junk():
    assert ps.filter_to_project([None, "x", {"project_id": "a"}], "a") == [{"project_id": "a"}]


def test_visible_in():
    assert ps.visible_in({"project_id": "alpha"}, "alpha") is True
    assert ps.visible_in({"project_id": "alpha"}, "beta") is False
    assert ps.visible_in({}, "alpha") is True
    assert ps.visible_in({"project_id": "alpha"}, None) is False


def test_layer_instructions():
    assert ps.layer_instructions("be terse", "use SI units").startswith("be terse")
    assert "Project context" in ps.layer_instructions("g", "p")
    assert ps.layer_instructions("g", "") == "g"
    assert ps.layer_instructions("", "p") == "p"
