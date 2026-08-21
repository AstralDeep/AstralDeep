"""Static ownership guard for Plane-owned generated-agent bundle mechanics."""

from __future__ import annotations

import ast
from pathlib import Path


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
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
    """Deep retains policy coordination, never a second immutable FS engine."""

    assert not (
        ROOT / "backend" / "orchestrator" / "artifact_publication.py"
    ).exists()
    assert not (
        ROOT / "backend" / "tests" / "test_agent_artifact_publication_060.py"
    ).exists()
    assert _legacy_importers() == []
