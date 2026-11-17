"""Runs the bounded migration/recovery check matrix and writes digest-only local
evidence, without retaining SQL parameters or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
SOURCE_ROOT: Final = REPOSITORY_ROOT / "src"
DEFAULT_OUTPUT: Final = REPOSITORY_ROOT / "provenance" / "checks.json"
TEST_DATABASE_ENV: Final = "ASTRALPLANE_TEST_POSTGRES_DSN"

CHECKS: Final = (
    (
        "plane.migration.template0_default_public",
        "tests/integration/test_empty_database_startup.py::"
        "test_template0_default_public_schema_is_exactly_qualified_then_hardened",
    ),
    (
        "plane.migration.template0_hostile_owner_rejected",
        "tests/integration/test_empty_database_startup.py::"
        "test_template0_public_schema_with_arbitrary_owner_is_not_normalized",
    ),
    (
        "plane.migration.predecessor_damage_rejected",
        "tests/integration/test_pre_split_upgrade.py::"
        "test_post_load_predecessor_damage_is_rejected_before_any_migration_repair",
    ),
    (
        "plane.migration.pre_split_upgrade",
        "tests/integration/test_pre_split_upgrade.py::"
        "test_pre_split_upgrade_preserves_representative_database_and_blobs",
    ),
    (
        "plane.migration.repeat_upgrade",
        "tests/integration/test_pre_split_upgrade.py::"
        "test_repeat_upgrade_is_a_noop_with_identical_evidence",
    ),
    (
        "plane.migration.transactional_failure_recovery",
        "tests/integration/test_pre_split_upgrade.py::"
        "test_transactional_failure_rolls_back_both_edges_and_retry_recovers",
    ),
    (
        "plane.migration.blob_failure",
        "tests/integration/test_pre_split_upgrade.py::"
        "test_blob_stage_failure_creates_neither_schema_nor_published_root",
    ),
    (
        "plane.migration.documented_joint_restore",
        "tests/integration/test_pre_split_upgrade.py::"
        "test_documented_joint_restore_returns_to_066_then_reapplies_upgrade",
    ),
)

INPUT_PATHS: Final = (
    "src/astralplane/database/migrations.py",
    "src/astralplane/database/legacy_baseline_066.py",
    "src/astralplane/database/revision.py",
    "tests/fixtures/pre_split/baseline.sql",
    "tests/fixtures/pre_split/database.json",
    "tests/fixtures/pre_split/expected.json",
    "tests/fixtures/pre_split/loader.py",
    "tests/fixtures/pre_split/blobs/fixture-owner-a/artifact-1/artifact.txt",
    "tests/fixtures/pre_split/blobs/fixture-owner-b/artifact-2/summary.json",
    "tests/integration/test_pre_split_upgrade.py",
    "tests/integration/test_empty_database_startup.py",
    "docs/migration-and-recovery.md",
    "scripts/record_migration_evidence.py",
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _run_git(*arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _input_evidence() -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    for relative in INPUT_PATHS:
        path = REPOSITORY_ROOT / relative
        content = path.read_bytes()
        evidence.append(
            {
                "path": relative,
                "sha256": _sha256(content),
                "sizeBytes": len(content),
            }
        )
    return evidence


def _run_check(check_id: str, selector: str) -> dict[str, object]:
    command = (sys.executable, "-m", "pytest", "-q", "-rA", selector)
    started = datetime.now(UTC)
    try:
        result = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
            timeout=900,
        )
        output = result.stdout or b""
        exit_code: int | None = result.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        raw_output = exc.output or b""
        output = (
            raw_output.encode("utf-8", errors="replace")
            if isinstance(raw_output, str)
            else raw_output
        )
        exit_code = None
        timed_out = True
    finished = datetime.now(UTC)
    skipped = re.search(rb"\b[1-9][0-9]* skipped\b", output.lower()) is not None
    status = "passed" if exit_code == 0 and not skipped and not timed_out else "failed"
    return {
        "command": ["python", "-m", "pytest", "-q", "-rA", selector],
        "completedAt": finished.isoformat().replace("+00:00", "Z"),
        "durationMilliseconds": round((finished - started).total_seconds() * 1000),
        "exitCode": exit_code,
        "id": check_id,
        "outputSha256": _sha256(output),
        "status": status,
        "timedOut": timed_out,
    }


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    parent = path.parent.resolve(strict=True)
    if path.parent.resolve(strict=True) != parent:
        raise RuntimeError("evidence output parent did not resolve exactly")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".astralplane-checks-",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=True, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not os.environ.get(TEST_DATABASE_ENV):
        raise SystemExit(f"{TEST_DATABASE_ENV} must name an isolated PostgreSQL test database")
    output = args.output.resolve()
    if output.parent != DEFAULT_OUTPUT.parent.resolve() or output.name != DEFAULT_OUTPUT.name:
        raise SystemExit("evidence output must be the repository provenance/checks.json path")

    sys.path.insert(0, str(SOURCE_ROOT))
    sys.path.insert(0, str(REPOSITORY_ROOT))
    migrations = importlib.import_module("astralplane.database.migrations")
    fixture_loader = importlib.import_module("tests.fixtures.pre_split.loader")

    results = [_run_check(check_id, selector) for check_id, selector in CHECKS]
    status = "passed" if all(item["status"] == "passed" for item in results) else "failed"
    fixture_expected = json.loads(
        (REPOSITORY_ROOT / "tests/fixtures/pre_split/expected.json").read_text(
            encoding="utf-8"
        )
    )
    payload: dict[str, object] = {
        "candidate": {
            "branch": _run_git("branch", "--show-current"),
            "head": _run_git("rev-parse", "HEAD"),
            "workingTree": "dirty" if _run_git("status", "--porcelain") else "clean",
        },
        "checks": results,
        "execution": {
            "maxWorkers": 1,
            "mode": "sequential",
            "resultOutput": "sha256-only",
        },
        "fixtureDigest": fixture_loader.fixture_digest(),
        "inputs": _input_evidence(),
        "legacyBaseline": fixture_expected["legacyBaseline"],
        "migrationRegistryDigest": migrations.MIGRATION_DIGEST,
        "preMigrationCatalog": fixture_expected["preMigrationCatalog"],
        "recordedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "schemaVersion": "astralplane.migration-checks/v1",
        "status": status,
    }
    _write_atomic(output, payload)
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
