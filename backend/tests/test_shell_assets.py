"""Template-level asset-pipeline checks for the web shell.

Pure file reads against ``webrender/templates/shell.html``,
``webrender/static/astral.css``, and ``webrender/static/fonts/`` — no server.
Asserts the render path never touches an external origin, every ``/static/``
URL in the shell carries a per-file ``%%ASTRAL_V:<path>%%`` version token,
plotly is no longer loaded from the shell ``<head>``, and the self-hosted
fonts are real woff2 binaries declared with ``font-display: swap``.
"""
import re
from pathlib import Path

from astralprojection.resources import static_path, static_root, template_path

SHELL_PATH = template_path("shell.html")
CSS_PATH = static_path("astral.css")
FONTS_DIR = Path(str(static_root())) / "fonts"


def _shell() -> str:
    """Return the shell template text."""
    return SHELL_PATH.read_text(encoding="utf-8")


def _css() -> str:
    """Return the main stylesheet text."""
    return CSS_PATH.read_text(encoding="utf-8")


def test_no_external_origins_in_render_path():
    """Neither the shell nor the stylesheet may reference an external origin."""
    shell = _shell()
    css = _css()
    for banned in ("googleapis", "gstatic"):
        assert banned not in shell, f"shell.html references {banned}"
        assert banned not in css, f"astral.css references {banned}"
    assert not re.search(r"https?://", shell), "shell.html carries an absolute external URL"
    assert not re.search(r"url\(\s*['\"]?https?://", css), "astral.css loads an external resource"
    assert "@import" not in css, "astral.css still uses a blocking @import"


def test_no_plotly_script_tag_in_shell():
    """Plotly must not be loaded from the shell head (lazy-injected by client.js)."""
    assert not re.search(r"<script[^>]+src=\"[^\"]*plotly", _shell(), re.IGNORECASE)


def test_plotly_lazy_url_is_versioned():
    """The lazy-loader URL global exists and carries its per-file version token."""
    assert '__ASTRAL_PLOTLY_URL__ = "/static/vendor/plotly.min.js?v=%%ASTRAL_V:vendor/plotly.min.js%%"' in _shell()


def test_every_static_reference_is_versioned():
    """Each /static/ URL in the shell carries a %%ASTRAL_V:<path>%% token."""
    shell = _shell()
    for match in re.finditer(r"/static/[^\"'\s?%>]+", shell):
        rest = shell[match.end():]
        assert rest.startswith("?v=%%ASTRAL_V:"), (
            f"unversioned static reference: {match.group(0)}"
        )


def test_font_files_vendored():
    """The fonts directory holds non-empty woff2 binaries (wOF2 magic)."""
    assert FONTS_DIR.is_dir(), "webrender/static/fonts/ is missing"
    files = sorted(FONTS_DIR.glob("*.woff2"))
    assert files, "no .woff2 files vendored"
    for f in files:
        data = f.read_bytes()
        assert data[:4] == b"wOF2", f"{f.name} is not a woff2 file"
        assert len(data) > 5000, f"{f.name} is suspiciously small ({len(data)} bytes)"


def test_font_faces_declared_with_swap():
    """astral.css self-hosts Inter and JetBrains Mono with font-display: swap."""
    css = _css()
    blocks = re.findall(r"@font-face\s*\{[^}]*\}", css)
    assert blocks, "no @font-face blocks in astral.css"
    families = set()
    for block in blocks:
        assert "font-display: swap" in block, f"@font-face without swap: {block[:80]}"
        src = re.search(r"url\(['\"]?(/static/fonts/[^'\")]+)", block)
        assert src, f"@font-face without a /static/fonts/ src: {block[:80]}"
        fam = re.search(r"font-family:\s*'([^']+)'", block)
        if fam:
            families.add(fam.group(1))
    assert {"Inter", "JetBrains Mono"} <= families
    for block in blocks:
        assert "%%ASTRAL_V" not in block, "css is served statically; tokens are never substituted"


def test_shell_preloads_primary_fonts():
    """The shell preloads the vendored fonts with versioned URLs and crossorigin."""
    shell = _shell()
    preloads = re.findall(r"<link[^>]+rel=\"preload\"[^>]*>", shell)
    font_preloads = [p for p in preloads if 'as="font"' in p]
    assert len(font_preloads) >= 2, "expected preloads for the primary font files"
    for p in font_preloads:
        assert "crossorigin" in p, f"font preload without crossorigin: {p}"
        assert "?v=%%ASTRAL_V:fonts/" in p, f"font preload without version token: {p}"
        assert 'type="font/woff2"' in p, f"font preload without woff2 type: {p}"
