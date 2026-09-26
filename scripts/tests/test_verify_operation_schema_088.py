"""Tests that the migration's data-only imports are explicit and tamper-evident:
reviewed digests, alias/rebinding refusal, and no inherited expression evaluator.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_verify_composition import (
    COMPONENT_PATHS,
    REPOSITORY_ROOT,
    _codes,
    _repin_component,
    _rewrite_manifest,
    _verify,
    _write,
    _write_migration,
    composition,
)
from test_verify_composition import checkout as checkout

ASSIGNMENT_IMPORT = (
    "from astralplane.database.assignment_schema import ASSIGNMENT_SCHEMA_STATEMENTS\n"
)
OPERATION_IMPORT = (
    "from astralplane.database.operation_schema import OPERATION_SCHEMA_STATEMENTS\n"
)
ASSIGNMENT_SQL = ("CREATE TABLE assignment (id UUID)",)
OPERATION_SQL = (
    "ALTER TABLE assignment ADD COLUMN profile TEXT",
    "CREATE TABLE receipt (note TEXT DEFAULT 'β')",
)
EXPECTED_PLANE_MIGRATION_SHA256 = (
    "35741bd0de148f836cd8b75b160531013836a61bd46b9e17e7790641412979d8"
)
REGISTRY = (
    "ASSIGNMENT_ALIAS = ASSIGNMENT_SCHEMA_STATEMENTS\n"
    "OPERATION_ALIAS = (*OPERATION_SCHEMA_STATEMENTS,)\n"
    'A = Migration(name="plane-079", source_revisions=("075.001",), '
    'target_revision="079.001", checksum=_statements_checksum(ASSIGNMENT_ALIAS), '
    "operation=apply_assignment)\n"
    'O = Migration(name="plane-088", source_revisions=("079.001",), '
    'target_revision="088.001", checksum=_statements_checksum(OPERATION_ALIAS), '
    "operation=apply_operation)\n"
    "MIGRATION_REGISTRY = MigrationRegistry((A, O))\n"
)


def _schema(root: Path, stem: str, statements: tuple[str, ...]) -> Path:
    path = root / f"src/astralplane/database/{stem}_schema.py"
    _write(path, f'"""Reviewed {stem} declarations."""\n'
           f"{stem.upper()}_SCHEMA_STATEMENTS = {statements!r}\n")
    return path


def _component(root: Path, *, operation_import: str = OPERATION_IMPORT,
               extra: str = "") -> Path:
    _write_migration(root, ASSIGNMENT_IMPORT + operation_import + extra + REGISTRY)
    _schema(root, "assignment", ASSIGNMENT_SQL)
    _schema(root, "operation", OPERATION_SQL)
    return root


def _expected_digest() -> str:
    entries = []
    for name, before, after, statements in (
        ("plane-079", "075.001", "079.001", ASSIGNMENT_SQL),
        ("plane-088", "079.001", "088.001", OPERATION_SQL),
    ):
        checksum = hashlib.sha256(json.dumps(
            statements, ensure_ascii=True, separators=(",", ":")
        ).encode("ascii")).hexdigest()
        entries.append({"name": name, "source_revisions": [before],
                        "target_revision": after, "checksum": checksum})
    return hashlib.sha256(json.dumps(
        entries, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")).hexdigest()


def test_both_reviewed_imports_match_independent_canonical_digest_without_execution(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "candidate-executed.txt"
    root = _component(tmp_path / "Plane", extra=(
        "import nonexistent_candidate_package\n"
        f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')\n"
    ))
    assert composition._plane_migration_digest(root) == _expected_digest()
    assert not marker.exists()


def test_exact_pinned_operation_and_assignment_files_match_current_digest() -> None:
    root = REPOSITORY_ROOT / COMPONENT_PATHS["astral-plane"]
    assert composition._plane_migration_digest(root) == EXPECTED_PLANE_MIGRATION_SHA256


@pytest.mark.parametrize("operation_import", [
    "from arbitrary.operation_schema import OPERATION_SCHEMA_STATEMENTS\n",
    "from astralplane.database.assignment_schema import OPERATION_SCHEMA_STATEMENTS\n",
    "from .operation_schema import OPERATION_SCHEMA_STATEMENTS\n",
    "from astralplane.database.operation_schema import OPERATION_SCHEMA_STATEMENTS as ALIAS\n",
    "from astralplane.database.operation_schema import OTHER as OPERATION_SCHEMA_STATEMENTS\n",
    "from astralplane.database.operation_schema import OPERATION_SCHEMA_STATEMENTS, OTHER\n",
    "from astralplane.database.operation_schema import *\n",
    OPERATION_IMPORT + OPERATION_IMPORT,
    "if True:\n    " + OPERATION_IMPORT,
])
def test_operation_import_refuses_aliases_wrong_modules_and_ambiguous_locations(
    tmp_path: Path, operation_import: str,
) -> None:
    root = _component(tmp_path / "Plane", operation_import=operation_import)
    with pytest.raises(composition.CompositionError, match="reviewed exact import|rebound or ambiguous"):
        composition._plane_migration_digest(root)


@pytest.mark.parametrize("shadow", [
    "OPERATION_SCHEMA_STATEMENTS = ('changed',)\n",
    "OPERATION_SCHEMA_STATEMENTS += ('changed',)\n",
    "del OPERATION_SCHEMA_STATEMENTS\n",
    "async def OPERATION_SCHEMA_STATEMENTS(): pass\n",
    "class OPERATION_SCHEMA_STATEMENTS: pass\n",
    "import arbitrary as OPERATION_SCHEMA_STATEMENTS\n",
    "from arbitrary import *\n",
])
def test_operation_import_cannot_be_rebound_after_assignment_import(
    tmp_path: Path, shadow: str,
) -> None:
    root = _component(tmp_path / "Plane", extra=shadow)
    with pytest.raises(composition.CompositionError, match="rebound or ambiguous"):
        composition._plane_migration_digest(root)


@pytest.mark.parametrize("value", [
    "('SQL'.strip(),)",
    "tuple(['SQL'])",
    "('SQL',) + ('SECOND',)",
    "(*OTHER,)",
    "['SQL']",
    "(1,)",
    "('',)",
])
def test_operation_module_does_not_inherit_general_expression_evaluator(
    tmp_path: Path, value: str,
) -> None:
    root = _component(tmp_path / "Plane")
    _write(root / "src/astralplane/database/operation_schema.py",
           f"OPERATION_SCHEMA_STATEMENTS = {value}\n")
    with pytest.raises(composition.CompositionError, match="reviewed literal tuple|bounded nonempty"):
        composition._plane_migration_digest(root)


def test_executable_operation_module_is_refused_without_executing_it(tmp_path: Path) -> None:
    root = _component(tmp_path / "Plane")
    marker = tmp_path / "operation-executed.txt"
    _write(root / "src/astralplane/database/operation_schema.py",
           "OPERATION_SCHEMA_STATEMENTS = ('SQL',)\n"
           f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')\n")
    with pytest.raises(composition.CompositionError, match="only the reviewed literal tuple"):
        composition._plane_migration_digest(root)
    assert not marker.exists()


def test_arbitrary_import_cannot_supply_operation_statements_or_be_resolved(tmp_path: Path) -> None:
    root = _component(tmp_path / "Plane", operation_import=(
        "from astralplane.database.unreviewed_schema import UNREVIEWED_STATEMENTS\n"
    ), extra="OPERATION_SCHEMA_STATEMENTS = UNREVIEWED_STATEMENTS\n")
    marker = tmp_path / "import-executed.txt"
    _write(root / "src/astralplane/database/unreviewed_schema.py",
           "UNREVIEWED_STATEMENTS = ('SQL',)\n"
           f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')\n")
    with pytest.raises(composition.CompositionError, match="non-literal compatibility declaration"):
        composition._plane_migration_digest(root)
    assert not marker.exists()


@pytest.mark.parametrize("stem", ["assignment", "operation"])
def test_mutating_either_schema_changes_digest_and_refuses_old_manifest(
    checkout: Path, stem: str,
) -> None:
    root = checkout / COMPONENT_PATHS["astral-plane"]
    _component(root)
    expected = _expected_digest()
    _rewrite_manifest(checkout, lambda document: document["compatibility"]["data_plane"].update(
        migration_sha256=expected
    ))
    _repin_component(checkout, "astral-plane")
    assert _verify(checkout).ok
    _schema(root, stem, ("CREATE TABLE changed (id UUID)",))
    _repin_component(checkout, "astral-plane")
    assert composition._plane_migration_digest(root) != expected
    report = _verify(checkout)
    assert not report.ok
    assert _codes(report, "astral-plane") == {"E_INCOMPATIBLE_CONTRACT"}
    assert any("migration_sha256" in diagnostic.message for diagnostic in report.diagnostics)
