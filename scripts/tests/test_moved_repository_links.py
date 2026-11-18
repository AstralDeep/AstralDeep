"""Checks that no tracked Deep file still points at a repository or folder that moved into this repository.
Spec history, provenance records and the files that detect or document the old layout on purpose are listed by path.
It complements test_moved_packages.py, which pins the moved packages themselves.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MOVED = re.compile(
    r"AstralDeep/(?:AstralVessel|AstralPlane|LETS-demo|mcp_server_examples)(?![A-Za-z0-9_-])"
    r"|components/AstralPlane"
)
HISTORY_PREFIXES = ("specs/", ".superpowers/", "plane/provenance/")
HISTORY_FILES = frozenset({".gitleaksignore", "vessel/docs/changelog.md"})
ARCHIVE_BY_FILE = {
    "plane/tests/test_provenance.py": "asserts the recorded origin of the Plane import",
    ".github/workflows/backend-web-qualification-protected.yml": "protected baseline checkout reads the archive",
    "backend/persistent_agents/framework_execution.md": "links to pull requests that exist only in the archive",
    "contracts/component-ownership.json": "ownership record keeps the canonical origin of Plane",
    "contracts/extraction-provenance.schema.json": "schema admits the recorded Plane origin",
    "migration/checkpoint-074.json": "checkpoint of the original extraction",
    "scripts/migration/build_extraction_manifest.py": "builds the extraction provenance record",
    "scripts/verify_migration_provenance.py": "verifies the extraction provenance record",
    "scripts/prepare_plane_upgrade_baseline.py": "rebuilds the pre-move gitlink baseline",
    "scripts/ci_plane_lane.py": "detects the pre-move gitlink path",
    "backend/tests/test_ci_plane_lane.py": "covers the pre-move gitlink detection",
    "scripts/tests/test_verify_migration_provenance.py": "covers the provenance verifier",
    "scripts/tests/test_verify_component_ownership.py": "covers the ownership verifier",
    "scripts/tests/test_install_local_components.py": "covers refusal of the pre-move component path",
    "scripts/tests/test_moved_repository_links.py": "names the moved repositories to search for them",
}


def _tracked() -> list[str]:
    done = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPOSITORY_ROOT, capture_output=True, check=True
    )
    return [name for name in done.stdout.decode().split("\0") if name]


def _hits(relative: str) -> list[int]:
    path = REPOSITORY_ROOT / relative
    if not path.is_file() or path.is_symlink():
        return []
    data = path.read_bytes()
    if b"\0" in data:
        return []
    return [
        number
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1)
        if MOVED.search(line)
    ]


def test_no_tracked_file_points_at_a_moved_repository() -> None:
    offenders = {
        name: lines
        for name in _tracked()
        if not name.startswith(HISTORY_PREFIXES)
        and name not in ARCHIVE_BY_FILE
        and name not in HISTORY_FILES
        and (lines := _hits(name))
    }
    assert offenders == {}


def test_every_listed_exception_still_needs_its_entry() -> None:
    stale = sorted(
        name
        for name in ARCHIVE_BY_FILE
        if not (REPOSITORY_ROOT / name).is_file() or not _hits(name)
    )
    assert stale == []


def test_search_pattern_catches_each_moved_name() -> None:
    for text in (
        "https://github.com/AstralDeep/AstralVessel",
        "git clone https://github.com/AstralDeep/AstralPlane.git",
        "repository: AstralDeep/LETS-demo",
        "AstralDeep/mcp_server_examples/issues",
        "path components/AstralPlane",
    ):
        assert MOVED.search(text), text
    for text in ("AstralDeep/AstralDeep", "AstralDeep/AstralVesselVscode", "plane/src"):
        assert not MOVED.search(text), text
