"""Tests for scripts/import_staging_fixture.py: the CLI never selects live storage or
accepts arbitrary fixture bytes without a reviewed fingerprint.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg2
import pytest

from scripts import import_staging_fixture as driver

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/import_staging_fixture.py"
ID = "a" * 32
DATABASE = f"astralplane_qualification_{ID}"
DSN = f"postgresql://synthetic:synthetic@localhost/{DATABASE}"
DIGEST = "b" * 64


def _args(tmp_path: Path) -> dict[str, Any]:
    return {
        "database_url": DSN,
        "qualification_id": ID,
        "expected_fixture_sha256": DIGEST,
        "blob_root": tmp_path / "new-blobs",
    }


class Connection:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = iter(rows)
        self.statements: list[str] = []
        self.closed = False

    def cursor(self) -> Connection:
        return self

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def fetchone(self) -> Any:
        return next(self.rows)

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("qualification_id", "../live", "qualification id"),
        ("expected_fixture_sha256", "bad", "fixture digest"),
        ("database_url", "", "explicit qualification"),
        ("database_url", "not a dsn", "malformed"),
        ("database_url", "postgresql://user@host/astraldeep", "database name"),
        ("database_url", DSN + "?options=-csearch_path%3Dpublic", "unsupported"),
        ("database_url", f"dbname={DATABASE}", "host and user"),
        ("blob_root", Path("relative"), "absolute"),
    ],
)
def test_invalid_input_never_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any, message: str
) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("invalid request reached PostgreSQL")

    monkeypatch.setattr(psycopg2, "connect", unexpected)
    args = {**_args(tmp_path), field: value}
    with pytest.raises(driver.QualificationImportError, match=message):
        driver.import_fixture(**args)


def test_reviewed_fingerprint_is_required_before_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(driver, "_loader", lambda: SimpleNamespace(fixture_digest=lambda: "c" * 64))
    with pytest.raises(driver.QualificationImportError, match="reviewed fixture digest"):
        driver.import_fixture(**_args(tmp_path))


def test_connection_error_is_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "_loader", lambda: SimpleNamespace(fixture_digest=lambda: DIGEST))

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("sensitive connection details")

    monkeypatch.setattr(psycopg2, "connect", fail)
    with pytest.raises(driver.QualificationImportError, match="connection failed") as caught:
        driver.import_fixture(**_args(tmp_path))
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([("wrong", False)], "isolated target"),
        ([(DATABASE, True)], "isolated target"),
        ([(DATABASE, False), (1,)], "private schemas"),
        ([(DATABASE, False), (0,), (1,)], "not empty"),
        ([(DATABASE, False), (0,), (0,), (1,)], "public functions"),
    ],
)
def test_connected_catalog_is_checked_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[Any], message: str
) -> None:
    connection = Connection(rows)
    monkeypatch.setattr(psycopg2, "connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(driver, "_loader", lambda: SimpleNamespace(fixture_digest=lambda: DIGEST))
    with pytest.raises(driver.QualificationImportError, match=message):
        driver.import_fixture(**_args(tmp_path))
    assert connection.closed
    assert connection.statements[0] == "SELECT pg_advisory_lock(1095980114, 60001)"


@pytest.mark.parametrize("fail_import", [False, True])
def test_exact_import_result_and_failure_close_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_import: bool
) -> None:
    connection = Connection([(DATABASE, False), (0,), (0,), (0,)])
    monkeypatch.setattr(psycopg2, "connect", lambda *_args, **_kwargs: connection)

    def load(received: Any, *, schema: str, blob_root: Path) -> Any:
        assert received is connection
        assert schema == f"astralplane_fixture_{ID}"
        assert blob_root == tmp_path / "new-blobs"
        if fail_import:
            raise RuntimeError("sensitive driver failure")
        return SimpleNamespace(to_dict=lambda: {"schemaRevision": "066.001"})

    monkeypatch.setattr(
        driver, "_loader", lambda: SimpleNamespace(fixture_digest=lambda: DIGEST, load_fixture=load)
    )
    if fail_import:
        with pytest.raises(driver.QualificationImportError, match="admission must remain closed"):
            driver.import_fixture(**_args(tmp_path))
    else:
        report = driver.import_fixture(**_args(tmp_path))
        assert report["release_authorized"] is False
        assert report["fixture_sha256"] == DIGEST
        assert report["database_options"] == f"-csearch_path=astralplane_fixture_{ID},pg_catalog"
        assert "synthetic:synthetic" not in json.dumps(report)
    assert connection.closed


def test_cli_emits_diagnostic_evidence_and_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = [
        "--qualification-id", ID, "--expected-fixture-sha256", DIGEST,
        "--blob-root", str(tmp_path / "blobs"),
    ]
    monkeypatch.delenv(driver.DATABASE_ENV, raising=False)
    assert driver.main(arguments) == 2
    assert "explicit qualification database" in capsys.readouterr().err
    monkeypatch.setattr(driver, "import_fixture", lambda **_args: {"release_authorized": False})
    assert driver.main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == {"release_authorized": False}


def test_loader_is_the_canonical_source_owned_fixture() -> None:
    assert SCRIPT.parent.parent / "tests/fixtures/pre_split" == driver._loader().FIXTURE_ROOT
