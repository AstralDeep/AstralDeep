"""Tests for the web shell's asset pipeline (astralprojection/resources.py,
webrender/templates/shell.html, static/astral.css): no external origins, versioned
static URLs, and vendored, swap-declared fonts.
"""

import re
from pathlib import Path

from astralprojection.resources import static_path, static_root, template_path

SHELL_PATH = template_path("shell.html")
CSS_PATH = static_path("astral.css")
FONTS_DIR = Path(str(static_root())) / "fonts"


def _shell() -> str:
    return SHELL_PATH.read_text(encoding="utf-8")


def _css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def test_no_external_origins_in_render_path():
    shell = _shell()
    css = _css()
    for banned in ("googleapis", "gstatic"):
        assert banned not in shell, f"shell.html references {banned}"
        assert banned not in css, f"astral.css references {banned}"
    assert not re.search(r"https?://", shell), "shell.html carries an absolute external URL"
    assert not re.search(r"url\(\s*['\"]?https?://", css), "astral.css loads an external resource"
    assert "@import" not in css, "astral.css still uses a blocking @import"


def test_no_plotly_script_tag_in_shell():
    assert not re.search(r"<script[^>]+src=\"[^\"]*plotly", _shell(), re.IGNORECASE)


def test_plotly_lazy_url_is_versioned():
    assert '__ASTRAL_PLOTLY_URL__ = "/static/vendor/plotly.min.js?v=%%ASTRAL_V:vendor/plotly.min.js%%"' in _shell()


def test_every_static_reference_is_versioned():
    shell = _shell()
    for match in re.finditer(r"/static/[^\"'\s?%>]+", shell):
        rest = shell[match.end():]
        assert rest.startswith("?v=%%ASTRAL_V:"), (
            f"unversioned static reference: {match.group(0)}"
        )


def test_font_files_vendored():
    assert FONTS_DIR.is_dir(), "webrender/static/fonts/ is missing"
    files = sorted(FONTS_DIR.glob("*.woff2"))
    assert files, "no .woff2 files vendored"
    for f in files:
        data = f.read_bytes()
        assert data[:4] == b"wOF2", f"{f.name} is not a woff2 file"
        assert len(data) > 5000, f"{f.name} is suspiciously small ({len(data)} bytes)"


def test_font_faces_declared_with_swap():
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
    assert families == {"Open Sans"}, (
        "one family serves the whole interface; a second would mean a font "
        "reference survived the 089 swap: {families}"
    )
    for block in blocks:
        assert "%%ASTRAL_V" not in block, "css is served statically; tokens are never substituted"


def test_shell_preloads_primary_fonts():
    shell = _shell()
    preloads = re.findall(r"<link[^>]+rel=\"preload\"[^>]*>", shell)
    font_preloads = [p for p in preloads if 'as="font"' in p]
    assert len(font_preloads) == 1, (
        f"expected exactly one preloaded font file, got {font_preloads}"
    )
    for p in font_preloads:
        assert "crossorigin" in p, f"font preload without crossorigin: {p}"
        assert "?v=%%ASTRAL_V:fonts/" in p, f"font preload without version token: {p}"
        assert 'type="font/woff2"' in p, f"font preload without woff2 type: {p}"
