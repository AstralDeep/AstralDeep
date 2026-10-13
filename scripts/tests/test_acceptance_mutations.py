"""Test the bounded Deep acceptance mutation runner's fail-closed evidence classification.
Import, collection, skip, missing-test, and survivor results cannot count as assertion kills.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_acceptance_mutations as gate
from scripts.check_acceptance_mutations import classify_result


@pytest.mark.parametrize(
    ("returncode", "body", "mutated", "expected"),
    [
        (0, "", False, "passed"),
        (0, "", True, "survived"),
        (
            1,
            '<failure message="Failed: DID NOT RAISE SignatureError"/>',
            True,
            "killed",
        ),
        (1, '<failure message="AssertionError: removed export"/>', True, "killed"),
        (1, '<failure message="ImportError: missing dependency"/>', True, "invalid"),
        (1, '<failure message="SyntaxError: invalid syntax"/>', True, "invalid"),
        (1, '<failure message="RuntimeError: disconnected"/>', True, "invalid"),
        (2, '<error message="ImportError"/>', True, "invalid"),
        (0, '<skipped message="disabled"/>', True, "invalid"),
        (1, '<failure message="AssertionError"/>', False, "invalid"),
        (2, '<failure message="AssertionError"/>', True, "invalid"),
    ],
)
def test_mutation_classifier_requires_assertion_failure(
    tmp_path: Path, returncode: int, body: str, mutated: bool, expected: str
) -> None:
    report = tmp_path / "report.xml"
    report.write_text(
        f'<testsuites><testsuite><testcase name="target">{body}</testcase></testsuite></testsuites>'
    )
    status, _ = classify_result(returncode, report, ("target",), mutated=mutated)
    assert status == expected


@pytest.mark.parametrize(
    "cases",
    [
        "",
        '<testcase name="other"/>',
        '<testcase name="target"/><testcase name="target"/>',
    ],
)
def test_mutation_classifier_requires_exact_focused_tests(
    tmp_path: Path, cases: str
) -> None:
    report = tmp_path / "report.xml"
    report.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    status, _ = classify_result(0, report, ("target",), mutated=True)
    assert status == "invalid"


@pytest.mark.parametrize("content", [None, "broken XML"])
def test_mutation_classifier_requires_readable_evidence(
    tmp_path: Path, content: str | None
) -> None:
    report = tmp_path / "report.xml"
    if content is not None:
        report.write_text(content)
    status, _ = classify_result(1, report, ("target",), mutated=True)
    assert status == "invalid"


def test_local_privacy_and_reader_mutations_detect_guard_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gate,
        "MUTATIONS",
        tuple(
            mutation
            for mutation in gate.MUTATIONS
            if mutation.path.startswith("shared/")
        ),
    )
    sources = {mutation.path for mutation in gate.MUTATIONS}
    before = {path: (gate.ROOT / "backend" / path).read_bytes() for path in sources}
    receipt = gate.qualify(tmp_path / "receipt.json")
    assert receipt["status"] == "passed"
    assert receipt["baseline"]["status"] == "passed"
    assert len(receipt["mutations"]) == 6
    assert all(row["status"] == "killed" for row in receipt["mutations"])
    assert before == {
        path: (gate.ROOT / "backend" / path).read_bytes() for path in sources
    }


@pytest.mark.parametrize(
    ("patterns", "credential_output"),
    [
        (
            "PHI_VALUE_PATTERNS",
            "Credential ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ.",
        ),
        (
            "PHI_VALUE_PATTERNS + SECRET_VALUE_PATTERNS",
            "Credential [REDACTED:token].",
        ),
    ],
)
def test_phi_mutation_removes_masking_without_disabling_added_credentials(
    patterns: str, credential_output: str
) -> None:
    mutation = next(
        item for item in gate.MUTATIONS if item.name == "phi-value-masking-removed"
    )
    original = (gate.ROOT / "backend/shared/phi_redactor.py").read_text()
    start = original.index("    for pattern, replacement in PHI_VALUE_PATTERNS")
    end = original.index("\n", start)
    source = (
        original[:start]
        + f"    for pattern, replacement in {patterns}:"
        + original[end:]
    )
    namespace = {}
    exec(compile(source.replace(mutation.before, mutation.after, 1), "mutant", "exec"), namespace)
    assert namespace["redact"]("Synthetic SSN 123-45-6789.") == (
        "Synthetic SSN 123-45-6789.",
        False,
    )
    assert namespace["redact"](
        "Credential ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ."
    ) == (credential_output, False)


def test_actual_proposal_collection_keeps_disposable_source_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source"
    for package in ("shared", "feedback"):
        shutil.copytree(gate.ROOT / "backend" / package, source / package)
    output = tmp_path / "collection"
    status, result = gate.run_focused(
        source,
        output,
        (
            "backend/tests/test_proposal_lifecycle_acceptance.py::"
            "test_proposal_rejects_artifact_tampering_before_review",
        ),
        mutated=False,
        collect_only=True,
    )
    assert status == "collected"
    origins = result["source_origins"]
    assert origins["collected"] == 1
    assert origins["modules"]["feedback.proposals"] == str(
        (source / "feedback/proposals.py").resolve()
    )
    assert origins["modules"]["shared"] == str(
        (source / "shared/__init__.py").resolve()
    )
    assert origins["sys_path"].index(str(gate.ROOT / "backend")) < origins[
        "sys_path"
    ].index(str(source))
    assert "1 test collected" in output.with_suffix(".log").read_text()
    monkeypatch.setattr(
        gate,
        "PYTEST_BOOTSTRAP",
        gate.PYTEST_BOOTSTRAP.replace(
            "sys, shared, feedback, pytest", "sys, shared, pytest"
        ),
    )
    status, _result = gate.run_focused(
        source,
        tmp_path / "unbound-collection",
        (
            "backend/tests/test_proposal_lifecycle_acceptance.py::"
            "test_proposal_rejects_artifact_tampering_before_review",
        ),
        mutated=False,
        collect_only=True,
    )
    assert status == "invalid"
    assert (
        "feedback escaped disposable source"
        in (tmp_path / "unbound-collection.log").read_text()
    )


@pytest.mark.parametrize("baseline_status", ["invalid", "survived"])
def test_mutation_gate_requires_passing_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline_status: str
) -> None:
    monkeypatch.setattr(
        gate, "run_focused", lambda *args, **kwargs: (baseline_status, {})
    )
    receipt = gate.qualify(tmp_path / "receipt.json")
    assert receipt["status"] == "failed"
    assert receipt["mutations"] == []


def test_mutation_gate_requires_unique_guard_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "run_focused", lambda *args, **kwargs: ("passed", {}))
    monkeypatch.setattr(
        gate,
        "MUTATIONS",
        (gate.Mutation("missing", "shared/phi_redactor.py", "absent guard", "", ()),),
    )
    receipt = gate.qualify(tmp_path / "receipt.json")
    assert receipt["status"] == "failed"
    assert receipt["mutations"][0]["status"] == "invalid"


@pytest.mark.parametrize("status", ["survived", "invalid"])
def test_mutation_gate_rejects_survivors_and_invalid_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    monkeypatch.setattr(
        gate,
        "run_focused",
        lambda *args, **kwargs: (status if kwargs["mutated"] else "passed", {}),
    )
    monkeypatch.setattr(gate, "MUTATIONS", (gate.MUTATIONS[0],))
    assert gate.qualify(tmp_path / "receipt.json")["status"] == "failed"


def test_focused_execution_timeout_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("focused-test", 60)

    monkeypatch.setattr(gate.subprocess, "run", timeout)
    status, _ = gate.run_focused(
        tmp_path, tmp_path / "receipt", ("test_target",), mutated=True
    )
    assert status == "invalid"


def test_prior_assertion_receipts_cannot_hide_current_source_authority_failure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "empty-source"
    source.mkdir()
    output = tmp_path / "receipt"
    output.with_suffix(".xml").write_text(
        '<testsuites><testsuite><testcase name="test_target">'
        '<failure message="AssertionError: prior guard kill"/>'
        "</testcase></testsuite></testsuites>"
    )
    output.with_suffix(".origins.json").write_text(
        json.dumps({"modules": {}, "collected": 1})
    )
    output.with_suffix(".log").write_text("prior passing source authority")
    status, result = gate.run_focused(source, output, ("test_target",), mutated=True)
    assert status == "invalid"
    assert result["reason"] == "missing disposable-source authority receipt"
    assert not output.with_suffix(".xml").exists()
    assert not output.with_suffix(".origins.json").exists()
    assert "escaped disposable source" in output.with_suffix(".log").read_text()


@pytest.mark.parametrize(("status", "exit_code"), [("passed", 0), ("failed", 1)])
def test_mutation_cli_preserves_gate_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, exit_code: int
) -> None:
    output = tmp_path / "receipt.json"
    monkeypatch.setattr(sys, "argv", ["mutation-gate", "--output", str(output)])
    monkeypatch.setattr(gate, "qualify", lambda path: {"status": status})
    assert gate.main() == exit_code
    assert json.loads(output.read_text()) == {"status": status}
