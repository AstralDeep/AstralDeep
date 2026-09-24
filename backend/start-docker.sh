#!/bin/bash
# Docker container entrypoint for the backend: refuses to start if legacy SQLite files are present
# under data/, pointing operators to AstralPlane's reviewed migration path, then execs start.py.
# Referenced by the Dockerfile.
set -e

echo "Starting AstralDeep Backend Services on port 8001..."
export ORCHESTRATOR_PORT=8001
export PYTHONIOENCODING=utf-8

cd /app/backend

SQLITE_MAIN="/app/backend/data/astral.db"
SQLITE_AUDIT="/app/backend/data/test_audit.db"

if [ -f "$SQLITE_MAIN" ] || [ -f "$SQLITE_AUDIT" ]; then
    echo "ERROR: legacy SQLite data was detected; AstralDeep will not start." >&2
    echo "The files were not modified. Do not delete them or run ad-hoc SQL." >&2
    echo "Follow AstralPlane docs/migration-and-recovery.md using a verified" >&2
    echo "PostgreSQL/blob backup or a separately reviewed import boundary." >&2
    exit 78
fi

exec python start.py
