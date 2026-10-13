"""Checks review diagnostics against removed tests, new skips and policy edits.
An isolated Git history proves receipts bind the actual immutable comparison range.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci_review_diagnostics import collect, inspect_change, main


def test_removed_assertions_and_added_skips_are_review_findings():
    before = "def test_denial():\n    assert denied()\n"
    after = 'import pytest\n@pytest.mark.skip(reason="x")\ndef test_allow():\n    try:\n        pass\n    except Exception:\n        pass\n'
    findings = inspect_change("tests/test_authority.py", before, after)
    assert {row["kind"] for row in findings} == {
        "removed-test",
        "added-skip-or-xfail",
        "added-swallowed-test-error",
    }
    assert findings[0]["tests"] == ["test_denial"]
    assert inspect_change("src/runtime.py", before, after) == []
    assert inspect_change("tests/test_authority.py", before, before) == []
    assert inspect_change(".github/workflows/ci.yml", "", "") == [
        {"path": ".github/workflows/ci.yml", "kind": "qualification-policy-change"}
    ]


@pytest.mark.parametrize("path", [".specify/memory/constitution.md", "uv.lock"])
def test_governance_and_lock_changes_require_review(path):
    assert inspect_change(path, "", "") == [
        {
            "path": path,
            "kind": "qualification-policy-change",
        }
    ]
    for path in (".specify/memory/constitution.md", "uv.lock"):
        assert inspect_change(path, "", "") == [
            {"path": path, "kind": "qualification-policy-change"}
        ]


def test_deletion_and_cli_produce_immutable_diagnostic_receipts(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.email", "synthetic@example.invalid")
    git("config", "user.name", "Synthetic")
    path = tmp_path / "test_security.py"
    path.write_text("def test_signed():\n    assert True\n")
    git("add", ".")
    git("commit", "-qm", "baseline")
    base = git("rev-parse", "HEAD")
    path.unlink()
    git("add", "-u")
    git("commit", "-qm", "candidate")
    head = git("rev-parse", "HEAD")
    receipt = collect(tmp_path, base, head)
    assert receipt == {
        "base": base,
        "head": head,
        "review_required": True,
        "findings": [
            {
                "path": "test_security.py",
                "kind": "removed-test",
                "tests": ["test_signed"],
            }
        ],
        "diagnostic_only": True,
    }
    with pytest.raises(ValueError):
        collect(tmp_path, "main", head)
    with pytest.raises(subprocess.CalledProcessError):
        collect(tmp_path, "f" * 40, head)
    output = tmp_path / "nested" / "receipt.json"
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "ci_review_diagnostics.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--root",
            str(tmp_path),
            "--base",
            base,
            "--head",
            head,
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    main(
        [
            "--root",
            str(tmp_path),
            "--base",
            base,
            "--head",
            head,
            "--output",
            str(output),
        ]
    )
    assert json.loads(output.read_text()) == receipt
    main(
        [
            "--root",
            str(tmp_path),
            "--base",
            head,
            "--head",
            head,
            "--output",
            str(output),
        ]
    )
    assert "::warning::" in result.stdout
    clean = subprocess.run(
        [
            sys.executable,
            str(script),
            "--root",
            str(tmp_path),
            "--base",
            head,
            "--head",
            head,
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "::warning::" not in clean.stdout
