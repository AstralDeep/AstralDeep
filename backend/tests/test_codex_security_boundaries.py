"""Regressions for filesystem, environment, artifact and admission boundaries of the review CLI."""

import json
import shutil
from pathlib import Path

import pytest

from devtools import codex_security_review as csr
from tests.test_codex_security_review import (
    build_checkout,
    run_args,
    write_fakes,
    write_policy,
)


def test_inventory_refuses_tracked_symlink_to_private_host_file(tmp_path):
    repo = build_checkout(tmp_path)
    policy = csr.load_policy(write_policy(repo))
    private = tmp_path / "private-canary"
    private.write_text("HOST_PRIVATE_CANARY")
    (repo / "scripts" / "link.py").symlink_to(private)
    from tests.test_codex_security_review import git

    git(repo, "add", "scripts/link.py")
    with pytest.raises(csr.Denial, match="regular|symlink"):
        csr.source_inventory(repo, policy)


def test_sandbox_never_mounts_host_root_or_shares_network(tmp_path):
    repo = build_checkout(tmp_path)
    argv = csr.sandbox_argv(
        Path("/usr/bin/bwrap"),
        tmp_path / "runtime",
        repo,
        tmp_path / "raw",
        ["codex-security"],
        [],
    )
    assert ["--ro-bind", "/", "/"] not in [argv[i : i + 3] for i in range(len(argv))]
    assert "--share-net" not in argv


def test_egress_is_rejected_before_any_scanner_invocation(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path)
    assert (
        csr.main(run_args(tmp_path, repo, policy, program, extra=["--egress-approve"]))
        == csr.EXIT_DENIED
    )
    assert not (tmp_path / "bwrap-record.json").exists()
    assert "unavailable" in capsys.readouterr().err


@pytest.mark.parametrize(
    "uri",
    [
        "../private.txt",
        "scripts/../../private.txt",
        "file:///etc/passwd",
        "https://example.invalid/a",
        "scripts\\..\\private.txt",
    ],
)
def test_sanitizer_rejects_escaped_or_network_paths(uri):
    policy = json.loads(Path("tooling/codex-security/review-policy.json").read_text())
    with pytest.raises(csr.SanitizationError):
        csr.sanitize_document({"uri": uri}, Path("/source"), policy)


def test_read_result_adapts_actual_upstream_mock_artifacts(tmp_path):
    fixtures = Path(__file__).parent / "fixtures" / "codex_security"
    for original, destination in [
        ("upstream-manifest.json", "scan-manifest.json"),
        ("upstream-findings.json", "findings.json"),
        ("upstream-coverage.json", "coverage.json"),
    ]:
        shutil.copyfile(fixtures / original, tmp_path / destination)
    (tmp_path / "exports").mkdir()
    shutil.copyfile(
        fixtures / "upstream-results.sarif", tmp_path / "exports/results.sarif"
    )
    policy = json.loads(Path("tooling/codex-security/review-policy.json").read_text())
    shutil.copyfile(
        fixtures / "upstream-adapter-validation.json",
        tmp_path / "adapter-validation.json",
    )
    result, sarif, coverage = csr.read_result(
        tmp_path,
        policy,
        "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
    )
    assert result["status"] == "complete"
    assert result["calls_used"] == 0 and result["cost_usd"] == 0
    assert len(sarif["runs"][0]["results"]) == 12
    assert coverage["completeness"] == "complete"
