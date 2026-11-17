"""Tests for src/astralplane/recovery.py: recovery input validation, driver-diagnostic
privacy, and refusal on incomplete repository delete results.
"""

from types import SimpleNamespace

import pytest

from astralplane import api, recovery
from astralplane.database.migrations import MIGRATION_DIGEST
from astralplane.database.revision import SCHEMA_REVISION
from astralplane.errors import PlaneError
from astralplane.repositories.history import SessionRepository


def arguments():
    return dict(
        database_url="PRIVATE-DSN",
        expected_database="target",
        expected_schema="public",
        expected_schema_revision=SCHEMA_REVISION,
        expected_migration_digest=MIGRATION_DIGEST,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_database", None),
        ("expected_database", ""),
        ("expected_database", True),
        ("expected_database", "\0"),
        ("expected_database", "x" * 64),
        ("expected_schema", "é" * 32),
        ("expected_schema", "\ud800"),
        ("expected_schema", "pg_temp"),
        ("expected_schema", "information_schema"),
        ("expected_schema_revision", None),
        ("expected_migration_digest", True),
    ],
)
def test_invalid_explicit_target_never_constructs_a_pool(monkeypatch, field, value):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid recovery target opened a driver")

    monkeypatch.setattr(recovery, "create_postgres_driver_pool", forbidden)
    selected = arguments()
    selected[field] = value
    with pytest.raises(recovery.SessionRetirementError) as error:
        recovery.retire_restored_sessions(**selected)
    assert str(error.value) == "restored session retirement unavailable"
    assert "PRIVATE" not in repr(error.value) and error.value.metadata == ()


def test_driver_diagnostics_never_escape_public_recovery(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("PRIVATE-DSN PASSWORD")

    monkeypatch.setattr(recovery, "create_postgres_driver_pool", unavailable)
    with pytest.raises(recovery.SessionRetirementError) as error:
        recovery.retire_restored_sessions(**arguments())
    assert "PRIVATE" not in str(error.value) and error.value.__suppress_context__
    assert api.retire_restored_sessions is recovery.retire_restored_sessions


@pytest.mark.parametrize(
    "rowcount,remaining", [(-1, False), (True, False), (1, True), (1, None), (1, "false"), (1, 0)]
)
def test_incomplete_or_invalid_delete_results_cannot_report_completion(rowcount, remaining):
    class Transaction:
        def execute(self, *_args):
            return SimpleNamespace(rowcount=rowcount)

        def fetch_one(self, *_args):
            return None if remaining is None else {"remaining": remaining}

    with pytest.raises(PlaneError):
        SessionRepository().retire_all_for_recovery(Transaction())
