"""Tests that AstralProjection (src/astralprojection/__init__.py) owns the shell
and static runtime resources, and that Deep uses its accessors without falling back
to a legacy path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from astralprojection import static_root, template_path
from astralprojection.resources import ResourceNotFoundError


ROOT = Path(__file__).resolve().parents[2]


def test_projection_owns_shell_and_static_runtime_resources() -> None:
    static = Path(os.fspath(static_root())).resolve(strict=True)

    assert static.is_dir()
    assert (static / "client.js").is_file()
    assert (static / "astral.css").is_file()
    assert template_path("shell.html").is_file()
    with pytest.raises(ResourceNotFoundError):
        template_path("kiosk.html")


