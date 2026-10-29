"""Exercise the pinned-runtime canonical validation adapter independently of scanner/network execution."""

import json
from types import SimpleNamespace

import pytest

from devtools import codex_security_adapter as adapter
from tests.test_codex_security_review import copy_upstream, read_json


@pytest.fixture
def canonical_bundle(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    scan = tmp_path / "scan"
    copy_upstream(scan)
    (scan / "adapter-validation.json").unlink()
    expected = read_json(scan / "exports/results.sarif")
    snapshot = read_json(scan / "scan-manifest.json")["scan"]["target"][
        "snapshotDigest"
    ]
    finalizer = SimpleNamespace(build_sarif_projection=lambda *_args: expected)
    targets = SimpleNamespace(
        directory_content_digest=lambda *_args, **_kwargs: snapshot
    )
    monkeypatch.setattr(
        adapter,
        "load_upstream",
        lambda name: finalizer if name == "finalize_scan_contract" else targets,
    )
    return source, scan


def test_validation_receipt_binds_all_raw_artifacts_and_both_source_identities(
    canonical_bundle,
):
    source, scan = canonical_bundle
    digest = adapter.snapshot_digest(source)
    adapter.validate_output(scan, source, digest)
    receipt = read_json(scan / "adapter-validation.json")
    assert receipt["status"] == "validated"
    assert (
        receipt["source_digest"]
        == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    )
    assert receipt["scan_id"] == "2bf35f0a-ac7a-4863-8d28-512ee71827bf"
    assert set(receipt["artifacts"]) == {
        "scan-manifest.json",
        "findings.json",
        "coverage.json",
        "exports/results.sarif",
    }
    assert (scan / "adapter-validation.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("change", ["export", "source", "snapshot"])
def test_canonical_validation_never_receipts_unrelated_export_or_source(
    canonical_bundle, change
):
    source, scan = canonical_bundle
    digest = adapter.snapshot_digest(source)
    if change == "export":
        path = scan / "exports/results.sarif"
        document = read_json(path)
        document["runs"][0]["results"][0]["message"]["text"] = "unrelated text"
        path.write_text(json.dumps(document))
    elif change == "source":
        (source / "different.py").write_text("CHANGED_SOURCE = True\n")
    else:
        path = scan / "scan-manifest.json"
        document = read_json(path)
        document["scan"]["target"]["snapshotDigest"] = (
            "codex-security-snapshot/v1:sha256:" + "0" * 64
        )
        path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        adapter.validate_output(scan, source, digest)
    assert not (scan / "adapter-validation.json").exists()


def test_main_refuses_failed_scanner_without_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=7),
    )
    assert adapter.main(["--source-digest", "0" * 64]) == 7


def test_main_translates_strict_validator_failure_to_nonzero(
    canonical_bundle, monkeypatch, capsys
):
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    def rejected(*_args):
        raise ValueError("canonical SARIF mismatch")

    monkeypatch.setattr(adapter, "validate_output", rejected)
    assert adapter.main(["--source-digest", "0" * 64]) == 5
    assert "canonical SARIF mismatch" in capsys.readouterr().err


def test_main_validates_after_real_command_success(canonical_bundle, monkeypatch):
    source, scan = canonical_bundle
    monkeypatch.setattr(adapter, "SOURCE", source)
    monkeypatch.setattr(adapter, "SCAN", scan)
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    assert adapter.main(["--source-digest", adapter.snapshot_digest(source)]) == 0
    assert (scan / "adapter-validation.json").is_file()


def test_loader_uses_the_trusted_runtime_script_directory(tmp_path, monkeypatch):
    (tmp_path / "trusted.py").write_text("VALUE = 37\n")
    monkeypatch.setattr(adapter, "SCRIPTS", tmp_path)
    assert adapter.load_upstream("trusted").VALUE == 37


def test_missing_upstream_loader_fails_closed(monkeypatch):
    monkeypatch.setattr(
        adapter.importlib.util, "spec_from_file_location", lambda *_args: None
    )
    with pytest.raises(ValueError, match="unavailable"):
        adapter.load_upstream("finalize_scan_contract")


def test_snapshot_identity_rejects_symlinks(tmp_path):
    (tmp_path / "link").symlink_to("missing-private-file")
    with pytest.raises(ValueError, match="non-regular"):
        adapter.snapshot_digest(tmp_path)


def test_snapshot_identity_matches_the_host_inventory(canonical_bundle, tmp_path):
    from devtools import codex_security_review as csr
    from tests.test_codex_security_review import build_checkout, write_policy

    repo = build_checkout(tmp_path)
    inventory, digest, _excluded = csr.source_inventory(
        repo, csr.load_policy(write_policy(repo))
    )
    snapshot = tmp_path / "materialized"
    csr.materialize_snapshot(repo, snapshot, inventory)
    assert adapter.snapshot_digest(snapshot) == digest
