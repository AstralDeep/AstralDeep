"""Tests that public offline assets are served by AstralProjection's real static-serving
class, never shell auth: worker scope, request-identity headers, font MIME, and exact
served type/size/digest per asset.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from astralprojection.resources import static_root
from starlette import responses
from starlette.applications import Starlette
from starlette.testclient import TestClient

from orchestrator.orchestrator import _NoCacheStaticFiles, _static_version_map


@pytest.fixture
def client():
    app = Starlette()
    app.mount("/static", _NoCacheStaticFiles(directory=str(static_root())), name="static")
    return TestClient(app)


@pytest.mark.parametrize("method", ["get", "head"])
def test_only_offline_worker_can_control_root_and_is_never_immutable(client, method):
    version = _static_version_map(str(static_root()))["service-worker.js"]
    for suffix in ("", f"?v={version}", "?v=stale"):
        response = getattr(client, method)("/static/service-worker.js" + suffix)
        assert response.status_code == 200
        assert response.headers["Service-Worker-Allowed"] == "/"
        assert response.headers["Cache-Control"] == "no-cache"
        assert response.headers["Content-Security-Policy"] == (
            "default-src 'none'; connect-src 'self'"
        )
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    etag = client.get("/static/service-worker.js").headers["etag"]
    response = client.get("/static/service-worker.js", headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.headers["Service-Worker-Allowed"] == "/"
    assert response.headers["Cache-Control"] == "no-cache"


@pytest.mark.parametrize("path", ["client.js", "offline-registration.js", "offline.css",
                                  "offline.html", "manifest.webmanifest", "missing-worker.js"])
def test_other_assets_never_receive_expanded_worker_scope(client, path):
    response = client.get("/static/" + path)
    assert "Service-Worker-Allowed" not in response.headers
    assert response.status_code == (404 if path == "missing-worker.js" else 200)


def test_offline_resources_are_public_even_with_request_identity_headers(client):
    headers = {"Authorization": "synthetic-private-header", "Cookie": "session=synthetic-owner"}
    for name in ("offline.html", "manifest.webmanifest", "service-worker.js"):
        public = client.get("/static/" + name)
        identified = client.get("/static/" + name, headers=headers)
        assert identified.content == public.content
        assert "synthetic" not in identified.text and "%%ASTRAL" not in identified.text
        assert "set-cookie" not in identified.headers


@pytest.mark.parametrize("font", ["open-sans-latin.woff2"])
@pytest.mark.parametrize("method", ["get", "head"])
def test_bundled_font_mime_is_independent_of_platform_database(client, monkeypatch, font, method):
    platform_guess = responses.guess_type
    monkeypatch.setattr(
        responses, "guess_type",
        lambda path: (None, None) if str(path).endswith(".woff2") else platform_guess(path),
    )
    name = "fonts/" + font
    version = _static_version_map(str(static_root()))[name]
    for suffix in ("", f"?v={version}", "?v=stale"):
        response = getattr(client, method)("/static/" + name + suffix)
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "font/woff2"
        expected_cache = (
            "public, max-age=31536000, immutable" if suffix == f"?v={version}" else "no-cache"
        )
        assert response.headers["Cache-Control"] == expected_cache
        assert "Service-Worker-Allowed" not in response.headers
    response = client.get("/static/" + name, headers={"Range": "bytes=0-15"})
    assert response.status_code == 206
    assert response.headers["Content-Type"] == "font/woff2"
    assert len(response.content) == 16
    response = client.get("/static/" + name, headers={"If-None-Match": response.headers["etag"]})
    assert response.status_code == 304
    assert response.headers["Cache-Control"] == "no-cache"


def test_every_worker_asset_matches_actual_deep_served_type_size_and_digest(client):
    worker = client.get("/static/service-worker.js")
    assert worker.headers["Content-Type"].split(";", 1)[0] in {
        "text/javascript", "application/javascript",
    }
    match = re.search(r"const PUBLIC_ASSETS = (\[[\s\S]*?\]);", worker.text)
    assert match
    assets = json.loads(match[1])
    assert len(assets) == 6
    for asset in assets:
        response = client.get(asset["path"])
        assert response.status_code == 200, asset["path"]
        assert response.headers["Content-Type"].split(";", 1)[0] == asset["type"], asset["path"]
        assert len(response.content) == asset["bytes"], asset["path"]
        assert hashlib.sha256(response.content).hexdigest() == asset["sha256"], asset["path"]
        assert response.headers["Cache-Control"] == "no-cache"
        assert "set-cookie" not in response.headers
