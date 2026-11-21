"""SQLite migration registry: one baseline that builds the full current schema, a structure digest
read from sqlite_master, and the revision record. It reuses MigrationRunner from migrations.py, so
opening a database fails closed on a different revision, migration digest or structure.
"""

from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from typing import Final

from astralplane.contracts import PlaneDatabase as PlaneDatabaseContract
from astralplane.contracts import Transaction
from astralplane.database.migrations import (
    Migration,
    MigrationRegistry,
    MigrationReport,
)
from astralplane.database.revision import SCHEMA_REVISION, DataPlaneRevision
from astralplane.database.sqlite_schema import SQLITE_BOOTSTRAP_STATEMENTS, SQLITE_SCHEMA_STATEMENTS
from astralplane.database.sqlite_sql import _segments
from astralplane.errors import SchemaRevisionError

SQLITE_BASELINE_NAME: Final = "astralplane-sqlite-baseline"
_STRUCTURE_QUERY: Final = (
    "SELECT type, name, tbl_name, sql FROM sqlite_master"
    " WHERE substr(name, 1, 7) <> 'sqlite_' ORDER BY type, name"
)
_WHITESPACE: Final = re.compile(r"\s+")


def sqlite_structure_digest(rows: tuple[tuple[str, str, str, str | None], ...]) -> str:
    canonical = [
        [
            kind,
            name,
            table,
            "".join(
                _WHITESPACE.sub(" ", text) if code else text for code, text in _segments(sql or "")
            ).strip(),
        ]
        for kind, name, table, sql in rows
    ]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def read_structure_digest(transaction: Transaction) -> str:
    records = transaction.fetch_all(_STRUCTURE_QUERY)
    return sqlite_structure_digest(
        tuple((r["type"], r["name"], r["tbl_name"], r["sql"]) for r in records)
    )


def _baseline_checksum() -> str:
    statements = (*SQLITE_SCHEMA_STATEMENTS, *SQLITE_BOOTSTRAP_STATEMENTS)
    joined = "\n--\n".join(statements).encode("utf-8")
    guards = files("astralplane.database").joinpath("sqlite_functions.py").read_bytes()
    return hashlib.sha256(joined + b"\n" + guards).hexdigest()


def _apply_baseline(transaction: Transaction) -> None:
    for statement in (*SQLITE_SCHEMA_STATEMENTS, *SQLITE_BOOTSTRAP_STATEMENTS):
        transaction.execute(statement)


SQLITE_SCHEMA_STRUCTURE_DIGEST: Final = (
    "5f45079d23dc20e1dc0bb1367e8076c55a582ba7df7a76224fc0cdbe1a2b4d0b"
)


def _verify_current_structure(transaction: Transaction) -> None:
    observed = read_structure_digest(transaction)
    if observed != SQLITE_SCHEMA_STRUCTURE_DIGEST:
        raise SchemaRevisionError(
            "SQLite schema structure does not match the declared data-plane revision",
            metadata={"observed": observed, "expected": SQLITE_SCHEMA_STRUCTURE_DIGEST},
        )


_VERIFIER_CHECKSUM: Final = hashlib.sha256(
    _STRUCTURE_QUERY.encode("utf-8") + SQLITE_SCHEMA_STRUCTURE_DIGEST.encode("ascii")
).hexdigest()

SQLITE_MIGRATION_REGISTRY: Final = MigrationRegistry(
    (
        Migration(
            name=SQLITE_BASELINE_NAME,
            source_revisions=(None,),
            target_revision=SCHEMA_REVISION,
            checksum=_baseline_checksum(),
            operation=_apply_baseline,
        ),
    ),
    current_schema_verifier=_verify_current_structure,
    current_schema_verifier_checksum=_VERIFIER_CHECKSUM,
)

SQLITE_DATA_PLANE_REVISION: Final = DataPlaneRevision(
    schema_revision=SCHEMA_REVISION,
    read_compatible_from=(SCHEMA_REVISION,),
    migration_digest=SQLITE_MIGRATION_REGISTRY.digest,
)
SQLITE_MIGRATION_DIGEST: Final = SQLITE_MIGRATION_REGISTRY.digest


class SQLiteBootRunner:
    def __init__(self, database: PlaneDatabaseContract, runner: object) -> None:
        if not callable(getattr(runner, "run", None)):
            raise TypeError("runner must expose run(expected_revision=...)")
        self._database = database
        self._runner = runner

    def run(self, *, expected_revision: str) -> MigrationReport:
        with self._database.transaction() as transaction:
            tables = {
                record["name"]
                for record in transaction.fetch_all(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                    " AND substr(name, 1, 7) <> 'sqlite_'"
                )
            }
        if tables and "schema_meta" not in tables:
            raise SchemaRevisionError(
                "database is neither empty nor an AstralPlane SQLite database",
                metadata={"reason_codes": "schema_metadata_missing"},
            )
        report = self._runner.run(expected_revision=expected_revision)  # type: ignore[attr-defined]
        if not isinstance(report, MigrationReport):
            raise TypeError("delegate migration runner returned an invalid report")
        return report


__all__ = (
    "SQLITE_BASELINE_NAME",
    "SQLITE_DATA_PLANE_REVISION",
    "SQLITE_MIGRATION_DIGEST",
    "SQLITE_MIGRATION_REGISTRY",
    "SQLITE_SCHEMA_STRUCTURE_DIGEST",
    "SQLiteBootRunner",
    "read_structure_digest",
    "sqlite_structure_digest",
)
