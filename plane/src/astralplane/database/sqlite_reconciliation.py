"""Serializes SQLite reconciliation across processes with a lock beside the database file.
Marker transitions reuse reconciliation_store's session while the file lock survives commits.
"""

from __future__ import annotations

import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from astralplane.errors import ReconciliationError
from astralplane.reconciliation import RECONCILIATION_ADVISORY_LOCK
from astralplane.reconciliation_store import PostgresReconciliationSession


class SQLiteReconciliationCoordinator:
    def __init__(self, pool: Any, path: Path, *, timeout_seconds: float) -> None:
        self._pool = pool
        self._path = Path(f"{path}.reconciliation-lock")
        self._timeout = timeout_seconds

    @contextmanager
    def coordinate(self, *, advisory_lock, schema_revision, plan_digest):
        import fcntl

        if advisory_lock != RECONCILIATION_ADVISORY_LOCK:
            raise ReconciliationError("reconciliation requires its canonical lock identity")
        descriptor = os.open(
            self._path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        acquired = False
        session = None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ReconciliationError("reconciliation lock must be a regular file")
            os.fchmod(descriptor, 0o600)
            deadline = time.monotonic() + self._timeout
            while not acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ReconciliationError("reconciliation lock deadline exceeded") from exc
                    time.sleep(min(0.02, remaining))
            with self._pool.connection() as connection:
                session = PostgresReconciliationSession(
                    connection, schema_revision=schema_revision, plan_digest=plan_digest
                )
                yield session
        finally:
            if session is not None:
                session._deactivate()
            try:
                if acquired:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
