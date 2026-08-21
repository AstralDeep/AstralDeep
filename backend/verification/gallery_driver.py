"""Feature 044 (T031, US2) — canonical component gallery driver.

Two things:

* :func:`build_gallery` — a **pure** function returning one flat list of
  astralprims component dicts covering EVERY renderable type
  (``webrender.allowed_primitive_types()`` / Projection ``contracts/ui_protocol.json``
  ``component_types`` — 35 today) plus the interactive variants
  (button+action+payload, input, a multi-field ``param_picker`` with a
  password field and a ``submit_action``, a paginated ``table`` with
  ``total_rows > page_size``, ``file_upload``, ``file_download`` and
  ``download_card``) and the edge cases (empty table, very long text, a
  malformed/missing-field component). Unit-testable without a socket.

* :func:`push_gallery` / the ``python -m verification.gallery_driver --user
  <id>`` CLI — push the gallery to a connected UI client over the REAL server
  send path (``Orchestrator.send_ui_render`` → ROTE adapt → webrender), the
  same seam a tool or a test uses. Given a live orchestrator, ``push_gallery``
  fans the gallery out to every socket of ``--user`` exactly like
  ``Orchestrator._push_canvas``; the CLI builds a minimal real-send harness
  (a captured socket + a chosen ROTE device profile) and prints the exact
  ``ui_render`` frame(s) a client of that device would receive — so an
  operator can eyeball fidelity per device or feed the frames to a client
  test harness (verification README §6 / T033).

Zero new dependencies; builds only on ``astralprims``-shaped dicts, ``ROTE``,
and the orchestrator's existing send path.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Dict, List, Optional

# A GitHub Release URL is the ONLY value the download_card renderer will turn
# into a live link (renderer._is_github_release_url); use a real-shaped one.
_RELEASE_URL = "https://github.com/AstralDeep/AstralDeep/releases/download/v1.0.0/AstralDeep-Setup.exe"
_RELEASE_HTML = "https://github.com/AstralDeep/AstralDeep/releases/tag/v1.0.0"

_LONG_TEXT = (
    "This is a deliberately long paragraph used to exercise wrapping, "
    "truncation and scroll behavior on every client. " * 12
).strip()


def build_gallery() -> List[Dict[str, Any]]:
    """Return the canonical gallery: one flat component-dict list.

    Covers all 35 renderable types (each at least once) plus the interactive
    and edge variants US2 verifies. Every element is a plain dict with a
    ``"type"`` key — no astralprims classes, no sockets — so it serializes and
    ROTE-adapts exactly like agent output.
    """
    return [
        # --- header / text family --------------------------------------------
        {"type": "hero", "title": "Component gallery",
         "eyebrow": "Feature 044 · cross-client parity",
         "subtitle": "Every renderable primitive, one canvas.",
         "icon": "✨", "badges": ["35 types", "interactive", "edge cases"],
         "variant": "gradient"},
        {"type": "text", "content": "Heading (h2)", "variant": "h2"},
        {"type": "text", "content": "Body copy renders as a paragraph.", "variant": "body"},
        {"type": "text", "content": "Caption / muted secondary text.", "variant": "caption"},
        {"type": "text",
         "content": "**Markdown** text with _emphasis_, `code`, and a "
                    "[link](https://example.com).",
         "variant": "markdown"},
        {"type": "divider"},

        # --- status / badges / alerts ----------------------------------------
        {"type": "badge", "label": "Live", "variant": "success", "icon": "●"},
        {"type": "alert", "variant": "info", "title": "Info", "message": "An informational alert."},
        {"type": "alert", "variant": "success", "title": "Success", "message": "Saved."},
        {"type": "alert", "variant": "warning", "title": "Warning", "message": "Heads up."},
        {"type": "alert", "variant": "error", "title": "Error", "message": "Something failed."},

        # --- cards / metrics / keyvalue --------------------------------------
        {"type": "card", "title": "A card", "variant": "default", "content": [
            {"type": "text", "content": "Cards wrap child components.", "variant": "body"},
            {"type": "badge", "label": "nested", "variant": "accent"},
        ]},
        {"type": "metric", "title": "Revenue", "value": "$12,480",
         "subtitle": "+8.2% vs last month", "variant": "success", "progress": 0.82},
        {"type": "keyvalue", "title": "Facts", "columns": 2, "items": [
            {"label": "Status", "value": "Active"},
            {"label": "Owner", "value": "Sam", "hint": "primary contact"},
            {"label": "Region", "value": "us-east"},
            {"label": "Plan", "value": "Pro"},
        ]},

        # --- lists / tables (incl. paginated + empty edge case) --------------
        {"type": "list", "ordered": False,
         "items": ["First bullet", "Second bullet", "Third bullet"]},
        _paginated_table(),
        {"type": "table", "title": "Empty table (no rows)",
         "headers": ["Name", "Value"], "rows": []},

        # --- interactive: button / input / param_picker ----------------------
        {"type": "button", "label": "Run action", "action": "component_action",
         "payload": {"tool": "demo_tool", "params": {"q": "hello"}}, "variant": "primary"},
        {"type": "input", "name": "search", "value": "",
         "placeholder": "Type to search…"},
        _interactive_param_picker(),

        # --- progress / code / image -----------------------------------------
        {"type": "progress", "value": 0.65, "label": "Uploading", "show_percentage": True},
        {"type": "code", "language": "python",
         "code": "def greet(name):\n    return f'Hello, {name}!'"},
        {"type": "image", "url": "https://placehold.co/600x200/png",
         "alt": "A placeholder image", "width": 600, "height": 200},

        # --- layout: grid / container / tabs / collapsible -------------------
        {"type": "grid", "columns": 3, "gap": 16, "children": [
            {"type": "metric", "title": "CPU", "value": "42%"},
            {"type": "metric", "title": "Memory", "value": "6.1 GB"},
            {"type": "metric", "title": "Disk", "value": "71%", "variant": "warning"},
        ]},
        {"type": "container", "direction": "row", "children": [
            {"type": "badge", "label": "one"},
            {"type": "badge", "label": "two"},
            {"type": "badge", "label": "three"},
        ]},
        {"type": "tabs", "tabs": [
            {"label": "Overview", "value": "overview",
             "content": [{"type": "text", "content": "Tab one body.", "variant": "body"}]},
            {"label": "Details", "value": "details",
             "content": [{"type": "text", "content": "Tab two body.", "variant": "body"}]},
        ]},
        {"type": "collapsible", "title": "Show more", "default_open": False, "content": [
            {"type": "text", "content": "Hidden until expanded.", "variant": "body"},
        ]},

        # --- charts ----------------------------------------------------------
        {"type": "bar_chart", "title": "Quarterly", "labels": ["Q1", "Q2", "Q3", "Q4"],
         "datasets": [{"label": "2026", "data": [12, 19, 7, 24]}]},
        {"type": "line_chart", "title": "Trend", "labels": ["Mon", "Tue", "Wed", "Thu", "Fri"],
         "datasets": [{"label": "visits", "data": [3, 5, 2, 8, 6]}]},
        {"type": "pie_chart", "title": "Share", "labels": ["A", "B", "C"],
         "data": [55, 30, 15], "colors": ["#6366F1", "#06B6D4", "#F97316"]},
        {"type": "plotly_chart",
         "data": [{"type": "scatter", "x": [1, 2, 3], "y": [4, 1, 6], "mode": "lines+markers"}],
         "layout": {"title": "Plotly scatter"}, "config": {"displayModeBar": False}},

        # --- theme / pickers / files -----------------------------------------
        {"type": "color_picker", "label": "Primary", "color_key": "primary", "value": "#6366F1"},
        {"type": "theme_apply", "preset": "midnight", "message": "Theme applied"},
        {"type": "file_upload", "label": "Upload a file",
         "accept": ".pdf,.csv,.png,.txt"},
        {"type": "file_download", "label": "Download report.csv",
         "url": "/api/download/report.csv", "filename": "report.csv"},
        {"type": "download_card", "title": "Astral desktop app",
         "description": "Native Windows client.",
         "download_url": _RELEASE_URL, "html_url": _RELEASE_HTML,
         "version": "1.0.0", "platform": "windows-x64",
         "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},

        # --- media / dashboard extras ----------------------------------------
        {"type": "audio", "src": "https://example.com/clip.mp3",
         "label": "Sample audio", "description": "A short clip.",
         "contentType": "audio/mpeg"},
        {"type": "timeline", "title": "Activity", "items": [
            {"title": "Created", "time": "09:00", "description": "Draft opened", "variant": "info"},
            {"title": "Approved", "time": "11:30", "description": "Went live", "variant": "success"},
        ]},
        {"type": "rating", "label": "Quality", "value": 4.0, "max_value": 5,
         "subtitle": "Based on 128 reviews", "show_value": True},
        {"type": "skeleton", "variant": "list", "count": 3, "label": "Loading…"},
        {"type": "chat_history", "title": "Recent chats", "items": [
            {"chat_id": "gallery-chat-1", "title": "Trip planning",
             "preview": "Let's book flights…", "time": "2h ago", "icon": "✈"},
            {"chat_id": "gallery-chat-2", "title": "Budget review",
             "preview": "Q3 numbers", "time": "yesterday", "saved": True},
        ]},
        {"type": "generative", "spec": {"kind": "callout",
                                        "title": "Generative widget",
                                        "body": "Composed from the constrained grammar."}},

        # --- edge cases ------------------------------------------------------
        {"type": "text", "content": _LONG_TEXT, "variant": "body"},
        # Malformed / missing-field: a card with no `content` key at all —
        # clients must degrade gracefully, not crash.
        {"type": "card", "title": "Malformed card (missing content field)"},
    ]


def _paginated_table() -> Dict[str, Any]:
    """A server-paginated table: ``total_rows > page_size`` with the
    source_tool/source_agent/component_id the pager needs (table_paginate)."""
    page_size = 25
    rows = [[i, f"Row {i}", i * 7] for i in range(1, page_size + 1)]
    return {
        "type": "table",
        "title": "Paginated results",
        "headers": ["ID", "Name", "Score"],
        "rows": rows,
        "total_rows": 137,
        "page_size": page_size,
        "page_offset": 0,
        "page_sizes": [25, 50, 100, 200],
        "source_tool": "list_things",
        "source_agent": "web-research-1",
        "source_params": {"q": "demo"},
        "component_id": "gallery_table_paged",
    }


def _interactive_param_picker() -> Dict[str, Any]:
    """A multi-field action-submit ``param_picker`` including a password field
    and a ``submit_action`` (feature 043 action-submit mode)."""
    return {
        "type": "param_picker",
        "title": "Connect a model",
        "description": "An interactive form (action-submit mode).",
        "fields": [
            {"name": "base_url", "label": "Base URL", "kind": "text",
             "default": "https://api.example.com/v1"},
            {"name": "model", "label": "Model", "kind": "select",
             "options": ["gpt-4o", "claude-3", "local"], "default": "gpt-4o"},
            {"name": "api_key", "label": "API key", "kind": "password"},
            {"name": "temperature", "label": "Temperature", "kind": "number",
             "default": 0.2, "step": 0.1},
            {"name": "stream", "label": "Stream responses", "kind": "boolean",
             "default": True},
        ],
        "submit_label": "Save",
        "submit_action": "chrome_llm_save",
        "submit_payload": {"tab": "llm"},
    }


async def push_gallery(orch: Any, user_id: str, *, target: str = "canvas") -> List[Any]:
    """Push the gallery to every connected socket of ``user_id`` via the REAL
    ``orch.send_ui_render`` path (ROTE-adapted per socket) — the same fan-out
    ``Orchestrator._push_canvas`` performs. Returns the sockets pushed to."""
    gallery = build_gallery()
    targets = [ws for ws in getattr(orch, "ui_clients", []) or []
               if orch._get_user_id(ws) == user_id]
    for ws in targets:
        await orch.send_ui_render(ws, gallery, target)
    return targets


# --------------------------------------------------------------------------- CLI

class _CaptureSocket:
    """A UI socket that buffers the exact server->client frames a client would
    receive (the VirtualWebSocket pattern used across the test suite)."""

    def __init__(self, label: str = "gallery") -> None:
        self.label = label
        self.outputs: List[Dict[str, Any]] = []

    async def send_text(self, data: str) -> None:
        try:
            self.outputs.append(json.loads(data))
        except (json.JSONDecodeError, TypeError):
            self.outputs.append({"type": "raw", "data": data})

    async def send_json(self, data: Any, mode: str = "text") -> None:
        if isinstance(data, dict):
            self.outputs.append(data)
        else:
            await self.send_text(str(data))

    async def close(self, code: int = 1000) -> None:  # pragma: no cover - unused
        return None

    @property
    def client(self):
        return ("gallery", self.label)


class _GalleryRote:
    """Returns a fixed device profile and adapts through the REAL
    ``ComponentAdapter`` — so ``send_ui_render`` runs its real ROTE + render
    path for the chosen device (mirrors the test-suite FakeRote pattern)."""

    def __init__(self, profile: Any) -> None:
        self._p = profile

    def get_profile(self, websocket: Any) -> Any:
        return self._p

    def adapt(self, websocket: Any, components: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        from rote.adapter import ComponentAdapter
        return ComponentAdapter.adapt(list(components), self._p)


def _profile_for(device: str) -> Any:
    from rote.capabilities import DeviceProfile
    if device in ("", "browser", "web"):
        try:
            return DeviceProfile.default()
        except Exception:
            return DeviceProfile.from_dict({"device_type": "browser"})
    return DeviceProfile.from_dict({"device_type": device})


def _build_capture_orch(user_id: str, device: str):
    """A minimal real-send harness: a captured socket registered as ``user_id``
    plus the real ``Orchestrator.send_ui_render`` bound over a chosen-device
    ROTE. No DB, no LLM — just the outbound render path a connected client sees."""
    import types

    from orchestrator.orchestrator import Orchestrator

    ws = _CaptureSocket(label=user_id)
    orch = types.SimpleNamespace()
    orch.rote = _GalleryRote(_profile_for(device))
    orch.ui_clients = [ws]
    orch._get_user_id = lambda w: user_id

    async def _safe_send(websocket, data):
        # Orchestrator.send_ui_render calls self._safe_send(websocket, json);
        # route it to that socket's buffer.
        await websocket.send_text(data)

    orch._safe_send = _safe_send
    orch.send_ui_render = types.MethodType(Orchestrator.send_ui_render, orch)
    return orch, ws


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="verification.gallery_driver",
        description="Push the canonical 35-type component gallery to a UI client "
                    "over the real send path (feature 044 / US2).")
    p.add_argument("--user", required=True, help="target user id (session owner)")
    p.add_argument("--device", default="browser",
                   help="ROTE device profile to adapt for: browser|windows|android|mobile|tablet")
    p.add_argument("--target", default="canvas", help="render target (canvas|chat|history)")
    p.add_argument("--out", default=None, help="write the captured frame(s) JSON to this file")
    p.add_argument("--pretty", action="store_true", help="pretty-print the JSON")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    gallery = build_gallery()
    orch, ws = _build_capture_orch(args.user, args.device)
    asyncio.run(push_gallery(orch, args.user, target=args.target))
    frames = list(ws.outputs)
    payload = {"user": args.user, "device": args.device, "target": args.target,
               "component_count": len(gallery), "frames": frames}
    text = json.dumps(payload, indent=2 if args.pretty else None)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"[gallery] {len(gallery)} components -> {len(frames)} frame(s) "
              f"for user={args.user} device={args.device} written to {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
