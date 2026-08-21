"""CLI tool for the Academic Testing Suite audit trail.

Usage:
    python -m backend.qual_audit.cli run [--categories ...]
    python -m backend.qual_audit.cli status [run_id]
    python -m backend.qual_audit.cli review <run_id> [--category ...]
    python -m backend.qual_audit.cli verify <case_id> --action ... [--rationale ...]
    python -m backend.qual_audit.cli export <run_id> --output <dir>
    python -m backend.qual_audit.cli rerun <case_id>
"""

import atexit
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

# Ensure backend is importable
_backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from qual_audit.database import AuditDatabase  # noqa: E402
from qual_audit.models import AuditAction, AuditEntry, VerificationStatus  # noqa: E402
from qual_audit.runner import run_backend_tests, run_frontend_tests  # noqa: E402
from orchestrator.plane_composition import compose_plane_from_environment  # noqa: E402

_composition = None
_database = None


def _close_plane_runtime() -> None:
    global _composition
    if _composition is not None:
        _composition.close()
        _composition = None


atexit.register(_close_plane_runtime)

def _db() -> AuditDatabase:
    global _composition, _database
    if _database is None:
        manifest = Path(_backend_dir).parent / "config" / "astral-composition.json"
        _composition = compose_plane_from_environment(manifest)
        _database = AuditDatabase(
            plane_runtime=_composition.runtime,
            plane_repositories=_composition.repositories,
            owner_id=os.getenv("QUAL_AUDIT_OWNER_ID"),
        )
    return _database


@click.group()
def cli():
    """Academic Testing Suite — audit trail CLI."""
    pass


@cli.command()
@click.option(
    "--categories", "-c", default=None,
    help="Comma-separated categories: tool_poisoning,prompt_injection,rote_adaptation,permission_delegation,transport_comparison,frontend",
)
@click.option(
    "--export", "-e", "do_export", is_flag=True, default=False,
    help="Auto-generate LaTeX table files after tests complete (skips verification check).",
)
@click.option(
    "--output", "-o", default=None,
    help="Output directory for .tex files (used with --export).",
)
def run(categories, do_export, output):
    """Execute test suites and record results."""
    db = _db()
    cats = [c.strip() for c in categories.split(",")] if categories else None
    include_frontend = cats is None or "frontend" in (cats or [])
    backend_cats = [c for c in cats if c != "frontend"] if cats else None
    click.echo("Starting test run...")
    run_id = run_backend_tests(db, categories=backend_cats)

    if include_frontend:
        click.echo("Running frontend tests (Vitest)...")
        frontend_ids = run_frontend_tests(db, run_id)
        if frontend_ids:
            click.echo(f"  Frontend: {len(frontend_ids)} test(s) recorded")
        else:
            click.echo("  Frontend: no results (Vitest not available or no tests found)")

    run_obj = db.get_run(run_id)
    cases = db.get_cases_for_run(run_id)
    passed = sum(1 for c in cases if c.outcome.value == "passed")
    failed = sum(1 for c in cases if c.outcome.value == "failed")
    click.echo(f"\nRun ID: {run_id}")
    click.echo(f"Status: {run_obj.status.value if run_obj else 'unknown'}")
    click.echo(f"Total: {len(cases)} | Passed: {passed} | Failed: {failed}")

    if do_export:
        from qual_audit.latex_export import _DEFAULT_OUTPUT, generate_all_artifacts

        out_dir = output or _DEFAULT_OUTPUT
        os.makedirs(out_dir, exist_ok=True)
        artifacts = generate_all_artifacts(db, run_id, out_dir)
        click.echo(f"\nExported {len(artifacts)} LaTeX file(s) to {out_dir}")
        for art in artifacts:
            click.echo(f"  {art.filename}")


@cli.command()
@click.argument("run_id", required=False)
def status(run_id):
    """Show run status and verification progress."""
    db = _db()
    run_obj = db.get_run(run_id) if run_id else db.get_latest_run()
    if not run_obj:
        click.echo("No test runs found.")
        return

    click.echo(f"Run: {run_obj.id}")
    click.echo(f"Status: {run_obj.status.value}")
    click.echo(f"Started: {run_obj.started_at.isoformat()}")
    if run_obj.finished_at:
        click.echo(f"Finished: {run_obj.finished_at.isoformat()}")

    cases = db.get_cases_for_run(run_obj.id)

    # Group by suite
    suites: dict = {}
    for c in cases:
        suites.setdefault(c.suite, []).append(c)

    click.echo(f"\n{'Category':<30} {'Total':>6} {'Pass':>6} {'Fail':>6} {'Verified':>10}")
    click.echo("-" * 70)
    for suite, suite_cases in sorted(suites.items()):
        total = len(suite_cases)
        passed = sum(1 for c in suite_cases if c.outcome.value == "passed")
        failed = sum(1 for c in suite_cases if c.outcome.value in ("failed", "error"))
        verified = sum(1 for c in suite_cases if c.verification_status == VerificationStatus.VERIFIED)
        pct = f"{verified}/{total}" if total > 0 else "0/0"
        click.echo(f"{suite:<30} {total:>6} {passed:>6} {failed:>6} {pct:>10}")


