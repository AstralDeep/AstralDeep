"""Offline evidence for the Fence egress pilot: the reviewed declaration stays exact,
candidate workflow text is accepted only in the reviewed shape, the generated patch is
deterministic and preserves the existing workflow contract suites, and the block-mode
decision fixtures pin success, edge, denial, failure, and recovery outcomes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.ci_fence_egress_pilot import (
    ACTION_PIN,
    ALLOWED_ENDPOINTS,
    DIAGNOSTIC_DENIED_PROBES,
    DIAGNOSTIC_POSITIVE_PROBES,
    FENCE_ACTION,
    JOB_PERMISSIONS,
    LOCK_INSTALL,
    REVIEWED_BUNDLE_SHA,
    REVIEWED_VERSION,
    RULE_ALLOW,
    RULE_BYPASS_ADDRESS,
    RULE_BYPASS_DNS,
    RULE_DESTINATION,
    RULE_FAIL_CLOSED,
    RULE_TRANSPORT,
    SOURCE_MAIN_SHA,
    STATE_ABSENT,
    STATE_CONFORMING,
    STATE_NONCONFORMING,
    ConnectionAttempt,
    Decision,
    PilotPatchError,
    decide_egress,
    evaluate_workflow,
    main,
    pilot_patch,
    render_pilot_steps,
    unified_patch,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE = REPO_ROOT / "scripts" / "ci_fence_egress_pilot.py"


def _checkout_step() -> str:
    return "      - uses: actions/checkout@" + "a" * 40 + " # v5.0.1\n"


def _lint_job(*, with_fence: bool = False) -> str:
    steps = render_pilot_steps() if with_fence else ""
    return (
        "  lint:\n"
        "    runs-on: ubuntu-24.04\n"
        "    timeout-minutes: 10\n"
        + (f"    permissions:\n      {JOB_PERMISSIONS}\n" if with_fence else "")
        + "    steps:\n"
        + steps
        + _checkout_step()
        + "        with:\n"
        "          ref: main\n"
        "      - uses: actions/setup-python@" + "b" * 40 + " # v6.3.0\n"
        "        with:\n"
        '          python-version: "3.11"\n'
        "          cache: pip\n"
        "          cache-dependency-path: tooling/python-ci/requirements.lock.txt\n"
        "      - name: Install hash-locked CI-only Python tooling\n"
        f"        run: {LOCK_INSTALL}\n"
        "      - name: Lint Deep-owned Python\n"
        "        run: ruff check .\n"
    )


def _workflow(lint_job: str, *, with_other_job: bool = True) -> str:
    other = (
        "  other:\n"
        "    runs-on: ubuntu-24.04\n"
        "    timeout-minutes: 5\n"
        "    steps:\n"
        "      - run: echo hi\n"
        if with_other_job
        else ""
    )
    return "name: CI\non: [push]\njobs:\n" + lint_job + other


def _nonconforming_job(mutation: str) -> str:
    job = _lint_job(with_fence=True)
    replacements = {
        "wrong_sha": (
            ACTION_PIN,
            FENCE_ACTION + "@" + "f" * 40 + " # " + REVIEWED_VERSION,
            1,
        ),
        "audit_mode": (
            "          mode: block\n",
            "          mode: block\n          mode: audit\n",
            1,
        ),
        "unsafe_preserve": (
            f"    permissions:\n      {JOB_PERMISSIONS}\n",
            f"    permissions:\n      {JOB_PERMISSIONS}\n"
            "          unsafe_preserve: true\n",
            1,
        ),
        "missing_allowlist_entry": ("            pypi.org:443\n", "", 1),
        "extra_allowlist_entry": (
            "            files.pythonhosted.org:443\n",
            "            files.pythonhosted.org:443\n            example.com:443\n",
            1,
        ),
        "no_permissions": (f"    permissions:\n      {JOB_PERMISSIONS}\n", "", 1),
        "no_lock_install": (LOCK_INSTALL, "pip install pytest", 1),
        "no_cache_pin": (
            "          cache-dependency-path: tooling/python-ci/requirements.lock.txt\n",
            "",
            1,
        ),
        "no_ruff": ("        run: ruff check .\n", "        run: echo linted\n", 1),
        "overtime": ("    timeout-minutes: 10\n", "    timeout-minutes: 45\n", 1),
        "secrets": (
            "        run: ruff check .\n",
            "        run: ruff check . ${{ secrets.X }}\n",
            1,
        ),
        "docker": (
            "        run: ruff check .\n",
            "        run: ruff check . && docker build .\n",
            1,
        ),
        "no_error_marker": ("::error::Fence pilot diagnostic:", "::error::other:", -1),
        "no_block_mode": ("          mode: block\n", "", 1),
        "missing_probe": ("          probe https://github.com/\n", "", 1),
        "fence_not_first": (
            render_pilot_steps() + _checkout_step(),
            _checkout_step() + render_pilot_steps(),
            1,
        ),
    }
    old, new, count = replacements[mutation]
    assert old in job, mutation
    return job.replace(old, new) if count < 0 else job.replace(old, new, count)


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("wrong_sha", "reviewed full-SHA pin"),
        ("audit_mode", "audit mode enforcement"),
        ("unsafe_preserve", "unsafe_preserve is forbidden"),
        ("missing_allowlist_entry", "endpoint set exactly"),
        ("extra_allowlist_entry", "endpoint set exactly"),
        ("no_permissions", "minimal permissions"),
        ("no_lock_install", "hash-locked tooling install"),
        ("no_cache_pin", "tooling cache pin"),
        ("no_ruff", "lint step"),
        ("overtime", "timeout-minutes"),
        ("secrets", "secrets, docker, or sudo"),
        ("docker", "secrets, docker, or sudo"),
        ("no_error_marker", "fail visibly"),
        ("no_block_mode", "explicit block mode"),
        ("missing_probe", "diagnostic probe missing"),
        ("fence_not_first", "first step"),
    ],
)
def test_nonconforming_pilot_variants_are_rejected(mutation, expected_reason):
    evaluation = evaluate_workflow(_workflow(_nonconforming_job(mutation)))
    assert evaluation.state == STATE_NONCONFORMING
    assert any(expected_reason in reason for reason in evaluation.reasons)


def test_absent_pilot_is_reported_and_patched_workflow_conforms():
    workflow = _workflow(_lint_job())
    assert evaluate_workflow(workflow).state == STATE_ABSENT
    patched = pilot_patch(workflow)
    evaluation = evaluate_workflow(patched)
    assert evaluation.state == STATE_CONFORMING, evaluation.reasons
    assert evaluate_workflow(patched).reasons == ()
    assert "      - uses: actions/checkout@" in patched


def test_duplicated_fence_action_is_rejected_without_further_evaluation():
    duplicated = _lint_job(with_fence=True).replace(
        _checkout_step(), render_pilot_steps() + _checkout_step(), 1
    )
    evaluation = evaluate_workflow(_workflow(duplicated))
    assert evaluation.state == STATE_NONCONFORMING
    assert evaluation.reasons == ("fence action must appear exactly once",)


def test_missing_lint_job_and_missing_checkout_fail_closed():
    assert (
        evaluate_workflow("name: CI\non: [push]\njobs:\n").state == STATE_NONCONFORMING
    )
    orphan = _workflow(
        "  lint:\n    runs-on: ubuntu-24.04\n    timeout-minutes: 10\n    steps:\n"
        "      - run: echo hi\n"
    )
    assert evaluate_workflow(orphan).state == STATE_ABSENT
    with pytest.raises(PilotPatchError, match="no checkout step"):
        pilot_patch(orphan)


def test_patch_refuses_nonconforming_input_and_needs_timeout_anchor():
    with pytest.raises(PilotPatchError, match="un-piloted workflow"):
        pilot_patch(_workflow(_nonconforming_job("audit_mode")))
    anchorless = _workflow(_lint_job()).replace(
        "    timeout-minutes: 10\n", "    timeout-minutes: 15\n", 1
    )
    with pytest.raises(PilotPatchError, match="timeout anchor"):
        pilot_patch(anchorless)


def test_patch_is_deterministic_idempotent_and_scoped_to_the_lint_job():
    workflow = _workflow(_lint_job())
    first = pilot_patch(workflow)
    assert first == pilot_patch(workflow)
    assert pilot_patch(first) == first
    assert unified_patch(first) == ""
    other_before = _workflow(_lint_job()).partition("  other:")[2]
    assert first.partition("  other:")[2] == other_before
    diff = unified_patch(workflow)
    assert diff.startswith("--- .github/workflows/ci.yml")
    assert (
        "+      - name: Install the reviewed Fence egress firewall in block mode"
        in diff
    )


def test_diagnostic_steps_are_bounded_fail_visible_and_ordered():
    patched = pilot_patch(_workflow(_lint_job()))
    fence = patched.index(FENCE_ACTION)
    diagnostic = patched.index(
        "Prove allowlisted egress and deny excluded and bypassed destinations"
    )
    checkout = patched.index("- uses: actions/checkout@")
    assert fence < diagnostic < checkout
    for probe in DIAGNOSTIC_POSITIVE_PROBES + DIAGNOSTIC_DENIED_PROBES:
        assert probe in patched
    assert patched.count("--max-time 8") == 1
    assert patched.count("::error::Fence pilot diagnostic:") == len(
        DIAGNOSTIC_DENIED_PROBES
    )


def test_declaration_pins_reviewed_bundle_distinct_from_source_main():
    assert len(REVIEWED_BUNDLE_SHA) == 40 and len(SOURCE_MAIN_SHA) == 40
    assert REVIEWED_BUNDLE_SHA != SOURCE_MAIN_SHA
    assert ACTION_PIN == f"{FENCE_ACTION}@{REVIEWED_BUNDLE_SHA} # {REVIEWED_VERSION}"
    steps = render_pilot_steps()
    assert "          mode: block\n" in steps
    assert "unsafe_preserve" not in steps
    assert "mode: audit" not in steps
    assert f"    permissions:\n      {JOB_PERMISSIONS}\n" not in steps


def test_allowlist_is_exact_ordered_and_purposed():
    hosts = [host for host, _, _ in ALLOWED_ENDPOINTS]
    assert hosts == [
        "github.com",
        "codeload.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "pypi.org",
        "files.pythonhosted.org",
    ]
    assert all(port == 443 for _, port, _ in ALLOWED_ENDPOINTS)
    assert all(purpose for _, _, purpose in ALLOWED_ENDPOINTS)
    assert "            pypi.org:443\n" in render_pilot_steps()


def _decision(
    host, port, *, scheme="https", resolved=(), verified=(), integrity="verified"
):
    return decide_egress(
        ConnectionAttempt(host, port, scheme, tuple(resolved), tuple(verified)),
        integrity,
    )


def _allowed(rule):
    return Decision(True, rule)


def _denied(rule):
    return Decision(False, rule)


def test_block_mode_allows_exact_allowlisted_https_destinations():
    assert _decision(
        "pypi.org", 443, resolved=("203.0.113.10",), verified=("203.0.113.10",)
    ) == _allowed(RULE_ALLOW)
    assert _decision("files.pythonhosted.org", 443) == _allowed(RULE_ALLOW)
    assert _decision(
        "github.com",
        443,
        resolved=("203.0.113.7", "203.0.113.8"),
        verified=("203.0.113.7", "203.0.113.8"),
    ) == _allowed(RULE_ALLOW)
    assert _decision("PYPI.org.", 443) == _allowed(RULE_ALLOW)


def test_block_mode_denies_excluded_destinations_and_near_miss_hosts():
    assert _decision("example.com", 443) == _denied(RULE_DESTINATION)
    assert _decision("evil-pypi.org", 443) == _denied(RULE_DESTINATION)
    assert _decision("pypi.org.evil.test", 443) == _denied(RULE_DESTINATION)
    assert _decision("pypi.org", 8443) == _denied(RULE_DESTINATION)
    assert _decision("pypi.org", 80) == _denied(RULE_DESTINATION)


def test_block_mode_denies_address_literal_and_dns_resolution_bypasses():
    assert _decision("1.1.1.1", 443) == _denied(RULE_BYPASS_ADDRESS)
    assert _decision("::1", 443) == _denied(RULE_BYPASS_ADDRESS)
    assert _decision(
        "pypi.org", 443, resolved=("127.0.0.1",), verified=("203.0.113.10",)
    ) == _denied(RULE_BYPASS_DNS)
    assert _decision("pypi.org", 443, resolved=("127.0.0.1",), verified=()) == _denied(
        RULE_BYPASS_DNS
    )
    assert _decision(
        "pypi.org",
        443,
        resolved=("203.0.113.10", "127.0.0.1"),
        verified=("203.0.113.10",),
    ) == _denied(RULE_BYPASS_DNS)


def test_block_mode_denies_insecure_transport_and_integrity_failures_then_recovers():
    assert _decision("pypi.org", 443, scheme="http") == _denied(RULE_TRANSPORT)
    assert _decision("pypi.org", 443, integrity="compromised") == _denied(
        RULE_FAIL_CLOSED
    )
    assert _decision("example.com", 443, integrity="compromised") == _denied(
        RULE_FAIL_CLOSED
    )
    assert _decision("pypi.org", 443, integrity="verified") == _allowed(RULE_ALLOW)


def test_cli_check_patch_and_usage(tmp_path, capsys):
    workflow_path = tmp_path / "ci.yml"
    workflow_path.write_text(_workflow(_lint_job()), encoding="utf-8")
    assert main(["--check", str(workflow_path)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == STATE_ABSENT
    patched_path = tmp_path / "patched.yml"
    patched_path.write_text(
        pilot_patch(workflow_path.read_text(encoding="utf-8")), encoding="utf-8"
    )
    assert main(["--check", str(patched_path)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == STATE_CONFORMING
    assert main(["--patch", str(workflow_path)]) == 0
    assert capsys.readouterr().out == unified_patch(
        workflow_path.read_text(encoding="utf-8")
    )
    assert main(["--bogus", str(workflow_path)]) == 2
    assert main([]) == 2
    subprocess_cli_check(tmp_path)


def subprocess_cli_check(tmp_path):
    workflow_path = tmp_path / "sub.yml"
    workflow_path.write_text(_workflow(_lint_job(with_fence=True)), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(MODULE), "--check", str(workflow_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["state"] == STATE_CONFORMING


_SCRATCH_COPIES = (
    ".github/workflows/build-windows-candidate.yml",
    ".github/workflows/release-readiness.yml",
    ".github/workflows/release-windows.yml",
    "tooling/python-ci/requirements.in",
    "tooling/python-ci/requirements.lock.txt",
    "tooling/backend-ci/requirements.lock.txt",
    ".gitleaksignore",
    "Dockerfile",
    "backend/requirements.txt",
    "backend/tests/__init__.py",
    "backend/tests/test_android_next_major_canary.py",
    "backend/tests/test_release_contract_schemas.py",
    "backend/tests/test_release_evidence_validator.py",
    "backend/tests/perf/release_backend_060.py",
    "components/AstralProjection/.github/workflows/apple-ci.yml",
    "components/AstralProjection/windows-client/astral_client/integrity.py",
    "components/AstralProjection/apple-clients/AstralApp/AstralAppUITests/"
    "ConversationContinuityUITests.swift",
)
_SCRATCH_SUITES = (
    "test_python_ci_supply_chain_060.py",
    "test_release_workflows_060.py",
)
_SCRATCH_TREES = (
    "specs/060-runtime-reliability-hardening/contracts",
    "backend/tests/fixtures/runtime_reliability_060/release_evidence",
)


def test_generated_patch_preserves_the_supply_chain_contract_suite(tmp_path):
    workflow_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    release_lane = workflow_text.partition("  release-tooling-tests:\n")[2].partition(
        "\n  component-contract-tests:"
    )[0]
    lane_deselect = (
        "--deselect=backend/tests/test_python_ci_supply_chain_060.py::"
        "test_ci_only_python_manifest_cannot_enter_projection_product_artifacts"
    )
    assert lane_deselect in release_lane
    scratch_deselect = (
        "--deselect=tests/test_python_ci_supply_chain_060.py::"
        "test_ci_only_python_manifest_cannot_enter_projection_product_artifacts"
    )
    scratch = tmp_path / "repo"
    (scratch / "backend" / "tests").mkdir(parents=True)
    (scratch / "scripts").mkdir()
    patched = pilot_patch(workflow_text)
    assert evaluate_workflow(patched).state == STATE_CONFORMING
    (scratch / ".github" / "workflows").mkdir(parents=True)
    (scratch / ".github" / "workflows" / "ci.yml").write_text(patched, encoding="utf-8")
    for workflow in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        if workflow.name != "ci.yml":
            shutil.copy2(workflow, scratch / ".github" / "workflows" / workflow.name)
    for relative in _SCRATCH_COPIES:
        destination = scratch / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, destination)
    for tree in _SCRATCH_TREES:
        shutil.copytree(REPO_ROOT / tree, scratch / tree)
    for script in (REPO_ROOT / "scripts").glob("*.py"):
        shutil.copy2(script, scratch / "scripts" / script.name)
    for suite in _SCRATCH_SUITES:
        shutil.copy2(
            REPO_ROOT / "backend" / "tests" / suite,
            scratch / "backend" / "tests" / suite,
        )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(scratch), str(scratch / "backend"))
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *(f"tests/{suite}" for suite in _SCRATCH_SUITES),
            scratch_deselect,
        ],
        cwd=scratch / "backend",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
