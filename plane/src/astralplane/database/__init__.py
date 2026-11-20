"""Public database kernel for AstralPlane, re-exporting baseline.py, pool.py,
postgres.py, and transaction.py.
"""

from astralplane.database.baseline import (
    BASELINE_MIGRATION_NAME,
    BASELINE_REQUIRED_TABLES,
    BASELINE_REVISION,
    BaselineCompatibilityReport,
    BaselineCompatibilityState,
    BaselineInitializationReport,
    BaselineMigrationRunner,
    initialize_empty_database,
    inspect_baseline_compatibility,
)
from astralplane.database.pool import ConnectionPool, DriverPool, PoolSnapshot
from astralplane.database.postgres import create_postgres_driver_pool
from astralplane.database.transaction import (
    CommandResult,
    DetachedRecord,
    PlaneDatabase,
    Transaction,
)

__all__ = (
    "BASELINE_MIGRATION_NAME",
    "BASELINE_REQUIRED_TABLES",
    "BASELINE_REVISION",
    "BaselineCompatibilityReport",
    "BaselineCompatibilityState",
    "BaselineInitializationReport",
    "BaselineMigrationRunner",
    "CommandResult",
    "ConnectionPool",
    "DetachedRecord",
    "DriverPool",
    "PlaneDatabase",
    "PoolSnapshot",
    "Transaction",
    "create_postgres_driver_pool",
    "initialize_empty_database",
    "inspect_baseline_compatibility",
)
