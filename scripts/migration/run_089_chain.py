"""Runs AstralPlane's real MigrationRunner over MIGRATION_REGISTRY against one target
database, so a rehearsal exercises the actual boot path rather than a
reimplementation of it.
"""

import asyncio
import os
import sys

from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION, MIGRATION_REGISTRY, MigrationRunner,
)
from astralplane.database.postgres import create_postgres_driver_pool
from astralplane.database.pool import ConnectionPool
from astralplane.database.transaction import PlaneDatabase


async def main() -> int:
    dsn = (
        f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
        f"@{os.environ['DB_HOST']}:{os.environ.get('DB_PORT', '5432')}"
        f"/{os.environ['DB_NAME']}"
    )
    driver_pool = create_postgres_driver_pool(dsn, application_name="astral-089-rehearsal")
    pool = ConnectionPool(driver_pool)
    database = PlaneDatabase(pool)
    runner = MigrationRunner(
        database, revision=CURRENT_DATA_PLANE_REVISION, registry=MIGRATION_REGISTRY
    )
    report = runner.run(
        expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision
    )
    if hasattr(report, "__await__"):
        report = await report
    print("target revision:", CURRENT_DATA_PLANE_REVISION.schema_revision)
    print("report:", report)
    for name in ("applied", "steps", "already_current", "skipped"):
        if hasattr(report, name):
            print(f"  {name}: {getattr(report, name)}")
    await pool.aclose() if hasattr(pool, "aclose") else None
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
