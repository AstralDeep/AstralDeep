"""Feature 026 — headless integration check of the web-UI serving layer.

The full interactive real-browser parity pass (T030) needs a live stack + browser
and runs separately. This test verifies, headlessly via FastAPI's TestClient, the
HTTP serving the orchestrator wires up: the shell route (with token injection) and
the StaticFiles mount that serves `client.js` / `astral.css` from Projection's
packaged static-resource root.
It builds a minimal app mirroring the orchestrator's mount (orchestrator.py:5347+),
so it exercises the real shell template + static assets without booting the DB.
"""
import re
from pathlib import Path

import pytest
from astralprojection.resources import static_root, template_path, vendor_path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

STATIC_ROOT = Path(str(static_root()))
SHELL = template_path("shell.html")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")  # so the shell gets 'dev-token'
    from orchestrator.orchestrator import _apply_asset_versions
    from orchestrator.web_auth import session_token

    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    async def shell(request: Request):
        html = SHELL.read_text(encoding="utf-8")
        html = html.replace("%%ASTRAL_TOKEN%%", session_token(request) or "")
        # Feature 052: mirror serve_shell's per-file content-hash substitution.
        return HTMLResponse(_apply_asset_versions(html, str(STATIC_ROOT)))

    app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")
    return TestClient(app)


def test_shell_served_with_token(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # The served UI is branded "AstralDeep" (shell <title>, favicon, topbar logo);
    # "AstralDeep" is the product/repo name and never appears in the shell. This
    # assertion previously checked the stale product name and failed post-rebrand.
    assert "AstralDeep" in body
    assert "/static/client.js" in body and "/static/astral.css" in body
    # Feature 052: every /static URL carries a per-file 12-hex content hash and
    # no raw %%ASTRAL_V:<path>%% token survives substitution.
    assert re.search(
        r'<link rel="icon" type="image/png" '
        r'href="/static/img/astra-fav\.png\?v=[0-9a-f]{12}">', body)
    assert re.search(r'/static/client\.js\?v=[0-9a-f]{12}', body)
    assert re.search(r'/static/astral\.css\?v=[0-9a-f]{12}', body)
    assert "%%ASTRAL_V:" not in body               # all version tokens substituted
    assert "%%ASTRAL_TOKEN%%" not in body          # placeholder replaced
    assert 'window.__ASTRAL_TOKEN__ = "dev-token"' in body  # mock token injected, JS var name intact


def test_brand_image_assets_served(client):
    for path in ("/static/img/AstralDeep.png", "/static/img/astra-fav.png"):
        resp = client.get(path)
        assert resp.status_code == 200, f"missing asset: {path}"
        assert resp.headers["content-type"] == "image/png"
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG payload


def test_client_js_served(client):
    resp = client.get("/static/client.js")
    assert resp.status_code == 200
    assert "register_ui" in resp.text and "ui_stream_data" in resp.text


def test_astral_css_served(client):
    resp = client.get("/static/astral.css")
    assert resp.status_code == 200
    assert "--astral-primary" in resp.text


def test_vendor_assets_present():
    # self-hosted (no external CDN at runtime)
    assert len(vendor_path("tailwind.js").read_bytes()) > 10000
    assert len(vendor_path("plotly.min.js").read_bytes()) > 100000