@cli.command()
@click.argument("run_id")
@click.option("--category", "-c", default=None, help="Filter to a single category")
def review(run_id, category):
    """Review test evidence for verification."""
    db = _db()
    cases = db.get_cases_for_run(run_id, suite=category)
    if not cases:
        click.echo("No test cases found.")
        return

    for case in cases:
        status_marker = {
            "pending": "[ ]",
            "verified": "[✓]",
            "disputed": "[✗]",
            "needs_rerun": "[↻]",
        }.get(case.verification_status.value, "[ ]")

        click.echo(f"\n{status_marker} {case.test_name}")
        click.echo(f"    Suite: {case.suite} | Outcome: {case.outcome.value} | {case.duration_ms:.1f}ms")
        click.echo(f"    ID: {case.id}")
        click.echo(f"    Status: {case.verification_status.value}")

        if case.qualitative:
            click.echo(f"    Details: {case.qualitative[:200]}")

        evidence = db.get_evidence_for_case(case.id)
        if evidence:
            click.echo(f"    Evidence: {len(evidence)} item(s)")
            for ev in evidence:
                click.echo(f"      - {ev.evidence_type} (SHA: {ev.sha256[:12]}...)")


@cli.command()
@click.argument("case_id")
@click.option("--action", "-a", required=True, type=click.Choice(["verified", "disputed", "needs_rerun"]))
@click.option("--rationale", "-r", default="", help="Rationale (required for disputed)")
@click.option("--reviewer", default=None, help="Reviewer identifier")
def verify(case_id, action, rationale, reviewer):
    """Record human verification for a test case."""
    db = _db()
    case = db.get_case(case_id)
    if not case:
        click.echo(f"Case not found: {case_id}")
        sys.exit(1)

    if action == "disputed" and not rationale:
        click.echo("Rationale is required for disputed results.")
        sys.exit(1)

    if not reviewer:
        reviewer = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))

    entry = AuditEntry(
        case_id=case_id,
        action=AuditAction(action),
        reviewer=reviewer,
        rationale=rationale,
        timestamp=datetime.now(timezone.utc),
    )
    persisted = db.review_case(entry)
    if persisted is None:
        click.echo(f"Case not found: {case_id}")
        sys.exit(1)

    click.echo(f"Audit entry: {persisted.id}")
    click.echo(f"Action: {action} | Reviewer: {reviewer}")
    click.echo(f"Case {case_id} → {action}")


@cli.command()
@click.argument("run_id")
@click.option(
    "--output", "-o", default=None,
    help="Output directory for .tex files (default: Qualifying_Exam/sources/tables/)",
)
def export(run_id, output):
    """Generate LaTeX files from verified results."""
    from qual_audit.latex_export import _DEFAULT_OUTPUT, generate_all_artifacts

    if output is None:
        output = _DEFAULT_OUTPUT

    db = _db()
    cases = db.get_cases_for_run(run_id)
    if not cases:
        click.echo("No test cases found for this run.")
        sys.exit(1)

    # Check all cases are verified
    unverified = [c for c in cases if c.verification_status == VerificationStatus.PENDING]
    if unverified:
        click.echo(f"ERROR: {len(unverified)} case(s) are still pending verification.")
        click.echo("All results must be verified before export.")
        for c in unverified[:5]:
            click.echo(f"  - {c.test_name} ({c.id})")
        sys.exit(2)

    # Verify audit chain integrity
    all_audits = db.get_all_audits_for_run(run_id)
    if all_audits and not db.verify_audit_chain_for_run(
        run_id,
        require_genesis=False,
    ):
        click.echo("ERROR: Audit trail hash chain integrity check FAILED.")
        click.echo("The audit trail may have been tampered with.")
        sys.exit(3)

    # Generate LaTeX
    os.makedirs(output, exist_ok=True)
    artifacts = generate_all_artifacts(db, run_id, output)
    for art in artifacts:
        click.echo(f"  Generated: {art.filename}")
    click.echo(f"\n{len(artifacts)} file(s) written to {output}")


@cli.command()
@click.argument("case_id")
def rerun(case_id):
    """Re-execute a specific test case."""
    db = _db()
    case = db.get_case(case_id)
    if not case:
        click.echo(f"Case not found: {case_id}")
        sys.exit(1)

    click.echo(f"Re-running: {case.test_name}")
    # Use pytest to run just this one test
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-xvs", "-k", case.test_name.split("::")[-1]],
        capture_output=True, text=True,
    )
    click.echo(result.stdout)
    if result.returncode != 0:
        click.echo(result.stderr)


def main():
    cli()


if __name__ == "__main__":
    main()
