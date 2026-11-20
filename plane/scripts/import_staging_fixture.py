"""Operator command that imports the synthetic predecessor fixture into an empty,
isolated qualification database via astralplane's guarded loader; accepts no SQL,
production DSN, or schema override.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATABASE_ENV = "ASTRALPLANE_QUALIFICATION_DATABASE_URL"
IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
CONTRACT = "astralplane.synthetic-staging-import/v1"


class QualificationImportError(ValueError):
    pass


def _loader() -> Any:
    sys.path.insert(0, str(ROOT / "src"))
    path = ROOT / "tests/fixtures/pre_split/loader.py"
    spec = importlib.util.spec_from_file_location("_plane_staging_fixture", path)
    if spec is None or spec.loader is None:
        raise QualificationImportError("the pinned Plane fixture loader is absent")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def import_fixture(
    *,
    database_url: str,
    qualification_id: str,
    expected_fixture_sha256: str,
    blob_root: Path,
) -> dict[str, object]:
    if not IDENTIFIER.fullmatch(qualification_id):
        raise QualificationImportError("qualification id must be 32 lowercase hex characters")
    if not DIGEST.fullmatch(expected_fixture_sha256):
        raise QualificationImportError("expected fixture digest must be lowercase SHA-256")
    if not isinstance(database_url, str) or not database_url.strip():
        raise QualificationImportError("an explicit qualification database URL is required")
    if not blob_root.is_absolute():
        raise QualificationImportError("blob root must be an explicit new absolute directory")

    import psycopg2
    from psycopg2.extensions import parse_dsn

    expected_database = f"astralplane_qualification_{qualification_id}"
    try:
        parameters = parse_dsn(database_url)
    except Exception:
        raise QualificationImportError("qualification database URL is malformed") from None
    if parameters.get("dbname") != expected_database:
        raise QualificationImportError("database name does not match the isolated qualification id")
    if set(parameters) - {"dbname", "host", "port", "user", "password", "sslmode"}:
        raise QualificationImportError("database URL contains unsupported connection options")
    if not parameters.get("host") or not parameters.get("user"):
        raise QualificationImportError("database host and user must be explicit")
    loader = _loader()
    if loader.fixture_digest() != expected_fixture_sha256:
        raise QualificationImportError("reviewed fixture digest does not match the pinned source")
    schema = f"astralplane_fixture_{qualification_id}"
    try:
        connection = psycopg2.connect(database_url, connect_timeout=10)
    except Exception:
        raise QualificationImportError("qualification database connection failed") from None
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(1095980114, 60001)")
            cursor.execute("SELECT current_database(), pg_is_in_recovery()")
            if cursor.fetchone() != (expected_database, False):
                raise QualificationImportError(
                    "connected database is not the writable isolated target"
                )
            cursor.execute(
                "SELECT count(*) FROM pg_namespace "
                "WHERE nspname NOT LIKE 'pg_%' AND nspname NOT IN ('public', 'information_schema')"
            )
            if cursor.fetchone() != (0,):
                raise QualificationImportError(
                    "qualification database already contains private schemas"
                )
            cursor.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public'"
            )
            if cursor.fetchone() != (0,):
                raise QualificationImportError("qualification database public schema is not empty")
            cursor.execute(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public'"
            )
            if cursor.fetchone() != (0,):
                raise QualificationImportError("qualification database contains public functions")
        report = loader.load_fixture(connection, schema=schema, blob_root=blob_root)
    except QualificationImportError:
        raise
    except Exception:
        raise QualificationImportError(
            "isolated fixture import failed; admission must remain closed"
        ) from None
    finally:
        connection.close()
    return {
        "contract": CONTRACT,
        "classification": "synthetic",
        "qualification_id": qualification_id,
        "database": expected_database,
        "database_options": f"-csearch_path={schema},pg_catalog",
        "source_schema_revision": "066.001",
        "fixture_sha256": expected_fixture_sha256,
        "importer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "fixture": report.to_dict(),
        "release_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-id", required=True)
    parser.add_argument("--expected-fixture-sha256", required=True)
    parser.add_argument("--blob-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = import_fixture(
            database_url=os.environ.get(DATABASE_ENV, ""),
            qualification_id=args.qualification_id,
            expected_fixture_sha256=args.expected_fixture_sha256,
            blob_root=args.blob_root,
        )
    except QualificationImportError as exc:
        print(f"staging import refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
