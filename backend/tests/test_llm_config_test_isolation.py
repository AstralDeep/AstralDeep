"""Tests that no DB-backed suite mutates the interactive mock user's (test_user)
persisted LLM config, since that write would silently replace the provider the
interactive client saved; scans source for direct and helper-wrapped store writes.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unicodedata

import pytest


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
    offenders: list[str] = []
    for path in sorted(BACKEND_ROOT.rglob("test_*.py")):
        if "tests" not in path.relative_to(BACKEND_ROOT).parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "_llm_store" not in unicodedata.normalize("NFKC", source):
            continue
        tree = ast.parse(source, filename=str(path))
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


def test_unrelated_sources_do_not_need_an_ast(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    unrelated = tests / "test_unrelated.py"
    unrelated.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "BACKEND_ROOT", tmp_path)
    parsed = []
    original = ast.parse

    def observed(source, filename="<unknown>", mode="exec", **kwargs):
        parsed.append(filename)
        return original(source, filename=filename, mode=mode, **kwargs)

    monkeypatch.setattr(ast, "parse", observed)
    test_db_backed_suites_never_mutate_interactive_mock_user_llm_config()
    assert parsed == []


@pytest.mark.parametrize("statement", [
    'orch._llm_store.set("test_user", model="synthetic")',
    'await asyncio.to_thread(orch._llm_store.clear, "test_user")',
    'orch._𝓁𝓁𝓂_store.set("test_user", model="synthetic")',
])
def test_source_selection_keeps_direct_helper_and_normalized_identifiers(
    tmp_path, monkeypatch, statement,
):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_violation.py").write_text(statement + "\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "BACKEND_ROOT", tmp_path)
    with pytest.raises(AssertionError, match="tests/test_violation.py:1"):
        test_db_backed_suites_never_mutate_interactive_mock_user_llm_config()
