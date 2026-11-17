"""Tests for AstralPlane's data-provenance records: extraction manifest digests,
transformation-ledger completeness, migration-slice provenance, and exact git-tuple
replay of selection roots.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXTRACTION = ROOT / "provenance" / "extraction.json"
TRANSFORMATIONS = ROOT / "provenance" / "transformations.json"
MIGRATION_CHECKS = ROOT / "provenance" / "checks.json"
ATTACHMENT_SLICE = ROOT / "provenance" / "attachment-parser-and-blob-composition.json"
WORK_ADMISSION_SLICE = ROOT / "provenance" / "work-admission-and-quality-audit.json"
SOURCE_REPOSITORY_ENV = "ASTRALDEEP_SOURCE_REPO"
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _load_json(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), path
    return document


def _canonical_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _safe_relative_path(value: object) -> str:
    assert isinstance(value, str) and value
    assert "\\" not in value and "\x00" not in value
    path = Path(value)
    assert not path.is_absolute()
    assert all(part not in {"", ".", ".."} for part in value.split("/"))
    assert path.as_posix() == value
    return value


def _sha256(path: Path) -> str:
    assert path.is_file() and not path.is_symlink(), path
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_extraction_manifest_digest_and_fixed_identities() -> None:
    extraction = _load_json(EXTRACTION)
    digest_document = dict(extraction)
    recorded_digest = digest_document.pop("manifestSha256")

    assert extraction["format"] == "astral.extraction-provenance/v1"
    assert extraction["digestAlgorithm"] == "sha256"
    assert recorded_digest == "faf3e958601379954875cbc928423bc95d4b1786b59d6d3992241c8f69ee91ba"
    assert hashlib.sha256(_canonical_bytes(digest_document)).hexdigest() == recorded_digest

    source = extraction["source"]
    destination = extraction["destination"]
    assert isinstance(source, dict) and isinstance(destination, dict)
    assert source == {
        "commit": "fc113c4f99121b2053bb71523835c5c4743f1f56",
        "repository": "https://github.com/AstralDeep/AstralDeep.git",
        "tree": "914b04d369faa4ee0d7c2bb59ce09db38a18d45a",
    }
    assert destination["repository"] == "https://github.com/AstralDeep/AstralPlane.git"
    assert destination["branch"] == "codex/074-extract-data-plane"
    baseline = destination["legacyBaseline"]
    assert isinstance(baseline, dict)
    assert baseline["commit"] == "2cd12602e361a0dfb3e74655070720be405c09a3"
    assert baseline["sourceRef"] == "refs/heads/master"


def test_transformation_ledger_completely_absorbs_selected_source() -> None:
    extraction = _load_json(EXTRACTION)
    transformations = _load_json(TRANSFORMATIONS)
    extracted_entries = extraction["entries"]
    ledger_entries = transformations["entries"]
    assert isinstance(extracted_entries, list) and isinstance(ledger_entries, list)

    assert transformations["format"] == "astral.extraction-transformations/v1"
    assert transformations["sourceManifestSha256"] == extraction["manifestSha256"]
    assert len(extracted_entries) == len(ledger_entries) == 52

    extracted = {entry["sourcePath"]: entry for entry in extracted_entries}
    assert len(extracted) == len(extracted_entries)
    ledger_paths = [entry["sourcePath"] for entry in ledger_entries]
    assert ledger_paths == sorted(ledger_paths)
    assert len(ledger_paths) == len(set(ledger_paths))
    assert set(ledger_paths) == set(extracted)

    for ledger in ledger_entries:
        imported = extracted[ledger["sourcePath"]]
        assert ledger["sourceBlob"] == imported["blob"]
        assert ledger["sourceMode"] == imported["mode"] == "100644"
        assert ledger["destinationPath"] == imported["destinationPath"]
        assert ledger["resultStatus"] == "absorbed"
        assert not (ROOT / _safe_relative_path(ledger["destinationPath"])).exists()
        assert "_legacy_import" in ledger["destinationPath"]
        assert isinstance(ledger["task"], str) and re.fullmatch(
            r"T[0-9]{3}(?:/T[0-9]{3})*", ledger["task"]
        )
        assert isinstance(ledger["reason"], str) and 20 <= len(ledger["reason"]) <= 500

        results = ledger["resultPaths"]
        assert isinstance(results, list) and results
        result_paths = [result["path"] for result in results]
        assert result_paths == sorted(result_paths)
        assert len(result_paths) == len(set(result_paths))
        for result in results:
            relative = _safe_relative_path(result["path"])
            assert "_legacy_import" not in relative
            assert SHA256.fullmatch(result["sha256"])
            assert _sha256(ROOT / relative) == result["sha256"]


def test_slice_provenance_matches_committed_migration_evidence() -> None:
    checks = _load_json(MIGRATION_CHECKS)
    attachment = _load_json(ATTACHMENT_SLICE)
    work_admission = _load_json(WORK_ADMISSION_SLICE)
    migration_evidence = attachment["migrationEvidence"]

    assert checks["schemaVersion"] == "astralplane.migration-checks/v1"
    assert checks["status"] == "passed"
    assert isinstance(checks["checks"], list) and len(checks["checks"]) == 8
    assert isinstance(migration_evidence, dict)
    assert migration_evidence == {
        "path": "provenance/checks.json",
        "status": "passed",
        "cases": 8,
        "fixtureDigest": checks["fixtureDigest"],
    }
    assert work_admission["migrationEvidence"] == "provenance/checks.json"


def test_selection_roots_replay_exact_immutable_git_tuples() -> None:
    raw_source_repository = os.environ.get(SOURCE_REPOSITORY_ENV)
    if not raw_source_repository:
        pytest.skip(f"{SOURCE_REPOSITORY_ENV} is required for immutable-source replay")
    source_repository = Path(raw_source_repository).resolve(strict=True)
    extraction = _load_json(EXTRACTION)
    source = extraction["source"]
    assert isinstance(source, dict)
    source_commit = source["commit"]
    assert isinstance(source_commit, str) and SHA1.fullmatch(source_commit)

    observed_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=source_repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(observed_root).resolve() == source_repository
    observed_tree = subprocess.run(
        ["git", "show", "-s", "--format=%T", source_commit],
        cwd=source_repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert observed_tree == source["tree"]

    entries = extraction["entries"]
    roots = extraction["selectionRoots"]
    assert isinstance(entries, list) and isinstance(roots, list)
    expected_paths = [entry["sourcePath"] for entry in entries]
    assert roots == expected_paths
    assert expected_paths == sorted(expected_paths)

    for entry in entries:
        path = _safe_relative_path(entry["sourcePath"])
        raw = subprocess.run(
            ["git", "ls-tree", "-z", source_commit, "--", path],
            cwd=source_repository,
            check=True,
            capture_output=True,
        ).stdout
        assert raw.endswith(b"\x00") and raw.count(b"\x00") == 1
        metadata, observed_path = raw[:-1].split(b"\t", 1)
        mode, object_type, blob = metadata.decode("ascii").split(" ")
        assert observed_path.decode("utf-8") == path
        assert (mode, object_type, blob) == (entry["mode"], "blob", entry["blob"])
        size = subprocess.run(
            ["git", "cat-file", "-s", blob],
            cwd=source_repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert int(size) == entry["bytes"]
