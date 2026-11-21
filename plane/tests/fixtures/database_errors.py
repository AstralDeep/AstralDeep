"""Asserts equivalent constraint categories while retaining each backend's native driver errors.
SQLite exposes extended result codes instead of PostgreSQL's exception subclasses.
"""

import sqlite3
from contextlib import contextmanager

import psycopg2
import pytest

from astralplane.database.sqlite import SQLiteLockTimeoutError

INTEGRITY_ERRORS = (psycopg2.IntegrityError, sqlite3.IntegrityError)
LOCK_ERRORS = (psycopg2.errors.LockNotAvailable, SQLiteLockTimeoutError)


@contextmanager
def check_violation():
    with pytest.raises((psycopg2.errors.CheckViolation, sqlite3.IntegrityError)) as caught:
        yield caught
    if isinstance(caught.value, sqlite3.IntegrityError):
        assert caught.value.sqlite_errorcode in {
            sqlite3.SQLITE_CONSTRAINT_CHECK,
            sqlite3.SQLITE_CONSTRAINT_TRIGGER,
        }
