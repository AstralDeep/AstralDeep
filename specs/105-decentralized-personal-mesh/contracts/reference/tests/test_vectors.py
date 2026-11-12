"""Runs every committed vector case through the reference and checks the generator reproduces the committed files.
The kernel's own harness reads the same files, so a failure here means the contract and the vectors disagree.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import generate_vectors
from astral_mesh_ref import runner

VECTORS = Path(__file__).resolve().parents[2] / "vectors"
FILES = sorted(p for p in VECTORS.glob("*.json") if p.name != "index.json")


def _cases() -> list[tuple[str, dict]]:
    out = []
    for path in FILES:
        doc = json.loads(path.read_text(encoding="utf-8"))
        out.extend((f"{doc['name']}/{case['name']}", case) for case in doc["cases"])
    return out


CASES = _cases()


@pytest.mark.parametrize("case", [c for _, c in CASES], ids=[i for i, _ in CASES])
def test_reference_reproduces_the_committed_expectation(case: dict) -> None:
    assert runner.run(case["kind"], case["input"]) == case["expect"]


def test_every_case_kind_is_implemented() -> None:
    assert {case["kind"] for _, case in CASES} <= set(runner.OPS)


def test_every_runner_operation_is_exercised() -> None:
    assert {case["kind"] for _, case in CASES} == set(runner.OPS)


def test_case_names_are_unique_per_file() -> None:
    for path in FILES:
        names = [
            c["name"] for c in json.loads(path.read_text(encoding="utf-8"))["cases"]
        ]
        assert len(names) == len(set(names)), path.name


def test_each_file_has_valid_and_invalid_cases() -> None:
    for path in FILES:
        cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
        errors = sum("error" in c["expect"] for c in cases)
        assert 0 < len(cases), path.name
        if path.name not in ("fold.json", "crypto.json", "feed.json"):
            assert 0 < errors < len(cases), path.name


def test_index_matches_the_files() -> None:
    index = json.loads((VECTORS / "index.json").read_text(encoding="utf-8"))
    assert set(index["files"]) == {p.name for p in FILES}
    for path in FILES:
        entry = index["files"][path.name]
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["cases"] == len(
            json.loads(path.read_text(encoding="utf-8"))["cases"]
        )


def test_generator_check_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["generate_vectors.py", "--check"])
    assert generate_vectors.main() == 0


REQUIRED_NAMES = {
    "feed/gap-then-fill",
    "feed/edited-header",
    "feed/fork-two-events-at-one-seq",
    "feed/future-clock",
    "feed/wrong-signature-context",
    "event/bad-header-signature",
    "event/signature-with-wrong-context",
    "event/edited-body",
    "event/future-clock-over-five-minutes",
    "roster/removed-device-later-entry-rejected",
    "roster/concurrent-add-by-removed-device-is-void",
    "fold/revoke-beats-a-concurrent-grant",
    "policy/concurrent-narrowings-merge-to-the-minimum",
    "policy/widening-without-consent-is-ignored",
    "sync/phi-body-withheld-from-a-non-phi-peer",
    "crypto/wrong-context",
}


def test_the_required_invalid_cases_exist() -> None:
    assert REQUIRED_NAMES <= {name for name, _ in CASES}
