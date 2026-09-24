"""Invokes pytest and Vitest for the qualification test suites and parses their JSON
reports into qual_audit/database.py records via evidence.py's hash chain; drives
backend and frontend runs for qual_audit/cli.py.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, Optional

from qual_audit.database import AuditDatabase
from qual_audit.evidence import compute_evidence_hash, create_evidence
from qual_audit.models import Outcome, RunStatus, TestCaseResult, TestRun

_OUTCOME_MAP = {
    "passed": Outcome.PASSED,
    "failed": Outcome.FAILED,
    "error": Outcome.ERROR,
    "skipped": Outcome.SKIPPED,
}

_SUITE_MAP = {
    "test_tool_poisoning": "tool_poisoning",
    "test_prompt_injection": "prompt_injection",
    "test_rote_adaptation": "rote_adaptation",
    "test_permission_delegation": "permission_delegation",
    "test_transport_comparison": "transport_comparison",
    "test_cost_overhead": "cost_overhead",
    "test_parallel_dispatch": "parallel_dispatch",
}


def _capture_system_state() -> Dict:
    state: Dict = {"captured_at": datetime.now(timezone.utc).isoformat()}

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            state["git_commit"] = result.stdout.strip()
    except Exception:
        state["git_commit"] = "unknown"

    state["python_version"] = sys.version

    state["mock_auth"] = os.environ.get("USE_MOCK_AUTH", "false")

    return state


def _extract_suite(nodeid: str) -> str:
    for key, name in _SUITE_MAP.items():
        if key in nodeid:
            return name
    return "unknown"


def _extract_qualitative(test_result: Dict) -> str:
    call = test_result.get("call", {})
    if call.get("longrepr"):
        return str(call["longrepr"])[:500]
    return test_result.get("outcome", "")


def run_backend_tests(
    db: AuditDatabase,
    categories: Optional[List[str]] = None,
    suites_dir: Optional[str] = None,
) -> str:
    if suites_dir is None:
        suites_dir = os.path.join(os.path.dirname(__file__), "suites")

    run = TestRun(
        system_state=_capture_system_state(),
        categories=categories or list(_SUITE_MAP.values()),
    )
    db.insert_run(run)

    json_file = tempfile.mktemp(suffix=".json")
    args = [
        sys.executable, "-m", "pytest",
        suites_dir,
        "--json-report", f"--json-report-file={json_file}",
        "-v", "--tb=short",
    ]

    if categories:
        keyword_expr = " or ".join(
            k for k, v in _SUITE_MAP.items() if v in categories
        )
        if keyword_expr:
            args.extend(["-k", keyword_expr])

    env = {**os.environ, "USE_MOCK_AUTH": "true"}
    try:
        subprocess.run(args, env=env, timeout=1800)
    except subprocess.TimeoutExpired:
        db.finish_run(run.id, RunStatus.FAILED)
        return run.id

    if not os.path.exists(json_file):
        db.finish_run(run.id, RunStatus.FAILED)
        return run.id

    with open(json_file, "r", encoding="utf-8") as f:
        report = json.load(f)

    os.unlink(json_file)

    tests = report.get("tests", [])
    for test in tests:
        nodeid = test.get("nodeid", "")
        outcome_str = test.get("outcome", "error")
        duration = test.get("call", {}).get("duration", 0) * 1000

        case = TestCaseResult(
            run_id=run.id,
            suite=_extract_suite(nodeid),
            test_name=nodeid,
            outcome=_OUTCOME_MAP.get(outcome_str, Outcome.ERROR),
            duration_ms=duration,
            qualitative=_extract_qualitative(test),
        )

        evidence_items = []
        ev = create_evidence(
            case_id=case.id,
            evidence_type="pytest_result",
            data={
                "nodeid": nodeid,
                "outcome": outcome_str,
                "duration_s": test.get("call", {}).get("duration", 0),
                "setup": test.get("setup", {}),
                "call": test.get("call", {}),
                "teardown": test.get("teardown", {}),
            },
        )
        evidence_items.append(ev)

        case.evidence_hash = compute_evidence_hash(evidence_items)
        db.insert_case(case)
        for ev in evidence_items:
            db.insert_evidence(ev)

    has_failures = any(
        t.get("outcome") in ("failed", "error") for t in tests
    )
    db.finish_run(run.id, RunStatus.COMPLETED if not has_failures else RunStatus.COMPLETED)

    return run.id


def run_frontend_tests(
    db: AuditDatabase,
    run_id: str,
    frontend_dir: Optional[str] = None,
) -> List[str]:
    if frontend_dir is None:
        frontend_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
        )

    json_file = os.path.join(frontend_dir, "_vitest_report.json")
    npx = "npx.cmd" if sys.platform == "win32" else "npx"
    args = [npx, "vitest", "run", "--reporter=json", f"--outputFile={json_file}"]

    env = {**os.environ, "USE_MOCK_AUTH": "true"}
    try:
        subprocess.run(args, cwd=frontend_dir, env=env, timeout=300)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if not os.path.exists(json_file):
        return []

    with open(json_file, "r", encoding="utf-8") as f:
        report = json.load(f)

    os.unlink(json_file)

    case_ids: List[str] = []
    _vitest_outcome = {"passed": Outcome.PASSED, "failed": Outcome.FAILED}

    for suite in report.get("testResults", []):
        for test in suite.get("assertionResults", []):
            status = test.get("status", "failed")
            duration = test.get("duration", 0)
            full_name = " > ".join(test.get("ancestorTitles", [])) + " > " + test.get("title", "")

            case = TestCaseResult(
                run_id=run_id,
                suite="frontend_rendering",
                test_name=full_name,
                outcome=_vitest_outcome.get(status, Outcome.FAILED),
                duration_ms=float(duration),
                qualitative="\n".join(test.get("failureMessages", [])) or status,
            )

            evidence_items = []
            ev = create_evidence(
                case_id=case.id,
                evidence_type="vitest_result",
                data={
                    "test_name": full_name,
                    "status": status,
                    "duration_ms": duration,
                    "failure_messages": test.get("failureMessages", []),
                },
            )
            evidence_items.append(ev)

            case.evidence_hash = compute_evidence_hash(evidence_items)
            db.insert_case(case)
            for ev in evidence_items:
                db.insert_evidence(ev)
            case_ids.append(case.id)

    return case_ids
