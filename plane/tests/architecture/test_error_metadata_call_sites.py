"""Tests that audit every literal PlaneError metadata construction in astralplane source.
Literal dictionaries must use bounded non-empty string-literal keys, stay within the
entry bound, and never inline nested payload collections; dynamic mappings remain
covered by the runtime bounds in astralplane.errors.
"""

from __future__ import annotations

import ast
from pathlib import Path

from astralplane.errors import MAX_METADATA_ENTRIES, MAX_METADATA_KEY_LENGTH

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "astralplane"

_NESTED_VALUE_NODES = (
    ast.Dict,
    ast.List,
    ast.Set,
    ast.DictComp,
    ast.ListComp,
    ast.SetComp,
)


def _error_constructor_name(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return ""


def _literal_metadata_violations(tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _error_constructor_name(node).endswith("Error"):
            continue
        for keyword in node.keywords:
            if keyword.arg != "metadata" or not isinstance(keyword.value, ast.Dict):
                continue
            literal = keyword.value
            if any(key is None for key in literal.keys):
                violations.append(f"line {node.lineno}: metadata uses a ** unpacking")
                continue
            keys = [key.value for key in literal.keys]
            if len(keys) > MAX_METADATA_ENTRIES:
                violations.append(f"line {node.lineno}: metadata has {len(keys)} entries")
            for key in keys:
                if not isinstance(key, str) or not key:
                    violations.append(f"line {node.lineno}: metadata key {key!r} is not text")
                elif len(key) > MAX_METADATA_KEY_LENGTH:
                    violations.append(f"line {node.lineno}: metadata key {key!r} is too long")
            for value in literal.values:
                if isinstance(value, _NESTED_VALUE_NODES):
                    violations.append(
                        f"line {node.lineno}: metadata value nests a {type(value).__name__}"
                    )
    return violations


def test_error_metadata_call_sites_use_bounded_literal_identifiers() -> None:
    violations: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for violation in _literal_metadata_violations(tree):
            violations.append(f"{path.relative_to(SOURCE_ROOT)}: {violation}")

    assert violations == []


if __name__ == "__main__":
    test_error_metadata_call_sites_use_bounded_literal_identifiers()
