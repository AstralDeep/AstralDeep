"""Tests that backend/shared/primitives.py is imported nowhere in backend source outside
its own file and the guarded parity cross-check, scanning import statements only, not
comments or docstrings.
"""

import ast
import os

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALLOWLIST = {
    os.path.join(BACKEND, "shared", "primitives.py"),
    os.path.join(BACKEND, "tests", "test_astralprims_parity.py"),
    os.path.join(BACKEND, "tests", "test_no_legacy_primitives.py"),
}


def _iter_py_files():
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in (".venv", "venv", "__pycache__", "node_modules", "static", "vendor")]
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(root, fn)


def test_no_legacy_primitives_imports():
    offenders = []
    for path in _iter_py_files():
        if path in ALLOWLIST:
            continue
        try:
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") == "shared.primitives":
                offenders.append(f"{path}:{node.lineno} (from shared.primitives import ...)")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "shared.primitives":
                        offenders.append(f"{path}:{node.lineno} (import shared.primitives)")
    assert not offenders, "Legacy shared.primitives still imported:\n" + "\n".join(offenders)
