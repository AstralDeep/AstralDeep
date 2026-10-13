"""Pins acceptance checks to existing read-only owner workflows and whole-suite routing.
Real PostgreSQL mutation cases belong to the modules gate while pure tooling keeps its coverage floor.
SDK isolation is exercised with actual Bash control and rejection inputs.
"""

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from scripts.run_backend_web_tests import group_commands, suite_commands

ROOT = Path(__file__).resolve().parents[2]


def job(text, name):
    matched = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text)
    assert matched is not None
    return matched[1]


def test_acceptance_cases_run_in_the_complete_owner_suite_groups():
    commands = suite_commands(ROOT)
    assert (ROOT / "backend", "tests", "backend-tests") in group_commands(
        commands, "tests"
    )
    assert (ROOT, "scripts/tests", "tooling") in group_commands(commands, "modules")
    for name in (
        "test_phi_clinical_acceptance.py",
        "test_reader_acceptance.py",
        "test_proposal_lifecycle_acceptance.py",
    ):
        assert (ROOT / "backend/tests" / name).is_file()
    assert (ROOT / "scripts/tests/test_acceptance_mutations_postgres.py").is_file()


def test_real_mutation_receipts_run_inside_the_isolated_database_gate():
    script = (ROOT / "scripts/backend_web_image_gate.sh").read_text()
    assert (
        'if [[ "$ASTRAL_GATE_GROUP" == modules || "$ASTRAL_GATE_GROUP" == all ]]; then'
        in script
    )
    assert (
        "/tmp/ci/bin/python /workspace/scripts/check_acceptance_mutations.py" in script
    )
    assert "--output /workspace/build/backend-web/acceptance-mutations.json" in script
    assert (
        '--env ASTRALPLANE_TEST_POSTGRES_DSN="$url" --env ASTRAL_TEST_ISOLATED=1'
        in script
    )
    assert script.index("run_backend_web_tests.py --root /workspace") < script.index(
        "/workspace/scripts/check_acceptance_mutations.py"
    )


def test_diagnostic_receipts_and_script_coverage_preserve_owner_authority():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    tooling = job(workflow, "release-tooling-tests")
    assert "timeout-minutes: 15" in tooling
    assert "contents: read" in workflow and "continue-on-error:" not in workflow
    assert (
        "HEAD_SHA: ${{ github.event.pull_request.head.sha || github.sha }}" in tooling
    )
    assert (
        "BASE_SHA: ${{ github.event.pull_request.base.sha || github.event.before || inputs.base_sha }}"
        in tooling
    )
    assert (
        'python scripts/ci_review_diagnostics.py --base "$BASE_SHA" --head "$HEAD_SHA"'
        in tooling
    )
    assert "--output build/060/review-diagnostics.json" in tooling
    assert "coverage report --fail-under=90" in tooling
    assert "--source=scripts" in tooling
    assert "scripts/tests/test_ci_review_diagnostics.py" in tooling
    assert "scripts/tests/test_acceptance_mutations.py" in tooling
    assert "scripts/tests/test_acceptance_ci_contract.py" in tooling
    assert "scripts/tests/test_prepare_plane_upgrade_baseline.py" in tooling
    assert "test_acceptance_mutations_postgres.py" not in tooling
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in tooling
    assert "if: always()" in tooling and "build/060/review-diagnostics.json" in tooling
    aggregate = job(workflow, "gates")
    assert "- backend-web" in aggregate and "- release-tooling-tests" in aggregate
    assert (
        "needs.backend-web.result" in aggregate
        and "needs.release-tooling-tests.result" in aggregate
    )


def test_embedded_plane_qualification_measures_owner_upgrade_and_integrity_tools():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    plane = job(workflow, "plane-postgres")
    assert "git fetch origin +refs/heads/main:refs/remotes/origin/main" not in plane
    assert "python -m scripts.prepare_plane_upgrade_baseline" in plane
    assert '--candidate-sha "$CANDIDATE_SHA"' in plane
    assert '--baseline-root "$RUNNER_TEMP/plane-consumer-baseline.git"' in plane
    assert '--github-env "$GITHUB_ENV"' in plane
    assert "consumer-baseline.json" in plane
    assert 'base_component="$PLANE_BASE_COMPONENT_SHA"' in plane
    assert plane.index("scripts.prepare_plane_upgrade_baseline") < plane.index("pytest -q")
    for module in (
        "scripts.qualify_main_upgrade",
        "scripts.check_acceptance_faults",
        "scripts.ci_review_diagnostics",
        "scripts.check_changed_coverage",
    ):
        assert f"--cov={module}" in plane
    assert "--fail-under=90" in plane
    assert "--cov-fail-under=88.75" in plane
    assert "timeout-minutes: 30" in plane


@pytest.mark.parametrize(
    ("dockerfile", "requirements", "exit_code"),
    [
        ("FROM python\nCOPY backend/ /app/backend/\n", "fastapi==0.1\n", 0),
        ("FROM python\nCOPY sdk/ /app/sdk/\n", "fastapi==0.1\n", 1),
        ("FROM python\nCOPY backend/ /app/backend/\n", "astral_sdk==0.1\n", 1),
        ("FROM python\nRUN pip install astral-sdk\n", "fastapi==0.1\n", 1),
    ],
    ids=["clean", "sdk-copy", "sdk-runtime-dependency", "sdk-image-distribution"],
)
def test_sdk_isolation_guard_rejects_contamination_in_actual_shell(
    tmp_path,
    dockerfile,
    requirements,
    exit_code,
):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    sdk = job(workflow, "sdk")
    block = sdk.partition(
        "      - name: Confirm the SDK never enters the product image\n"
    )[2]
    script = textwrap.dedent(
        block.partition("        run: |\n")[2].partition("      - name:")[0]
    )
    assert script.strip()
    (tmp_path / "backend").mkdir()
    (tmp_path / "Dockerfile").write_text(dockerfile)
    (tmp_path / "backend/requirements.txt").write_text(requirements)
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == exit_code, "SDK isolation guard lost a failure"
    assert ("::error::SDK isolation violation" in result.stdout) is (exit_code == 1)
