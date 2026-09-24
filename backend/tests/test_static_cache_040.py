"""Tests that static assets get a content-hash version and no-cache headers
(orchestrator/orchestrator.py), so a rebuilt client.js is never served stale from
browser heuristic caching.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.orchestrator import _NoCacheStaticFiles, _static_asset_version  # noqa: E402


def test_asset_version_is_stable_and_content_sensitive(tmp_path):
    (tmp_path / "client.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "astral.css").write_text(".a{}", encoding="utf-8")
    v1 = _static_asset_version(str(tmp_path))
    assert v1 and len(v1) == 12
    assert _static_asset_version(str(tmp_path)) == v1

    d2 = tmp_path / "d2"
    d2.mkdir()
    (d2 / "client.js").write_text("console.log(2)", encoding="utf-8")
    (d2 / "astral.css").write_text(".a{}", encoding="utf-8")
    assert _static_asset_version(str(d2)) != v1


def test_static_files_send_no_cache(tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    (tmp_path / "client.js").write_text("x = 1;", encoding="utf-8")
    app = Starlette()
    app.mount("/static", _NoCacheStaticFiles(directory=str(tmp_path)), name="static")

    resp = TestClient(app).get("/static/client.js")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"
