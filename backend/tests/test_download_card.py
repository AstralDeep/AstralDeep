"""Feature 039 — desktop codegen download-card tests.

Covers:
  - the ``download_card`` webrender primitive (registry, renderer structure,
    URL-validation defense-in-depth, escaping, unavailable variant)
  - ROTE adaptation per device (browser/tv passthrough, mobile compaction,
    watch collapse, voice speech)
  - the ``desktop_codegen`` meta-tool: tool definition, injection gate,
    build_download_card (available + unavailable), release-info cache +
    fail-open last-good + SHA256 extraction, and handle_meta_tool end-to-end
    with the GitHub API mocked.

Pure Python — no DB. The GitHub Releases API is monkeypatched so no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from rote.adapter import ComponentAdapter  # noqa: E402
from rote.capabilities import DeviceProfile  # noqa: E402
from webrender.renderer import allowed_primitive_types, render_one  # noqa: E402


def _profile(device_type: str) -> DeviceProfile:
    return DeviceProfile.from_dict({"device_type": device_type})


# --------------------------------------------------------------------------- #
# Primitive / renderer
# --------------------------------------------------------------------------- #

def test_download_card_registered():
    assert "download_card" in allowed_primitive_types()


def _card(**kw):
    base = {"type": "download_card", "variant": "available",
            "title": "Astral desktop app", "description": "Install me",
            "download_url": "https://github.com/AstralDeep/AstralDeep/releases/download/v1/AstralDeep.exe",
            "sha256": "a" * 64, "sigstore_bundle_url": "https://github.com/AstralDeep/AstralDeep/releases/download/v1/cosign.bundle",
            "version": "1.2.3", "platform": "windows-x64",
            "html_url": "https://github.com/AstralDeep/AstralDeep/releases/latest"}
    base.update(kw)
    return base


def test_render_available_has_github_link_and_hash():
    html = render_one(_card())
    assert "https://github.com/AstralDeep/AstralDeep/releases/download/v1/AstralDeep.exe" in html
    assert "a" * 64 in html  # sha256 shown
    assert "Download for Windows" in html
    assert "v1.2.3" in html


def test_render_refuses_non_github_url():
    # A crafted non-GitHub URL must NOT become a clickable download link.
    html = render_one(_card(download_url="https://evil.example/a.exe", variant="available"))
    assert "evil.example" not in html
    assert "Download temporarily unavailable" in html


def test_render_unavailable_variant():
    html = render_one(_card(variant="unavailable", download_url="", sha256=""))
    assert "Download temporarily unavailable" in html
    assert "GitHub Releases" in html  # link to releases page still present


def test_render_escapes_title():
    html = render_one(_card(title="<script>x</script>"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# --------------------------------------------------------------------------- #
# ROTE adaptation
# --------------------------------------------------------------------------- #

def test_rote_browser_passthrough():
    c = _card()
    out = ComponentAdapter.adapt([c], _profile("browser"))
    assert out[0]["type"] == "download_card"
    assert out[0].get("sha256") == "a" * 64  # full card preserved


def test_rote_mobile_drops_sha_and_note():
    out = ComponentAdapter.adapt([_card()], _profile("mobile"))
    assert out[0]["type"] == "download_card"
    assert "sha256" not in out[0]
    assert "description" not in out[0]
    assert out[0]["download_url"]  # link still present


def test_rote_watch_collapses_to_button():
    out = ComponentAdapter.adapt([_card()], _profile("watch"))
    assert out[0]["type"] == "button"
    assert "v1.2.3" in out[0]["label"]


def test_rote_voice_speaks():
    out = ComponentAdapter.adapt([_card()], _profile("voice"))
    assert out[0]["type"] == "text"
    assert "GitHub" in out[0]["content"]
    assert "SHA-256" in out[0]["content"]


# --------------------------------------------------------------------------- #
# desktop_codegen meta-tool (GitHub API mocked)
# --------------------------------------------------------------------------- #

from orchestrator import desktop_codegen as dc  # noqa: E402


def test_meta_tool_definition():
    defs = dc.meta_tool_definitions()
    assert defs[0]["function"]["name"] == "offer_desktop_codegen"
    # 057: `code` is optional — a link-only ask calls the tool without it.
    assert defs[0]["function"]["parameters"]["required"] == []
    assert "code" in defs[0]["function"]["parameters"]["properties"]


def test_should_inject_respects_flag_and_draft(monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "desktop_codegen", True)
    assert dc.should_inject(None) is True
    assert dc.should_inject("draft-1") is False  # draft-test exclusion
    monkeypatch.setitem(flags._flags, "desktop_codegen", False)
    assert dc.should_inject(None) is False


def test_build_card_available():
    info = {"exe_url": "https://github.com/AstralDeep/AstralDeep/releases/download/v1/AstralDeep.exe",
            "sha256": "b" * 64, "bundle_url": "u", "version": "1.0.0", "html_url": "h",
            "sha_url": "s"}
    card = dc.build_download_card(info)
    assert card["variant"] == "available"
    assert card["sha256"] == "b" * 64
    assert card["download_url"].startswith("https://github.com/")


def test_build_card_unavailable_when_no_info():
    card = dc.build_download_card(None)
    assert card["variant"] == "unavailable"
    assert card["download_url"] == ""


def _mock_release(monkeypatch, *, sha="c" * 64):
    def fake_fetch():
        return {"version": "v9.9.9", "tag": "v9.9.9",
                "exe_url": "https://github.com/AstralDeep/AstralDeep/releases/download/v9.9.9/AstralDeep.exe",
                "sha_url": "https://github.com/AstralDeep/AstralDeep/releases/download/v9.9.9/SHA256SUMS",
                "bundle_url": "https://github.com/AstralDeep/AstralDeep/releases/download/v9.9.9/cosign.bundle",
                "html_url": "https://github.com/AstralDeep/AstralDeep/releases/latest"}
    monkeypatch.setattr(dc, "_fetch_release_info", fake_fetch)
    monkeypatch.setattr(dc, "_fetch_sha256", lambda url: sha)


def test_get_release_info_caches_and_fails_open(monkeypatch):
    dc._CACHE.clear()
    _mock_release(monkeypatch)
    info = dc.get_release_info()
    assert info["sha256"] == "c" * 64
    # Second call within TTL must not re-fetch: make the fetcher raise.
    monkeypatch.setattr(dc, "_fetch_release_info", lambda: None)
    info2 = dc.get_release_info()
    assert info2 is info or info2["sha256"] == "c" * 64  # last-good kept


def test_get_release_info_none_when_no_cache_and_fetch_fails(monkeypatch):
    dc._CACHE.clear()
    monkeypatch.setattr(dc, "_fetch_release_info", lambda: None)
    assert dc.get_release_info() is None


def test_fetch_sha256_extracts_hash(monkeypatch):
    body = f"{'d' * 64}  AstralDeep.exe\n{'e' * 64}  cosign.bundle\n"

    class _Resp:
        status_code = 200
        def iter_content(self, chunk_size=0):
            yield body.encode()

    import requests
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _Resp())
    h = dc._fetch_sha256("https://github.com/AstralDeep/AstralDeep/releases/download/v1/SHA256SUMS")
    assert h == "d" * 64


def test_handle_meta_tool_returns_code_and_card(monkeypatch):
    import asyncio
    _mock_release(monkeypatch)
    dc._CACHE.clear()
    res = asyncio.run(dc.handle_meta_tool(
        None, "offer_desktop_codegen",
        {"language": "python", "code": "print('hi')", "summary": "sorts files"},
        user_id="u1", chat_id="c1", websocket=None))
    assert res.error is None
    types = [c["type"] for c in res.ui_components]
    assert "code" in types and "download_card" in types
    assert res.result["status"] == "offered"


def test_handle_meta_tool_link_only_returns_card_without_code(monkeypatch):
    # 057: asking for the app without any generated code yields just the card.
    import asyncio
    _mock_release(monkeypatch)
    dc._CACHE.clear()
    res = asyncio.run(dc.handle_meta_tool(
        None, "offer_desktop_codegen", {"language": "python", "code": ""},
        user_id="u1", chat_id="c1", websocket=None))
    assert res.error is None
    types = [c["type"] for c in res.ui_components]
    assert types == ["download_card"]
    assert res.result["status"] == "offered"
    card = res.ui_components[0]
    assert card["title"] == "Astral desktop app for Windows"
    assert "run this code" not in card["description"]


def test_handle_meta_tool_link_only_unavailable_keeps_honest_card(monkeypatch):
    # No cached release + fetch failure -> unavailable variant, untouched text.
    import asyncio
    dc._CACHE.clear()
    monkeypatch.setattr(dc, "_fetch_release_info", lambda: None)
    res = asyncio.run(dc.handle_meta_tool(
        None, "offer_desktop_codegen", {}, user_id="u1", chat_id="c1",
        websocket=None))
    assert res.error is None
    assert [c["type"] for c in res.ui_components] == ["download_card"]
    assert res.ui_components[0]["variant"] == "unavailable"
    assert res.result["status"] == "unavailable"


def test_handle_meta_tool_unavailable_when_no_release(monkeypatch):
    import asyncio
    dc._CACHE.clear()
    monkeypatch.setattr(dc, "_fetch_release_info", lambda: None)
    res = asyncio.run(dc.handle_meta_tool(
        None, "offer_desktop_codegen",
        {"language": "python", "code": "x"}, user_id="u1", chat_id="c1", websocket=None))
    types = [c["type"] for c in res.ui_components]
    assert "download_card" in types
    assert res.result["status"] == "unavailable"


def test_handle_meta_tool_unknown_tool():
    import asyncio
    res = asyncio.run(dc.handle_meta_tool(
        None, "no_such_tool", {}, user_id="u1", chat_id="c1", websocket=None))
    assert res.error is not None
    assert "Unknown meta-tool" in res.error["message"]


def test_handle_meta_tool_exception_returns_error_card(monkeypatch):
    import asyncio
    async def boom(*a, **k):
        raise RuntimeError("explode")
    monkeypatch.setattr(dc, "_offer", boom)
    res = asyncio.run(dc.handle_meta_tool(
        None, "offer_desktop_codegen", {"language": "python", "code": "x"},
        user_id="u1", chat_id="c1", websocket=None))
    assert res.result["status"] == "error"
    assert res.ui_components and res.ui_components[0]["variant"] == "error"


# --------------------------------------------------------------------------- #
# Real fetch-path coverage (requests + egress mocked)
# --------------------------------------------------------------------------- #

_GH_RELEASE = {
    "name": "v2.0.0", "tag_name": "v2.0.0", "html_url": "https://github.com/AstralDeep/AstralDeep/releases/latest",
    "assets": [
        {"name": "AstralDeep.exe", "browser_download_url": "https://github.com/AstralDeep/AstralDeep/releases/download/v2.0.0/AstralDeep.exe", "size": 12345},
        {"name": "SHA256SUMS", "browser_download_url": "https://github.com/AstralDeep/AstralDeep/releases/download/v2.0.0/SHA256SUMS"},
        {"name": "cosign.bundle", "browser_download_url": "https://github.com/AstralDeep/AstralDeep/releases/download/v2.0.0/cosign.bundle"},
    ],
}


class _FakeResp:
    def __init__(self, status=200, body=b"", chunks=None):
        self.status_code = status
        self._body = body
        self._chunks = chunks if chunks is not None else [body]

    def iter_content(self, chunk_size=0):
        for c in self._chunks:
            yield c

    def close(self):
        pass


def test_fetch_release_info_success(monkeypatch):
    import json
    import requests
    body = json.dumps(_GH_RELEASE).encode()
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(200, body))
    info = dc._fetch_release_info()
    assert info is not None
    assert info["version"] == "v2.0.0"
    assert info["exe_url"].endswith("AstralDeep.exe")
    assert info["sha_url"].endswith("SHA256SUMS")
    assert info["bundle_url"].endswith("cosign.bundle")


def test_fetch_release_info_with_token(monkeypatch):
    import json
    import requests
    captured = {}
    def fake_get(url, headers=None, **k):
        captured["headers"] = headers
        return _FakeResp(200, json.dumps(_GH_RELEASE).encode())
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    assert dc._fetch_release_info() is not None
    assert captured["headers"]["Authorization"] == "Bearer ghp_test"


def test_fetch_release_info_non200(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(404, b"{}"))
    assert dc._fetch_release_info() is None


def test_fetch_release_info_transport_error(monkeypatch):
    import requests
    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "get", boom)
    assert dc._fetch_release_info() is None


def test_fetch_release_info_oversize(monkeypatch):
    import requests
    big = b"x" * (3 * 1024 * 1024)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(200, big, chunks=[big]))
    assert dc._fetch_release_info() is None


def test_fetch_release_info_no_exe_asset(monkeypatch):
    import json
    import requests
    payload = {"name": "v1", "tag_name": "v1", "html_url": "h",
               "assets": [{"name": "other", "browser_download_url": "u"}]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(200, json.dumps(payload).encode()))
    assert dc._fetch_release_info() is None


def test_fetch_sha256_non200_and_exception(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(500))
    assert dc._fetch_sha256("https://github.com/x/SHA256SUMS") is None
    def boom(*a, **k):
        raise requests.Timeout("slow")
    monkeypatch.setattr(requests, "get", boom)
    assert dc._fetch_sha256("https://github.com/x/SHA256SUMS") is None


def test_fetch_sha256_fallback_single_hash(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _FakeResp(200, ("f" * 64).encode()))
    assert dc._fetch_sha256("https://github.com/x/SHA256SUMS") == "f" * 64


def test_fetch_sha256_no_valid_hash(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _FakeResp(200, b"not a hash here\n"))
    assert dc._fetch_sha256("https://github.com/x/SHA256SUMS") is None


def test_get_release_info_refresh_fetches_sha(monkeypatch):
    """On a cache miss/refresh, get_release_info fetches the release then the SHA."""
    dc._CACHE.clear()
    import json
    import requests
    body = json.dumps(_GH_RELEASE).encode()
    sha_body = ("a" * 64 + "  AstralDeep.exe\n").encode()
    calls = {"n": 0}

    def fake_get(url, **k):
        calls["n"] += 1
        if "SHA256SUMS" in url:
            return _FakeResp(200, sha_body)
        return _FakeResp(200, body)
    monkeypatch.setattr(requests, "get", fake_get)
    info = dc.get_release_info()
    assert info["sha256"] == "a" * 64
    assert calls["n"] == 2  # release + sha


def test_get_release_info_cache_hit_skips_fetch(monkeypatch):
    """Within TTL, a second call does not re-fetch."""
    dc._CACHE.clear()
    import requests
    calls = {"n": 0}

    def fake_get(url, **k):
        calls["n"] += 1
        import json
        if "SHA256SUMS" in url:
            return _FakeResp(200, ("a" * 64 + "  AstralDeep.exe\n").encode())
        return _FakeResp(200, json.dumps(_GH_RELEASE).encode())
    monkeypatch.setattr(requests, "get", fake_get)
    dc.get_release_info()
    first = calls["n"]
    dc.get_release_info()  # cached
    assert calls["n"] == first  # no new fetches


def test_get_release_info_refresh_failure_keeps_last_good(monkeypatch):
    dc._CACHE.clear()
    import json
    import requests
    body = json.dumps(_GH_RELEASE).encode()
    seq = {"i": 0}

    def fake_get(url, **k):
        seq["i"] += 1
        if "SHA256SUMS" in url:
            return _FakeResp(200, ("a" * 64 + "  AstralDeep.exe\n").encode())
        return _FakeResp(200, body)
    monkeypatch.setattr(requests, "get", fake_get)
    first = dc.get_release_info()
    assert first is not None
    # Force a TTL expiry + a failing fetcher; last-good must survive.
    dc._CACHE[dc._repo()] = (0.0, first)
    monkeypatch.setattr(dc, "_fetch_release_info", lambda: None)
    again = dc.get_release_info()
    assert again is first  # fail-open last-good


def test_offer_includes_summary_text(monkeypatch):
    import asyncio
    _mock_release(monkeypatch)
    dc._CACHE.clear()
    res = asyncio.run(dc.handle_meta_tool(
        None, "offer_desktop_codegen",
        {"language": "python", "code": "x", "summary": "sorts your downloads"},
        user_id="u1", chat_id="c1", websocket=None))
    types = [c["type"] for c in res.ui_components]
    assert "text" in types  # summary text present
    assert res.result["status"] == "offered"


# --------------------------------------------------------------------------- #
# Orchestrator dispatch wiring (the __desktop_codegen__ branch in
# Orchestrator.execute_single_tool)
# --------------------------------------------------------------------------- #

def test_execute_single_tool_dispatches_desktop_codegen(monkeypatch):
    """execute_single_tool routes a __desktop_codegen__-mapped tool call to the
    desktop_codegen meta-tool handler (covers the orchestrator dispatch branch)."""
    import asyncio
    import json
    from orchestrator import orchestrator as orch_mod
    from orchestrator import desktop_codegen

    captured = {}

    async def fake_handle(orch, tool_name, args, *, user_id, chat_id, websocket):
        captured["tool"] = tool_name
        captured["args"] = args
        captured["user_id"] = user_id
        from shared.protocol import MCPResponse
        return MCPResponse(result={"status": "offered"})

    monkeypatch.setattr(desktop_codegen, "handle_meta_tool", fake_handle)

    class _Fn:
        name = "offer_desktop_codegen"
        arguments = json.dumps({"language": "python", "code": "x"})

    class _TC:
        function = _Fn()

    # Keep the real meta-tool authority guard and bind an attended owner session.
    orch = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    websocket = object()
    orch.ui_sessions = {websocket: {"sub": "u1"}}
    tool_to_agent = {"offer_desktop_codegen": "__desktop_codegen__"}

    res = asyncio.run(orch.execute_single_tool(
        websocket=websocket, tool_call=_TC(), tool_to_agent=tool_to_agent,
        chat_id="c1", user_id="u1"))
    assert captured["tool"] == "offer_desktop_codegen"
    assert captured["args"] == {"language": "python", "code": "x"}
    assert captured["user_id"] == "u1"
    assert res.result["status"] == "offered"
