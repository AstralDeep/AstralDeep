# Shared fixtures: a tiny agent package whose tools run in the sandbox.

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def package_dir(tmp_path: Path) -> Path:
    root = tmp_path / "agent"
    (root / "tools").mkdir(parents=True)
    work = tmp_path / "work"
    (work / "notes").mkdir(parents=True)
    (work / "notes" / "ada-lovelace.md").write_text("---\ntitle: Ada Lovelace\n---\n# Ada\nAnalytical engine.\n")
    (root / "tools" / "echo.toml").write_text(
        'name = "echo"\ndescription = "Echo the input"\ncommand = ["python3", "-c", '
        '"import sys,json; print(json.dumps(json.load(sys.stdin)))"]\n'
        '[parameters]\ntype = "object"\n[parameters.properties.word]\ntype = "string"\n')
    (root / "vessel.toml").write_text(f"""
[agent]
name = "tester"
description = "A test agent."

[tools]
builtin = ["read", "grep", "glob", "list", "bash"]
declared = ["tools/echo.toml"]

[sandbox]
workdir = "{work}"
deadline_seconds = 20
tool_timeout_seconds = 5
""")
    return root


def _astral_exec_source() -> Path | None:
    configured = os.environ.get("VESSEL_ASTRAL_EXEC")
    found = Path(configured) if configured else Path(__file__).resolve().parents[2] / "backend" / "astral_exec"
    return found if (found / "__init__.py").is_file() else None


def pytest_configure(config):
    # Tests that use astral_exec import the copy of the Deep checkout's package, never a Deep module otherwise.
    try:
        import astral_exec  # noqa: F401
    except ImportError:
        source = _astral_exec_source()
        if source is not None:
            holder = Path(tempfile.mkdtemp(prefix="vessel-exec-"))
            shutil.copytree(source, holder / "astral_exec", ignore=shutil.ignore_patterns("__pycache__"))
            sys.path.insert(0, str(holder))
