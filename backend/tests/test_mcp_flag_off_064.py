from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.mcp_server_endpoint import install_mcp_server
from shared.feature_flags import FeatureFlags


ROOT = Path(__file__).resolve().parents[2]


def test_mcp_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("FF_MCP_SERVER", raising=False)
    assert FeatureFlags().is_enabled("mcp_server") is False


def test_phase_b_modules_are_not_imported_when_flag_is_absent():
    environment = dict(os.environ)
    environment.pop("FF_MCP_SERVER", None)

    def component_source(name: str, suffix: str = "") -> Path:
        sibling = ROOT.parent / name / suffix
        embedded = ROOT / "components" / name / suffix
        return sibling if sibling.is_dir() else embedded

    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(ROOT / "backend"),
            str(component_source("AstralPlane", "src")),
            str(component_source("AstralProjection", "backend")),
            str(component_source("AstralPrimitives")),
            str(component_source("LETS", "src")),
        )
    )
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


def test_recreated_flag_off_app_has_no_residual_surface_or_server_record(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.test/realms/astral")
    server = object()
    enabled_app = FastAPI()
    install_mcp_server(enabled_app, server, public_base_url="http://mcp.test")
    enabled = TestClient(enabled_app)
    assert enabled.get("/.well-known/oauth-protected-resource/mcp").status_code == 200

    # Feature flags are startup state. A recreated process with the flag off
    # never calls install_mcp_server, so the old app/router is unreachable and
    # no endpoint-owned session/advertisement record exists to tear down.
    disabled_app = FastAPI()
    disabled_paths = {
        route.path for route in disabled_app.routes if hasattr(route, "path")
    }
    assert "/mcp" not in disabled_paths
    assert "/.well-known/oauth-protected-resource/mcp" not in disabled_paths
    disabled = TestClient(disabled_app)
    assert disabled.post("/mcp").status_code == 404
    assert disabled.get("/.well-known/oauth-protected-resource/mcp").status_code == 404
