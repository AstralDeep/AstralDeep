"""Executable PostgreSQL and blob loader for the synthetic 066.001 fixture."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from astralplane.database.legacy_baseline_066 import (
    LEGACY_BASELINE_SOURCE_BLOB,
    _LegacyBaseline066Builder,
)

FIXTURE_ROOT: Final = Path(__file__).resolve().parent
BASELINE_SQL: Final = FIXTURE_ROOT / "baseline.sql"
EXPECTED_PATH: Final = FIXTURE_ROOT / "expected.json"
BLOB_SOURCE_ROOT: Final = FIXTURE_ROOT / "blobs"
TEST_DATABASE_ENV: Final = "ASTRALPLANE_TEST_POSTGRES_DSN"

_SCHEMA_PATTERN = re.compile(r"^astralplane_fixture_[0-9a-f]{32}$")
_STAGING_PREFIX = ".astralplane-fixture-stage-"
_SQL_STATEMENT_SEPARATOR = "-- astralplane-fixture-statement\n"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class FixtureLoadError(RuntimeError):
    """The bounded synthetic fixture could not be loaded or verified."""


@dataclass(frozen=True, slots=True)
class BlobEvidence:
    storage_key: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "sizeBytes": self.size_bytes,
            "storageKey": self.storage_key,
        }


@dataclass(frozen=True, slots=True)
class CatalogEvidence:
    row_count: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "rowCount": self.row_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class FixtureLoadReport:
    schema: str
    schema_revision: str
    blob_root: str
    blobs: tuple[BlobEvidence, ...]
    fixture_digest: str
    legacy_baseline_builder_sha256: str
    legacy_baseline_source_blob: str
    loader_sha256: str
    pre_migration_catalog: CatalogEvidence

    def to_dict(self) -> dict[str, object]:
        return {
            "blobRoot": self.blob_root,
            "blobs": [blob.to_dict() for blob in self.blobs],
            "fixtureDigest": self.fixture_digest,
            "legacyBaselineBuilderSha256": self.legacy_baseline_builder_sha256,
            "legacyBaselineSourceBlob": self.legacy_baseline_source_blob,
            "loaderSha256": self.loader_sha256,
            "preMigrationCatalog": self.pre_migration_catalog.to_dict(),
            "schema": self.schema,
            "schemaRevision": self.schema_revision,
        }


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _validate_schema(schema: str) -> str:
    if not isinstance(schema, str) or _SCHEMA_PATTERN.fullmatch(schema) is None:
        raise FixtureLoadError(
            "fixture schema must match astralplane_fixture_<32 lowercase hex characters>"
        )
    return schema


def _expected() -> dict[str, Any]:
    try:
        value = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureLoadError("fixture expectation manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise FixtureLoadError("fixture expectation manifest must be an object")
    return value


def _blob_evidence_from_manifest() -> tuple[BlobEvidence, ...]:
    records = _expected().get("blobs")
    if not isinstance(records, list) or not records:
        raise FixtureLoadError("fixture expectation manifest must declare blobs")
    evidence: list[BlobEvidence] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise FixtureLoadError("fixture blob records must be objects")
        storage_key = record.get("storageKey")
        size_bytes = record.get("sizeBytes")
        sha256 = record.get("sha256")
        if (
            not isinstance(storage_key, str)
            or not storage_key
            or Path(storage_key).is_absolute()
            or "\\" in storage_key
            or any(part in {"", ".", ".."} for part in storage_key.split("/"))
            or storage_key in seen
        ):
            raise FixtureLoadError("fixture blob storage keys must be unique relative paths")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise FixtureLoadError("fixture blob sizes must be non-negative integers")
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
            raise FixtureLoadError("fixture blob digests must be lowercase SHA-256")
        seen.add(storage_key)
        evidence.append(BlobEvidence(storage_key=storage_key, size_bytes=size_bytes, sha256=sha256))
    return tuple(sorted(evidence, key=lambda item: item.storage_key))


def _fixture_input_paths() -> tuple[Path, ...]:
    fixed = (
        BASELINE_SQL,
        FIXTURE_ROOT / "database.json",
        EXPECTED_PATH,
        Path(__file__).resolve(),
    )
    blobs = tuple(sorted(path for path in BLOB_SOURCE_ROOT.rglob("*") if path.is_file()))
    builder_source = inspect.getsourcefile(_LegacyBaseline066Builder)
    if builder_source is None:
        raise FixtureLoadError("canonical legacy baseline source is unavailable")
    return (*fixed, *blobs, Path(builder_source).resolve())


def _legacy_builder_path() -> Path:
    builder_source = inspect.getsourcefile(_LegacyBaseline066Builder)
    if builder_source is None:
        raise FixtureLoadError("canonical legacy baseline source is unavailable")
    return Path(builder_source).resolve()


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise FixtureLoadError("fixture evidence input is unreadable") from exc


def _pre_migration_catalog_evidence(cursor: Any) -> CatalogEvidence:
    """Digest the exact live 066 catalog before any Plane migration runs."""

    from astralplane.database.migrations import CURRENT_SCHEMA_STRUCTURE_QUERY

    cursor.execute(CURRENT_SCHEMA_STRUCTURE_QUERY)
    rows = tuple(cursor.fetchall())
    canonical = json.dumps(
        [
            {
                "definition": str(row[2]),
                "identity": str(row[1]),
                "kind": str(row[0]),
            }
            for row in rows
        ],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return CatalogEvidence(
        row_count=len(rows),
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def fixture_digest() -> str:
    """Bind the SQL, semantic inventory, expectations, and blob bytes."""

    records: list[dict[str, object]] = []
    for path in _fixture_input_paths():
        try:
            metadata = path.lstat()
            if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
                raise FixtureLoadError("fixture input must be a real regular file")
            relative = (
                path.relative_to(FIXTURE_ROOT).as_posix()
                if path.is_relative_to(FIXTURE_ROOT)
                else "<astralplane>/database/legacy_baseline_066.py"
            )
            content = path.read_bytes()
        except OSError as exc:
            raise FixtureLoadError("fixture input is unreadable") from exc
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "sizeBytes": len(content),
            }
        )
    source_blob = LEGACY_BASELINE_SOURCE_BLOB.encode("ascii")
    records.append(
        {
            "path": "<legacy-baseline-source-blob>",
            "sha256": hashlib.sha256(source_blob).hexdigest(),
            "sizeBytes": len(source_blob),
        }
    )
    canonical = json.dumps(
        records,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _validate_destination(destination: Path) -> Path:
    if not destination.is_absolute():
        raise FixtureLoadError("fixture blob root must be an absolute path")
    if destination.exists() or destination.is_symlink():
        raise FixtureLoadError("fixture blob root must not already exist")
    supplied_parent = destination.parent
    try:
        metadata = supplied_parent.lstat()
        parent = supplied_parent.resolve(strict=True)
    except OSError as exc:
        raise FixtureLoadError("fixture blob parent is unavailable") from exc
    if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise FixtureLoadError("fixture blob parent must be a real directory")
    if supplied_parent != parent:
        raise FixtureLoadError("fixture blob parent must not cross a link or alias")
    return destination


def _remove_generated_tree(path: Path, *, parent: Path, name: str) -> None:
    """Remove one exact loader-created directory without following links."""

    resolved_parent = parent.resolve(strict=True)
    if path.parent.resolve(strict=True) != resolved_parent or path.name != name:
        raise FixtureLoadError("refusing to remove an unexpected fixture directory")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise FixtureLoadError("refusing to remove a linked or non-directory fixture path")
    shutil.rmtree(path)


def _stage_blob_fixture(
    destination: Path,
    *,
    fail_after_files: int | None = None,
) -> tuple[Path, tuple[BlobEvidence, ...]]:
    exact_destination = _validate_destination(destination)
    if fail_after_files is not None and (
        isinstance(fail_after_files, bool)
        or not isinstance(fail_after_files, int)
        or fail_after_files < 0
    ):
        raise FixtureLoadError("fail_after_files must be a non-negative integer or None")
    expected = _blob_evidence_from_manifest()
    staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(exact_destination.parent)))
    try:
        for index, item in enumerate(expected):
            if fail_after_files is not None and index == fail_after_files:
                raise FixtureLoadError("injected synthetic blob materialization failure")
            source = BLOB_SOURCE_ROOT.joinpath(*item.storage_key.split("/"))
            metadata = source.lstat()
            if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
                raise FixtureLoadError("fixture blob source must be a real regular file")
            content = source.read_bytes()
            if len(content) != item.size_bytes:
                raise FixtureLoadError("fixture blob size does not match its manifest")
            if hashlib.sha256(content).hexdigest() != item.sha256:
                raise FixtureLoadError("fixture blob digest does not match its manifest")
            target = staging.joinpath(*item.storage_key.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return staging, expected
    except BaseException:
        _remove_generated_tree(staging, parent=exact_destination.parent, name=staging.name)
        raise


def materialize_blob_fixture(
    destination: str | os.PathLike[str],
    *,
    fail_after_files: int | None = None,
) -> tuple[BlobEvidence, ...]:
    """Atomically publish the digest-verified synthetic blob snapshot."""

    exact_destination = _validate_destination(Path(destination))
    staging, evidence = _stage_blob_fixture(
        exact_destination,
        fail_after_files=fail_after_files,
    )
    try:
        os.replace(staging, exact_destination)
    except BaseException:
        _remove_generated_tree(staging, parent=exact_destination.parent, name=staging.name)
        raise
    return evidence


def verify_blob_fixture(root: str | os.PathLike[str]) -> tuple[BlobEvidence, ...]:
    """Verify exact file membership, byte counts, and digests without following links."""

    supplied = Path(root)
    resolved = supplied.resolve(strict=True)
    metadata = supplied.lstat()
    if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise FixtureLoadError("materialized blob root must be a real directory")
    expected = _blob_evidence_from_manifest()
    observed_keys: set[str] = set()
    for path in resolved.rglob("*"):
        item_metadata = path.lstat()
        if _is_reparse(item_metadata):
            raise FixtureLoadError("materialized blob root contains a link")
        if stat.S_ISREG(item_metadata.st_mode):
            observed_keys.add(path.relative_to(resolved).as_posix())
        elif not stat.S_ISDIR(item_metadata.st_mode):
            raise FixtureLoadError("materialized blob root contains an unsupported object")
    if observed_keys != {item.storage_key for item in expected}:
        raise FixtureLoadError("materialized blob membership does not match its manifest")
    for item in expected:
        content = resolved.joinpath(*item.storage_key.split("/")).read_bytes()
        if len(content) != item.size_bytes or hashlib.sha256(content).hexdigest() != item.sha256:
            raise FixtureLoadError("materialized blob content does not match its manifest")
    return expected


def _execute_baseline(connection: Any, *, schema: str) -> tuple[str, CatalogEvidence]:
    exact_schema = _validate_schema(schema)
    quoted_schema = f'"{exact_schema}"'
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT to_regnamespace(%s)", (exact_schema,))
        row = cursor.fetchone()
        if row is not None and row[0] is not None:
            raise FixtureLoadError("fixture schema already exists")
        cursor.execute(f"CREATE SCHEMA {quoted_schema}")
        cursor.execute(f"SET LOCAL search_path TO {quoted_schema}, pg_catalog")
        _LegacyBaseline066Builder().apply(cursor)
        baseline = BASELINE_SQL.read_text(encoding="utf-8")
        statements = tuple(
            statement.strip()
            for statement in baseline.split(_SQL_STATEMENT_SEPARATOR)[1:]
            if statement.strip()
        )
        if not statements or baseline.count(_SQL_STATEMENT_SEPARATOR) != len(statements):
            raise FixtureLoadError("fixture SQL statement boundaries are invalid")
        for statement in statements:
            cursor.execute(statement)
        cursor.execute("SELECT value FROM schema_meta WHERE key = 'revision'")
        revision_row = cursor.fetchone()
        if revision_row is None or revision_row[0] != "066.001":
            raise FixtureLoadError("fixture did not create the expected schema marker")
        catalog_evidence = _pre_migration_catalog_evidence(cursor)
        return str(revision_row[0]), catalog_evidence
    finally:
        cursor.close()


def load_fixture(
    connection: Any,
    *,
    schema: str,
    blob_root: str | os.PathLike[str],
    fail_after_blob_files: int | None = None,
) -> FixtureLoadReport:
    """Load PostgreSQL and blobs as one bounded pre-split fixture unit."""

    exact_schema = _validate_schema(schema)
    destination = _validate_destination(Path(blob_root))
    staging, _evidence = _stage_blob_fixture(
        destination,
        fail_after_files=fail_after_blob_files,
    )
    promoted = False
    prior_autocommit = bool(getattr(connection, "autocommit", False))
    with suppress(BaseException):
        connection.rollback()
    if prior_autocommit:
        connection.autocommit = False
    try:
        revision, catalog_evidence = _execute_baseline(connection, schema=exact_schema)
        os.replace(staging, destination)
        promoted = True
        connection.commit()
    except BaseException as exc:
        with suppress(BaseException):
            connection.rollback()
        if promoted:
            _remove_generated_tree(
                destination,
                parent=destination.parent,
                name=destination.name,
            )
        else:
            with suppress(FixtureLoadError):
                _remove_generated_tree(staging, parent=destination.parent, name=staging.name)
        if isinstance(exc, FixtureLoadError):
            raise
        raise FixtureLoadError("synthetic fixture load failed") from exc
    finally:
        if prior_autocommit:
            connection.autocommit = True
    return FixtureLoadReport(
        schema=exact_schema,
        schema_revision=revision,
        blob_root=str(destination),
        blobs=verify_blob_fixture(destination),
        fixture_digest=fixture_digest(),
        legacy_baseline_builder_sha256=_file_sha256(_legacy_builder_path()),
        legacy_baseline_source_blob=LEGACY_BASELINE_SOURCE_BLOB,
        loader_sha256=_file_sha256(Path(__file__).resolve()),
        pre_migration_catalog=catalog_evidence,
    )


def drop_postgres_fixture(connection: Any, *, schema: str) -> None:
    """Drop one exact generated fixture schema; intended only for isolated test cleanup."""

    exact_schema = _validate_schema(schema)
    quoted_schema = f'"{exact_schema}"'
    prior_autocommit = bool(getattr(connection, "autocommit", False))
    with suppress(BaseException):
        connection.rollback()
    if prior_autocommit:
        connection.autocommit = False
    cursor = connection.cursor()
    try:
        cursor.execute("SET LOCAL search_path TO pg_catalog")
        cursor.execute(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE")
        connection.commit()
    except BaseException:
        with suppress(BaseException):
            connection.rollback()
        raise
    finally:
        cursor.close()
        if prior_autocommit:
            connection.autocommit = True


def connect_fixture_database(database_url: str) -> Any:
    """Connect through either supported PostgreSQL driver already present on the test host."""

    if not isinstance(database_url, str) or not database_url.strip():
        raise FixtureLoadError("a non-empty PostgreSQL database URL is required")
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        try:
            import psycopg2  # type: ignore[import-not-found]
        except ImportError as exc:
            raise FixtureLoadError("psycopg or psycopg2 is required by this test loader") from exc
        return psycopg2.connect(database_url)
    return psycopg.connect(database_url)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get(TEST_DATABASE_ENV),
        help=f"isolated PostgreSQL URL (defaults to {TEST_DATABASE_ENV})",
    )
    parser.add_argument("--schema", required=True)
    parser.add_argument("--blob-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.database_url is None:
        raise SystemExit(f"--database-url or {TEST_DATABASE_ENV} is required")
    connection = connect_fixture_database(args.database_url)
    try:
        report = load_fixture(
            connection,
            schema=args.schema,
            blob_root=args.blob_root,
        )
    finally:
        connection.close()
    print(json.dumps(report.to_dict(), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
