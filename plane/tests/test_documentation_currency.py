"""Tests that README.md, docs/, and provenance/README.md state the live schema revision,
migration path, registry digest, and repository catalog taken from astralplane's code. A schema
or catalog change therefore cannot leave a current-state claim or the joint-restore list stale.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from astralplane import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_DIGEST,
    SCHEMA_REVISION,
    create_repository_catalog,
)
from astralplane.database.migrations import MIGRATION_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
MIGRATION_GUIDE = ROOT / "docs" / "migration-and-recovery.md"
DOCUMENTS = (README, *sorted((ROOT / "docs").glob("*.md")), ROOT / "provenance" / "README.md")
CURRENT_REVISION = re.compile(
    r"\bcurrent (?:Plane )?(?:schema(?: is|:)? )?`(\d{3}\.\d{3})`", re.IGNORECASE
)
CURRENT_DIGEST = re.compile(
    r"\bcurrent (?:migration )?registry digest (?:is )?`([0-9a-f]{64})`", re.IGNORECASE
)


def _text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _canonical_path() -> str:
    migrations = MIGRATION_REGISTRY.migrations
    return " -> ".join(
        (migrations[0].source_revisions[0], *(item.target_revision for item in migrations))
    )


def test_readme_states_the_live_schema_path_and_catalog() -> None:
    text = _text(README)
    catalog = tuple(create_repository_catalog().as_mapping())
    members = ", ".join(f"`{key}`" for key in catalog[:-1]) + f", and `{catalog[-1]}`"

    assert f"Current schema: `{SCHEMA_REVISION}`" in text
    assert f"`{_canonical_path()}`" in text
    assert f"Its {len(catalog)} members, in `RepositoryCatalog.as_mapping()` order, are" in text
    assert f"are {members}." in text


def test_migration_guide_states_the_live_path_and_restores_every_revision() -> None:
    text = _text(MIGRATION_GUIDE)
    restore = text.split("### Joint restore procedure", 1)[1].split(" ## ", 1)[0]
    predecessors = CURRENT_DATA_PLANE_REVISION.read_compatible_from

    assert f"The canonical current path is `{_canonical_path()}`" in text
    assert [revision for revision in predecessors if f"`{revision}`" not in restore] == []
    assert f"A restored `{SCHEMA_REVISION}` state must carry the exact current registry" in restore


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_current_state_claims_name_the_live_revision_and_digest(document: Path) -> None:
    text = _text(document)

    assert set(CURRENT_REVISION.findall(text)) <= {SCHEMA_REVISION}
    assert set(CURRENT_DIGEST.findall(text)) <= {MIGRATION_DIGEST}


def test_current_state_patterns_detect_stale_claims() -> None:
    stale = (
        "The current Plane schema is `079.001`. Current schema: `074.004`. The current schema is "
        "`088.003`. A current `075.001` marker. The current migration registry digest is "
    )

    assert CURRENT_REVISION.findall(stale) == ["079.001", "074.004", "088.003", "075.001"]
    assert CURRENT_DIGEST.findall(f"{stale}`{'0' * 64}`") == ["0" * 64]
