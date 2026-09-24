"""Tests for the authenticated shell's per-response CSP (orchestrator/orchestrator.py,
astralprojection/resources.py): nonce'd inline scripts, same-origin script sources,
and a connect-src scoped to self plus LiveKit.
"""

import re

import pytest
from astralprojection.resources import template_path


SHELL = template_path("shell.html")


def _shell_text():
    return SHELL.read_text(encoding="utf-8")


def test_every_inline_script_carries_the_nonce_placeholder():
    text = _shell_text()
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", text)
    assert inline, "shell has no inline scripts — update this test"
    for tag in inline:
        assert 'nonce="%%ASTRAL_NONCE%%"' in tag, (
            f"inline script would be blocked by the CSP: {tag}"
        )


def test_external_scripts_are_same_origin():
    for src in re.findall(r'<script[^>]*\bsrc="([^"]+)"', _shell_text()):
        assert src.startswith("/static/"), src


def _orchestrator_source():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "orchestrator", "orchestrator.py"),
              encoding="utf-8") as fh:
        return fh.read()


# Trailing '; ' pins the match; a bare prefix matches a looser CSP too
@pytest.mark.parametrize("directive", [
    "default-src 'self'; ",
    "script-src 'self' 'nonce-",
    "object-src 'none'; ",
    "base-uri 'none'; ",
    "frame-ancestors 'none'; ",
    "form-action 'self'",
    "media-src 'self' data: blob: https:; ",
])
def test_policy_source_declares_directive(directive):
    assert directive in _orchestrator_source()


def test_connect_src_never_allows_bare_websocket_schemes():
    source = _orchestrator_source()
    for bad in ("connect-src 'self' ws:", "connect-src 'self' wss:",
                "'self' ws: wss:"):
        assert bad not in source, f"connect-src regressed to a bare scheme: {bad}"


@pytest.mark.parametrize("public_url,expected", [
    ("ws://localhost:7880", "'self' ws://localhost:7880"),
    ("wss://voice.example.org", "'self' wss://voice.example.org"),
    ("wss://voice.example.org:443/rtc?x=1", "'self' wss://voice.example.org:443"),
    ("", "'self'"),
    ("   ", "'self'"),
    ("not-a-url", "'self'"),
    ("javascript:alert(1)", "'self'"),
])
def test_csp_connect_src_allows_only_the_livekit_origin(monkeypatch, public_url, expected):
    from orchestrator.orchestrator import csp_connect_src
    monkeypatch.setenv("LIVEKIT_PUBLIC_URL", public_url)
    assert csp_connect_src() == expected


def test_csp_connect_src_unset_env_is_self_only(monkeypatch):
    from orchestrator.orchestrator import csp_connect_src
    monkeypatch.delenv("LIVEKIT_PUBLIC_URL", raising=False)
    assert csp_connect_src() == "'self'"


def test_style_src_keeps_unsafe_inline():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "orchestrator", "orchestrator.py"),
              encoding="utf-8") as fh:
        assert "style-src 'self' 'unsafe-inline'" in fh.read()
