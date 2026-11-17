"""Tests for docs/recovery-and-writable-readiness.md and the facade evidence it names:
exact writable schema identity, read-compatible predecessors, local-only health, and
resolvable traceability to executable acceptance cases.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from astralplane import (
    BLOB_LAYOUT_VERSION,
    CONTRACT_VERSION,
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_DIGEST,
    PACKAGE_VERSION,
    RECOVERY_CONTRACT_VERSION,
    SCHEMA_REVISION,
    BootStatus,
    PlaneHealth,
    inspect_compatibility,
)
from astralplane.database.pool import PoolSnapshot

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "recovery-and-writable-readiness.md"
TEST_REFERENCE = re.compile(r"`(tests/[^`]+?\.py::[A-Za-z_][A-Za-z0-9_]*)`")
SOURCE_REFERENCE = re.compile(r"`(src/[^`]+?\.py)`")
PRECONDITION_ROW = re.compile(r"^\| (R[0-9]{1,2}) \|")


def _text() -> str:
    assert CONTRACT.is_file(), f"recovery contract is missing: {CONTRACT}"
    return CONTRACT.read_text(encoding="utf-8")


def _defined_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.update(
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return names


def test_contract_document_declares_status_and_traceability() -> None:
    text = _text()

    for section in (
        "## Status and scope",
        "## Terms",
        "## Ownership boundary",
        "## Normative preconditions",
        "## Unsupported states",
        "## Why common shortcuts are insufficient",
        "## Source traceability",
        "## Traceability to current behavior and executable cases",
        "## Acceptance decision",
    ):
        assert section in text
    assert "specification status is **defined and reviewable**" in text
    assert "runtime status is **partial**" in text
    assert RECOVERY_CONTRACT_VERSION in text
    assert "PlaneHealth.ready" in text
    assert "PostgreSQL-only" in text
    assert "host-owned" in text

    references = TEST_REFERENCE.findall(text)
    assert len(set(references)) >= 15
    for reference in sorted(set(references)):
        relative, test_name = reference.split("::", 1)
        path = ROOT / relative
        assert path.is_file(), f"traceability path does not exist: {reference}"
        assert test_name.startswith("test_"), f"traceability node is not a test: {reference}"
        assert test_name in _defined_tests(path), f"traceability node does not exist: {reference}"

    sources = SOURCE_REFERENCE.findall(text)
    assert len(set(sources)) >= 8
    for source in sorted(set(sources)):
        assert (ROOT / source).is_file(), f"source traceability path does not exist: {source}"

    rows = {
        match.group(1): line
        for line in text.splitlines()
        if (match := PRECONDITION_ROW.match(line)) is not None
    }
    assert set(rows) == {f"R{number}" for number in range(1, 13)}
    for precondition, row in rows.items():
        assert TEST_REFERENCE.search(row), f"{precondition} has no executable acceptance case"


def test_current_revision_is_the_only_declared_writable_target() -> None:
    current = inspect_compatibility(
        expected_contract_version=CONTRACT_VERSION,
        observed_schema_revision=SCHEMA_REVISION,
        consumer_version=PACKAGE_VERSION,
    )

    assert current.compatible
    assert current.schema_revision == SCHEMA_REVISION
    assert current.schema_revision == CURRENT_DATA_PLANE_REVISION.schema_revision
    assert current.migration_digest == MIGRATION_DIGEST
    assert current.migration_digest == CURRENT_DATA_PLANE_REVISION.migration_digest
    assert current.blob_layout_version == BLOB_LAYOUT_VERSION
    assert current.recovery_contract_version == RECOVERY_CONTRACT_VERSION
    assert current.reasons == ()

    predecessors = CURRENT_DATA_PLANE_REVISION.read_compatible_from
    assert predecessors
    assert SCHEMA_REVISION not in predecessors
    for predecessor in predecessors:
        readable = inspect_compatibility(
            expected_contract_version=CONTRACT_VERSION,
            observed_schema_revision=predecessor,
            consumer_version=PACKAGE_VERSION,
        )
        assert readable.compatible
        assert CURRENT_DATA_PLANE_REVISION.accepts_reader_at(predecessor)
        assert predecessor != CURRENT_DATA_PLANE_REVISION.schema_revision

    future = inspect_compatibility(
        expected_contract_version=CONTRACT_VERSION,
        observed_schema_revision="999.001",
        consumer_version=PACKAGE_VERSION,
    )
    assert not future.compatible
    assert "schema_revision_incompatible" in future.reasons


def test_health_ready_is_local_and_not_restore_readiness() -> None:
    compatibility = inspect_compatibility(
        expected_contract_version=CONTRACT_VERSION,
        observed_schema_revision=SCHEMA_REVISION,
        consumer_version=PACKAGE_VERSION,
    )
    health = PlaneHealth(
        compatibility=compatibility,
        boot_status=BootStatus.READY,
        pool=PoolSnapshot(borrowed=0, closed=False),
    )

    assert health.ready
    evidence = health.to_dict()
    assert set(evidence) == {"boot_status", "compatibility", "pool", "ready"}
    assert evidence["ready"] is True
    assert not {"restore", "blobs", "sessions", "fencing", "authority"} & set(evidence)
    assert not {"restore", "blobs", "sessions", "fencing", "authority"} & set(
        evidence["compatibility"]
    )
    assert not {"restore", "blobs", "sessions", "fencing", "authority"} & set(evidence["pool"])

    not_ready = PlaneHealth(
        compatibility=compatibility,
        boot_status=BootStatus.NEW,
        pool=PoolSnapshot(borrowed=0, closed=False),
    )
    assert not not_ready.ready