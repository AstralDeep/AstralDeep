"""Feature 052 (T023) — per-file static asset versioning + immutable caching.

Covers the version map (per-file sha1[:12], subdirectories, memoization,
content sensitivity), the shell %%ASTRAL_V:<path>%% token substitution, and
the _NoCacheStaticFiles header matrix from
specs/052-perf-comment-hygiene/contracts/static-asset-caching.md: a matching
?v= gets a year-long immutable Cache-Control, everything else keeps the
legacy no-cache + ETag flow (unversioned @font-face requests included).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from astralprojection.resources import static_root, template_path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.orchestrator import (  # noqa: E402
    _NoCacheStaticFiles,
    _apply_asset_versions,
    _static_version_map,
)

IMMUTABLE = "public, max-age=31536000, immutable"


def _seed_static(root: Path) -> None:
    """Create a miniature static tree with a nested fonts/ directory."""
    (root / "client.js").write_text("var x = 1;", encoding="utf-8")
    (root / "astral.css").write_text(".a{color:red}", encoding="utf-8")
    fonts = root / "fonts"
    fonts.mkdir()
    (fonts / "inter-latin.woff2").write_bytes(b"wOF2fakefontbytes")


def _sha12(data: bytes) -> str:
    """The map's hash convention: sha1 of the file bytes, first 12 hex chars."""
    return hashlib.sha1(data).hexdigest()[:12]


def test_version_map_hashes_every_file_including_subdirs(tmp_path):
    """Every file appears under its forward-slash relpath with sha1[:12]."""
    _seed_static(tmp_path)
    versions = _static_version_map(str(tmp_path))
    assert versions["client.js"] == _sha12(b"var x = 1;")
    assert versions["fonts/inter-latin.woff2"] == _sha12(b"wOF2fakefontbytes")
    assert all(len(v) == 12 for v in versions.values())


def test_version_map_is_memoized_per_directory(tmp_path):
    """The map is built once per directory and reused for the process life."""
    _seed_static(tmp_path)
    first = _static_version_map(str(tmp_path))
    (tmp_path / "client.js").write_text("var x = 2;", encoding="utf-8")
    assert _static_version_map(str(tmp_path)) is first


def test_changed_content_changes_hash_in_a_fresh_directory(tmp_path):
    """Different bytes yield a different version (URL change by construction)."""
    d1 = tmp_path / "a"
    d2 = tmp_path / "b"
    for d in (d1, d2):
        d.mkdir()
        (d / "astral.css").write_text(".a{}", encoding="utf-8")
    (d1 / "client.js").write_text("one", encoding="utf-8")
    (d2 / "client.js").write_text("two", encoding="utf-8")
    v1 = _static_version_map(str(d1))
    v2 = _static_version_map(str(d2))
    assert v1["client.js"] != v2["client.js"]
    assert v1["astral.css"] == v2["astral.css"]


def test_apply_asset_versions_substitutes_every_token(tmp_path):
    """%%ASTRAL_V:<path>%% tokens become that file's hash; unknown paths 'dev'."""
    _seed_static(tmp_path)
    shell = (
        '<link href="/static/astral.css?v=%%ASTRAL_V:astral.css%%">'
        '<link href="/static/fonts/inter-latin.woff2?v=%%ASTRAL_V:fonts/inter-latin.woff2%%">'
        '<script src="/static/missing.js?v=%%ASTRAL_V:missing.js%%"></script>'
    )
    out = _apply_asset_versions(shell, str(tmp_path))
    versions = _static_version_map(str(tmp_path))
    assert "%%ASTRAL_V" not in out
    assert f'astral.css?v={versions["astral.css"]}' in out
    assert f'inter-latin.woff2?v={versions["fonts/inter-latin.woff2"]}' in out
    assert "missing.js?v=dev" in out


def test_real_shell_template_tokens_all_resolve():
    """The checked-in shell's tokens all resolve against the real static dir."""
    static = Path(str(static_root()))
    shell = template_path("shell.html").read_text(encoding="utf-8")
    assert "%%ASTRAL_V:" in shell
    out = _apply_asset_versions(shell, str(static))
    assert "%%ASTRAL_V" not in out
    assert "?v=dev" not in out


def _client(tmp_path):
    """A Starlette TestClient with the versioned static mount."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    app = Starlette()
    app.mount("/static", _NoCacheStaticFiles(directory=str(tmp_path)), name="static")
    return TestClient(app)


def test_matching_version_gets_immutable_cache_control(tmp_path):
    """?v= equal to the current hash => year-long immutable caching."""
    _seed_static(tmp_path)
    versions = _static_version_map(str(tmp_path))
    client = _client(tmp_path)
    resp = client.get(f"/static/client.js?v={versions['client.js']}")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == IMMUTABLE


def test_subdirectory_asset_gets_immutable_cache_control(tmp_path):
    """The fonts/ subdirectory participates in the versioned contract."""
    _seed_static(tmp_path)
    versions = _static_version_map(str(tmp_path))
    client = _client(tmp_path)
    resp = client.get(
        f"/static/fonts/inter-latin.woff2?v={versions['fonts/inter-latin.woff2']}")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == IMMUTABLE


def test_unversioned_request_keeps_no_cache_and_etag_flow(tmp_path):
    """No ?v= (the @font-face case) => today's no-cache revalidation flow."""
    _seed_static(tmp_path)
    client = _client(tmp_path)
    resp = client.get("/static/fonts/inter-latin.woff2")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    etag = resp.headers.get("etag")
    assert etag
    resp304 = client.get("/static/fonts/inter-latin.woff2",
                         headers={"If-None-Match": etag})
    assert resp304.status_code == 304


def test_mismatched_version_keeps_no_cache(tmp_path):
    """A stale/wrong ?v= must never be cached as immutable."""
    _seed_static(tmp_path)
    client = _client(tmp_path)
    resp = client.get("/static/client.js?v=aaaaaaaaaaaa")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
