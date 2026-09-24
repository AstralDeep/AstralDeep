"""AST-based (no import execution) guard asserting product runtime never imports
security_benchmark or an external benchmark package; runs as a unit test and as
`python -m security_benchmark.isolation_check` for CI.
"""

from __future__ import annotations

import ast
import os
import sys
from typing import List, Tuple

PRODUCT_ROOTS = ("orchestrator", "agents", "shared")

FORBIDDEN_PREFIXES = (
    "security_benchmark",
    "agentdojo",
    "agent_security_bench",
    "injecagent",
)


def _backend_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _iter_py_files(root: str):
    for dirpath, _dirs, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _imported_names(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as fh:
        try:
            tree = ast.parse(fh.read(), filename=path)
        except SyntaxError:
            return []
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.append(node.module)
    return names


def find_violations() -> List[Tuple[str, str]]:
    backend = _backend_dir()
    violations: List[Tuple[str, str]] = []
    for product_root in PRODUCT_ROOTS:
        root = os.path.join(backend, product_root)
        if not os.path.isdir(root):
            continue
        for path in _iter_py_files(root):
            for name in _imported_names(path):
                if any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PREFIXES):
                    rel = os.path.relpath(path, backend)
                    violations.append((rel, name))
    return violations


def main() -> int:
    violations = find_violations()
    if violations:
        print("DEPENDENCY-ISOLATION VIOLATION(S) — product runtime imports eval/benchmark code:")
        for path, name in violations:
            print(f"  {path}  imports  {name}")
        return 1
    print(f"dependency-isolation OK: no product module under {PRODUCT_ROOTS} "
          f"imports {FORBIDDEN_PREFIXES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
