"""Applies astralplane.database.migrations' guarded registry to one explicitly isolated
qualification schema, first at the baseline environment then the candidate image;
proves migration only, not product readiness.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

from astralplane import MIGRATION_DIGEST, SCHEMA_REVISION
from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_REGISTRY,
    MigrationRunner,
)
from astralplane.database.pool import ConnectionPool
from astralplane.database.postgres import create_postgres_driver_pool
from astralplane.database.transaction import PlaneDatabase


def migrate(
    *, database_url: str, qualification_id: str, expected_revision: str, expected_digest: str
) -> dict[str, Any]:
    from psycopg2.extensions import make_dsn, parse_dsn

    if re.fullmatch(r"[0-9a-f]{32}", qualification_id) is None:
        raise ValueError("qualification id must be 32 lowercase hex characters")
    if expected_revision != SCHEMA_REVISION or expected_digest != MIGRATION_DIGEST:
        raise ValueError("loaded Plane registry differs from the expected revision/digest")
    try:
        parameters = parse_dsn(database_url)
    except Exception:
        raise ValueError("qualification database URL is malformed") from None
    database = f"astralplane_qualification_{qualification_id}"
    if parameters.get("dbname") != database:
        raise ValueError("database name does not match the isolated qualification id")
    if set(parameters) - {"dbname", "host", "port", "user", "password", "sslmode"}:
        raise ValueError("database URL contains unsupported connection options")
    if not parameters.get("host") or not parameters.get("user"):
        raise ValueError("database host and user must be explicit")
    schema = f"astralplane_fixture_{qualification_id}"
    configured = make_dsn(database_url, options=f"-csearch_path={schema},pg_catalog")
    pool = ConnectionPool(create_postgres_driver_pool(configured, minimum_connections=1))
    try:
        plane = PlaneDatabase(pool)
        with plane.transaction() as transaction:
            record = transaction.fetch_one(
                "SELECT current_database() AS database, current_schema() AS schema"
            )
            if record is None or record["database"] != database or record["schema"] != schema:
                raise ValueError("connected database/schema differs from the isolated target")
        report = MigrationRunner(
            plane, revision=CURRENT_DATA_PLANE_REVISION, registry=MIGRATION_REGISTRY
        ).run(expected_revision=expected_revision)
    finally:
        pool.close()
    return {
        "contract": "astralplane.qualification-migration/v1",
        "qualification_id": qualification_id,
        "source_revision": report.source_revision,
        "target_revision": report.target_revision,
        "migration_digest": report.migration_digest,
        "applied_steps": list(report.applied_steps),
        "already_current": report.already_current,
        "product_reconciliation_completed": False,
        "release_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-id", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-migration-digest", required=True)
    args = parser.parse_args(argv)
    try:
        report = migrate(
            database_url=os.environ.get("ASTRALPLANE_QUALIFICATION_DATABASE_URL", ""),
            qualification_id=args.qualification_id,
            expected_revision=args.expected_revision,
            expected_digest=args.expected_migration_digest,
        )
    except Exception:
        print("qualification migration failed; admission must remain closed", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
