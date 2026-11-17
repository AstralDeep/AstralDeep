"""Tests for scripts/migrate_qualification_database.py: refuses unbound registries and
ambiguous databases, and keeps secrets out of CLI output.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scripts import migrate_qualification_database as driver

ID = "a" * 32
DSN = f"postgresql://synthetic:synthetic@localhost/astralplane_qualification_{ID}"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("qualification_id", "invalid", "qualification id"),
        ("expected_revision", "088.003", "expected revision/digest"),
        ("expected_digest", "wrong", "expected revision/digest"),
        ("database_url", "not a dsn", "malformed"),
        ("database_url", "postgresql://host/astraldeep", "isolated qualification id"),
        ("database_url", DSN + "?options=-csearch_path%3Dpublic", "unsupported"),
        ("database_url", f"dbname=astralplane_qualification_{ID}", "host and user"),
    ],
)
def test_validation_precedes_connection(field: str, value: str, message: str) -> None:
    args = {
        "database_url": DSN,
        "qualification_id": ID,
        "expected_revision": driver.SCHEMA_REVISION,
        "expected_digest": driver.MIGRATION_DIGEST,
        field: value,
    }
    with pytest.raises(ValueError, match=message):
        driver.migrate(**args)


def test_cli_failure_and_success_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = [
        "--qualification-id", ID,
        "--expected-revision", driver.SCHEMA_REVISION,
        "--expected-migration-digest", driver.MIGRATION_DIGEST,
    ]

    def fail(**_args: Any) -> None:
        raise RuntimeError("sensitive DSN")

    monkeypatch.setattr(driver, "migrate", fail)
    assert driver.main(arguments) == 2
    assert capsys.readouterr().err == (
        "qualification migration failed; admission must remain closed\n"
    )
    monkeypatch.setattr(driver, "migrate", lambda **_args: {"release_authorized": False})
    assert driver.main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == {"release_authorized": False}
