"""Run selected privacy, reader-bound and artifact guards against focused Deep acceptance tests.
Disposable source copies and JUnit receipts distinguish assertion kills from broken test execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYTEST_BOOTSTRAP = """
import json, pathlib, sys, shared, feedback, pytest
source = pathlib.Path(sys.argv[1]).resolve()
receipt = pathlib.Path(sys.argv[2])
selected = ('shared', 'shared.phi_redactor', 'shared.process_supervision',
            'feedback', 'feedback.proposals')
class SourceAuthority:
    collected = 0
    def pytest_collection_finish(self, session):
        self.collected = len(session.items)
authority = SourceAuthority()
def origins():
    result = {}
    for name in selected:
        module = sys.modules.get(name)
        if module is None:
            continue
        path = pathlib.Path(module.__file__).resolve()
        assert path.is_relative_to(source), f'{name} escaped disposable source: {path}'
        result[name] = str(path)
    return result
origins()
status = pytest.main(sys.argv[3:], plugins=[authority])
receipt.write_text(json.dumps({'modules': origins(), 'collected': authority.collected,
                              'sys_path': sys.path}) + '\\n', encoding='utf-8')
raise SystemExit(status)
"""


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    before: str
    after: str
    tests: tuple[str, ...]


MUTATIONS = (
    Mutation(
        "phi-value-masking-removed",
        "shared/phi_redactor.py",
        "    for pattern, replacement in PHI_VALUE_PATTERNS",
        "    for pattern, replacement in ()",
        (
            "backend/tests/test_phi_clinical_acceptance.py::test_synthetic_phi_controls_are_masked[synthetic-ssn]",
        ),
    ),
    Mutation(
        "clinical-fields-overmasked",
        "shared/phi_redactor.py",
        "            if _matches_phi_field_key(k):\n",
        '            if _matches_phi_field_key(k) or "secret" in k.lower() or "token" in k.lower():\n',
        (
            "backend/tests/test_phi_clinical_acceptance.py::test_benign_clinical_and_accounting_content_preserves_bytes[tracheal-secretions]",
            "backend/tests/test_phi_clinical_acceptance.py::test_benign_clinical_and_accounting_content_preserves_bytes[prompt-token-count]",
        ),
    ),
    Mutation(
        "private-output-guard-removed",
        "shared/process_supervision.py",
        "                if self._private_output:\n                    with self._condition:\n",
        "                if False:\n                    with self._condition:\n",
        (
            "backend/tests/test_reader_acceptance.py::test_private_reader_never_forwards_or_retains_diagnostics[False]",
        ),
    ),
    Mutation(
        "reader-ring-bound-removed",
        "shared/process_supervision.py",
        "            > self._limits.ring_capacity_bytes_per_stream\n",
        "            > self._limits.ring_capacity_bytes_per_stream * 1000\n",
        (
            "backend/tests/test_reader_acceptance.py::test_reader_ring_retains_bounded_recent_complete_lines",
        ),
    ),
    Mutation(
        "proposal-artifact-digest-fence-removed",
        "feedback/proposals.py",
        "    if current_sha != existing.artifact_sha_at_gen:\n",
        "    if False:\n",
        (
            "backend/tests/test_proposal_lifecycle_acceptance.py::test_proposal_rejects_artifact_tampering_before_review",
        ),
    ),
    Mutation(
        "reader-close-state-fabricated",
        "shared/process_supervision.py",
        "                self._pipe_closed = self._pipe.closed is True\n",
        "                self._pipe_closed = True\n",
        (
            "backend/tests/test_reader_acceptance.py::"
            "test_close_failure_settles_reader_with_truthful_bounded_metadata[False-partial-True]",
        ),
    ),
    Mutation(
        "private-read-exception-filter-restored",
        "shared/process_supervision.py",
        "        except Exception as exc:\n            with self._condition:\n",
        "        except (OSError, ValueError) as exc:\n            with self._condition:\n",
        (
            "backend/tests/test_reader_acceptance.py::"
            "test_unknown_read_errors_settle_without_private_thread_diagnostics[RuntimeError-True]",
        ),
    ),
)


def classify_result(
    returncode: int, report: Path, expected: tuple[str, ...], *, mutated: bool
) -> tuple[str, dict[str, Any]]:
    try:
        cases = ET.parse(report).findall(".//testcase")
    except (OSError, ET.ParseError) as exc:
        return "invalid", {"reason": f"missing or invalid JUnit receipt: {exc}"}
    names = [case.attrib.get("name", "") for case in cases]
    failures = [
        {"test": case.attrib.get("name"), "message": failure.attrib.get("message", "")}
        for case in cases
        for failure in case.findall("failure")
    ]
    result: dict[str, Any] = {
        "returncode": returncode,
        "tests": names,
        "failures": failures,
    }
    if (
        set(names) != set(expected)
        or len(names) != len(expected)
        or any(
            case.find("error") is not None or case.find("skipped") is not None
            for case in cases
        )
    ):
        return "invalid", {
            **result,
            "reason": "missing, extra, skipped, or errored focused tests",
        }
    if not mutated:
        return ("passed" if returncode == 0 and not failures else "invalid"), result
    if returncode == 0 and not failures:
        return "survived", result
    assertion_failures = all(
        failure["message"].startswith(("AssertionError", "Failed: DID NOT RAISE"))
        for failure in failures
    )
    if returncode == 1 and failures and assertion_failures:
        return "killed", result
    return "invalid", {
        **result,
        "reason": "execution failures do not demonstrate guard detection",
    }


def run_focused(
    source: Path,
    output: Path,
    tests: tuple[str, ...],
    *,
    mutated: bool,
    collect_only: bool = False,
) -> tuple[str, dict[str, Any]]:
    for suffix in (".xml", ".origins.json", ".log"):
        output.with_suffix(suffix).unlink(missing_ok=True)
    env = dict(os.environ)
    env["ASTRAL_ENV"] = "development"
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        (str(source), str(ROOT / "backend"), str(ROOT), env.get("PYTHONPATH", ""))
    )
    env.pop("PYTEST_ADDOPTS", None)
    command = [
        sys.executable,
        "-c",
        PYTEST_BOOTSTRAP,
        str(source),
        str(output.with_suffix(".origins.json")),
        "-c",
        str(ROOT / "backend/pytest.ini"),
        "-o",
        "addopts=",
        "--strict-config",
        "--strict-markers",
        "-p",
        "no:cacheprovider",
        "-q",
        "--tb=short",
        f"--junitxml={output.with_suffix('.xml')}",
        *(("--collect-only",) if collect_only else ()),
        *(str(ROOT / node) for node in tests),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "invalid", {
            "reason": "focused execution exceeded its fixed execution budget"
        }
    output.with_suffix(".log").write_text(
        completed.stdout + completed.stderr, encoding="utf-8"
    )
    try:
        origins = json.loads(output.with_suffix(".origins.json").read_text())
    except (OSError, ValueError):
        return "invalid", {"reason": "missing disposable-source authority receipt"}
    if collect_only:
        return (
            "collected" if completed.returncode == 0 else "invalid",
            {"returncode": completed.returncode, "source_origins": origins},
        )
    expected = tuple(node.rsplit("::", 1)[1] for node in tests)
    status, details = classify_result(
        completed.returncode, output.with_suffix(".xml"), expected, mutated=mutated
    )
    return status, {**details, "source_origins": origins}


def qualify(output: Path) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = output.parent / f"{output.stem}-details"
    evidence.mkdir(exist_ok=True)
    tests = tuple(sorted({test for mutation in MUTATIONS for test in mutation.tests}))
    result: dict[str, Any] = {
        "schema": "astraldeep.acceptance-mutations/v1",
        "tests_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in sorted({node.split("::")[0] for node in tests})
        },
        "fixture_sha256": hashlib.sha256(
            (ROOT / "backend/tests/fixtures/phi-clinical-controls.json").read_bytes()
        ).hexdigest(),
        "mutations": [],
    }
    with tempfile.TemporaryDirectory(prefix="deep-acceptance-") as temporary:
        workspace = Path(temporary)
        source = workspace / "baseline"
        source.mkdir()
        for package in ("shared", "feedback"):
            shutil.copytree(
                ROOT / "backend" / package,
                source / package,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        status, baseline = run_focused(
            source, evidence / "baseline", tests, mutated=False
        )
        result["baseline"] = {"status": status, **baseline}
        if status != "passed":
            result["status"] = "failed"
            return result
        for mutation in MUTATIONS:
            copied = workspace / mutation.name
            shutil.copytree(source, copied)
            path = copied / mutation.path
            original = path.read_text(encoding="utf-8")
            record: dict[str, Any] = {
                "name": mutation.name,
                "path": f"backend/{mutation.path}",
                "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
            }
            if original.count(mutation.before) != 1:
                record.update(status="invalid", reason="guard must match exactly once")
            else:
                changed = original.replace(mutation.before, mutation.after, 1)
                path.write_text(changed, encoding="utf-8")
                status, details = run_focused(
                    copied, evidence / mutation.name, mutation.tests, mutated=True
                )
                record.update(
                    status=status,
                    mutated_sha256=hashlib.sha256(changed.encode()).hexdigest(),
                    **details,
                )
            result["mutations"].append(record)
    result["status"] = (
        "passed"
        if all(record["status"] == "killed" for record in result["mutations"])
        else "failed"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = qualify(arguments.output)
    arguments.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
