"""The authenticated shell must ship a CSP whose nonce matches its inline scripts.

``GET /`` hands the access token to page JavaScript, so an injected script is a
full impersonation rather than a defacement. The shell previously set only
``Cache-Control``. Both inline blocks are server-substituted, so they can carry a
per-response nonce and every other executable source can be pinned to same-origin.
"""

import re

import pytest


SHELL = "backend/webrender/templates/shell.html"


def _shell_text():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "webrender", "templates", "shell.html"),
              encoding="utf-8") as fh:
        return fh.read()


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


@pytest.mark.parametrize("directive", [
    "default-src 'self'",
    "script-src 'self' 'nonce-",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "connect-src 'self' ws: wss:",
    "media-src 'self' data: blob:",
])
def test_policy_source_declares_directive(directive):
    """Pins the policy at its source; serve_shell builds it inline."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "orchestrator", "orchestrator.py"),
              encoding="utf-8") as fh:
        source = fh.read()
    assert directive in source


def test_style_src_keeps_unsafe_inline():
    """Non-negotiable: the Tailwind runtime injects <style>, and renderers emit
    inline style="" attributes. Removing this blanks the whole UI."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "orchestrator", "orchestrator.py"),
              encoding="utf-8") as fh:
        assert "style-src 'self' 'unsafe-inline'" in fh.read()
