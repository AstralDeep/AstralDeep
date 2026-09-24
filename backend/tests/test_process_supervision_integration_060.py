"""Tests that every feature's child-process launch goes through
backend/shared/process_supervision.py: agent lifecycle start/stop and the start
entrypoint inject one supervisor, and no module owns its own process tree or pipes.
"""

from __future__ import annotations

import ast
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_LIFECYCLE_PATH = _ROOT / "backend" / "orchestrator" / "agent_lifecycle.py"
_START_PATH = _ROOT / "backend" / "start.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"missing function {name}")


def _attribute_calls(node: ast.AST) -> list[str]:
    return [
        call.func.attr
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    ]


def _direct_process_calls(tree: ast.Module) -> list[str]:
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess":
            findings.append(f"subprocess.{node.func.attr}")
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
            if node.func.attr in {"kill", "killpg"}:
                findings.append(f"os.{node.func.attr}")
    return findings


def _assert_shared_supervisor_import(tree: ast.Module) -> None:
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "shared.process_supervision"
        for alias in node.names
    }
    assert "ProcessSupervisor" in imports
    assert "ProcessOwner" in imports
    assert "TerminationReason" in imports


def test_agent_lifecycle_injects_one_supervisor_for_start_and_stop() -> None:
    tree = _tree(_LIFECYCLE_PATH)
    _assert_shared_supervisor_import(tree)
    assert _direct_process_calls(tree) == []

    constructor = _function(tree, "__init__")
    assert "process_supervisor" in {
        argument.arg for argument in constructor.args.args + constructor.args.kwonlyargs
    }
    start = _function(tree, "start_draft_agent")
    stop = _function(tree, "stop_draft_agent")
    assert "spawn" in _attribute_calls(start)
    assert "terminate" in _attribute_calls(stop)

    # A post-exit sync stderr read can deadlock or race the reader
    source = _LIFECYCLE_PATH.read_text(encoding="utf-8")
    assert "proc.stderr.read" not in source
    assert "subprocess.PIPE" not in source


def test_start_entrypoint_uses_supervisor_for_every_child_and_cleanup() -> None:
    tree = _tree(_START_PATH)
    _assert_shared_supervisor_import(tree)
    assert _direct_process_calls(tree) == []

    main = _function(tree, "main")
    assert "process_supervisor" in {
        argument.arg for argument in main.args.args + main.args.kwonlyargs
    }
    calls = _attribute_calls(main)
    assert "spawn" in calls
    assert "terminate" in calls or "terminate_all" in calls

    source = _START_PATH.read_text(encoding="utf-8")
    assert "subprocess.Popen" not in source
    assert "taskkill" not in source


def test_only_shared_module_may_own_process_tree_and_pipe_policy() -> None:
    for path in (_LIFECYCLE_PATH, _START_PATH):
        tree = _tree(path)
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "subprocess" not in imported_modules
        assert _direct_process_calls(tree) == []
