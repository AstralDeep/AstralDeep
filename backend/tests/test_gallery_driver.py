"""Feature 044 (T031, US2) — canonical gallery driver coverage.

``build_gallery()`` must cover every renderable primitive type (parity with
``webrender.allowed_primitive_types()`` / Projection ``contracts/ui_protocol.json``), carry
the interactive + edge variants US2 verifies, and be well-formed (every element
a dict with a ``"type"`` key). Pure — no socket required.
"""
from __future__ import annotations

import json
from pathlib import Path

from verification.gallery_driver import build_gallery, parse_args, main
from webrender import allowed_primitive_types


def _by_type(comps):
    out = {}
    for c in comps:
        out.setdefault(c.get("type"), []).append(c)
    return out


def test_every_component_is_a_dict_with_a_type():
    gallery = build_gallery()
    assert gallery, "gallery is non-empty"
    for c in gallery:
        assert isinstance(c, dict), f"non-dict gallery entry: {c!r}"
        assert "type" in c and isinstance(c["type"], str) and c["type"], \
            f"gallery entry missing a string 'type': {c!r}"


def test_covers_every_renderable_primitive_type():
    """Every type the renderer can draw appears at least once (set superset).

    No documented exclusions: audio and generative (the natives' KNOWN_DEGRADED
    set) are represented as dicts too — the driver's job is to emit them; the
    client decides how to degrade.
    """
    present = {c["type"] for c in build_gallery()}
    required = set(allowed_primitive_types())
    missing = required - present
    assert not missing, f"gallery omits renderable types: {sorted(missing)}"


def test_gallery_uses_only_known_types():
    """No stray/typo'd types — every emitted type is renderable."""
    present = {c["type"] for c in build_gallery()}
    unknown = present - set(allowed_primitive_types())
    assert not unknown, f"gallery emits unrenderable types: {sorted(unknown)}"


def test_interactive_variants_present():
    by = _by_type(build_gallery())

    # A button carrying an action AND a payload.
    assert any(b.get("action") and isinstance(b.get("payload"), dict) and b["payload"]
               for b in by.get("button", [])), "no button with action+payload"

    # A standalone input.
    assert by.get("input"), "no input component"

    # A multi-field param_picker with a password field AND a submit_action.
    pickers = by.get("param_picker", [])
    assert pickers, "no param_picker"
    rich = [p for p in pickers if p.get("submit_action")
            and any(f.get("kind") == "password" for f in (p.get("fields") or []))
            and len(p.get("fields") or []) >= 3]
    assert rich, "no multi-field param_picker with a password field + submit_action"

    # A server-paginated table: total_rows > page_size, with the pager context.
    tables = by.get("table", [])
    paged = [t for t in tables
             if t.get("total_rows") and t.get("page_size")
             and t["total_rows"] > t["page_size"]
             and t.get("source_tool") and t.get("source_agent")]
    assert paged, "no paginated table (total_rows>page_size + source_tool/agent)"

    # File upload + download + the desktop download card.
    assert by.get("file_upload"), "no file_upload"
    assert by.get("file_download"), "no file_download"
    assert by.get("download_card"), "no download_card"


def test_edge_variants_present():
    by = _by_type(build_gallery())

    # An empty table (headers present, zero rows).
    assert any(t.get("rows") == [] for t in by.get("table", [])), "no empty-table edge case"

    # A very long text run.
    assert any(len(str(t.get("content") or "")) > 400 for t in by.get("text", [])), \
        "no very-long-text edge case"

    # A malformed / missing-field component: a card with no content field.
    assert any(c.get("type") == "card" and "content" not in c for c in build_gallery()), \
        "no malformed/missing-field component"


def test_cli_writes_frames_through_the_real_send_path(tmp_path):
    """``python -m verification.gallery_driver`` runs end-to-end: the CLI pushes
    the gallery through the real Orchestrator.send_ui_render path and captures a
    ui_render frame carrying the full gallery."""
    out = tmp_path / "frames.json"
    rc = main(["--user", "u-gallery", "--device", "windows",
               "--out", str(out), "--pretty"])
    assert rc == 0
    payload = json.loads(Path(out).read_text(encoding="utf-8"))
    assert payload["user"] == "u-gallery" and payload["device"] == "windows"
    assert payload["component_count"] == len(build_gallery())
    frames = payload["frames"]
    assert frames, "no frames captured"
    renders = [f for f in frames if f.get("type") == "ui_render"]
    assert renders, f"no ui_render frame (got {[f.get('type') for f in frames]})"
    # The single canvas render carries the whole (ROTE-adapted) gallery.
    comps = renders[-1].get("components") or []
    assert len(comps) >= 1
    assert renders[-1].get("target") == "canvas"


def test_parse_args_requires_user():
    ns = parse_args(["--user", "abc"])
    assert ns.user == "abc" and ns.device == "browser" and ns.target == "canvas"
