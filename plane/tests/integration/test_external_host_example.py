"""Qualifies examples/external_host_example.py against a test-owned real PostgreSQL instance:
the example boots an empty database through the guarded migrations, exercises the public facade
scenarios in-process, and also runs as a standalone subprocess. Both databases and every blob
root live outside the source tree for the duration of the tests and are removed afterwards."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg2
import pytest
from psycopg2.extensions import make_dsn

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PATH = REPOSITORY_ROOT / "examples" / "external_host_example.py"
SCENARIOS = (
    "boot",
    "explicit_durable_root",
    "owner_scoped_transactions",
    "idempotent_replay",
    "compare_and_set_fence",
    "caller_rollback",
    "explicit_reconciliation",
    "boot_refusal",
    "shutdown",
)


def _administer(administrator_dsn: str, statement: str) -> None:
    connection = psycopg2.connect(administrator_dsn)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(statement)
    finally:
        connection.close()


@pytest.fixture(scope="module")
def external_host_databases(postgres_administrator_dsn: str) -> Iterator[tuple[str, str]]:
    application = f"plane_ext_app_{uuid.uuid4().hex}"
    refusal = f"plane_ext_refusal_{uuid.uuid4().hex}"
    _administer(
        postgres_administrator_dsn, f'CREATE DATABASE "{application}" TEMPLATE template0'
    )
    try:
        _administer(
            postgres_administrator_dsn, f'CREATE DATABASE "{refusal}" TEMPLATE template0'
        )
        connection = psycopg2.connect(make_dsn(postgres_administrator_dsn, dbname=refusal))
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("CREATE TABLE external_host_decoy (component_id TEXT)")
        finally:
            connection.close()
        yield (
            make_dsn(postgres_administrator_dsn, dbname=application),
            make_dsn(postgres_administrator_dsn, dbname=refusal),
        )
    finally:
        _administer(
            postgres_administrator_dsn, f'DROP DATABASE IF EXISTS "{application}" WITH (FORCE)'
        )
        _administer(postgres_administrator_dsn, f'DROP DATABASE IF EXISTS "{refusal}" WITH (FORCE)')


def _load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("external_host_example", EXAMPLE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("the external-host example module is not loadable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def observed_example(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, list[Any]]:
    example = _load_example()
    create_runtime = example.create_postgres_runtime
    runtimes: list[Any] = []

    def capture_runtime(*args: Any, **kwargs: Any) -> Any:
        runtime = create_runtime(*args, **kwargs)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(example, "create_postgres_runtime", capture_runtime)
    return example, runtimes


def _assert_shared_shape(outcomes: list[Any], blob_root: Path) -> dict[str, Any]:
    by_name = {outcome.scenario: outcome.detail for outcome in outcomes}
    assert tuple(by_name) == SCENARIOS
    assert not blob_root.is_relative_to(REPOSITORY_ROOT)
    assert blob_root.is_dir()
    return by_name


def test_example_qualifies_the_public_facade_on_an_empty_database(
    external_host_databases: tuple[str, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    application_dsn, refusal_dsn = external_host_databases
    example = _load_example()
    blob_root = tmp_path_factory.mktemp("external-host-inprocess") / "blobs"

    outcomes = example.run_example(application_dsn, str(blob_root), refusal_dsn)

    by_name = _assert_shared_shape(outcomes, blob_root)
    boot = by_name["boot"]
    assert boot["application_schema_revision"] == boot["migrated_to"]
    assert boot["applied_steps"] >= 1
    assert boot["reconciler"] == "external-host-example"
    assert boot["reconciliation_complete"] is True
    assert boot["ready"] is True
    root = by_name["explicit_durable_root"]
    assert root["relative_root_refusal"] == {
        "error_type": "SQLContractError",
        "code": "sql_contract",
    }
    assert root["unset_key_absent"] is True
    transactions = by_name["owner_scoped_transactions"]
    assert transactions["draft_revision"] == 0
    assert transactions["foreign_owner_read_absent"] is True
    replay = by_name["idempotent_replay"]
    assert replay["replayed_revision"] == 0
    assert replay["conflicting_replay"] == {
        "error_type": "RepositoryConflictError",
        "code": "repository_conflict",
    }
    fence = by_name["compare_and_set_fence"]
    assert fence["applied_revision"] == 1
    assert fence["stale_fence"] == {
        "error_type": "RepositoryConflictError",
        "code": "repository_conflict",
    }
    assert fence["foreign_owner_fence"] == {
        "error_type": "RepositoryNotFoundError",
        "code": "repository_not_found",
    }
    rollback = by_name["caller_rollback"]
    assert rollback["host_abort"]["error_type"] == "HostAbortError"
    assert rollback["draft_absent"] is True
    reconciliation = by_name["explicit_reconciliation"]
    assert reconciliation["hooks"] == ["external-host-example"]
    assert reconciliation["already_complete"] == [True]
    assert reconciliation["durably_complete"] is True
    refusal = by_name["boot_refusal"]
    assert refusal["error_type"] == "SchemaRevisionError"
    assert refusal["code"] == "schema_revision_incompatible"
    assert refusal["metadata"]["reason_codes"] == "schema_metadata_missing"
    shutdown = by_name["shutdown"]
    assert shutdown["pool_closed"] is True
    assert shutdown["post_shutdown_refusal"] == {
        "error_type": "InitializationError",
        "code": "initialization_failed",
    }


def test_example_runs_as_a_standalone_executable(
    external_host_databases: tuple[str, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    application_dsn, refusal_dsn = external_host_databases
    blob_root = tmp_path_factory.mktemp("external-host-subprocess") / "blobs"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPOSITORY_ROOT / "src"), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    completed = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE_PATH),
            "--database-url",
            application_dsn,
            "--blob-root",
            str(blob_root),
            "--refusal-database-url",
            refusal_dsn,
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=900,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    reports = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert reports[-1] == {"status": "passed", "scenarios": list(SCENARIOS)}
    assert [report["scenario"] for report in reports[:-1]] == list(SCENARIOS)
    assert reports[0]["detail"]["ready"] is True
    assert reports[-2]["detail"]["pool_closed"] is True


def test_example_cli_succeeds_without_optional_refusal_database(
    external_host_databases: tuple[str, str],
    observed_example: tuple[ModuleType, list[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    application_dsn, _ = external_host_databases
    example, runtimes = observed_example
    blob_root = tmp_path / "cli-blobs"

    result = example.main(["--database-url", application_dsn, "--blob-root", str(blob_root)])

    captured = capsys.readouterr()
    reports = [json.loads(line) for line in captured.out.splitlines()]
    expected = [scenario for scenario in SCENARIOS if scenario != "boot_refusal"]
    assert result == 0
    assert captured.err == ""
    assert reports[-1] == {"status": "passed", "scenarios": expected}
    assert [report["scenario"] for report in reports[:-1]] == expected
    assert reports[-2]["detail"]["pool_closed"] is True
    assert blob_root.is_dir()
    assert runtimes and all(runtime.health().pool.closed for runtime in runtimes)


def test_example_cli_refuses_relative_root_before_connecting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    example = _load_example()

    result = example.main(
        ["--database-url", "not-a-database-url", "--blob-root", "relative-blobs"]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err == (
        "--blob-root must be an absolute path outside source trees: relative-blobs\n"
    )


def test_example_cli_reports_typed_boot_failure_and_releases_connections(
    external_host_databases: tuple[str, str],
    observed_example: tuple[ModuleType, list[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, refusal_dsn = external_host_databases
    example, runtimes = observed_example
    blob_root = tmp_path / "failed-cli-blobs"

    result = example.main(["--database-url", refusal_dsn, "--blob-root", str(blob_root)])

    captured = capsys.readouterr()
    failure = json.loads(captured.err)
    assert result == 1
    assert captured.out == ""
    assert failure["status"] == "failed"
    assert failure["error_type"] == "SchemaRevisionError"
    assert failure["code"] == "schema_revision_incompatible"
    assert "passed" not in captured.err
    assert not blob_root.exists()
    assert runtimes and all(runtime.health().pool.closed for runtime in runtimes)


def test_example_closes_ready_runtime_when_blob_root_is_refused(
    external_host_databases: tuple[str, str],
    observed_example: tuple[ModuleType, list[Any]],
) -> None:
    application_dsn, _ = external_host_databases
    example, runtimes = observed_example

    with pytest.raises(example.SQLContractError) as raised:
        example.run_example(application_dsn, "relative-blobs")

    assert raised.value.code == "sql_contract"
    assert runtimes and all(runtime.health().pool.closed for runtime in runtimes)


def test_example_does_not_report_an_unmet_scenario_as_passed() -> None:
    example = _load_example()

    with pytest.raises(RuntimeError, match="scenario failed"):
        example._require(False, "scenario failed")


def test_example_repeated_boot_refusal_remains_typed_and_closes_each_pool(
    external_host_databases: tuple[str, str],
    observed_example: tuple[ModuleType, list[Any]],
) -> None:
    _, refusal_dsn = external_host_databases
    example, runtimes = observed_example

    for _ in range(2):
        outcome = example.demonstrate_boot_refusal(refusal_dsn)
        assert outcome.detail["error_type"] == "SchemaRevisionError"
        assert outcome.detail["code"] == "schema_revision_incompatible"
        assert outcome.detail["metadata"]["reason_codes"] == "schema_metadata_missing"

    assert runtimes and all(runtime.health().pool.closed for runtime in runtimes)
