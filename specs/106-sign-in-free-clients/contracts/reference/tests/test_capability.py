"""Runs the capability vectors through capability.py and holds the README contract table to the reference and vector files."""

from __future__ import annotations

import json
from pathlib import Path

import capability
import pytest

HERE = Path(__file__).resolve().parent
VECTORS = json.loads((HERE.parents[1] / "vectors" / "capability-v1.json").read_text())
README = (HERE.parents[1] / "capability.md").read_text()


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_the_reference_answers_every_vector(case):
    assert capability.run(case["kind"], case["input"]) == case["expect"]


def test_the_reference_carries_the_contract_format():
    assert capability.FORMAT == "astral.capability/v1"
    assert capability.INDEX_FORMAT == "astral.capability-index/v1"
    assert capability.BASELINE == "astral.core/v1"


def test_the_readme_table_lists_the_reference_and_vectors():
    assert "reference/capability.py" in README
    assert "vectors/capability-v1.json" in README
    assert "\\v" not in README and "\\r" not in README


@pytest.mark.parametrize(
    "uses,expected",
    [
        (["astral.cad-kernel@^1.0"], "1.4.2"),
        (["astral.cad-kernel@1.0.0"], "1.0.0"),
        (["astral.cad-kernel@^2.0"], "capability.missing"),
    ],
    ids=["caret-picks-highest", "exact-pin", "unsatisfied"],
)
def test_resolution_picks_the_highest_satisfying_version(uses, expected):
    ledger = [
        {"id": "astral.cad-kernel", "version": "1.0.0"},
        {"id": "astral.cad-kernel", "version": "1.4.2"},
        {"id": "astral.cad-kernel", "version": "1.2.0"},
        {"id": "astral.web-fetch", "version": "2.0.0"},
    ]
    out = capability.run("resolve", {"uses": uses, "ledger": ledger})
    if expected.startswith("capability."):
        assert out == {"code": expected}
    else:
        assert out == {"resolved": [{"id": "astral.cad-kernel", "version": expected}]}


def test_install_verification_follows_the_documented_order():
    entry = {"id": "astral.cad-kernel", "version": "1.0.0", "kind": "kernel",
             "manifest_sha256": "a" * 64, "package_sha256": "b" * 64, "signer": "astral"}
    fetched = "a" * 64
    # digest first, before every other failure
    assert capability.run("check_install", {"entry": {**entry, "manifest_sha256": "c" * 64},
                                            "fetched_manifest_sha256": fetched, "signer": "astral",
                                            "allow_signers": ["astral"], "host_python": "3.12",
                                            "phi_excluded": False, "revoked": True,
                                            "manifest": {}}) == {"code": "capability.digest"}
    # signer before incompatibility and revocation
    assert capability.run("check_install", {"entry": entry, "fetched_manifest_sha256": fetched,
                                            "signer": "other", "allow_signers": ["astral"],
                                            "host_python": "3.12", "phi_excluded": False,
                                            "revoked": True, "manifest": {}}) == {"code": "capability.signer"}
    # incompatibility before revocation
    assert capability.run("check_install", {"entry": entry, "fetched_manifest_sha256": fetched,
                                            "signer": "astral", "allow_signers": ["astral"],
                                            "host_python": "3.11", "phi_excluded": False,
                                            "revoked": True,
                                            "manifest": {"runtime": {"python": "3.12", "wheels": ["build123d==0.13.0"]},
                                                         "permissions": {"phi": "none"}}}) == {"code": "capability.incompatible"}
    assert capability.run("check_install", {"entry": entry, "fetched_manifest_sha256": fetched,
                                            "signer": "astral", "allow_signers": ["astral"],
                                            "host_python": "3.12", "phi_excluded": False,
                                            "revoked": True,
                                            "manifest": {"runtime": {"python": "3.12", "wheels": ["build123d==0.13.0"]},
                                                         "permissions": {"phi": "none"}}}) == {"code": "capability.revoked"}


def test_kernel_compatibility_reads_the_fetched_manifest():
    entry = {"id": "astral.cad-kernel", "version": "1.0.0", "kind": "kernel",
             "manifest_sha256": "a" * 64, "package_sha256": "b" * 64, "signer": "astral"}
    manifest = {"runtime": {"python": "3.12", "wheels": ["build123d==0.13.0"]},
                "permissions": {"phi": "none"}}
    ok = {"entry": entry, "fetched_manifest_sha256": "a" * 64, "signer": "astral",
          "allow_signers": ["astral"], "phi_excluded": False, "revoked": False, "manifest": manifest}
    assert capability.run("check_install", {**ok, "host_python": "3.12.8"}) == {"installed": True, "id": "astral.cad-kernel", "version": "1.0.0"}
    assert capability.run("check_install", {**ok, "host_python": "3.11.9"}) == {"code": "capability.incompatible"}
    phi = {**ok, "manifest": {**manifest, "permissions": {"phi": "reviewed"}}}
    assert capability.run("check_install", {**phi, "host_python": "3.12", "phi_excluded": True}) == {"code": "capability.incompatible"}
