"""Static test confirming generated-agent bundle publication is Plane-owned, with no
second filesystem publication engine coordinated by Deep itself.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unicodedata

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _production_python() -> tuple[Path, ...]:
    paths: list[Path] = []
    for path in (ROOT / "backend").rglob("*.py"):
        relative = path.relative_to(ROOT)
        if (
            "tests" in relative.parts
            or "tmp" in relative.parts
            or path.name.startswith("test_")
        ):
            continue
        paths.append(path)
    return tuple(sorted(paths))


def _legacy_importers() -> list[str]:
    paths: list[str] = []
    for path in _production_python():
        source = path.read_text(encoding="utf-8")
        if "artifact_publication" not in unicodedata.normalize("NFKC", source):
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                forbidden = any(
                    alias.name == "orchestrator.artifact_publication"
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                forbidden = (
                    node.module == "orchestrator"
                    and any(
                        alias.name == "artifact_publication"
                        for alias in node.names
                    )
                ) or node.module == "orchestrator.artifact_publication"
            else:
                forbidden = False
            if forbidden:
                paths.append(path.relative_to(ROOT).as_posix())
                break
    return sorted(paths)


def test_generated_agent_filesystem_publication_is_plane_owned() -> None:
    assert not (
        ROOT / "backend" / "orchestrator" / "artifact_publication.py"
    ).exists()
    assert not (
        ROOT / "backend" / "tests" / "test_agent_artifact_publication_060.py"
    ).exists()
    assert _legacy_importers() == []


@pytest.mark.parametrize(("source", "forbidden"), [
    ("value = 1\n", False),
    ("import orchestrator.artifact_publication\n", True),
    ("from orchestrator import artifact_publication\n", True),
    ("from orchestrator import 𝒶rtifact_publication\n", True),
    ("value = 'artifact_publication'\n", False),
])
def test_import_inventory_keeps_normalized_candidates_without_unrelated_asts(
    tmp_path, monkeypatch, source, forbidden,
):
    backend = tmp_path / "backend"
    backend.mkdir()
    path = backend / "sample.py"
    path.write_text(source, encoding="utf-8")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    original = ast.parse
    parsed = []

    def observed(source, filename="<unknown>", mode="exec", **kwargs):
        parsed.append(filename)
        return original(source, filename=filename, mode=mode, **kwargs)

    monkeypatch.setattr(ast, "parse", observed)
    assert _legacy_importers() == (["backend/sample.py"] if forbidden else [])
    if source == "value = 1\n":
        assert parsed == []
