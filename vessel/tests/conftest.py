# Shared fixtures: a tiny agent package whose tools run in the sandbox.

from __future__ import annotations

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
