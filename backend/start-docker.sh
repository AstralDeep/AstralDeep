#!/bin/bash
set -e

# Feature 026: no separate frontend static server. The orchestrator serves the
# server-driven web UI (shell + static assets) directly on port 8001.
echo "Starting AstralDeep Backend Services on port 8001..."
export ORCHESTRATOR_PORT=8001
export PYTHONIOENCODING=utf-8

cd /app/backend

# AstralPlane owns PostgreSQL connection readiness, guarded schema evolution,
# and recovery. Deep must never open a second driver connection or run a
# best-effort migration before the application-scoped Plane runtime starts.
#
# The retired SQLite importer could partially copy rows and then continue
# startup after errors. Refuse legacy inputs instead: keep every source byte
# untouched and require the reviewed Plane migration/recovery procedure.
SQLITE_MAIN="/app/backend/data/astral.db"
SQLITE_AUDIT="/app/backend/data/test_audit.db"

if [ -f "$SQLITE_MAIN" ] || [ -f "$SQLITE_AUDIT" ]; then
    echo "ERROR: legacy SQLite data was detected; AstralDeep will not start." >&2
    echo "The files were not modified. Do not delete them or run ad-hoc SQL." >&2
    echo "Follow AstralPlane docs/migration-and-recovery.md using a verified" >&2
    echo "PostgreSQL/blob backup or a separately reviewed import boundary." >&2
    exit 78
fi

# Normal startup composes exactly one Plane runtime. It owns connection retry,
# migration, compatibility, and readiness; any failure propagates fail-closed.
exec python start.py
