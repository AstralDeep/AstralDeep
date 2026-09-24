"""Contract tests for the release-trust workflow set (release-readiness,
release-trusted-builder, release-windows plus its publisher/controller,
release-evidence-exception): checkout provenance, permission scoping, and evidence
policy enforcement.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
if not (
    (REPO_ROOT / ".github").is_dir() and (REPO_ROOT / "scripts").is_dir()
):
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SPEC_ROOT = REPO_ROOT / "specs" / "060-runtime-reliability-hardening"
CONTRACT_ROOT = SPEC_ROOT / "contracts"
SCRIPT_PATH = REPO_ROOT / "scripts" / "validate_release_evidence.py"
CONTRACT_TEST_PATH = (
    REPO_ROOT / "backend" / "tests" / "test_release_contract_schemas.py"
)
VALIDATOR_TEST_PATH = (
    REPO_ROOT / "backend" / "tests" / "test_release_evidence_validator.py"
)
FIXTURE_ROOT = (
    REPO_ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "runtime_reliability_060"
    / "release_evidence"
)

CI_WORKFLOW = WORKFLOWS / "ci.yml"
PROJECTION_WORKFLOWS = (
    REPO_ROOT / "components" / "AstralProjection" / ".github" / "workflows"
)
APPLE_CI = PROJECTION_WORKFLOWS / "apple-ci.yml"
READINESS = WORKFLOWS / "release-readiness.yml"
APPLE_NORMALIZER = WORKFLOWS / "release-apple-evidence-normalizer.yml"
WINDOWS_CANDIDATE = WORKFLOWS / "build-windows-candidate.yml"
PROTECTED_TRIGGER = WORKFLOWS / "release-readiness-protected.yml"
TRUSTED_BUILDER = WORKFLOWS / "release-trusted-builder.yml"
EXCEPTION = WORKFLOWS / "release-evidence-exception.yml"
BRIDGE = WORKFLOWS / "release-windows.yml"
CONTROLLER = WORKFLOWS / "release-windows-publisher-controller.yml"
PUBLISHER = WORKFLOWS / "release-windows-publisher.yml"
RELEASE_WORKFLOW_FILES = (
    PROTECTED_TRIGGER,
    READINESS,
    TRUSTED_BUILDER,
    EXCEPTION,
    BRIDGE,
    CONTROLLER,
    PUBLISHER,
)

EVIDENCE_PRODUCER_JOBS = (
    "backend-producer",
    "web-producer",
    "windows-producer",
    "android-producer",
    "macos-producer",
    "ios-producer",
    "watchos-producer",
    "docs-producer",
)
RAW_APPLE_PRODUCER_JOBS = (
    "macos-raw-producer",
    "ios-raw-producer",
    "watchos-raw-producer",
)
COMPONENT_CONSUMER_JOBS = (
    "backend-producer",
    "web-producer",
    "windows-producer",
    "android-producer",
    *RAW_APPLE_PRODUCER_JOBS,
)
PRODUCER_JOBS = (
    *EVIDENCE_PRODUCER_JOBS,
    *RAW_APPLE_PRODUCER_JOBS,
    "windows-candidate",
)

ATTEST_ACTION = (
    "actions/attest-build-provenance@0f67c3f4856b2e3261c31976d6725780e5e4c373 # v4.1.1"
)
SETUP_PYTHON_ACTION = (
    "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6.3.0"
)


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def validator() -> Any:
    return _load_module("release_workflows_validator_060", SCRIPT_PATH)


@pytest.fixture(scope="module")
def contract_examples() -> Any:
    return _load_module("release_workflows_contract_examples_060", CONTRACT_TEST_PATH)


@pytest.fixture(scope="module")
def evidence_examples() -> Any:
    return _load_module("release_workflows_evidence_examples_060", VALIDATOR_TEST_PATH)


def _workflow_text(path: Path) -> str:
    assert path.is_file(), (
        f"missing workflow (not yet authored for spec 060): "
        f".github/workflows/{path.name}"
    )
    return path.read_text(encoding="utf-8")


def _workflow_head(workflow: str) -> str:
    return workflow.partition("\njobs:\n")[0]


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


def _job_ids(workflow: str) -> list[str]:
    jobs = workflow.partition("\njobs:\n")[2]
    assert jobs, "workflow does not define jobs"
    return re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", jobs)


def _assert_immediate_exact_component_checkout(
    job: str,
    *,
    checkout_name: str = "Check out the exact candidate",
    expected_ref: str = "ref: ${{ inputs.candidate_sha }}",
) -> None:
    marker = f"- name: {checkout_name}"
    assert marker in job
    checkout_and_tail = job.partition(marker)[2]
    checkout, separator, tail = checkout_and_tail.partition("\n      - ")
    assert separator, f"{checkout_name} is the final workflow step"
    assert expected_ref in checkout
    assert "submodules: recursive" in checkout
    assert "persist-credentials: false" in checkout
    assert tail.startswith("name: Verify the exact initialized component composition\n")
    verification, _separator, _remaining = tail.partition("\n      - ")
    assert "scripts/verify_composition.py --root ." in verification


def _permission_lines(scope: str) -> set[str]:
    return {
        f"{key}: {value}"
        for key, value in re.findall(
            r"(?m)^\s*([a-z-]+):\s*(read|write)\s*(?:#.*)?$", scope
        )
    }


def _top_permissions(workflow: str) -> set[str]:
    head = _workflow_head(workflow)
    match = re.search(r"(?ms)^permissions:\s*\n(?P<body>(?:^  [a-z-]+:[^\n]*\n)+)", head)
    assert match, "workflow lacks an explicit top-level permissions block"
    return _permission_lines(match.group("body"))


def _write_grants(scope: str) -> set[str]:
    return {line for line in _permission_lines(scope) if line.endswith(": write")}


def _bridge_is_live() -> bool:
    return "\n  bridge-sign:\n" in _workflow_text(BRIDGE)


bridge_parked = pytest.mark.skipif(
    not _bridge_is_live(),
    reason=(
        "protected bridge parked at d3cb9a51 (cc15b033 restored the direct "
        "tag-push release path; v0.4.0 shipped on it) — see specs/060-runtime-"
        "reliability-hardening/verification/release-trust-bootstrap.md"
    ),
)


def test_release_readiness_identity_triggers_and_read_only_top_level() -> None:
    workflow = _workflow_text(READINESS)
    head = _workflow_head(workflow)

    assert re.search(r"(?m)^name: release-readiness$", head)
    assert "run-name: release-readiness ${{ inputs.request_id }}" in head
    assert "workflow_call:" in head
    assert "workflow_dispatch:" not in head
    for input_name in ("candidate_sha", "base_sha", "source_run_id", "request_id"):
        assert f"{input_name}:" in head
    assert "required: true" in head
    assert _top_permissions(workflow) == {"contents: read"}
    assert "group: release-readiness-staging" in head
    assert "cancel-in-progress: false" in head
    assert "release-readiness-${{ inputs.candidate_sha }}" not in head


def test_candidate_ci_checks_out_the_exact_source_run_head() -> None:
    workflow = _workflow_text(CI_WORKFLOW)
    checkout = (
        "uses: actions/checkout@"
        "93cb6efe18208431cddfb8368fd83d5badbf9bfd # v5.0.1"
    )
    expected_ref = "ref: ${{ github.event.pull_request.head.sha || github.sha }}"
    segments = workflow.split(checkout)
    assert len(segments) > 1
    for index, segment in enumerate(segments[1:], start=1):
        step = segment.partition("\n      - ")[0]
        assert expected_ref in step, f"CI checkout #{index} does not bind the source head"


def test_release_readiness_jobs_form_the_stage_producer_decision_pipeline() -> None:
    workflow = _workflow_text(READINESS)
    job_ids = _job_ids(workflow)
    for job_id in (
        "stage-deploy",
        *PRODUCER_JOBS,
        "trusted-builder",
        "protected-decision",
        "stage-cleanup",
    ):
        assert job_id in job_ids, f"release-readiness.yml lacks job {job_id}"

    stage = _workflow_job(workflow, "stage-deploy")
    assert "runs-on: [self-hosted, astral-staging]" in stage
    assert "environment: release-readiness-staging" in stage
    assert "ASTRAL_STAGING_ENDPOINT" in stage
    assert "persist-credentials: false" in stage
    assert "--leave-running" in stage
    assert "--trusted-manifest" in stage
    assert "trusted-stage-deploy.json" in stage
    assert "stage-outputs-" in stage
    assert "stage-manifest-" in stage
    assert "stage-outputs-${{ inputs.request_id || github.run_id }}-${{ github.run_attempt }}" in stage
    assert "stage-manifest-${{ inputs.request_id || github.run_id }}-${{ github.run_attempt }}" in stage
    assert "write-trusted-manifest" in stage
    assert "steps.outputs-upload.outputs.artifact-id" in stage
    blocker = (
        "- name: Block staging until the external ephemeral credential issuer "
        "is integrated"
    )
    runner_binding = "- name: Validate and export the canonical staging runner name"
    docker_setup = "- name: Create job-temporary Docker registry configuration"
    deploy = "- name: Deploy the candidate namespace and leave it running"
    assert blocker in stage
    assert (
        "external ephemeral credential issuer integration is not implemented; "
        "candidate staging remains fail-closed"
    ) in stage
    assert f"steps:\n      {blocker}" in stage
    assert stage.index(blocker) < stage.index(runner_binding)
    assert stage.index(runner_binding) < stage.index(docker_setup) < stage.index(deploy)
    assert "runner_name: ${{ steps.runner-binding.outputs.runner_name }}" in stage
    assert "printf 'runner_name=%s\\n' \"$RUNNER_NAME\" >> \"$GITHUB_OUTPUT\"" in stage
    assert "runner_name_pattern='^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$'" in stage
    assert stage.index("Archive the immutable stage outputs") < stage.index(
        "Generate the protected manifest from the actual immutable artifact id"
    ) < stage.index("Archive the post-upload protected stage manifest separately")
    deploy_step = stage.partition("- name: Deploy the candidate namespace")[2].partition(
        "- name: Archive the immutable stage outputs"
    )[0]
    assert "--trusted-manifest" not in deploy_step
    assert "protected-policy/scripts/run_candidate_staging.py" in stage
    assert "--candidate-source-root" in stage
    assert (
        "ASTRAL_STAGING_EXPECTED_RUNNER_NAME: "
        "${{ vars.ASTRAL_STAGING_RUNNER_NAME }}"
    ) in stage
    assert "secrets.ASTRAL_STAGING_RUNNER_NAME" not in stage
    assert 'test "$RUNNER_NAME" = "$ASTRAL_STAGING_EXPECTED_RUNNER_NAME"' in stage

    stage_direct_producers = (
        "backend-producer",
        "web-producer",
        "windows-producer",
        "android-producer",
        *RAW_APPLE_PRODUCER_JOBS,
        "docs-producer",
        "windows-candidate",
    )
    for producer in stage_direct_producers:
        body = _workflow_job(workflow, producer)
        assert "needs:" in body, f"{producer} must depend on stage-deploy"
        assert "stage-deploy" in body, f"{producer} must depend on stage-deploy"
    for producer in EVIDENCE_PRODUCER_JOBS:
        body = _workflow_job(workflow, producer)
        platform = producer.removesuffix("-producer")
        key = "final_artifact_name" if platform in {"macos", "ios", "watchos"} else "name"
        expected_artifact = (
            f"{key}: evidence-{platform}-"
            "${{ inputs.request_id || github.run_id }}-${{ github.run_attempt }}"
        )
        assert expected_artifact in body
        if platform not in {"macos", "ios", "watchos"}:
            assert f"release-evidence/{platform}.json" in body

    candidate = _workflow_job(workflow, "windows-candidate")
    assert "uses: ./.github/workflows/build-windows-candidate.yml" in candidate
    assert "staging_access_token" in candidate
    assert "ASTRAL_WINDOWS_SMOKE_TOKEN" in candidate

    backend = _workflow_job(workflow, "backend-producer")
    assert "release_backend_060.py" in backend
    web = _workflow_job(workflow, "web-producer")
    assert "playwright-image.txt" in web
    assert "browser:release" in web
    assert "NODE_V8_COVERAGE" in web
    assert "test:coverage-conversion:node" in web
    assert "test:coverage-union" in web
    assert web.count("corepack npm run coverage:node") == 3
    assert "corepack npm run coverage:union" in web
    assert "--coverage-istanbul-output \"$BROWSER_COVERAGE\"" in web
    assert "--node \"$NODE_COVERAGE\"" in web
    assert "--browser \"$BROWSER_COVERAGE\"" in web
    assert "--repo-root ../.." in web
    assert "web-istanbul.json" in web
    assert 'NODE_V8_COVERAGE="$OFFLINE_V8_DIRECTORY" node --test tests/offline-worker-088.test.mjs' in web
    assert '--node-v8-directory "$OFFLINE_V8_DIRECTORY"' in web
    assert '--export-coverage-output "$EXPORT_COVERAGE"' in web
    assert '--offline-node "$OFFLINE_COVERAGE"' in web
    assert '--export-browser "$EXPORT_COVERAGE"' in web
    assert 'test ! -e "$OFFLINE_V8_DIRECTORY"' in web
    assert web.index('tests/offline-worker-088.test.mjs') < web.index('corepack npm run coverage:union')
    windows = _workflow_job(workflow, "windows-producer")
    assert "windows-candidate" in windows
    assert "release_evidence_060.py" in windows
    assert "executable_sha256" in windows
    android = _workflow_job(workflow, "android-producer")
    assert ":app:prepareCoverageInputs" in android
    assert "--lane staging" in android
    assert "--lanes fixtures,staging" in android
    for slug in ("macos", "ios"):
        assert "ReleaseEvidenceUITests" in _workflow_job(workflow, f"{slug}-raw-producer")
    watch = _workflow_job(workflow, "watchos-raw-producer")
    assert "AstralWatchTests/ReleaseEvidenceTests" in watch
    docs = _workflow_job(workflow, "docs-producer")
    assert "check_doc_links.py" in docs

    builder = _workflow_job(workflow, "trusted-builder")
    assert "uses: ./.github/workflows/release-trusted-builder.yml" in builder
    assert "always()" in builder
    for producer in (
        "stage-deploy",
        *EVIDENCE_PRODUCER_JOBS,
        *RAW_APPLE_PRODUCER_JOBS,
    ):
        assert producer in builder, f"trusted-builder must wait on {producer}"

    cleanup = _workflow_job(workflow, "stage-cleanup")
    assert "github.event_name != 'workflow_dispatch'" in cleanup
    assert "github.ref == 'refs/heads/main'" in cleanup
    assert "always()" in cleanup
    assert "needs.stage-deploy.outputs.runner_name != ''" in cleanup
    assert "runs-on: [self-hosted, astral-staging]" in cleanup
    assert "ubuntu-latest" not in cleanup
    assert "cleanup" in cleanup
    assert (
        "ASTRAL_STAGING_EXPECTED_RUNNER_NAME: "
        "${{ needs.stage-deploy.outputs.runner_name }}"
    ) in cleanup
    assert "secrets.ASTRAL_STAGING_RUNNER_NAME" not in cleanup
    assert "environment:" not in cleanup
    assert "runner_name_pattern='^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$'" in cleanup
    assert 'test "$RUNNER_NAME" = "$ASTRAL_STAGING_EXPECTED_RUNNER_NAME"' in cleanup


def _android_producer_step(name: str) -> str:
    body = _workflow_job(READINESS.read_text(encoding="utf-8"), "android-producer")
    marker = f"      - name: {name}\n"
    assert marker in body
    return body.split(marker, 1)[1].split("\n      - ", 1)[0]


def _android_producer_python(name: str) -> str:
    body = _android_producer_step(name)
    code = body.split("          python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    return "\n".join(line[10:] for line in code.splitlines()) + "\n"


def test_android_coverage_producer_requires_build_once_and_both_device_lanes() -> None:
    body = _workflow_job(READINESS.read_text(encoding="utf-8"), "android-producer")
    assert body.count(":app:prepareCoverageInputs") == 1
    assert body.count(":core:koverXmlReport") == 1
    built, devices = body.split("Collect fixture and real staging observations", 1)
    assert "./gradlew -PastralCoverage=true" in built
    assert ":app:prepareCoverageInputs :core:koverXmlReport" in built
    assert "android_coverage.py prepare" in built
    assert "gradle" not in devices
    assert "connectedDebugAndroidTest" not in body
    assert "emulator-port: 5584" in devices
    assert devices.index("--lane fixtures") < devices.index("--lane staging")
    assert devices.index("--lane staging") < devices.index("--lanes fixtures,staging")
    assert devices.count("--serial emulator-5584") == 2
    assert "--arguments-file \"$ASTRAL_ANDROID_STAGING_ARGUMENTS\"" in devices
    assert "../build/060" not in body
    assert "adb logcat" not in body
    assert "test ! -e /sdcard/Android/data/com.personalailabs.astraldeep/files/release-evidence" in devices
    assert 'test ! -e "$GITHUB_WORKSPACE/build/060/release-evidence"' in devices
    assert "path: build/060/release-evidence/" in devices
    diagnostics = _android_producer_step("Retain Android coverage attempts and raw execution diagnostics")
    assert "if: always()" in diagnostics
    assert "path: build/060/android-coverage/" in diagnostics
    assert "if-no-files-found: error" in diagnostics
    assert "ASTRAL_STAGING_ACCESS_TOKEN: ${{ secrets.ASTRAL_STAGING_ACCESS_TOKEN }}" in body
    assert "external ephemeral credential issuer integration is not implemented" in _workflow_job(READINESS.read_text(encoding="utf-8"), "stage-deploy")


@pytest.mark.parametrize("fail_environment_append", [False, True])
def test_android_staging_arguments_are_private_and_cleanup_is_owned(
    tmp_path: Path, fail_environment_append: bool,
) -> None:
    stage = tmp_path / "build/060/stage"
    stage.mkdir(parents=True)
    (stage / "staging-outputs.json").write_text('{"environment_id":"synthetic"}')
    (stage.parent / "android-next-major-readiness.json").write_text('{"status":"passed"}')
    temporary = tmp_path / "runner-temp"
    temporary.mkdir()
    environment_file = temporary / "github-env"
    private_value = "synthetic token with spaces and a newline\nsecond line"
    environment = {
        **os.environ,
        "GITHUB_WORKSPACE": str(tmp_path),
        "RUNNER_TEMP": str(temporary),
        "GITHUB_ENV": str(environment_file),
        "ASTRAL_STAGING_URL": "https://staging.invalid",
        "ASTRAL_STAGING_ACCESS_TOKEN": private_value,
        "ASTRAL_RELEASE_CANDIDATE_SHA": "a" * 40,
        "ASTRAL_RELEASE_ID": "candidate-synthetic",
        "ASTRAL_RELEASE_VERSION": "1.0.0",
        "ASTRAL_RELEASE_LIFECYCLE_AGENT_ID": "synthetic-agent",
        "ASTRAL_RELEASE_LIFECYCLE_STATES": "starting,online",
        "RUNNER_NAME": "synthetic-runner", "RUNNER_ARCH": "X64",
        "GITHUB_WORKFLOW": "release-readiness", "GITHUB_RUN_ID": "1",
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "android-producer",
    }
    code = _android_producer_python("Write private Android staging arguments")
    if fail_environment_append:
        environment_file.mkdir()
        neighbor = temporary / "android-staging-arguments-unrelated.json"
        neighbor.write_bytes(b"unrelated existing file")
        result = subprocess.run([sys.executable, "-c", code], env=environment, capture_output=True, text=True)
        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr.strip() == "android_staging_arguments_write_failed"
        assert private_value not in result.stderr
        assert sorted(temporary.glob("android-staging-arguments-*.json")) == [neighbor]
        assert neighbor.read_bytes() == b"unrelated existing file"
        return
    paths = []
    for _ in range(2):
        result = subprocess.run([sys.executable, "-c", code], env=environment, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout == result.stderr == ""
        line = environment_file.read_text().splitlines()[-1]
        key, path = line.split("=", 1)
        assert key == "ASTRAL_ANDROID_STAGING_ARGUMENTS"
        paths.append(Path(path))
        assert paths[-1].stat().st_mode & 0o777 == 0o600
        values = json.loads(paths[-1].read_text())
        assert len(values) == 17
        assert values["astralAccessToken"] == private_value
        assert values["astralStagingUrl"] == "https://staging.invalid"
        assert private_value not in environment_file.read_text()
    assert paths[0] != paths[1]
    original = paths[0].read_bytes()
    cleanup = _android_producer_step("Remove private Android staging arguments")
    assert "always() && env.ASTRAL_ANDROID_STAGING_ARGUMENTS != ''" in cleanup
    command = cleanup.split("        run: ", 1)[1].strip()
    subprocess.run(["bash", "-euc", command], env={**environment, "ASTRAL_ANDROID_STAGING_ARGUMENTS": str(paths[1])}, check=True)
    assert not paths[1].exists()
    assert paths[0].read_bytes() == original


def _android_binding_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    evidence = tmp_path / "build/060/release-evidence"
    retained = tmp_path / "build/060/android-coverage"
    evidence.mkdir(parents=True)
    (retained / "report").mkdir(parents=True)
    apk = b"synthetic tested APK bytes"
    (retained / "app-coverage.apk").write_bytes(apk)
    (retained / "report/report.xml").write_bytes(b"synthetic app observations")
    (retained / "android-core.xml").write_bytes(b"synthetic core observations")
    digest = hashlib.sha256(apk).hexdigest()
    report = {
        "candidate_sha": "a" * 40, "release_id": "candidate-synthetic",
        "release_version": "1.0.0", "platform": "android",
        "artifact": {"name": "base.apk", "kind": "android_apk", "immutable_reference": "bundle://android/base.apk", "sha256": digest},
    }
    (evidence / "android.json").write_text(json.dumps(report))
    (retained / "manifest.json").write_text(json.dumps({"artifacts": {"app-coverage.apk": digest}}))
    union = {
        "lanes": ["fixtures", "staging"], "protected_staging_included": True,
        "manifest_sha256": hashlib.sha256((retained / "manifest.json").read_bytes()).hexdigest(),
        "report_sha256": hashlib.sha256((retained / "report/report.xml").read_bytes()).hexdigest(),
    }
    (retained / "report.json").write_text(json.dumps(union))
    return evidence, retained, {
        **os.environ, "GITHUB_WORKSPACE": str(tmp_path),
        "ASTRAL_RELEASE_CANDIDATE_SHA": "a" * 40,
        "ASTRAL_RELEASE_ID": "candidate-synthetic", "ASTRAL_RELEASE_VERSION": "1.0.0",
    }


@pytest.mark.parametrize("mutation", [
    None, "candidate", "release", "version", "platform", "kind", "traversal",
    "nonstring_name", "reference", "apk", "manifest", "lanes", "staging",
    "xml", "array_report", "array_artifact", "existing_destination",
])
def test_android_workflow_binds_exact_tested_artifact_and_real_staging(
    tmp_path: Path, mutation: str | None,
) -> None:
    evidence, retained, environment = _android_binding_fixture(tmp_path)
    report = json.loads((evidence / "android.json").read_text())
    union = json.loads((retained / "report.json").read_text())
    if mutation in {"candidate", "release", "version", "platform"}:
        field = {"candidate": "candidate_sha", "release": "release_id", "version": "release_version", "platform": "platform"}[mutation]
        report[field] = "incorrect"
    elif mutation in {"kind", "traversal", "nonstring_name", "reference"}:
        field, value = {"kind": ("kind", "source_tree"), "traversal": ("name", "../base.apk"), "nonstring_name": ("name", 123), "reference": ("immutable_reference", "bundle://other/base.apk")}[mutation]
        report["artifact"][field] = value
    elif mutation == "apk":
        (retained / "app-coverage.apk").write_bytes(b"different APK bytes")
    elif mutation == "manifest":
        (retained / "manifest.json").write_text('{"artifacts":{"app-coverage.apk":"wrong"}}')
    elif mutation == "lanes":
        union["lanes"] = ["fixtures"]
    elif mutation == "staging":
        union["protected_staging_included"] = False
    elif mutation == "xml":
        (retained / "report/report.xml").write_bytes(b"changed report")
    elif mutation == "array_report":
        report = []
    elif mutation == "array_artifact":
        report["artifact"] = []
    elif mutation == "existing_destination":
        (evidence / "android").mkdir()
        (evidence / "android/base.apk").write_bytes(b"existing artifact")
    (evidence / "android.json").write_text(json.dumps(report))
    (retained / "report.json").write_text(json.dumps(union))
    code = _android_producer_python("Bind Android report members to the retained tested bytes")
    result = subprocess.run([sys.executable, "-c", code], env=environment, capture_output=True, text=True)
    if mutation is None:
        assert result.returncode == 0, result.stderr
        assert (evidence / "android/base.apk").read_bytes() == (retained / "app-coverage.apk").read_bytes()
        assert not (evidence / "android/app-coverage.apk").exists()
        assert (evidence / "coverage/android-app.xml").read_bytes() == (retained / "report/report.xml").read_bytes()
        assert (evidence / "coverage/android-core.xml").read_bytes() == (retained / "android-core.xml").read_bytes()
    else:
        assert result.returncode != 0
        assert not (evidence / "coverage").exists()
        if mutation == "existing_destination":
            assert (evidence / "android/base.apk").read_bytes() == b"existing artifact"
        else:
            assert not (evidence / "android").exists()


@pytest.mark.parametrize("job_id", COMPONENT_CONSUMER_JOBS)
def test_release_readiness_component_consumers_verify_exact_initialized_gitlinks(
    job_id: str,
) -> None:
    workflow = _workflow_text(READINESS)
    _assert_immediate_exact_component_checkout(_workflow_job(workflow, job_id))


def test_release_readiness_backend_producer_mounts_plane_source_read_only() -> None:
    backend = _workflow_job(_workflow_text(READINESS), "backend-producer")
    assert (
        '-v "$PWD/components/AstralPlane:/app/components/AstralPlane:ro"'
        in backend
    )


def test_delegated_component_consumers_verify_exact_initialized_gitlinks() -> None:
    windows = _workflow_job(_workflow_text(WINDOWS_CANDIDATE), "windows-candidate")
    _assert_immediate_exact_component_checkout(
        windows,
        expected_ref="ref: ${{ env.CANDIDATE_SHA }}",
    )

    apple = _workflow_job(_workflow_text(APPLE_NORMALIZER), "normalize")
    checkout = apple.split(
        "- name: Check out the exact candidate as normalization data", 1
    )[1].split("\n      - ", 1)[0]
    assert "submodules: recursive" in checkout
    assert "persist-credentials: false" in checkout
    assert "ref: ${{ inputs.candidate_sha }}" in checkout
    assert "python3 -I scripts/" not in apple
    verifier = "python3 -I protected-policy/scripts/verify_composition.py"
    assert verifier in apple
    assert apple.index("path: protected-policy") < apple.index(verifier)
    assert (
        "--schema protected-policy/specs/074-multirepo-lets-integration/contracts/composition-manifest.schema.json"
        in apple
    )


def test_release_readiness_candidate_jobs_never_carry_write_authority() -> None:
    workflow = _workflow_text(READINESS)
    for job_id in ("stage-deploy", *PRODUCER_JOBS, "stage-cleanup"):
        grants = _write_grants(_workflow_job(workflow, job_id))
        assert not grants, f"candidate-facing job {job_id} must stay read-only: {sorted(grants)}"
    decision = _permission_lines(_workflow_job(workflow, "protected-decision"))
    assert decision == {
        "id-token: write",
        "attestations: write",
        "actions: read",
        "contents: read",
    }
    builder = _permission_lines(_workflow_job(workflow, "trusted-builder"))
    assert builder == {
        "id-token: write",
        "attestations: write",
        "actions: read",
        "contents: read",
    }


def test_release_readiness_protected_decision_runs_pinned_policy_only() -> None:
    workflow = _workflow_text(READINESS)
    decision = _workflow_job(workflow, "protected-decision")

    assert "trusted-builder" in decision
    assert "gh attestation verify" in decision
    assert "--signer-workflow" in decision
    assert '--signer-digest "$RELEASE_TRUSTED_BUILDER_SHA"' in decision
    assert '--cert-identity "$RELEASE_TRUSTED_BUILDER_IDENTITY"' in decision
    assert "release-trusted-builder.yml" in decision
    assert "release-evidence-debt" in decision
    assert "git archive" in decision
    assert "RELEASE_TRUSTED_BUILDER_SHA" in decision
    assert "vars.RELEASE_TRUSTED_BUILDER_SHA" in workflow
    assert "check_changed_coverage.py" in decision
    assert "scripts/prepare_release_evidence.py" in decision
    assert re.search(
        r'"astral_prepare_release_evidence",\s*'
        r'"build/060/protected-policy/scripts/prepare_release_evidence\.py"',
        decision,
    )
    assert (
        '"astral_prepare_release_evidence", "scripts/prepare_release_evidence.py"'
        not in decision
    )
    assert "validate_release_evidence.py" in decision
    assert "--decision-output" in decision
    assert "trusted-release-decision.json" in decision
    assert "--protected-workflow-ref" in decision
    assert "release-readiness.yml@$GITHUB_WORKFLOW_SHA" in decision
    assert "--protected-policy-sha" in decision
    assert "name: release-evidence" in decision
    assert "name: trusted-release-decision" in decision
    assert "path: build/060/protected-decision/trusted-release-decision.json" in decision
    assert "path: build/060/protected-decision/\n" not in decision
    assert "Independently reconcile the protected stage manifest" in decision
    assert "attested stage manifest has a false stage-output artifact claim" in decision
    assert "stage-manifest-" in decision
    assert "scripts/extract_release_artifact.py" in decision
    assert 'git show "$RELEASE_TRUSTED_BUILDER_SHA:scripts/extract_release_artifact.py"' in decision
    assert "wrong attempt-scoped input artifact name" in decision
    assert "trusted-manifests-{suffix}" in decision
    assert "windows-unsigned-{candidate}-{run_id}-{run_attempt}" in decision
    assert "512 * 1024 * 1024" in decision
    assert "extract_release_artifact.py" in workflow
    assert "unzip" not in decision
    assert "raw-apple-evidence-" in decision
    assert "build/060/raw-apple-evidence/{platform}" in decision
    assert "--raw-apple-evidence-dir build/060/raw-apple-evidence" in decision
    assert ATTEST_ACTION in decision
    assert decision.index("--decision-output") < decision.index(ATTEST_ACTION)
    assert decision.index(ATTEST_ACTION) < decision.index(
        "name: trusted-release-decision"
    )


def test_release_readiness_binds_voice_runtime_to_protected_staging() -> None:
    workflow = _workflow_text(READINESS)
    publish = _workflow_job(workflow, "publish-candidate")
    stage = _workflow_job(workflow, "stage-deploy")

    assert "voice-worker-image" in publish
    assert "run-id: ${{ inputs.source_run_id }}" in publish
    assert "source run is not the exact successful protected CI candidate" in publish
    assert "voice_worker_image" in publish
    assert "astraldeep-voice-worker.tar" in publish
    assert "ghcr.io/$REPO_LC/voice-worker:candidate-${{ inputs.candidate_sha }}" in publish
    assert publish.index("Download the shared-run voice-worker image") < publish.index(
        'docker push "ghcr.io/$REPO_LC:candidate-'
    )
    assert (
        "livekit/livekit-server:v1.13.5@sha256:"
        "3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1"
        in publish
    )

    assert "deploy/livekit/livekit.staging.yaml" in stage
    assert "VOICE_SPEECH_BASE_URL" in stage
    assert "VOICE_SPEECH_API_KEY" in stage
    assert "ProxyHandler({})" in stage
    assert "HTTPRedirectHandler" in stage
    assert "urllib.request.urlopen" not in stage
    assert "SPEECH_INVENTORY_SHA256" in stage
    assert "SPEECH_PROFILE_SHA256" in stage
    assert "9b857a3d788a5d6c4ff67278eca4a169028fb2e185ccaf0b01961283f629445b" in stage
    for option in (
        "--voice-worker-image",
        "--livekit-image",
        "--livekit-config-sha256",
        "--speech-inventory-sha256",
        "--speech-profile-sha256",
    ):
        assert option in stage, f"stage deployment is missing {option}"
    for variable in (
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "LIVEKIT_PUBLIC_URL",
        "LIVEKIT_TURN_DOMAIN",
    ):
        assert f"{variable}: ${{{{ secrets.{variable} }}}}" in stage
    for variable in (
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "VOICE_CONTROL_SECRET",
    ):
        assert f"{variable}: ${{{{ secrets.{variable} }}}}" not in stage
        assert f'echo "::add-mask::${variable}"' in stage
    assert "openssl rand -hex" in stage
    assert "voice-worker" in stage


def test_release_readiness_stage_private_pull_uses_ephemeral_token_config() -> None:
    workflow = _workflow_text(READINESS)
    stage = _workflow_job(workflow, "stage-deploy")

    assert _permission_lines(stage) == {"contents: read", "packages: read"}
    assert "DOCKER_CONFIG" in stage
    assert (
        "$RUNNER_TEMP/release-readiness-docker-"
        "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${GITHUB_JOB}"
    ) in stage
    login_action = (
        "docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9"
        " # v3.7.0"
    )
    assert login_action in stage
    assert "registry: ghcr.io" in stage
    assert "username: ${{ github.actor }}" in stage
    assert "password: ${{ github.token }}" in stage
    assert "password: ${{ secrets.GITHUB_TOKEN }}" not in stage
    assert "logout: false" in stage

    cleanup = stage.partition(
        "- name: Remove job-temporary Docker registry credentials"
    )[2]
    assert cleanup
    assert "if: always()" in cleanup
    assert '"$RUNNER_TEMP"/release-readiness-docker-*' in cleanup
    assert 'find "$DOCKER_CONFIG" -mindepth 1 -depth -delete' in cleanup
    assert 'rmdir -- "$DOCKER_CONFIG"' in cleanup
    assert stage.index(login_action) < stage.index("--candidate-image")
    assert stage.index("--candidate-image") < stage.index(
        "- name: Remove job-temporary Docker registry credentials"
    )


def test_release_readiness_protected_coverage_includes_voice_worker_report() -> None:
    workflow = _workflow_text(READINESS)
    decision = _workflow_job(workflow, "protected-decision")

    assert "voice-worker-coverage" in decision
    assert "build/060/coverage-inputs/voice-worker" in decision
    assert re.search(
        r"--backend-python\s+build/060/coverage-inputs/backend/coverage\.xml\s+\\\n"
        r"\s*--voice-worker-python\s+build/060/coverage-inputs/voice-worker/voice-worker\.xml",
        decision,
    )
    coverage_step = decision.partition(
        "- name: Run the protected changed-code coverage gate"
    )[2].partition("- name: Assemble the canonical evidence set")[0]
    assert "--repository-profile deep" in coverage_step
    assert "--windows-python" not in coverage_step
    assert "--ios" not in coverage_step
    assert "--macos" not in coverage_step
    assert "--watchos" not in coverage_step
    assert "--apple " not in decision
    assert "--coverage-mode strict" in decision
    assert (
        "--windows-product-evidence-dir build/060/coverage-inputs/windows"
        in decision
    )
    policy_step = decision.partition("- name: Extract the pinned protected policy")[2]
    policy_step = policy_step.partition("- name: Run the protected changed-code coverage gate")[0]
    assert 'git archive "$RELEASE_TRUSTED_BUILDER_SHA"' in policy_step
    assert "inputs.candidate_sha" not in policy_step


def _assert_release_policy_archive_inventory(workflow: str) -> None:
    decision = _workflow_job(workflow, "protected-decision")
    policy_step = decision.partition("- name: Extract the pinned protected policy")[2]
    policy_step = policy_step.partition(
        "- name: Run the protected changed-code coverage gate"
    )[0]
    commands = re.findall(r"git archive[^\n]*", policy_step.replace("\\\n", " "))
    assert len(commands) == 1
    tokens = shlex.split(commands[0])
    assert tokens[:3] == ["git", "archive", "$RELEASE_TRUSTED_BUILDER_SHA"]
    assert tokens[-2:] == [">", "build/060/protected-policy.tar"]
    assert tokens[3:-2] == [
        "scripts/extract_release_artifact.py",
        "scripts/validate_release_evidence.py",
        "scripts/check_changed_coverage.py",
        "scripts/prepare_release_evidence.py",
        "scripts/apple_coverage_artifacts.py",
        "scripts/native_xccov_domain.py",
        "scripts/merge_xccov_line_coverage.py",
        "scripts/export_xccov_line_coverage.py",
        "scripts/verify_composition.py",
        "specs/074-multirepo-lets-integration/contracts/composition-manifest.schema.json",
        "specs/060-runtime-reliability-hardening/contracts/windows-deployment-profile.schema.json",
        "specs/060-runtime-reliability-hardening/contracts/release-evidence.schema.json",
        "specs/060-runtime-reliability-hardening/contracts/release-trust.schema.json",
    ]
    assert "sha256sum build/060/protected-policy.tar" in policy_step
    assert 'echo "PROTECTED_POLICY_SHA256=$PROTECTED_POLICY_SHA256"' in policy_step


def test_release_policy_archive_includes_exact_normalizer_policy_closure() -> None:
    _assert_release_policy_archive_inventory(_workflow_text(READINESS))


@pytest.mark.parametrize(
    "policy_file",
    [
        "scripts/apple_coverage_artifacts.py",
        "scripts/native_xccov_domain.py",
        "scripts/merge_xccov_line_coverage.py",
        "scripts/export_xccov_line_coverage.py",
        "scripts/verify_composition.py",
        "specs/074-multirepo-lets-integration/contracts/composition-manifest.schema.json",
        "scripts/validate_release_evidence.py",
    ],
)
def test_release_policy_inventory_guard_refuses_omitted_policy_bytes(
    policy_file: str,
) -> None:
    workflow = _workflow_text(READINESS)
    line = f"            {policy_file} \\\n"
    assert line in workflow
    with pytest.raises(AssertionError):
        _assert_release_policy_archive_inventory(workflow.replace(line, "", 1))


@pytest.mark.parametrize(
    "original,replacement",
    [
        ('git archive "$RELEASE_TRUSTED_BUILDER_SHA"', 'git archive "$CANDIDATE_SHA"'),
        (
            "            scripts/apple_coverage_artifacts.py ",
            "            scripts/apple_coverage_artifacts.py scripts/candidate_helper.py ",
        ),
        (
            "            scripts/apple_coverage_artifacts.py ",
            "            scripts/apple_coverage_artifacts.py scripts/apple_coverage_artifacts.py ",
        ),
    ],
)
def test_release_policy_inventory_guard_refuses_unbound_or_extra_bytes(
    original: str, replacement: str
) -> None:
    workflow = _workflow_text(READINESS)
    assert original in workflow
    with pytest.raises(AssertionError):
        _assert_release_policy_archive_inventory(workflow.replace(original, replacement, 1))


def test_apple_coverage_workflows_use_per_file_normalized_exporters() -> None:
    apple_ci = _workflow_text(APPLE_CI)
    assert apple_ci.count("python3 scripts/export_xccov_line_coverage.py") == 4
    assert "xcrun xccov view --archive --json" not in apple_ci
    assert "--platform '${{ matrix.slug }}'" in apple_ci
    assert "--platform watchos" in apple_ci

    readiness = _workflow_text(READINESS)
    assert "python3 scripts/export_xccov_line_coverage.py" not in readiness
    assert "xcrun xccov view --archive --json" not in readiness
    normalizer = _workflow_text(APPLE_NORMALIZER)
    assert _job_ids(normalizer) == ["normalize"]
    assert "runs-on: macos-26" in normalizer
    assert 'XCODE_VERSION: "26.6"' in normalizer
    assert 'XCODE_BUILD: "17F113"' in normalizer
    assert "Build version ${XCODE_BUILD}" in normalizer
    assert SETUP_PYTHON_ACTION in normalizer
    assert 'python-version: "3.11"' in normalizer
    assert '--archive-repo-root "$ARCHIVE_REPO_ROOT"' in normalizer
    assert 'PYTHONNOUSERSITE: "1"' in normalizer
    assert (
        "python3 -I protected-policy/scripts/export_xccov_line_coverage.py"
        in normalizer
    )
    assert (
        "python3 -I protected-policy/scripts/extract_release_artifact.py" in normalizer
    )
    assert "python3 - <<'PY'" not in normalizer
    assert "python3 protected-policy/" not in normalizer
    assert "actions/download-artifact" not in normalizer
    assert "raw Apple artifact id is not unique in the current run" in normalizer
    assert (
        'test ! -e "$raw/coverage/apple-${PRODUCER_PLATFORM}-xccov.json"' in normalizer
    )
    assert "GH_TOKEN: ${{ github.token }}" in _workflow_job(normalizer, "normalize")
    assert normalizer.index("env:\n          GH_TOKEN") > normalizer.index(
        "Fetch the exact current-run raw evidence artifact"
    )
    for platform in ("macos", "ios", "watchos"):
        raw_job = _workflow_job(readiness, f"{platform}-raw-producer")
        final_job = _workflow_job(readiness, f"{platform}-producer")
        assert "export_xccov_line_coverage.py" not in raw_job
        assert "protected-policy" not in raw_job
        assert "xcodebuild" in raw_job
        assert "raw-evidence-upload.outputs.artifact-id" in raw_job
        assert f"raw-apple-evidence-{platform}-" in raw_job
        assert raw_job.index(
            "Record the raw archive checkout root before candidate execution"
        ) < raw_job.index("xcodebuild")
        assert (
            "uses: ./.github/workflows/release-apple-evidence-normalizer.yml"
            in final_job
        )
        assert f"needs: {platform}-raw-producer" in final_job
        assert f"producer_job_id: {platform}-producer" in final_job
        assert "xcodebuild" not in final_job
        assert "export_xccov_line_coverage.py" not in final_job


def test_protected_python_launches_cannot_import_candidate_sitecustomize(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "candidate-sitecustomize-loaded"
    (tmp_path / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['ASTRAL_TEST_SITE_MARKER']).write_text('loaded')\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["ASTRAL_TEST_SITE_MARKER"] = str(marker)
    result = subprocess.run(
        [sys.executable, "-I", "-"],
        cwd=tmp_path,
        env=environment,
        input="import sys\nassert sys.flags.isolated == 1\n",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()

    normalizer = _workflow_job(_workflow_text(APPLE_NORMALIZER), "normalize")
    protected_decision = _workflow_job(_workflow_text(READINESS), "protected-decision")
    for protected_job in (normalizer, protected_decision):
        assert 'PYTHONNOUSERSITE: "1"' in protected_job
        assert "python3 - <<'PY'" not in protected_job
    assert "python3 protected-policy/" not in normalizer
    assert "python3 build/060/protected-policy/" not in protected_decision
    assert re.search(
        r'"python3",\s*\n\s*"-I",\s*\n\s*extractor,', protected_decision
    )


def test_release_trusted_builder_is_a_single_attest_job_with_exact_grants() -> None:
    workflow = _workflow_text(TRUSTED_BUILDER)
    head = _workflow_head(workflow)

    assert re.search(r"(?m)^name: release-trusted-builder$", head)
    assert "workflow_call:" in head
    for trigger in ("workflow_dispatch", "push:", "pull_request", "schedule"):
        assert trigger not in head, f"trusted builder must be call-only, found {trigger}"
    assert _job_ids(workflow) == ["attest"]
    assert _permission_lines(workflow) == {
        "id-token: write",
        "attestations: write",
        "actions: read",
        "contents: read",
    }

    body = _workflow_job(workflow, "attest")
    assert "runs-on: ubuntu-latest" in body
    assert "github.run_id" in body
    assert "/jobs" in body
    assert "/artifacts" in body
    assert 'job.get("status") != "completed"' in body
    assert 'job.get("conclusion") != "success"' in body
    assert 'job_for("stage-deploy")' in body
    assert "stage_outputs_artifact" in body
    assert "stage manifest does not bind the exact immutable outputs artifact" in body
    assert "stage-manifest-" in body
    assert 'artifact.get("expired") is not False' in body
    assert ATTEST_ACTION in workflow
    assert "trusted-manifests" in body
    assert (
        "name: trusted-manifests-"
        "${{ inputs.request_id || github.run_id }}-${{ github.run_attempt }}"
    ) in body
    assert 'f"evidence-{platform}-{suffix}"' in body
    assert 'f"raw-apple-evidence-{platform}-{suffix}"' in body
    assert 'raw_job_id = f"{platform}-raw-producer"' in body
    assert 'job = job_for(f"{platform}-producer")' in body
    assert 'manifest["source_provenance"]' in body
    assert 'f"windows-unsigned-{candidate}-{run_id}-{run_attempt}"' in body
    assert 'job_for("windows-candidate")' in body
    assert 'platform in {"backend", "web"}' in body
    assert '"kind": "oci_manifest"' in body
    assert 'f"oci://{candidate_image}"' in body
    assert '"artifacts": members(' in body
    assert "wrong attempt-scoped input artifact name" in body
    assert "scripts/extract_release_artifact.py" in body
    assert "512 * 1024 * 1024" in body
    assert "unzip" not in body
    assert "trusted_workflow_provenance" in workflow
    guard = body.partition("- name: Refuse authority outside the installed protected workflow commit")[2]
    assert guard
    assert 'actual != expected' in guard
    assert "GITHUB_WORKFLOW_SHA" in guard
    assert guard.index("GITHUB_WORKFLOW_SHA") < guard.index("Reconstruct producer identities")


def test_release_evidence_exception_registrar_is_environment_gated() -> None:
    workflow = _workflow_text(EXCEPTION)
    head = _workflow_head(workflow)

    assert re.search(r"(?m)^name: release-evidence-exception$", head)
    assert "workflow_dispatch:" in head
    for input_name in (
        "action",
        "source_run_id",
        "request_artifact_id",
        "exception_id",
        "resolution_id",
        "candidate_sha",
    ):
        assert f"{input_name}:" in head, f"dispatch input missing: {input_name}"
    assert "approve-exception" in head
    assert "register-resolution" in head

    job_ids = _job_ids(workflow)
    assert "verify-request" in job_ids
    assert "register" in job_ids

    verify = _workflow_job(workflow, "verify-request")
    assert not _write_grants(verify), "verify-request must be read-only"
    assert "evidence_exception_request" in verify

    register = _workflow_job(workflow, "register")
    assert re.search(r"environment:\s*(?:\n\s+name:\s*)?release-evidence-exception", register)
    assert _permission_lines(register) == {
        "contents: write",
        "actions: read",
        "id-token: write",
        "attestations: write",
    }
    assert "requester_login" in workflow
    assert "github.actor" in workflow
    assert "expires_at" in workflow
    assert "release-evidence-debt" in workflow
    assert "debts/" in workflow
    assert "resolutions/" in workflow
    assert "release_evidence_debt" in workflow
    assert "trusted_exception_approval" in workflow
    assert "trusted_debt_resolution" in workflow
    assert ATTEST_ACTION in workflow
    assert "release-evidence-exception-" in register
    assert "apple_first_login_llm" in workflow
    assert "candidate_staging" in workflow


def test_release_windows_signing_identity_surface_matches_the_shipped_client() -> None:
    workflow = _workflow_text(BRIDGE)
    head = _workflow_head(workflow)

    assert BRIDGE.name == "release-windows.yml"
    assert re.search(r"(?m)^name: Release Windows client$", head)

    # Must trigger on tag push — branch push would change the cert SAN
    assert re.search(r'(?m)^on:$', head)
    assert re.search(r'(?m)^  push:$', head)
    assert re.search(r'(?m)^    tags: \["v\*"\]$', head)

    identity = (
        "https://github.com/AstralDeep/AstralDeep/.github/workflows/"
        "release-windows.yml@refs/tags/${{ github.ref_name }}"
    )
    assert identity in workflow, "the in-job self-verify identity drifted"
    assert (
        "--cert-oidc-issuer https://token.actions.githubusercontent.com" in workflow
    )

    integrity = (
        REPO_ROOT
        / "components"
        / "AstralProjection"
        / "windows-client"
        / "astral_client"
        / "integrity.py"
    ).read_text(encoding="utf-8")
    assert (
        '"https://github.com/AstralDeep/AstralDeep'
        '/.github/workflows/release-windows.yml"' in integrity
    ), "components/AstralProjection/windows-client/astral_client/integrity.py no longer pins this workflow"
    assert 'f"{signing_workflow}@refs/tags/{tag}"' in integrity


@bridge_parked
def test_release_windows_bridge_keeps_pinned_identity_with_no_write_authority() -> None:
    workflow = _workflow_text(BRIDGE)
    head = _workflow_head(workflow)

    assert re.search(r"(?m)^name: Release Windows client$", head)
    assert "release-windows-bridge ${{ inputs.tag }}" in head
    assert "readiness-${{ inputs.readiness_run_id }}" in head
    assert "decision-${{ inputs.decision_artifact_id }}" in head
    assert "push:" not in head
    assert "workflow_dispatch:" in head
    for input_name in ("tag", "readiness_run_id", "decision_artifact_id"):
        assert re.search(rf"(?m)^\s*{input_name}:", head)
    assert 'EXPECTED_REF="refs/tags/$TAG"' in workflow
    assert 'test "$GITHUB_REF" = "$EXPECTED_REF"' in workflow
    assert 'test "$GITHUB_SHA" = "$TARGET_SHA"' in workflow
    assert "resolved tag target differs from the workflow tag-ref commit" in workflow
    assert _top_permissions(workflow) == {
        "contents: read",
        "actions: read",
        "attestations: read",
        "id-token: write",
    }
    assert _permission_lines(workflow) == {
        "contents: read",
        "actions: read",
        "attestations: read",
        "id-token: write",
    }
    assert _job_ids(workflow) == ["bridge-sign"]


@bridge_parked
def test_release_windows_bridge_isolates_candidate_checkout_from_signer_runtime() -> None:
    workflow = _workflow_text(BRIDGE)

    assert "path: candidate-source" in workflow
    assert 'git -C candidate-source rev-parse HEAD' in workflow
    assert "isolated candidate checkout differs from the tagged commit" in workflow
    assert "candidate-source/.github/workflows/release-windows.yml" in workflow
    assert "working-directory: candidate-source" not in workflow
    assert re.search(r"(?m)^\s*(?:cd|pushd)\s+candidate-source(?:/|\s|$)", workflow) is None
    assert re.search(r"(?m)^\s*python3\s+(?!-I(?:\s|$))", workflow) is None


@bridge_parked
def test_release_windows_bridge_never_rebuilds_and_never_mutates_releases() -> None:
    workflow = _workflow_text(BRIDGE)
    lower = workflow.lower()

    assert "pyinstaller" not in lower
    assert "astraldeep.spec" not in lower
    assert re.search(r"pip install[^\n]*requirements", workflow) is None
    assert "- name: Build the exe" not in workflow
    assert "trusted-release-decision" in workflow
    assert "RELEASE_TRUSTED_BUILDER_SHA" in workflow
    assert '[[ "$RELEASE_TRUSTED_BUILDER_SHA" =~ ^[0-9a-f]{40}$ ]]' in workflow
    assert (
        '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/release-readiness.yml"'
        in workflow
    )
    assert '--signer-digest "$RELEASE_TRUSTED_BUILDER_SHA"' in workflow
    assert "unzip" not in lower
    assert "head_sha=$TARGET_SHA" not in workflow
    assert "per_page=20" not in workflow
    assert "inputs.readiness_run_id" in workflow
    assert "inputs.decision_artifact_id" in workflow
    assert 'actions/runs/$READINESS_RUN_ID"' in workflow
    assert 'actions/artifacts/$DECISION_ARTIFACT_ID"' in workflow
    assert "artifacts?per_page" not in workflow
    assert "release-readiness-protected" in workflow
    assert 'run.get("run_attempt")' in workflow
    assert 'run.get("head_sha") != os.environ["RELEASE_TRUSTED_BUILDER_SHA"]' in workflow
    assert (
        'str(artifact.get("id")) != os.environ["DECISION_ARTIFACT_ID"]'
        in workflow
    )
    assert "contents/scripts/extract_release_artifact.py?ref=" in workflow
    assert "Accept: application/vnd.github.raw+json" in workflow
    assert "protected extractor API response exceeds its byte bound" in workflow
    assert "pre-existing or symlinked decision path" in workflow
    assert 'python3 -I "$DECISION_EXTRACTOR"' in workflow
    assert "--expected-member trusted-release-decision.json" in workflow
    assert 'python3 -I "$RELEASE_ARTIFACT_EXTRACTOR"' in workflow
    assert "release evidence member digest differs from the signed decision" in workflow
    assert "decision evidence artifact identity is malformed" in workflow
    assert "evidence artifact metadata differs from the signed decision" in workflow
    assert "evidence artifact name is not unique in the exact run" not in workflow
    assert "windows artifact identity is not canonical or decision-run bound" in workflow
    assert 'match.group(1) != os.environ["DECISION_RUN_ID"]' in workflow
    assert "candidate artifact metadata differs from the signed evidence" in workflow
    assert "candidate artifact name is not unique in the exact run" not in workflow
    assert "protected bridge output path is pre-existing or symlinked" in workflow
    assert "for path in build build/060 build/060/bridge" in workflow
    assert "Re-check decision lifetime immediately before OIDC signing" in workflow
    assert "MINIMUM_DECISION_REMAINING_SECONDS" in workflow
    assert "attested decision expiry is not canonical UTC" in workflow
    assert "trusted decision expiry is not canonical UTC" in workflow
    assert "trusted decision has insufficient lifetime for OIDC signing" in workflow
    assert '"decision_run_id": os.environ["DECISION_RUN_ID"]' in workflow
    assert '"decision_run_attempt": int(os.environ["DECISION_RUN_ATTEMPT"])' in workflow
    assert '"decision_artifact_id": os.environ["DECISION_ARTIFACT_ID"]' in workflow
    assert "decision-recorded executable_sha256" in workflow
    assert re.search(r"gh api[^\n]*artifacts", workflow)
    assert "/zip" in workflow
    assert "executable_sha256" in workflow
    assert "sigstore" in lower
    assert "cosign.bundle" in workflow
    assert "token.actions.githubusercontent.com" in workflow
    assert "rebuild_performed" in workflow
    assert "executable_bytes_modified" in workflow
    assert "windows-bridge-signing-" in workflow
    assert "softprops" not in lower
    assert "gh release" not in workflow
    assert "/releases" not in workflow


def test_release_windows_publisher_controller_verifies_decision_read_only() -> None:
    workflow = _workflow_text(CONTROLLER)
    head = _workflow_head(workflow)

    assert re.search(r"(?m)^name: release-windows-publisher-controller$", head)
    assert "workflow_dispatch:" in head
    for input_name in (
        "candidate_sha",
        "release_version",
        "mode",
        "readiness_run_id",
        "decision_artifact_id",
    ):
        assert f"{input_name}:" in head, f"dispatch input missing: {input_name}"
    assert "disposable" in head and "official" in head
    assert "default: disposable" in head
    assert _top_permissions(workflow) == {
        "contents: read",
        "actions: read",
        "attestations: read",
    }

    job_ids = _job_ids(workflow)
    assert "verify-decision" in job_ids
    assert "publish" in job_ids

    verify = _workflow_job(workflow, "verify-decision")
    assert not _write_grants(verify), "verify-decision must be read-only"
    assert "gh attestation verify" in verify
    assert "RELEASE_TRUSTED_BUILDER_SHA" in verify
    assert '[[ "$RELEASE_TRUSTED_BUILDER_SHA" =~ ^[0-9a-f]{40}$ ]]' in verify
    assert (
        '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/release-readiness.yml"'
        in verify
    )
    assert '--signer-digest "$RELEASE_TRUSTED_BUILDER_SHA"' in verify
    assert "unzip" not in verify
    assert "release-readiness-protected.yml" in verify
    assert "one unique trusted decision artifact" not in verify
    assert 'artifact_id != os.environ["DECISION_ARTIFACT_ID"]' in verify
    assert 'actions/artifacts/$DECISION_ARTIFACT_ID"' in verify
    assert "actions/runs/$READINESS_RUN_ID/artifacts" not in verify
    assert "contents/scripts/extract_release_artifact.py?ref=" in verify
    assert "protected extractor API response exceeds its byte bound" in verify
    assert 'python3 -I "$DECISION_EXTRACTOR"' in verify
    assert "--expected-member trusted-release-decision.json" in verify
    assert "decision workflow identity differs from the selected readiness run" in verify
    assert "valid_until" in verify
    assert "bridge_workflow_sha256" in verify

    publish = _workflow_job(workflow, "publish")
    assert "uses: ./.github/workflows/release-windows-publisher.yml" in publish
    assert "secrets: inherit" not in publish
    assert _permission_lines(publish) == {
        "contents: write",
        "actions: write",
        "attestations: read",
        "deployments: read",
    }
    assert "attestations: read" in publish


def test_release_windows_publisher_publishes_draft_only_with_exact_assets() -> None:
    workflow = _workflow_text(PUBLISHER)
    head = _workflow_head(workflow)

    assert re.search(r"(?m)^name: release-windows-publisher$", head)
    assert "workflow_call:" in head
    for input_name in (
        "candidate_sha",
        "release_version",
        "mode",
        "readiness_run_id",
        "decision_artifact_id",
    ):
        assert f"{input_name}:" in head, f"call input missing: {input_name}"

    assert _job_ids(workflow) == ["publish"]
    body = _workflow_job(workflow, "publish")
    assert re.search(r"environment:\s*(?:\n\s+name:\s*)?release-publisher", body)
    assert re.search(
        r"concurrency:\s*\n\s+group:\s*release-windows-publisher\s*\n"
        r"\s+cancel-in-progress:\s*false",
        body,
    )
    assert _permission_lines(body) == {
        "contents: write",
        "actions: write",
        "attestations: read",
        "deployments: read",
    }
    secret_refs = set(re.findall(r"secrets\.([A-Za-z_0-9]+)", body))
    assert secret_refs <= {"GITHUB_TOKEN"}, f"unexpected secrets: {sorted(secret_refs)}"
    assert "GH_TOKEN: ${{ github.token }}" in body
    assert "secrets.GITHUB_TOKEN" not in body
    assert "path: candidate-source" in body
    assert "path: protected-policy" in body
    assert 'test "$GITHUB_WORKFLOW_SHA" = "$RELEASE_TRUSTED_BUILDER_SHA"' in body
    assert "publisher mode must be exactly disposable or official" in body

    assert "gh attestation verify" in body
    assert "RELEASE_TRUSTED_BUILDER_SHA" in body
    assert '[[ "$RELEASE_TRUSTED_BUILDER_SHA" =~ ^[0-9a-f]{40}$ ]]' in body
    assert (
        '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/release-readiness.yml"'
        in body
    )
    assert '--signer-digest "$RELEASE_TRUSTED_BUILDER_SHA"' in body
    assert re.search(r"git/refs", body)
    assert "windows-bridge-signing-" in body
    assert "pyinstaller" not in body.lower()
    for asset in ("AstralDeep.exe", "SHA256SUMS", "cosign.bundle"):
        assert asset in body, f"missing draft asset {asset}"
    assert re.search(r"(?m)\S+  AstralDeep\.exe", body), "SHA256SUMS line format"
    assert re.search(r"draft=true|--draft\b|draft:\s*true", body)
    assert "prerelease" in body
    assert "--clobber" not in body
    assert "softprops" not in workflow.lower()
    assert "assets/" in body
    assert "releases/latest" in body
    assert re.search(r"mode\s*==\s*'official'", body)
    assert re.search(r"mode\s*==\s*'disposable'", body)
    assert "always()" in body
    assert "delete" in body.lower()
    assert "windows_draft_verification_provenance" in body
    assert "windows-draft-provenance" in body
    assert "make_latest_on_publish" in body
    assert "token_broker_policy_sha256" in body
    assert "bridge_workflow_sha256" in body


def test_release_windows_publisher_safely_binds_evidence_and_bridge_archives() -> None:
    body = _workflow_job(_workflow_text(PUBLISHER), "publish")

    assert "unzip" not in body.lower()
    assert "RELEASE_ARTIFACT_EXTRACTOR" in body

    evidence = body.partition(
        "- name: Resolve the matrix-tested executable digest from the evidence set"
    )[2].partition("- name: Re-check collisions and create the tag")[0]
    assert "signed evidence-set artifact member is not exact" in evidence
    assert "evidence artifact metadata differs from the signed decision" in evidence
    assert 'member.get("artifact_name") != "release-evidence"' in evidence
    assert 'member.get("run_attempt") != run_attempt' in evidence
    assert 'str(workflow_run.get("id")) != run_id' in evidence
    assert 'python3 -I "$RELEASE_ARTIFACT_EXTRACTOR"' in evidence
    assert "downloaded evidence set differs from the signed member digest" in evidence
    assert evidence.index("ACTUAL_EVIDENCE_SHA256") < evidence.index(
        "evidence_set = json.loads"
    )

    bridge = body.partition(
        "- name: Wait for the exact tag-ref bridge signing dispatch"
    )[2].partition("- name: Verify the signed bytes with the shipped v0.3.0 policy")[0]
    assert 'run.get("name") != "Release Windows client"' in bridge
    assert 'run.get("path") != ".github/workflows/release-windows.yml"' in bridge
    assert 'run.get("head_branch") != os.environ["TAG"]' in bridge
    assert 'run.get("head_sha") != os.environ["CANDIDATE_SHA"]' in bridge
    assert 'run.get("conclusion") != "success"' in bridge
    assert "bridge run must expose one exact signing artifact" in bridge
    assert 'expected_name = f"windows-bridge-signing-{run_id}-{attempt}"' in bridge
    assert "startswith(\"windows-bridge-signing-\")" not in bridge
    assert 'python3 -I "$RELEASE_ARTIFACT_EXTRACTOR"' in bridge
    for member in (
        "AstralDeep.exe",
        "cosign.bundle",
        "bridge-signing-manifest.json",
    ):
        assert f"--expected-member {member}" in bridge


def test_release_windows_publisher_isolates_candidate_code_and_exact_dispatch() -> None:
    body = _workflow_job(_workflow_text(PUBLISHER), "publish")

    dispatch_index = body.index("actions/workflows/release-windows.yml/dispatches")
    assert body.index("candidate bridge bytes differ from the owner-pinned") < dispatch_index
    assert body.index("bridge bytes differ from the installed protected digest") < dispatch_index
    assert '-F return_run_details=true' in body
    assert '-f "ref=$TAG"' in body
    assert '-f "inputs[tag]=$TAG"' in body
    assert '-f "inputs[readiness_run_id]=$READINESS_RUN_ID"' in body
    assert '-f "inputs[decision_artifact_id]=$DECISION_ARTIFACT_ID"' in body
    assert 'json.loads(data).get("workflow_run_id", "")' in body
    assert 'actions/runs/$BRIDGE_RUN_ID' in body

    assert "pip install --require-hashes" in body
    assert "protected-policy/components/AstralProjection/windows-client/requirements-release.lock.txt" in body
    assert "candidate-source/components/AstralProjection/windows-client/requirements-release.lock.txt" not in body
    assert "protected-policy/scripts/validate_release_evidence.py" in body
    assert "protected-policy/specs/060-runtime-reliability-hardening/" in body
    assert "protected-policy/components/AstralProjection/windows-client/astral_client/integrity.py" in body
    assert 'git -C candidate-source show "$CANDIDATE_SHA:' in body

    for match in re.finditer(r"(?<![A-Za-z0-9_-])(python3?|python)(?=\s)", body):
        invocation = body[match.start() : body.find("\n", match.start())]
        assert re.search(r"\bpython3?\s+-I(?:\s|$)", invocation), invocation


def test_release_windows_publisher_cleanup_is_owned_and_fail_closed() -> None:
    body = _workflow_job(_workflow_text(PUBLISHER), "publish")
    cleanup = body.partition("- name: Force-clean the just-created draft and tag")[2]

    assert "publish-state.json" in cleanup
    assert 'state.get("tag_created") is not True' in cleanup
    assert 'candidate != os.environ["CANDIDATE_SHA"]' in cleanup
    assert 'obj.get("type") != "commit"' in cleanup
    assert 'obj.get("sha") != os.environ["STATE_CANDIDATE_SHA"]' in cleanup
    assert 'release.get("tag_name") != os.environ["STATE_TAG"]' in cleanup
    assert "state-owned release still resolves after deletion" in cleanup
    assert "state-owned tag still resolves after deletion" in cleanup
    assert "cancelled()" in cleanup
    assert "|| true" not in cleanup


def test_release_windows_publisher_binds_exact_environment_approval() -> None:
    body = _workflow_job(_workflow_text(PUBLISHER), "publish")
    approval = body.partition(
        "- name: Verify the exact non-self-approved publisher environment gate"
    )[2].partition("- name: Re-check collisions and create the tag")[0]

    assert "actions/runs/$GITHUB_RUN_ID/approvals" in approval
    assert "publisher approval history is malformed or unbounded" in approval
    assert 'review.get("state") == "approved"' in approval
    assert 'names == ["release-publisher"]' in approval
    assert "publisher run must have one exact approved environment review" in approval
    assert 'reviewer.casefold() == os.environ["REQUESTER_LOGIN"].casefold()' in approval
    assert "deployments?environment=release-publisher" in approval
    assert "sha=$GITHUB_WORKFLOW_SHA&ref=$GITHUB_REF_NAME" in approval
    assert "task=deploy&per_page=100" in approval
    assert 'deployment.get("sha") == os.environ["GITHUB_WORKFLOW_SHA"]' in approval
    assert 'deployment.get("ref") == os.environ["GITHUB_REF_NAME"]' in approval
    assert "publisher run must bind one exact current-run deployment" in approval
    assert re.search(r"per_page=1(?:\D|$)", approval) is None
    assert ".[0]" not in approval
    assert body.index("Verify the exact non-self-approved") < body.index(
        "Re-check collisions and create the tag"
    )


def test_release_windows_publisher_rechecks_decision_before_each_mutation() -> None:
    body = _workflow_job(_workflow_text(PUBLISHER), "publish")

    assert "trusted decision expires too soon to dispatch bridge signing" in body
    assert "trusted decision expired before draft release mutation" in body
    assert "trusted decision expired before official publication mutation" in body
    assert body.index("trusted decision expired before draft release mutation") < body.index(
        'repos/$GITHUB_REPOSITORY/releases"'
    )
    assert body.index("trusted decision expired before official publication mutation") < body.index(
        'gh api -X PATCH "repos/$GITHUB_REPOSITORY/releases/$RELEASE_DB_ID"'
    )


@pytest.mark.parametrize("path", RELEASE_WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_third_party_action_is_sha_pinned_with_version_comment(path: Path) -> None:
    workflow = _workflow_text(path)
    for value in re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*(.+?)\s*$", workflow):
        if value.startswith("./"):
            continue
        assert "app-token" not in value, (
            f"{path.name} must not mint App/installation tokens: {value}"
        )
        assert re.fullmatch(
            r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+@[0-9a-f]{40}\s+# v\d+(?:\.\d+)*",
            value,
        ), f"{path.name} action is not SHA-pinned with a version comment: {value}"


def test_ci_release_tooling_lane_covers_the_new_release_test_files() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    job = _workflow_job(workflow, "release-tooling-tests")
    for test_path in (
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
        "scripts/tests/test_backend_web_gate.py",
        "scripts/tests/test_install_local_components.py",
        "scripts/tests/test_verify_component_ownership.py",
        "scripts/tests/test_verify_composition.py",
        "scripts/tests/test_verify_migration_provenance.py",
        "scripts/tests/test_verify_primitive_coverage.py",
    ):
        assert test_path in job, f"RELEASE_TOOL_TESTS must include {test_path}"
    assert "needs: [backend-image]" in job
    assert "tooling/backend-ci/requirements.lock.txt" in job
    assert "org.opencontainers.image.revision" in job
    assert "coverage report --fail-under=90" in job
    assert "secrets." not in job and "docker.sock" not in job


def test_default_branch_workflow_run_is_the_only_readiness_caller() -> None:
    candidate_workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "release-readiness" not in _job_ids(candidate_workflow)
    assert "secrets: inherit" not in candidate_workflow

    workflow = _workflow_text(PROTECTED_TRIGGER)
    head = _workflow_head(workflow)
    assert "workflow_run:" in head
    assert "workflows: [CI]" in head
    assert "workflow_dispatch:" not in head
    job = _workflow_job(workflow, "release-readiness")
    assert "vars.RELEASE_READINESS_ACTIVE == 'true'" in job
    assert "vars.RELEASE_EPHEMERAL_CREDENTIALS_READY == 'true'" in job
    assert "vars.VOICE_WORKER_CLOSURE_APPROVED == 'true'" in job
    assert "uses: ./.github/workflows/release-readiness.yml" in job
    assert "candidate_sha:" in job
    assert "github.event.workflow_run.head_sha" in job
    assert "base_sha:" in job
    assert "github.event.workflow_run.pull_requests[0].base.sha" in job
    assert "source_run_id: ${{ github.event.workflow_run.id }}" in job
    assert "github.event.workflow_run.path == '.github/workflows/ci.yml'" in job
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in job
    assert "secrets: inherit" not in job


def test_ci_voice_worker_always_qualifies_without_publishing() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    job = _workflow_job(workflow, "voice-worker-test")

    job_header = job.split("    steps:", 1)[0]
    assert "if:" not in job_header
    assert "VOICE_WORKER_CLOSURE_APPROVED" not in job
    assert "succeeding as a no-op" not in job
    assert "if:" not in job
    assert "Dockerfile.voice" in job
    assert "--target runtime" in job
    assert "--target test" in job
    assert "astraldeep-voice-worker.tar" not in job
    assert "docker save" not in job
    assert "voice_agent/tests" in job
    assert "Dockerfile.voice.dockerignore" in job
    assert '$PWD/backend:/app/backend' not in job
    assert "/opt/voice-test-venv/bin/python -m coverage run" in job
    assert "--source=voice_agent" in job
    assert '--omit="*/voice_agent/tests/*"' in job
    assert "-m pytest voice_agent/tests -q" in job
    assert re.search(r"coverage xml\s+\\?\s*-o /coverage/voice-worker\.xml", job)
    assert re.search(r"coverage report\s+\\?\s*--fail-under=90", job)
    assert "ElementTree.parse" in job
    assert 'root.tag == \\"coverage\\"' in job
    assert "statements > 0 and rate >= 0.90" in job
    assert "--iidfile build/065/coverage/voice-runtime-image-id.txt" in job
    assert "--iidfile build/065/coverage/voice-test-image-id.txt" in job
    assert "--junitxml=/coverage/voice-worker-tests.xml" in job
    assert "name: voice-worker-image" not in job
    assert "name: voice-worker-coverage" in job
    assert "continue-on-error" not in job

    gates = _workflow_job(workflow, "gates")
    assert "- voice-worker-test" in gates
    assert "needs.voice-worker-test.result }}' == 'success'" in gates
    assert "needs.component-contract-tests.result }}' == 'success'" in gates
    assert "skipped" not in gates
    assert "exit 1" not in gates
    assert "publish" not in _job_ids(workflow)


def test_ci_has_no_stale_composed_or_client_release_claims() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    head = _workflow_head(workflow)
    assert (
        "types: [opened, synchronize, reopened, ready_for_review, "
        "converted_to_draft]" in head
    )

    job_ids = set(_job_ids(workflow))
    assert job_ids == {
        "backend-image", "backend-boot", "backend-web", "backend-changed-coverage",
        "projection-backend-web", "plane-postgres", "ui-v2-contracts",
        "lint",
        "sdk",
        "release-tooling-tests",
        "component-contract-tests",
        "composition-declarations",
        "voice-worker-test",
        "secret-scan",
        "gates",
    }
    assert not job_ids & {
        "javascript-lint",
        "voice-contract-validator",
        "voice-web-conformance",
        "windows-client",
        "build",
        "test",
        "test-flags-off",
        "coverage-gate",
        "smoke",
        "publish",
    }
    assert "packages: write" not in workflow
    assert "id-token: write" not in workflow
    assert "secrets." not in workflow
    assert "actions/download-artifact" in _workflow_job(workflow, "backend-boot")
    assert "docker save" in _workflow_job(workflow, "backend-image")
    assert "docker push" not in workflow
    assert "name: voice-worker-image" not in workflow


def test_privileged_manual_dispatch_jobs_refuse_candidate_refs() -> None:
    guard = (
        "github.event_name != 'workflow_dispatch' || "
        "github.ref == 'refs/heads/main'"
    )
    workflows = (
        WORKFLOWS / "apple-release.yml",
        WORKFLOWS / "build-windows-candidate.yml",
        EXCEPTION,
        CONTROLLER,
        BRIDGE,
    )
    for path in workflows:
        workflow = _workflow_text(path)
        assert "workflow_dispatch:" in _workflow_head(workflow)
        for job_id in _job_ids(workflow):
            assert guard in _workflow_job(workflow, job_id), (
                f"{path.name}:{job_id} can run from a candidate dispatch ref"
            )

    assert "workflow_dispatch:" not in _workflow_head(_workflow_text(READINESS))


def test_mac_store_resource_repair_uses_exported_package_before_upload() -> None:
    workflow = _workflow_text(WORKFLOWS / "apple-release.yml")
    helper = "components/AstralProjection/apple-clients/Scripts/repair_macos_store_package.py"
    assert workflow.index(f"test -f {helper}") < workflow.index("- name: Require signing material")
    export = workflow.index("- name: Export both archives for the App Store")
    repair = workflow.index(f"python3 {helper}")
    retain = workflow.index('echo "PKG=$PKG" >> "$GITHUB_ENV"')
    assert export < repair < retain
    repair_body = workflow[repair:retain].replace("\\\n", " ")
    command = shlex.split(repair_body.split("\n", 1)[0])
    assert command[2:6] == ["--input", "$PKG", "--output", "$REPAIRED_PKG"]
    assert 'PKG="$REPAIRED_PKG"' in repair_body
    assert "macos-store-signature-receipt.json" in repair_body
    assert "--deep" not in repair_body


def _diagnostic_argv(
    validator: Any,
    evidence_set: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> list[str]:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    for name in ("provenance", "approvals", "resolutions", "attestations"):
        (tmp_path / name).mkdir()
    stage = copy.deepcopy(evidence_set["evidence"][0]["staging_environment"])
    support_image = "registry.invalid/support@sha256:" + "9" * 64
    stage.update(
        request_namespace="astral060-request",
        capability_manifest_sha256="7" * 64,
        service_identity_sha256="8" * 64,
        service_image_references={
            "postgres": support_image,
            "keycloak-postgres": support_image,
            "keycloak": "registry.invalid/keycloak@sha256:" + "6" * 64,
            "livekit": stage["voice_runtime"]["livekit_image_reference"],
            "schema-baseline": "registry.invalid/baseline@sha256:" + "5" * 64,
            "astraldeep": stage["candidate_image_reference"],
            "voice-worker": stage["voice_runtime"][
                "voice_worker_image_reference"
            ],
        },
        livekit_turn_tls={
            "advertised_uri": (
                "turns:turn.stage-060.astraldeep.invalid:443?transport=tcp"
            ),
            "public_port": 443,
            "external_tls": True,
            "terminator_upstream_host": "127.0.0.1",
            "terminator_upstream_port": 15349,
            "livekit_listener_port": 5349,
        },
    )
    stage_document = {"deployment": stage}
    schemas = {
        "evidence-schema.json": validator.load_json_document(
            CONTRACT_ROOT / "release-evidence.schema.json"
        ),
        "trust-schema.json": validator.load_json_document(
            CONTRACT_ROOT / "release-trust.schema.json"
        ),
        "profile-schema.json": validator.load_json_document(
            CONTRACT_ROOT / "windows-deployment-profile.schema.json"
        ),
    }
    original_load = validator.load_json_document

    def load(path: str | Path) -> dict[str, Any]:
        name = Path(path).name
        if name == "stage.json":
            return stage_document
        if name in schemas:
            return schemas[name]
        return original_load(path)

    monkeypatch.setattr(validator, "load_json_document", load)
    monkeypatch.setattr(
        validator,
        "_load_evidence_documents",
        lambda _root, _schema: (evidence_set, [evidence_set]),
    )
    monkeypatch.setattr(validator, "_load_json_directory", lambda _path: [])
    monkeypatch.setattr(
        validator, "_verify_attestation_receipts", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(validator, "bind_report_to_producer", lambda *args, **kwargs: {})
    monkeypatch.setattr(validator, "validate_document", lambda *args, **kwargs: None)
    ledger = validator.LedgerSnapshot(
        repository="AstralDeep/AstralDeep",
        ref="refs/heads/release-evidence-debt",
        commit_sha="d" * 40,
        tree_sha="e" * 40,
        snapshot_sha256="9" * 64,
        paths={},
        records={},
    )
    monkeypatch.setattr(validator, "read_ledger_snapshot", lambda *args, **kwargs: ledger)
    monkeypatch.setattr(
        validator, "validate_exception_history", lambda *args, **kwargs: None
    )
    return [
        "--schema", "evidence-schema.json",
        "--trust-schema", "trust-schema.json",
        "--deployment-profile-schema", "profile-schema.json",
        "--evidence-dir", str(evidence_dir),
        "--base-sha", "b" * 40,
        "--candidate-sha", "a" * 40,
        "--repository", "AstralDeep/AstralDeep",
        "--trusted-provenance-dir", str(tmp_path / "provenance"),
        "--trusted-stage-deploy", str(tmp_path / "stage.json"),
        "--trusted-approvals-dir", str(tmp_path / "approvals"),
        "--trusted-debt-resolutions-dir", str(tmp_path / "resolutions"),
        "--attestation-verification-dir", str(tmp_path / "attestations"),
        "--protected-builder-sha", "f" * 40,
        "--protected-builder-identity", "protected-identity",
        "--protected-policy-sha", "6" * 64,
        "--exception-ledger-repository", "AstralDeep/AstralDeep",
        "--exception-ledger-ref", "refs/heads/release-evidence-debt",
        "--exception-ledger-commit", "d" * 40,
        "--exception-ledger-checkout", str(tmp_path),
        "--now", "2026-07-16T12:00:00Z",
    ]


def test_local_diagnostic_parse_is_deterministic_and_never_authorizes(
    validator: Any,
    contract_examples: Any,
    evidence_examples: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_set = evidence_examples._passing_set(contract_examples)
    argv = _diagnostic_argv(validator, evidence_set, monkeypatch, tmp_path)

    assert validator.main(argv) == 0
    first = capsys.readouterr().out
    assert validator.main(argv) == 0
    second = capsys.readouterr().out
    assert first == second, "local diagnostic output must be deterministic"

    result = json.loads(first)
    assert result["decision"] == "diagnostic_policy_passed"
    assert result["protected_release_authorization"] is False
    assert result["candidate_sha"] == "a" * 40
    assert result["required_targets"] == [
        "backend", "web", "windows", "android", "macos", "ios", "watchos", "docs",
    ]


def test_substituted_local_verdict_cannot_mint_a_trusted_decision(
    validator: Any,
    contract_examples: Any,
    evidence_examples: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_set = evidence_examples._passing_set(contract_examples)
    assert evidence_set["decision"] == "passed"
    argv = _diagnostic_argv(validator, evidence_set, monkeypatch, tmp_path)

    assert validator.main(argv) == 0
    diagnostic = json.loads(capsys.readouterr().out)
    assert diagnostic["decision"] == "diagnostic_policy_passed"
    assert diagnostic["protected_release_authorization"] is False

    decision_path = tmp_path / "protected-decision" / "trusted-release-decision.json"
    protected_argv = [
        *argv,
        "--decision-output", str(decision_path),
        "--protected-workflow-ref",
        "AstralDeep/AstralDeep/.github/workflows/release-trusted-builder.yml" + "@" + "f" * 40,
        "--coverage-percent", "95",
        "--coverage-artifact", str(tmp_path / "coverage-artifact.json"),
        "--evidence-set-artifact", str(tmp_path / "evidence-artifact.json"),
        "--valid-until", "2026-07-16T20:00:00Z",
    ]
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_JOB", raising=False)
    assert validator.main(protected_argv) == 2
    assert "prefetched product artifact" in capsys.readouterr().err
    assert not decision_path.exists()

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_JOB", "release-tooling-tests")
    assert validator.main(protected_argv) == 2
    assert "prefetched product artifact" in capsys.readouterr().err
    assert not decision_path.exists()


def test_self_approved_exception_gains_no_authorization(
    validator: Any, tmp_path: Path
) -> None:
    request_path = FIXTURE_ROOT / "requests/legal/windows-runner-unavailable-a.json"
    request = validator.load_json_document(request_path)
    receipt = validator.load_json_document(
        FIXTURE_ROOT / "receipts/approval-registration-a.json"
    )
    assert receipt["requester_login"] != receipt["reviewer_login"]
    resolver = validator.ArtifactResolver(
        bundle_root=tmp_path,
        resolved={receipt["exception_artifact"]["immutable_reference"]: request_path},
    )
    ledger = validator.LedgerSnapshot(
        repository=receipt["ledger_repository"],
        ref=receipt["ledger_ref"],
        commit_sha=receipt["ledger_commit_sha"],
        tree_sha="a" * 40,
        snapshot_sha256="b" * 64,
        paths={receipt["ledger_entry_path"]: receipt["ledger_entry_sha256"]},
        records={receipt["ledger_entry_path"]: receipt["ledger_entry"]},
    )
    now = datetime(2026, 7, 16, tzinfo=UTC)
    validator.validate_exception_approval(
        request, receipt, now=now, resolver=resolver, ledger=ledger
    )

    self_approved = copy.deepcopy(receipt)
    self_approved["reviewer_login"] = self_approved["requester_login"]
    with pytest.raises(validator.ProvenanceError, match="own request"):
        validator.validate_exception_approval(
            request, self_approved, now=now, resolver=resolver, ledger=ledger
        )


def test_windows_draft_provenance_binds_identical_digests_and_rejects_rebuild(
    validator: Any, tmp_path: Path
) -> None:
    executable = tmp_path / "AstralDeep.exe"
    executable.write_bytes(b"frozen-build-once-executable")
    executable_sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text(f"{executable_sha}  AstralDeep.exe\n", encoding="utf-8")
    bundle = tmp_path / "cosign.bundle"
    bundle.write_bytes(b"detached-synthetic-bundle")
    wrong_checksums = tmp_path / "SHA256SUMS-substituted"
    wrong_checksums.write_text(f"{'0' * 64}  AstralDeep.exe\n", encoding="utf-8")
    refs = {
        "gh://AstralDeep/AstralDeep/releases/10/assets/11": executable,
        "gh://AstralDeep/AstralDeep/releases/10/assets/12": checksums,
        "gh://AstralDeep/AstralDeep/releases/10/assets/13": bundle,
        "gh://AstralDeep/AstralDeep/releases/10/assets/14": wrong_checksums,
    }
    resolver = validator.ArtifactResolver(bundle_root=tmp_path, resolved=refs)
    artifact = {
        "name": "AstralDeep.exe",
        "kind": "windows_exe",
        "immutable_reference": "gh://AstralDeep/AstralDeep/releases/10/assets/11",
        "sha256": executable_sha,
        "build_identity": "windows-build-once-6001",
    }
    decision = {
        "candidate_sha": "a" * 40,
        "release_id": "release-060-a",
        "readiness_evidence_set_id": "99999999-9999-4999-8999-999999999999",
    }
    document = {
        **decision,
        "release_version": "0.4.0",
        "trusted_decision": {"valid_until": "2026-07-17T00:00:00Z"},
        "protected_publisher": {
            "reviewer_login": "release-owner",
            "requester_login": "release-requester",
            "bridge_workflow_sha256": "f" * 64,
        },
        "signing": {"bridge_workflow_sha256": "f" * 64},
        "tested_executable": dict(artifact),
        "draft_executable": dict(artifact),
        "draft_checksum_manifest": {
            "immutable_reference": "gh://AstralDeep/AstralDeep/releases/10/assets/12",
            "sha256": hashlib.sha256(checksums.read_bytes()).hexdigest(),
        },
        "draft_signature_bundle": {
            "immutable_reference": "gh://AstralDeep/AstralDeep/releases/10/assets/13",
            "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        },
        "draft_release": {
            "tag": "v0.4.0",
            "release_name": "v0.4.0",
            "executable_asset_database_id": 11,
            "checksum_asset_database_id": 12,
            "signature_bundle_asset_database_id": 13,
        },
    }
    now = datetime(2026, 7, 16, tzinfo=UTC)
    validator.validate_windows_draft_provenance(
        document, trusted_decision=decision, now=now, resolver=resolver
    )

    mutated = copy.deepcopy(document)
    mutated["draft_executable"]["sha256"] = "0" * 64
    with pytest.raises(validator.PolicyError, match="rebuilt or modified"):
        validator.validate_windows_draft_provenance(
            mutated, trusted_decision=decision, now=now, resolver=resolver
        )

    moved_bridge = copy.deepcopy(document)
    moved_bridge["signing"]["bridge_workflow_sha256"] = "e" * 64
    with pytest.raises(validator.PolicyError, match="bridge bytes differ"):
        validator.validate_windows_draft_provenance(
            moved_bridge, trusted_decision=decision, now=now, resolver=resolver
        )

    substituted = copy.deepcopy(document)
    substituted["draft_checksum_manifest"] = {
        "immutable_reference": "gh://AstralDeep/AstralDeep/releases/10/assets/14",
        "sha256": hashlib.sha256(wrong_checksums.read_bytes()).hexdigest(),
    }
    with pytest.raises(validator.PolicyError, match="does not bind"):
        validator.validate_windows_draft_provenance(
            substituted, trusted_decision=decision, now=now, resolver=resolver
        )

    schema = validator.load_json_document(CONTRACT_ROOT / "release-evidence.schema.json")
    signing_schema = schema["$defs"]["windows_draft_verification_provenance"][
        "properties"
    ]["signing"]
    signing = {
        "signature_mode": "detached_sigstore_bundle",
        "signer_identity": (
            "https://github.com/AstralDeep/AstralDeep/.github/workflows/"
            "release-windows.yml@refs/tags/v0.4.0"
        ),
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "signature_verifier": "astraldeep-v0.3.0-sigstore-identity-policy",
        "bridge_workflow_sha256": "f" * 64,
        "legacy_v0_3_0_verifier_outcome": "passed",
        "verification_outcome": "passed",
        "executable_bytes_modified": False,
        "rebuild_performed": False,
    }
    validator.validate_document(signing, signing_schema, root_schema=schema)
    with pytest.raises(validator.SchemaValidationError, match="rebuild_performed"):
        validator.validate_document(
            dict(signing, rebuild_performed=True), signing_schema, root_schema=schema
        )
    with pytest.raises(
        validator.SchemaValidationError, match="executable_bytes_modified"
    ):
        validator.validate_document(
            dict(signing, executable_bytes_modified=True),
            signing_schema,
            root_schema=schema,
        )


def test_apple_raw_jobs_instrument_before_archiving_and_never_rebuild_afterward() -> (
    None
):
    readiness = _workflow_text(READINESS)
    for platform in ("macos", "ios"):
        job = _workflow_job(readiness, f"{platform}-raw-producer")
        build = job.partition(
            "- name: Build for testing and digest the exact tested app"
        )[2].partition("      - name:")[0]
        assert "-enableCodeCoverage YES" in build
        assert build.index("-enableCodeCoverage YES") < build.index("build-for-testing")
        assert build.index("build-for-testing") < build.index(
            "scripts/apple_coverage_artifacts.py prepare"
        )
        assert build.index("scripts/apple_coverage_artifacts.py prepare") < build.index(
            "ARTIFACT_SHA256="
        )
        assert (
            "find " not in build and "head -n 1" not in build and "ditto" not in build
        )
        after = job.partition("scripts/apple_coverage_artifacts.py prepare")[2]
        assert "build-for-testing" not in after
        assert (
            "test-without-building" in after
            and "scripts/apple_coverage_artifacts.py verify" in after
        )
        assert "Retain raw Apple coverage attempts\n        if: always()" in after
        assert "coverage/raw/" in after
        assert 'test ! -e "$result" && test ! -L "$result"' in after
        assert 'rm -rf "$result"' not in after
        label = "iOS" if platform == "ios" else "macOS"
        staging = job.partition(
            f"- name: Produce {label} release evidence against staging"
        )[2].partition("      - name:")[0]
        for secret in (
            "secrets.ASTRAL_RELEASE_USERNAME",
            "secrets.ASTRAL_RELEASE_PASSWORD",
        ):
            assert secret in staging
            assert secret not in job.replace(staging, "")
        assert "PRODUCER_ONLY_TESTING: AstralAppUITests/ReleaseEvidenceUITests" in job
        assert (
            'test -f "build/060/release-evidence/${PRODUCER_PLATFORM}.json"' in staging
        )
    ios = _workflow_job(readiness, "ios-raw-producer")
    assert "for lane in core unit; do" in ios
    assert "-only-testing:AstralCoreTests test-without-building" in ios
    assert "-only-testing:AstralAppTests test-without-building" in ios
    for selector in (
        "Accessibility060UITests",
        "LLMFirstLoginUITests",
        "VoiceConversationUITests",
        "WorkspacePresentationUITests",
        "WorkspaceActionsUITests",
        "ConversationContinuityUITests/testDeterministicProcessRelaunchRestoresSemanticConversationTwentyTimes",
    ):
        assert "-only-testing:AstralAppUITests/" + selector in ios
    assert (
        "apple-ios-ui.xcresult" in ios
        and "apple-${PRODUCER_PLATFORM}-staging.xcresult" in ios
    )


def _assert_apple_normalizer_python_is_protected(text: str) -> None:
    entries = re.findall(r"\bpython(?:3(?:\.\d+)?)?\s+(\S+)\s+(\S+)", text)
    assert entries
    for isolated, entry in entries:
        assert isolated == "-I"
        assert entry == "-" or (
            entry.startswith("protected-policy/scripts/") and entry.endswith(".py")
        )


@pytest.mark.parametrize("command", [
    "python3 -I scripts/verify_composition.py",
    "python3 -I components/AstralProjection/scripts/export_xccov_line_coverage.py",
    "python3 scripts/verify_composition.py --root .",
    'python3 -I "$CANDIDATE_SCRIPT"',
    "python3 -c 'import candidate'",
])
def test_normalizer_guard_rejects_candidate_and_nonisolated_script_execution(command: str) -> None:
    text = _workflow_text(APPLE_NORMALIZER) + "\n        run: " + command + "\n"
    with pytest.raises(AssertionError):
        _assert_apple_normalizer_python_is_protected(text)


def test_apple_normalizer_executes_only_pinned_policy_and_requires_four_raw_ios_lanes() -> (
    None
):
    text = _workflow_text(APPLE_NORMALIZER)
    _assert_apple_normalizer_python_is_protected(text)
    assert "python3 -I scripts/" not in text and "python3 scripts/" not in text
    assert "components/AstralProjection/scripts/merge_xccov_line_coverage.py" in text
    assert (
        "cmp -s protected-policy/scripts/merge_xccov_line_coverage.py components/AstralProjection/scripts/merge_xccov_line_coverage.py"
        in text
    )
    policy = "protected-policy/specs/074-multirepo-lets-integration/contracts/composition-manifest.schema.json"
    assert (
        f"python3 -I protected-policy/scripts/verify_composition.py --root . --schema {policy}"
        in text
    )
    assert text.index(
        "Check out the owner-reviewed Apple normalization policy"
    ) < text.index("Verify composition using only protected policy")
    for filename in (
        "verify_composition.py",
        "apple_coverage_artifacts.py",
        "native_xccov_domain.py",
        "merge_xccov_line_coverage.py",
        "export_xccov_line_coverage.py",
    ):
        assert (
            "protected-policy/scripts/" + filename
            in text.partition("for policy in ")[2].partition("; do")[0]
        )
    assert "for lane in core unit ui staging; do" in text
    assert "--platform ios --profile release" in text
    for lane in ("core", "unit", "ui", "staging"):
        assert (
            f'--{lane}-input "$final/coverage/lanes/apple-ios-{lane}-xccov.json"'
            in text
        )
    assert '"$raw/coverage/raw/apple-ios-${lane}.xcresult"' in text
    assert (
        "python3 -I protected-policy/scripts/apple_coverage_artifacts.py validate"
        in text
    )
    lane_validation = (
        "python3 -I protected-policy/scripts/apple_coverage_artifacts.py validate-observations"
    )
    assert lane_validation in text
    assert text.index(lane_validation) < text.index(
        "python3 -I protected-policy/scripts/export_xccov_line_coverage.py"
    )
    assert 'elif [[ "$PRODUCER_PLATFORM" == macos ]]; then' in text
    assert 'report="$final/coverage/apple-${PRODUCER_PLATFORM}-xccov.json"' in text
    assert 'test ! -e "$report" && test ! -L "$report"' in text


def _assert_ios_native_domains_are_protected_and_reconstructed(text):
    assert (
        "cmp -s protected-policy/scripts/native_xccov_domain.py components/AstralProjection/scripts/native_xccov_domain.py"
        in text
    )
    policy_list = text.partition("for policy in ")[2].partition("; do")[0]
    assert "protected-policy/scripts/native_xccov_domain.py" in policy_list
    collect = (
        "python3 -I protected-policy/scripts/apple_coverage_artifacts.py native-domain"
    )
    verify = "python3 -I protected-policy/scripts/apple_coverage_artifacts.py verify-native-domains"
    assert text.count(collect) == text.count(verify) == 1
    assert text.index("validate-observations") < text.index(collect)
    assert text.index('test ! -e "$final"') < text.index(collect)
    assert (
        text.index(collect)
        < text.index("python3 -I protected-policy/scripts/merge_xccov_line_coverage.py")
        < text.index(verify)
    )
    assert (
        '--native-domain "$final/coverage/native-domains/apple-ios-${lane}.json"'
        in text
    )
    assert '--repo "$PWD" --platform ios --output "$raw" --report "$report"' in text
    assert (
        'else\n          python3 -I protected-policy/scripts/export_xccov_line_coverage.py --repo "$PWD"'
        in text
    )


def test_ios_native_mapping_policy_is_byte_bound_and_independently_reconstructed():
    _assert_ios_native_domains_are_protected_and_reconstructed(
        _workflow_text(APPLE_NORMALIZER)
    )


@pytest.mark.parametrize(
    "remove",
    [
        "cmp -s protected-policy/scripts/native_xccov_domain.py components/AstralProjection/scripts/native_xccov_domain.py",
        "python3 -I protected-policy/scripts/apple_coverage_artifacts.py verify-native-domains",
        '--native-domain "$final/coverage/native-domains/apple-ios-${lane}.json"',
    ],
)
def test_native_domain_workflow_guard_refuses_candidate_claim_or_omitted_recheck(
    remove,
):
    text = _workflow_text(APPLE_NORMALIZER)
    assert remove in text
    with pytest.raises(AssertionError):
        _assert_ios_native_domains_are_protected_and_reconstructed(
            text.replace(remove, "", 1)
        )
