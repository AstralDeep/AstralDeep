"""Supply-chain contracts for Spec 060's isolated Python CI tooling."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
if not (
    (REPO_ROOT / "tooling").is_dir() and (REPO_ROOT / ".github").is_dir()
):  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )
TOOLING_ROOT = REPO_ROOT / "tooling" / "python-ci"
INPUT = TOOLING_ROOT / "requirements.in"
LOCK = TOOLING_ROOT / "requirements.lock.txt"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
ANDROID_CANARY_TEST = REPO_ROOT / "backend" / "tests" / "test_android_next_major_canary.py"
ANDROID_CANARY_SCRIPT = REPO_ROOT / "scripts" / "run_android_next_major_canary.py"
GITLEAKS_IGNORE = REPO_ROOT / ".gitleaksignore"
REVIEWED_074_FINGERPRINTS = {
    "7bc9d1f683c535863b5426ab3053db3bdefc6a1a:config/astral-composition.json:generic-api-key:56",
    "839b4e3840ac31c2cadb7c7ab7657818f0ad46a0:windows-client/tests/test_win_agent_startup_gate.py:generic-api-key:121",
    "839b4e3840ac31c2cadb7c7ab7657818f0ad46a0:windows-client/tests/test_win_agent_startup_gate.py:generic-api-key:133",
    "839b4e3840ac31c2cadb7c7ab7657818f0ad46a0:windows-client/tests/test_win_agent_startup_gate.py:generic-api-key:201",
    "839b4e3840ac31c2cadb7c7ab7657818f0ad46a0:windows-client/tests/test_win_agent_inbound_auth.py:generic-api-key:124",
    "839b4e3840ac31c2cadb7c7ab7657818f0ad46a0:windows-client/tests/test_win_agent_inbound_auth.py:generic-api-key:267",
    "40cc17aba0c6bd4d7ca3e22b76829b7b657e5b90:windows-client/tests/test_remote_machines_surface.py:private-key:42",
}
REVIEWED_079_FINGERPRINT = (
    "756b338f3054bb8f509a2b94f0ac7c8b9b1b8cc3:"
    "scripts/tests/test_verify_persistent_agents_079.py:generic-api-key:35"
)
WINDOWS_CANDIDATE = (
    REPO_ROOT / ".github" / "workflows" / "build-windows-candidate.yml"
)
WINDOWS_RELEASE_BRIDGE = (
    REPO_ROOT / ".github" / "workflows" / "release-windows.yml"
)
LOCK_INSTALL = (
    "python -m pip install --require-hashes -r "
    "tooling/python-ci/requirements.lock.txt"
)


def _bridge_is_live() -> bool:
    """See backend/tests/test_release_workflows_060.py::_bridge_is_live.

    The protected bridge is parked at d3cb9a51; cc15b033 restored the direct
    tag-push release path and Windows client v0.4.0 shipped on it.
    """
    return "\n  bridge-sign:\n" in WINDOWS_RELEASE_BRIDGE.read_text(encoding="utf-8")


bridge_parked = pytest.mark.skipif(
    not _bridge_is_live(),
    reason=(
        "protected bridge parked at d3cb9a51 — see specs/060-runtime-"
        "reliability-hardening/verification/release-trust-bootstrap.md"
    ),
)


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _logical_requirements(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for requirement in _logical_requirements(path):
        match = re.match(
            r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^ ;\\]+)",
            requirement,
        )
        assert match, f"requirement is not an exact direct pin: {requirement}"
        pins[_normalized(match.group(1))] = match.group(2)
    return pins


def _workflow_job(workflow: str, job_name: str) -> str:
    jobs = workflow.partition("\njobs:\n")[2]
    assert jobs, "workflow does not define jobs"
    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\n(?P<body>.*?)"
        rf"(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        jobs,
    )
    assert match, f"workflow job is missing: {job_name}"
    return match.group("body")


def test_python_ci_direct_inputs_are_exact_and_minimal() -> None:
    assert _pins(INPUT) == {
        "coverage": "7.15.2",
        "diff-cover": "10.3.0",
        "psycopg2-binary": "2.9.12",
        "pytest": "9.1.1",
        "pytest-cov": "7.0.0",
        "ruff": "0.15.21",
    }
    text = INPUT.read_text(encoding="utf-8")
    assert "-r " not in text
    assert "--index-url" not in text
    assert "--extra-index-url" not in text


def test_python_ci_lock_hashes_every_exact_transitive_block() -> None:
    requirements = _logical_requirements(LOCK)
    assert len(requirements) >= 15
    for requirement in requirements:
        assert re.match(
            r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^ ;\\]+",
            requirement,
        ), f"lock requirement is not exact: {requirement}"
        assert "--hash=sha256:" in requirement, (
            f"lock requirement has no SHA-256 artifact hash: {requirement}"
        )
        assert " @ " not in requirement

    direct = _pins(INPUT)
    locked = _pins(LOCK)
    assert all(locked.get(name) == version for name, version in direct.items())


def test_ci_uses_one_hash_lock_for_every_python_test_tool_install() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    for job_name in (
        "lint",
        "release-tooling-tests",
        "component-contract-tests",
        "composition-declarations",
    ):
        job = _workflow_job(workflow, job_name)
        assert LOCK_INSTALL in job
        assert "cache-dependency-path: tooling/python-ci/requirements.lock.txt" in job

    assert "pip install ruff" not in workflow
    assert "pip install diff-cover" not in workflow
    assert "pip install pytest-cov" not in workflow
    assert "pip install pytest 'coverage" not in workflow
    assert "requirements.txt pytest" not in workflow
    assert "python -m pip install -r " not in workflow


def test_ci_secret_scan_uses_checksum_pinned_secret_free_cli() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    job = _workflow_job(workflow, "secret-scan")
    assert "gitleaks/gitleaks-action" not in job
    assert 'GITLEAKS_VERSION: "8.30.1"' in job
    assert (
        'GITLEAKS_SHA256: "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"'
        in job
    )
    assert "sha256sum --check --strict" in job
    assert 'test "$("$install_root/gitleaks" version)" = "$GITLEAKS_VERSION"' in job
    assert 'gitleaks git --redact --config "$GITHUB_WORKSPACE/.gitleaks.toml"' in job
    assert '--gitleaks-ignore-path "$GITHUB_WORKSPACE/.gitleaksignore"' in job
    assert '--log-opts="--all"' in job
    assert "secrets." not in job
    assert "GITLEAKS_LICENSE" not in job


def test_gitleaks_history_baseline_is_exact_fingerprint_only() -> None:
    lines = GITLEAKS_IGNORE.read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line.startswith("#")] == [
        "# Reviewed 079 test-only constructed JWT: private-owner payload, invalid literal signature."
    ]
    fingerprints = [line for line in lines if not line.startswith("#")]
    assert len(fingerprints) == 20
    assert len(fingerprints) == len(set(fingerprints))
    assert REVIEWED_074_FINGERPRINTS <= set(fingerprints)
    assert REVIEWED_079_FINGERPRINT in fingerprints
    assert all(
        re.fullmatch(
            r"[0-9a-f]{40}:[^:]+:(?:generic-api-key|private-key):[1-9][0-9]*",
            fingerprint,
        )
        for fingerprint in fingerprints
    )


def test_release_tooling_job_covers_owned_scripts_with_one_exact_omission() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    job = _workflow_job(workflow, "release-tooling-tests")
    assert "RELEASE_TOOL_TESTS=(" in job
    assert 'test "${#RELEASE_TOOL_TESTS[@]}" -gt 0' in job
    assert "coverage run --source=scripts" in job
    assert "coverage report --fail-under=90" in job
    omissions = set(re.findall(r"--omit=([^\s\\]+)", job))
    assert omissions == {"scripts/windows_release_candidate.py"}
    assert not any("*" in omission for omission in omissions)

    expected_scripts = {
        "check_changed_coverage.py",
        "check_doc_links.py",
        "export_xccov_line_coverage.py",
        "extract_release_artifact.py",
        "install_local_components.py",
        "prepare_release_evidence.py",
        "run_android_next_major_canary.py",
        "run_candidate_staging.py",
        "validate_release_evidence.py",
        "verify_component_ownership.py",
        "verify_composition.py",
        "verify_migration_provenance.py",
        "verify_primitive_coverage.py",
        "verify_persistent_agents_079.py",
        "verify_release_evidence_bootstrap.py",
        "windows_release_candidate.py",
    }
    assert {path.name for path in (REPO_ROOT / "scripts").glob("*.py")} == (
        expected_scripts
    )
    expected_test_paths = {
        "backend/tests/test_changed_coverage_060.py",
        "backend/tests/test_release_tooling_coverage_060.py",
        "backend/tests/test_documentation_060.py",
        "backend/tests/test_quickstart_commands.py",
        "backend/tests/test_python_ci_supply_chain_060.py",
        "backend/tests/test_android_next_major_canary.py",
        "backend/tests/test_candidate_staging_060.py",
        "backend/tests/test_release_evidence_validator.py",
        "backend/tests/test_prepare_release_evidence_060.py",
        "backend/tests/test_extract_release_artifact_060.py",
        "backend/tests/test_release_evidence_bootstrap.py",
        "scripts/tests/test_component_build_surfaces_074.py",
        "scripts/tests/test_install_local_components.py",
        "scripts/tests/test_verify_component_ownership.py",
        "scripts/tests/test_verify_composition.py",
        "scripts/tests/test_verify_migration_provenance.py",
        "scripts/tests/test_verify_primitive_coverage.py",
        "scripts/tests/test_verify_persistent_agents_079.py",
        "scripts/tests/test_csharp_native_coverage_079.py",
    }
    array = job.partition("RELEASE_TOOL_TESTS=(")[2].partition(")")[0]
    assert set(re.findall(r"(?m)^\s+([^\s]+\.py)\s*$", array)) == expected_test_paths


def test_release_tooling_job_excludes_real_component_checkout_tests() -> None:
    """Public release-tooling CI remains source-free while local gates use pins."""
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    job = _workflow_job(workflow, "release-tooling-tests")

    assert "submodules: false" in job
    assert set(re.findall(r"--deselect=([^\s\\]+)", job)) == {
        "backend/tests/test_documentation_060.py::test_byo_guide_is_reachable_from_projection_apple_client_docs",
        "backend/tests/test_python_ci_supply_chain_060.py::test_ci_only_python_manifest_cannot_enter_projection_product_artifacts",
        "scripts/tests/test_verify_composition.py::test_current_composition_has_exact_pins_canonical_urls_and_contracts",
        "scripts/tests/test_verify_composition.py::test_real_checkout_verification_executes_only_local_git_commands",
    }


def test_android_canary_suite_runs_without_projection_checkout(tmp_path: Path) -> None:
    """The public source-free lane must still exercise its Deep-owned driver."""
    source_root = tmp_path / "source-free"
    test_path = source_root / "backend" / "tests" / ANDROID_CANARY_TEST.name
    script_path = source_root / "scripts" / ANDROID_CANARY_SCRIPT.name
    test_path.parent.mkdir(parents=True)
    script_path.parent.mkdir()
    shutil.copy2(ANDROID_CANARY_TEST, test_path)
    shutil.copy2(ANDROID_CANARY_SCRIPT, script_path)

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test_path)],
        cwd=source_root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    assert completed.returncode == 0, completed.stdout


def test_windows_candidate_installs_test_lock_only_after_candidate_build() -> None:
    workflow = WINDOWS_CANDIDATE.read_text(encoding="utf-8")
    build = workflow.index("- name: Build the unsigned executable exactly once")
    test_install = workflow.index(
        "python -m pip install --require-hashes -r "
        "tooling\\python-ci\\requirements.lock.txt"
    )
    assert build < test_install
    assert "pytest==" not in workflow
    assert "pytest-cov==" not in workflow
    assert "tooling/python-ci/requirements.lock.txt" in workflow


@bridge_parked
def test_windows_release_bridge_signs_archived_bytes_without_rebuild() -> None:
    """The 060 bridge signs the exact archived build-once EXE (T119).

    No rebuild toolchain, no requirements install, no ad-hoc tool install:
    sigstore comes only from its SHA-pinned official action, and the bridge
    holds read/read/attestation-read/id-token permissions — never
    release-mutation authority.
    """

    workflow = WINDOWS_RELEASE_BRIDGE.read_text(encoding="utf-8")
    lower = workflow.lower()
    assert "pyinstaller" not in lower
    assert "astraldeep.spec" not in lower
    assert re.search(r"pip install[^\n]*requirements", workflow) is None
    assert "pip install --upgrade" not in workflow
    assert "pip install" not in workflow
    assert "sigstore>=" not in workflow
    assert re.search(
        r"(?m)^\s*uses:\s*sigstore/gh-action-sigstore-python@[0-9a-f]{40}\s+# v\d+(?:\.\d+)*$",
        workflow,
    ), "the bridge must obtain sigstore from the SHA-pinned official action"
    grants = {
        f"{key}: {value}"
        for key, value in re.findall(
            r"(?m)^\s*([a-z-]+):\s*(read|write)\s*(?:#.*)?$", workflow
        )
    }
    assert grants == {
        "contents: read",
        "actions: read",
        "attestations: read",
        "id-token: write",
    }


def test_ci_only_python_manifest_cannot_enter_deep_product_artifacts() -> None:
    product_inputs = (
        REPO_ROOT / "Dockerfile",
        REPO_ROOT / "backend" / "requirements.txt",
    )
    for path in product_inputs:
        assert "tooling/python-ci" not in path.read_text(encoding="utf-8"), path

    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"(?mi)^\s*COPY\s+\.\s", dockerfile) is None


def test_ci_only_python_manifest_cannot_enter_projection_product_artifacts() -> None:
    projection_inputs = (
        REPO_ROOT / "components/AstralProjection/windows-client/AstralDeep.spec",
        REPO_ROOT / "components/AstralProjection/windows-client/requirements.in",
        REPO_ROOT / "components/AstralProjection/apple-clients/AstralCore/Package.swift",
        REPO_ROOT / "components/AstralProjection/android-client/settings.gradle.kts",
    )
    for path in projection_inputs:
        assert "tooling/python-ci" not in path.read_text(encoding="utf-8"), path


def test_windows_release_installs_only_hash_locked_build_and_signing_deps() -> None:
    """The signing toolchain must be immutably pinned however it is obtained.

    This is the LIVE half of the parked
    test_windows_release_bridge_signs_archived_bytes_without_rebuild. The bridge
    got sigstore from a SHA-pinned official action; the direct tag-push release
    gets pyinstaller AND the sigstore CLI from the complete hash lock, which is
    an equal-or-stronger control — a hash-pinned wheel cannot be repointed the
    way a floating action tag can. But it must be the ONLY install path into a
    job that holds id-token: write.

    The parked test's literal ``assert "pip install" not in workflow`` was a
    mechanism artifact, not the property; this asserts the property.
    """
    workflow = WINDOWS_RELEASE_BRIDGE.read_text(encoding="utf-8")
    installs = [
        line.strip() for line in workflow.splitlines() if "pip install" in line
    ]
    assert installs, "the release workflow must install its build/signing deps"
    for line in installs:
        assert "--require-hashes" in line, f"unhashed pip install: {line}"
        assert (
            "components/AstralProjection/windows-client/requirements-release.lock.txt" in line
        ), f"install is not from the release lock: {line}"
    assert "pip install --upgrade" not in workflow
    assert "sigstore>=" not in workflow
    # The lock's own exactness (every line hashed, sigstore/pyinstaller present)
    # is enforced by components/AstralProjection/windows-client/tests/test_release_lock_060.py.
