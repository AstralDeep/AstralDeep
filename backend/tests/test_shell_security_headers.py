"""The authenticated shell must ship a CSP whose nonce matches its inline scripts.

``GET /`` hands the access token to page JavaScript, so an injected script is a
full impersonation rather than a defacement. The shell previously set only
``Cache-Control``. Both inline blocks are server-substituted, so they can carry a
per-response nonce and every other executable source can be pinned to same-origin.
"""

import re

import pytest
from astralprojection.resources import template_path


SHELL = template_path("shell.html")


def _shell_text():
    return SHELL.read_text(encoding="utf-8")


def test_every_inline_script_carries_the_nonce_placeholder():
    """A nonce'd CSP silently breaks any inline block that lacks the attribute."""
    text = _shell_text()
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", text)
    assert inline, "shell has no inline scripts — update this test"
    for tag in inline:
        assert 'nonce="%%ASTRAL_NONCE%%"' in tag, (
            f"inline script would be blocked by the CSP: {tag}"
        )


def test_external_scripts_are_same_origin():
    """script-src is 'self' + nonce, so a cross-origin src would be refused."""
    for src in re.findall(r'<script[^>]*\bsrc="([^"]+)"', _shell_text()):
        assert src.startswith("/static/"), src


def _orchestrator_source():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "orchestrator", "orchestrator.py"),
              encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("directive", [
    # TERMINATED forms ("; " / end-of-policy). An unterminated prefix such as
    # "default-src 'self'" would still match a widened "default-src 'self' *;",
    # so the trailing separator is what actually pins the directive.
    "default-src 'self'; ",
    "script-src 'self' 'nonce-",
    "object-src 'none'; ",
    "base-uri 'none'; ",
    "frame-ancestors 'none'; ",
    "form-action 'self'",
    "media-src 'self' data: blob: https:; ",
])
def test_policy_source_declares_directive(directive):
    """Pins the policy at its source; serve_shell builds it inline."""
    assert directive in _orchestrator_source()


def test_connect_src_never_allows_bare_websocket_schemes():
    """``ws:``/``wss:`` with no host match EVERY origin, so an injected script
    could stream the page-embedded access token anywhere. connect-src must be
    'self' plus at most the single derived LiveKit origin (see
    ``csp_connect_src``) — never a bare scheme."""
    source = _orchestrator_source()
    for bad in ("connect-src 'self' ws:", "connect-src 'self' wss:",
                "'self' ws: wss:"):
        assert bad not in source, f"connect-src regressed to a bare scheme: {bad}"


@pytest.mark.parametrize("public_url,expected", [
    # The signalling socket is cross-origin in every real deployment: a different
    # PORT in dev and a different HOST in production. 'self' alone would block
    # room.connect() and silently kill voice in the web shell.
    ("ws://localhost:7880", "'self' ws://localhost:7880"),
    ("wss://voice.example.org", "'self' wss://voice.example.org"),
    ("wss://voice.example.org:443/rtc?x=1", "'self' wss://voice.example.org:443"),
    # Unconfigured or unusable => 'self' alone, never widened.
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
    """Non-negotiable: the Tailwind runtime injects <style>, and renderers emit
    inline style="" attributes. Removing this blanks the whole UI."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "orchestrator", "orchestrator.py"),
              encoding="utf-8") as fh:
        assert "style-src 'self' 'unsafe-inline'" in fh.read()
