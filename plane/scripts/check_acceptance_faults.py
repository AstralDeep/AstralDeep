"""Runs bounded guard mutations against real PostgreSQL lifecycle acceptance
in isolated source copies. Only the declared behavioral assertion counts as a kill;
survivors, skips, import failures, and collection errors fail qualification.
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
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = "tests/integration/test_lifecycle_acceptance.py"


@dataclass(frozen=True)
class Fault:
    name: str
    path: str
    start: str
    end: str
    before: str
    after: str
    case: str
    failure_marker: str
    test_path: str = ACCEPTANCE


FAULTS = (
    Fault(
        "owner-predicate",
        "repositories/drafts.py",
        "    def compare_and_set_draft(",
        "    def claim_generation(",
        "WHERE id = %s AND user_id = %s AND state_revision = %s",
        "WHERE id = %s AND %s IS NOT NULL AND state_revision = %s",
        "test_owner_predicate_blocks_foreign_read_and_write",
        "DID NOT RAISE",
    ),
    Fault(
        "version-fence",
        "repositories/drafts.py",
        "    def compare_and_set_draft(",
        "    def claim_generation(",
        "WHERE id = %s AND user_id = %s AND state_revision = %s",
        "WHERE id = %s AND user_id = %s AND %s >= 0",
        "test_stale_version_cannot_overwrite_committed_state",
        "DID NOT RAISE",
    ),
    Fault(
        "generation-claim",
        "repositories/drafts.py",
        "    def claim_generation(",
        "    def get_exact_live_generation_claim(",
        "WHERE id = %s AND user_id = %s AND state_revision = %s\n"
        "              AND (generation_claim_id IS NULL\n"
        "                   OR generation_claim_expires_at <= clock_timestamp()\n"
        "                   OR generation_claim_id = %s)",
        "WHERE id = %s AND user_id = %s AND %s >= 0 AND %s IS NOT NULL",
        "test_two_claimers_commit_exactly_one_generation",
        "assert 2 == 1",
    ),
    Fault(
        "caller-rollback",
        "database/transaction.py",
        "    def _finish(",
        "\n\nclass PlaneDatabase:",
        "should_rollback = failed or self._failed",
        "should_rollback = self._failed",
        "test_repository_write_keeps_caller_rollback_and_connection_custody",
        "is None",
    ),
    Fault(
        "proposal-nul-lock",
        "repositories/knowledge.py",
        "class KnowledgeProposalRepository:",
        "    def get_for_administration(",
        "lock_key = _canonical_json(\n"
        '            ("knowledge-proposal", proposal.agent_id, proposal.tool_name), '
        '"proposal_lock"\n'
        "        )",
        'lock_key = f"knowledge-proposal\\0{proposal.agent_id}\\0{proposal.tool_name}"',
        "test_valid_proposal_persists_and_replays_exact_bytes[ascii]",
        "valid proposal rejected by a NUL advisory lock key",
        "tests/repositories/test_knowledge_proposals_postgres.py",
    ),
    Fault(
        "revocation-recheck",
        "repositories/share_grants.py",
        "    def record_open(",
        "\n\ndef _digest(",
        "AND revoked_at IS NULL",
        "AND TRUE",
        "test_share_open_rechecks_revocation_after_row_lock[revoke]",
        "is None",
    ),
)


def apply_fault(root: Path, target: Path, fault: Fault) -> str:
    shutil.copytree(
        root / "src", target / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    path = target / "src" / "astralplane" / fault.path
    text = path.read_text(encoding="utf-8")
    start = text.index(fault.start)
    end = text.index(fault.end, start)
    section = text[start:end]
    if section.count(fault.before) != 1:
        raise ValueError("declared fault no longer matches exactly one guard")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text(
        text[:start] + section.replace(fault.before, fault.after) + text[end:], encoding="utf-8"
    )
    return digest


def classify(returncode: int, report: Path, *, case: str | None, marker: str = "") -> str:
    document = ElementTree.parse(report)
    tests = document.findall(".//testcase")
    if not tests or any(
        test.find("error") is not None or test.find("skipped") is not None for test in tests
    ):
        return "invalid"
    failures = tuple(test for test in tests if test.find("failure") is not None)
    if case is None:
        return "passed" if returncode == 0 and len(tests) == 6 and not failures else "invalid"
    if len(tests) != 1 or tests[0].get("name") != case:
        return "invalid"
    if returncode == 0 and not failures:
        return "survived"
    if returncode != 1 or len(failures) != 1:
        return "invalid"
    failure = failures[0].find("failure")
    message, kind = failure.get("message", ""), failure.get("type", "")
    if kind and kind not in {"AssertionError", "Failed", "_pytest.outcomes.Failed"}:
        return "invalid"
    if not kind and not message.startswith(
        ("assert ", "AssertionError", "Failed: ", "DID NOT RAISE")
    ):
        return "invalid"
    return (
        "killed" if marker and marker in message and marker in (failure.text or "") else "invalid"
    )


def run_case(
    root: Path,
    source: Path,
    output: Path,
    *,
    case: str | None,
    marker: str = "",
    test_path: str = ACCEPTANCE,
) -> str:
    selector = test_path + (f"::{case}" if case is not None else "")
    try:
        output.unlink(missing_ok=True)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                f"pythonpath={source / 'src'}",
                f"--junitxml={output}",
                selector,
            ],
            cwd=root,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        return classify(completed.returncode, output, case=case, marker=marker)
    except (subprocess.TimeoutExpired, OSError, ElementTree.ParseError):
        return "invalid"


def check(*, root: Path = ROOT) -> dict:
    if not os.environ.get("ASTRALPLANE_TEST_POSTGRES_DSN"):
        raise ValueError("real PostgreSQL DSN is required")
    with tempfile.TemporaryDirectory(prefix="plane-acceptance-faults-") as directory:
        temporary = Path(directory)
        control = run_case(root, root, temporary / "control.xml", case=None)
        if control != "passed":
            raise ValueError("acceptance control did not pass without skips")
        results = []
        for fault in FAULTS:
            target = temporary / fault.name
            digest = apply_fault(root, target, fault)
            outcome = run_case(
                root,
                target,
                temporary / f"{fault.name}.xml",
                case=fault.case,
                marker=fault.failure_marker,
                test_path=fault.test_path,
            )
            results.append(
                {
                    "fault": fault.name,
                    "case": fault.case,
                    "status": outcome,
                    "source_sha256": digest,
                }
            )
        return {
            "contract": "astralplane.acceptance-faults/v1",
            "candidate_sha": subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                text=True,
            ).strip(),
            "status": "passed" if all(item["status"] == "killed" for item in results) else "failed",
            "control": control,
            "faults": results,
            "release_authorized": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qualify bounded PostgreSQL acceptance faults.")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = check()
    except Exception:
        report = {
            "contract": "astralplane.acceptance-faults/v1",
            "status": "failed",
            "release_authorized": False,
        }
    args.report.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
