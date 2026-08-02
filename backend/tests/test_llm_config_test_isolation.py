"""Regression guard for the interactive mock user's persisted LLM config."""
from __future__ import annotations

import ast
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_MOCK_USER = "test_user"
LLM_STORE_MUTATORS = {"set", "set_sync", "clear", "clear_sync"}


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        value = statement.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def _resolved_string(node: ast.AST, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def test_db_backed_suites_never_mutate_interactive_mock_user_llm_config():
    """Live-DB tests must use disposable owners, never ``test_user``.

    In development, mock-auth browser sessions resolve to ``test_user``.  A
    test that seeds ``Orchestrator._llm_store`` for that owner silently
    replaces the provider saved by the interactive client.  Check both direct
    calls and helpers such as ``await _t(store.set_sync, user_id, ...)``.
    """
    offenders: list[str] = []
    for path in sorted(BACKEND_ROOT.rglob("test_*.py")):
        if "tests" not in path.relative_to(BACKEND_ROOT).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants = _module_string_constants(tree)
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            target: ast.AST | None = None
            owner_arg: ast.AST | None = None
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr in LLM_STORE_MUTATORS
            ):
                target = call.func
                owner_arg = call.args[0] if call.args else None
            elif (
                call.args
                and isinstance(call.args[0], ast.Attribute)
                and call.args[0].attr in LLM_STORE_MUTATORS
            ):
                target = call.args[0]
                owner_arg = call.args[1] if len(call.args) > 1 else None
            if (
                target is None
                or "_llm_store" not in _dotted_name(target)
                or owner_arg is None
                or _resolved_string(owner_arg, constants) != PROTECTED_MOCK_USER
            ):
                continue
            offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{call.lineno}")

    assert not offenders, (
        "live-DB tests must not mutate the interactive mock user's LLM "
        f"configuration; use a unique disposable user instead: {offenders}"
    )
