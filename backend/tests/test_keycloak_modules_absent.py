"""Static guard that the removed sign-in, session and identity-server modules stay gone: no file exists for them and no source or test file imports or names them.
It walks the backend, scripts and sdk trees with the ast module, so deferred and string-form references count, and later removal tasks extend REMOVED_MODULES.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("backend", "scripts", "sdk")
SKIP_PARTS = {".venv", "venv", "node_modules", "__pycache__", "data", "tmp", "build"}

REMOVED_MODULES = (
    "orchestrator.delegation",
    "orchestrator.device_login",
    "orchestrator.institution_api",
    "orchestrator.institution_meshes",
    "orchestrator.institution_policy",
    "orchestrator.institution_runtime",
    "orchestrator.mesh_admission",
    "orchestrator.mesh_api",
    "orchestrator.mesh_web",
    "orchestrator.native_session_custody",
    "orchestrator.offline_grant",
    "orchestrator.remote_mcp_api",
    "orchestrator.session_consent",
    "orchestrator.ui_auth_renewal",
    "orchestrator.web_auth",
    "orchestrator.work_continuation_authority",
    "orchestrator.work_resume",
    "orchestrator.work_wake",
    "shared.auth_clients",
    "shared.institution_profile",
    "shared.jwks_cache",
    "shared.qr",
    "shared.token_verify",
)


def _python_files():
    for root in SCAN_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if SKIP_PARTS.isdisjoint(path.relative_to(REPO).parts):
                yield path


def _references(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module
            for alias in node.names:
                yield node.lineno, f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value.isidentifier() or all(part.isidentifier() for part in value.split(".")):
                yield node.lineno, value


def _hits(module: str, reference: str) -> bool:
    return reference == module or reference.startswith(module + ".")


def test_removed_modules_have_no_file():
    for module in REMOVED_MODULES:
        relative = Path("backend", *module.split("."))
        assert not (REPO / relative.with_suffix(".py")).exists(), module
        assert not (REPO / relative).exists(), module


def test_no_file_references_a_removed_module():
    offenders = []
    for path in _python_files():
        if path.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for lineno, reference in _references(tree):
            for module in REMOVED_MODULES:
                if _hits(module, reference):
                    offenders.append(f"{path.relative_to(REPO)}:{lineno} {reference}")
    assert offenders == []


@pytest.mark.parametrize("name", ["web_auth_router", "kiosk_router"])
def test_orchestrator_mounts_no_sign_in_router(name):
    source = (REPO / "backend" / "orchestrator" / "orchestrator.py").read_text(encoding="utf-8")
    assert name not in source
