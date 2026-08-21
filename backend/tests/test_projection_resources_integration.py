"""Runtime resource ownership checks for the composed Projection package."""

from __future__ import annotations

import os
from pathlib import Path

from astralprojection import static_root, template_path


ROOT = Path(__file__).resolve().parents[2]


def test_projection_owns_shell_kiosk_and_static_runtime_resources() -> None:
    static = Path(os.fspath(static_root())).resolve(strict=True)

    assert static.is_dir()
    assert (static / "client.js").is_file()
    assert (static / "astral.css").is_file()
    assert template_path("shell.html").is_file()
    assert template_path("kiosk.html").is_file()


def test_deep_uses_projection_accessors_without_legacy_path_fallback() -> None:
    orchestrator = (ROOT / "backend/orchestrator/orchestrator.py").read_text(
        encoding="utf-8"
    )
    web_auth = (ROOT / "backend/orchestrator/web_auth.py").read_text(
        encoding="utf-8"
    )

    assert "from astralprojection import static_root" in orchestrator
    assert "template_path as _projection_template_path" in orchestrator
    assert "_projection_template_path(\"shell.html\")" in orchestrator
    assert "from astralprojection import template_path" in web_auth
    assert "template_path(\"kiosk.html\")" in web_auth
    assert '"webrender", "templates", "shell.html"' not in orchestrator
    assert '"webrender", "templates", "kiosk.html"' not in web_auth
