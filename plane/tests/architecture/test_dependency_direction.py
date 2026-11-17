"""Tests that astralplane's runtime package never imports AstralDeep,
agent_constitution, or UI/PySide6 modules, keeping the dependency direction one-way.
"""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "astraldeep",
        "astralprims",
        "astralprojection",
        "lets",
        "agent_constitution",
        "agents",
        "audit",
        "backend",
        "dreaming",
        "feedback",
        "knowledge",
        "knowledge_packs",
        "llm_config",
        "onboarding",
        "orchestrator",
        "personalization",
        "qual_audit",
        "rote",
        "scheduler",
        "seeds",
        "shared",
        "verification",
        "voice_agent",
        "webrender",
        "PySide6",
        "fastapi",
        "fastmcp",
        "httpx",
        "jinja2",
        "livekit",
        "mcp",
        "paramiko",
        "requests",
        "starlette",
        "uvicorn",
        "websockets",
    }
)


def _imported_modules(tree: ast.AST) -> tuple[tuple[int, str], ...]:
    imports: list[tuple[int, str]] = []
    importlib_aliases: set[str] = set()
    import_module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append((node.lineno, node.module))
            if node.module == "importlib":
                import_module_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        module_argument = node.args[0]
        if not isinstance(module_argument, ast.Constant) or not isinstance(
            module_argument.value, str
        ):
            continue
        function = node.func
        is_dynamic_import = (
            isinstance(function, ast.Name) and function.id in {"__import__", *import_module_aliases}
        ) or (
            isinstance(function, ast.Attribute)
            and function.attr == "import_module"
            and isinstance(function.value, ast.Name)
            and function.value.id in importlib_aliases
        )
        if is_dynamic_import:
            imports.append((node.lineno, module_argument.value))
    return tuple(imports)


def _forbidden_imports(source_root: Path) -> tuple[str, ...]:
    violations: list[str] = []
    for source_path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for line_number, module_name in _imported_modules(tree):
            root = module_name.partition(".")[0]
            if root in FORBIDDEN_IMPORT_ROOTS:
                relative_path = source_path.relative_to(source_root.parent)
                violations.append(
                    f"{relative_path}:{line_number}: forbidden import {module_name!r}"
                )
    return tuple(violations)


def test_astralplane_runtime_has_no_forbidden_imports() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    source_root = repository_root / "src" / "astralplane"

    assert source_root.is_dir(), f"AstralPlane source root is missing: {source_root}"
    violations = _forbidden_imports(source_root)
    assert not violations, "AstralPlane dependency-direction violations:\n" + "\n".join(violations)


def test_literal_dynamic_import_aliases_are_scanned() -> None:
    tree = ast.parse(
        """
import importlib as il
from importlib import import_module as load

il.import_module("backend.shared")
load("orchestrator.policy")
__import__("astralprojection.chrome", fromlist=("chrome",))
"""
    )

    imported = {module for _, module in _imported_modules(tree)}

    assert {"backend.shared", "orchestrator.policy", "astralprojection.chrome"} <= imported


def test_nonliteral_and_unrelated_calls_are_not_misclassified() -> None:
    tree = ast.parse(
        """
import importlib

module_name = "backend.shared"
importlib.import_module(module_name)
registry.import_module("backend.shared")
helper("orchestrator.policy")
"""
    )

    assert _imported_modules(tree) == ((2, "importlib"),)


if __name__ == "__main__":
    test_astralplane_runtime_has_no_forbidden_imports()
