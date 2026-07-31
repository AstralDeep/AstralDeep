from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from shared.feature_flags import FeatureFlags


ROOT = Path(__file__).resolve().parents[2]


def test_mcp_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("FF_MCP_SERVER", raising=False)
    assert FeatureFlags().is_enabled("mcp_server") is False


def test_phase_b_modules_are_not_imported_when_flag_is_absent():
    environment = dict(os.environ)
    environment.pop("FF_MCP_SERVER", None)
    environment["PYTHONPATH"] = str(ROOT / "backend")
    probe = (
        "import sys; import orchestrator.orchestrator; "
        "names=('orchestrator.mcp_server_endpoint','orchestrator.mcp_authz',"
        "'orchestrator.mcp_projection'); "
        "assert not any(name in sys.modules for name in names)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_endpoint_install_is_structurally_guarded_by_the_flag():
    source_path = ROOT / "backend" / "orchestrator" / "orchestrator.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        condition = ast.unparse(node.test)
        if condition != "flags.is_enabled('mcp_server')":
            continue
        body = "\n".join(ast.unparse(item) for item in node.body)
        guarded = (
            "from orchestrator.mcp_server_endpoint import install_mcp_server" in body
            and "install_mcp_server(app, self)" in body
        )
        if guarded:
            break
    assert guarded
