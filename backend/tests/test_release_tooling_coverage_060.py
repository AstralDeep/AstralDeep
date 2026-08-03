"""Isolation and public-surface guards for changed-coverage release tooling."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_changed_coverage.py"
XCCOV_EXPORTER = REPO_ROOT / "scripts" / "export_xccov_line_coverage.py"

if not (REPO_ROOT / "scripts").is_dir():  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )


def test_changed_coverage_tool_is_stdlib_only_and_documents_public_apis() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
    imported: set[str] = set()
    public_functions: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            public_functions[node.name] = node
    imported.discard("__future__")
    assert imported <= sys.stdlib_module_names
    expected = {
        "classify_path",
        "select_revisions",
        "validate_revisions",
        "read_changed_lines",
        "parse_coverage_report",
        "evaluate_changed_coverage",
        "main",
    }
    assert expected <= set(public_functions)
    for name in expected:
        assert ast.get_docstring(public_functions[name]), (
            f"{name} needs a public contract"
        )


def test_changed_coverage_cli_exposes_every_platform_report_partition() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for option in (
        "--backend-python",
        "--voice-worker-python",
        "--tooling-python",
        "--windows-python",
        "--javascript",
        "--android-app",
        "--android-core",
        "--ios",
        "--macos",
        "--watchos",
        "--coverage-mode",
        "--base-sha",
        "--candidate-sha",
        "--event-name",
        "--event-path",
        "--fail-under",
        "--output",
    ):
        assert option in completed.stdout
    assert "--apple " not in completed.stdout


def test_collector_source_pins_nul_diff_and_has_no_shell_execution() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"--name-only"' in source
    assert '"-z"' in source
    assert '"--diff-filter=AM"' in source
    assert '"--no-renames"' in source
    assert "shell=True" not in source
    assert "os.system" not in source


def test_xccov_exporter_is_stdlib_only_documented_and_has_no_shell_execution() -> None:
    source = XCCOV_EXPORTER.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(XCCOV_EXPORTER))
    imported: set[str] = set()
    public_functions: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            public_functions[node.name] = node
    imported.discard("__future__")
    assert imported <= sys.stdlib_module_names
    assert {"export_xccov", "main"} <= set(public_functions)
    assert all(ast.get_docstring(node) for node in public_functions.values())
    assert "shell=True" not in source
    assert "os.system" not in source

    completed = subprocess.run(
        [sys.executable, str(XCCOV_EXPORTER), "--help"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for option in ("--repo", "--xcresult", "--output", "--platform"):
        assert option in completed.stdout
