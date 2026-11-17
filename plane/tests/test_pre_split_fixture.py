"""Tests for the synthetic pre-split fixture (tests/fixtures/pre_split): digest-bound
coverage of every durable cluster, baseline SQL identity, and partial-stage blob
recovery.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from astralplane.database.legacy_baseline_066 import (
    LEGACY_BASELINE_SOURCE_BLOB,
    _LegacyBaseline066Builder,
)
from tests.fixtures.pre_split import loader as fixture_loader
from tests.fixtures.pre_split.loader import (
    FixtureLoadError,
    fixture_digest,
    materialize_blob_fixture,
    verify_blob_fixture,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "pre_split"


def test_fixture_is_digest_bound_and_covers_every_durable_cluster() -> None:
    database = json.loads((FIXTURE_ROOT / "database.json").read_text(encoding="utf-8"))
    expected = json.loads((FIXTURE_ROOT / "expected.json").read_text(encoding="utf-8"))

    assert database["schemaRevision"] == expected["schemaRevisionBeforeUpgrade"] == "066.001"
    assert expected["schemaPath"] == [
        "066.001",
        "067.001",
        "074.001",
        "074.002",
        "074.003",
        "074.004",
    ]
    assert expected["schemaRevisionAfterUpgrade"] == "074.004"
    builder_path = Path(inspect.getsourcefile(_LegacyBaseline066Builder) or "")
    assert expected["legacyBaseline"] == {
        "builderSha256": hashlib.sha256(builder_path.read_bytes()).hexdigest(),
        "loaderSha256": hashlib.sha256(Path(fixture_loader.__file__).read_bytes()).hexdigest(),
        "sourceBlob": LEGACY_BASELINE_SOURCE_BLOB,
    }
    assert expected["preMigrationCatalog"] == {
        "rowCount": 1593,
        "sha256": "84cc9f0af555517013af26ada3920aebb8cc10e0d05fed75d424f960c810aa5f",
    }
    assert len(database["owners"]) == expected["ownerCount"] == 2
    assert sorted(database["records"]) == sorted(expected["recordDomains"])
    assert {
        domain: len(records) for domain, records in database["records"].items()
    } == expected["recordCounts"]
    for record in expected["blobs"]:
        blob_path = FIXTURE_ROOT / "blobs" / record["storageKey"]
        blob = blob_path.read_bytes()
        assert len(blob) == record["sizeBytes"]
        assert hashlib.sha256(blob).hexdigest() == record["sha256"]
        assert blob_path.resolve().is_relative_to(FIXTURE_ROOT.resolve())
    assert len(fixture_digest()) == 64


def test_baseline_sql_contains_every_semantic_fixture_identity() -> None:
    database = json.loads((FIXTURE_ROOT / "database.json").read_text(encoding="utf-8"))
    expected = json.loads((FIXTURE_ROOT / "expected.json").read_text(encoding="utf-8"))
    baseline = (FIXTURE_ROOT / "baseline.sql").read_text(encoding="utf-8")

    for records in database["records"].values():
        for record in records:
            identifiers = (
                value
                for key, value in record.items()
                if key.endswith("Id") and isinstance(value, str)
            )
            assert all(identifier in baseline for identifier in identifiers)
    assert "('revision', '066.001')" in baseline
    assert "astralplane_migration_digest" not in baseline
    for blob in expected["blobs"]:
        assert blob["storageKey"] in baseline
        assert blob["sha256"] in baseline
        assert str(blob["sizeBytes"]) in baseline


def test_blob_loader_discards_partial_stage_and_recovers(tmp_path: Path) -> None:
    destination = tmp_path / "materialized"

    with pytest.raises(FixtureLoadError, match="injected synthetic"):
        materialize_blob_fixture(destination, fail_after_files=1)

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".astralplane-fixture-stage-*"))

    evidence = materialize_blob_fixture(destination)

    assert verify_blob_fixture(destination) == evidence
    assert {item.storage_key for item in evidence} == {
        "fixture-owner-a/artifact-1/artifact.txt",
        "fixture-owner-b/artifact-2/summary.json",
    }


def test_fixture_contains_only_explicit_synthetic_non_sensitive_values() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(FIXTURE_ROOT.rglob("*"))
        if path.is_file()
        and path.name != "README.md"
        and path.suffix in {".json", ".py", ".sql", ".txt"}
    ).lower()
    denied = (
        "@uky.edu",
        "authorization: bearer",
        "api_key",
        "apikey",
        "password",
        "patient",
        "private key",
        "secret",
        "token",
    )
    assert all(marker not in text for marker in denied)
    assert "synthetic" in text
    assert "placeholder_summary_sha256" not in text
