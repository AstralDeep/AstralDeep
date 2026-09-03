"""Static integration guards for feature-074 build/bootstrap/CI surfaces."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _workflow_job(workflow: str, job: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert match is not None, f"missing workflow job: {job}"
    return match.group("body")


def test_docker_builds_locked_component_wheels_before_backend_copy() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.11-slim AS component-builder" in dockerfile
    assert "scripts/install_local_components.py build" in dockerfile
    assert "scripts/install_local_components.py install" in dockerfile
    assert "scripts/install_local_components.py verify" in dockerfile
    assert "--no-index" not in dockerfile  # enforced centrally by the installer
    assert "astral-component-wheels.lock.json" in dockerfile
    assert "COPY components/AstralProjection/backend/webrender/" in dockerfile
    assert "COPY components/AstralProjection/contracts/" in dockerfile
    assert "COPY components/AstralProjection/src/astralprojection/" in dockerfile
    assert "COPY components/ ./components/" not in dockerfile
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
        local = tomllib.load(stream)["tool"]["astraldeep"]["local-components"]
    for key in local["install-order"]:
        component = local[key]
        for build_input in component["build-inputs"]:
            assert f"{component['path']}/{build_input}" in dockerfile
    assert dockerfile.index("install_local_components.py build") < dockerfile.index(
        "COPY backend/requirements.txt"
    )
    assert dockerfile.index("install_local_components.py verify") < dockerfile.index(
        "COPY backend/ ./backend/"
    )


def test_backend_resolver_has_no_first_party_component_fallback() -> None:
    requirements = (REPOSITORY_ROOT / "backend/requirements.txt").read_text(
        encoding="utf-8"
    )
    active = [
        line.strip().lower()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    forbidden = ("astralprojection", "astralplane", "astralprims", "lets-agent")
    assert all(not line.startswith(forbidden) for line in active)


def test_make_bootstrap_and_lifecycle_require_full_composition_preflight() -> None:
    makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "bootstrap:" in makefile
    assert "git submodule sync --recursive" in makefile
    assert "git submodule update --init --recursive" in makefile
    assert "setuptools==80.9.0" in makefile
    assert "hatchling==1.27.0" in makefile
    assert "uv_build==0.12.3" in makefile
    assert "-r backend/requirements.txt" in makefile
    assert "composition-preflight:" in makefile
    assert "scripts/verify_composition.py" in makefile
    assert "validate --root . --require-gitlinks" in makefile
    assert "up: composition-preflight" in makefile
    assert "build: composition-preflight" in makefile
    assert "sync-components: composition-preflight" in makefile
    assert "sync: sync-components" in makefile


def test_public_ci_contains_only_repository_owned_qualification() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    job_ids = set(
        re.findall(
            r"(?m)^  ([A-Za-z0-9_-]+):\s*$",
            workflow.partition("\njobs:\n")[2],
        )
    )
    assert job_ids == {
        "lint",
        "release-tooling-tests",
        "component-contract-tests",
        "composition-declarations",
        "voice-worker-test",
        "secret-scan",
        "gates",
    }
    assert not (REPOSITORY_ROOT / ".github/workflows/android-ci.yml").exists()
    assert not (REPOSITORY_ROOT / ".github/workflows/apple-ci.yml").exists()
    for stale in (
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
    ):
        assert stale not in job_ids

    composition = _workflow_job(workflow, "composition-declarations")
    assert "submodules: false" in composition
    assert 'test "${#COMPONENT_STATUS[@]}" -eq 4' in composition
    assert "--declarations-only --require-gitlinks" in composition
    assert "submodules: recursive" not in workflow
    assert "components/AstralProjection/" not in workflow

    gates = _workflow_job(workflow, "gates")
    assert "if: always()" in gates
    for owner_job in job_ids - {"gates"}:
        assert f"- {owner_job}" in gates
        assert f"needs.{owner_job}.result }}}}' == 'success'" in gates
    assert (
        "Full private composition remains a required local Feature 074 "
        "qualification."
    ) in gates
    assert "Composed qualification unavailable" not in gates
    assert "exit 1" not in gates

    assert "packages: write" not in workflow
    assert "id-token: write" not in workflow
    assert "secrets." not in workflow
    assert "actions/download-artifact" not in workflow
    assert "docker save" not in workflow
    assert "docker push" not in workflow
    assert "docker/login-action" not in workflow
    assert "name: image" not in workflow
    assert "name: voice-worker-image" not in workflow


    external_actions = {
        value.split(" #", 1)[0]
        for value in re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*(.+?)\s*$", workflow)
        if not value.startswith("./")
    }
    assert external_actions == {
        "actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd",
        "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    }
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", value) for value in external_actions)

    component_tests = _workflow_job(workflow, "component-contract-tests")
    assert "submodules: false" in component_tests
    for test_path in (
        "scripts/tests/test_install_local_components.py",
        "scripts/tests/test_verify_component_ownership.py",
        "scripts/tests/test_verify_composition.py",
        "scripts/tests/test_verify_migration_provenance.py",
        "scripts/tests/test_verify_primitive_coverage.py",
    ):
        assert test_path in component_tests
    deselections = set(re.findall(r"--deselect=([^\s\\]+)", component_tests))
    assert deselections == {
        "scripts/tests/test_install_local_components.py::test_real_initialized_sources_match_the_declarations",
        "scripts/tests/test_verify_composition.py::test_current_composition_has_exact_pins_canonical_urls_and_contracts",
        "scripts/tests/test_verify_composition.py::test_real_checkout_verification_executes_only_local_git_commands",
    }
    assert " -k " not in component_tests
    assert "--ignore" not in component_tests


def test_store_and_signing_workflows_require_explicit_release_events() -> None:
    """Merging the composition must never publish a client implicitly."""

    apple = (REPOSITORY_ROOT / ".github/workflows/apple-release.yml").read_text(
        encoding="utf-8"
    )
    apple_triggers = apple.partition("\npermissions:\n")[0]
    assert 'tags: ["apple-v*"]' in apple_triggers
    assert "workflow_dispatch:" in apple_triggers
    assert "branches:" not in apple_triggers
    assert "pull_request:" not in apple_triggers

    windows = (REPOSITORY_ROOT / ".github/workflows/release-windows.yml").read_text(
        encoding="utf-8"
    )
    windows_triggers = windows.partition("\npermissions:\n")[0]
    assert 'tags: ["v*"]' in windows_triggers
    assert "workflow_dispatch:" in windows_triggers
    assert "branches:" not in windows_triggers
    assert "pull_request:" not in windows_triggers


def test_publish_image_workflow_publishes_only_after_green_main_ci() -> None:
    """The composed image reaches GHCR only from a green push run on main."""
    workflow = (REPOSITORY_ROOT / ".github/workflows/publish-image.yml").read_text(
        encoding="utf-8"
    )
    job_ids = set(
        re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", workflow.partition("\njobs:\n")[2])
    )
    assert job_ids == {"publish"}

    trigger = workflow.partition("\non:\n")[2].partition("\npermissions:")[0]
    assert "workflow_run:" in trigger
    assert "workflows: [CI]" in trigger
    assert "types: [completed]" in trigger
    assert "branches: [main]" in trigger
    assert "push:" not in trigger
    assert "pull_request" not in trigger
    assert "workflow_dispatch" not in trigger

    publish = _workflow_job(workflow, "publish")
    assert "github.event.workflow_run.conclusion == 'success'" in publish
    assert "github.event.workflow_run.event == 'push'" in publish
    assert "github.event.workflow_run.head_branch == 'main'" in publish
    assert (
        "github.event.workflow_run.head_repository.full_name == github.repository"
        in publish
    )
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in publish
    assert "submodules: recursive" in publish
    assert "packages: write" in publish
    assert "python scripts/verify_composition.py --root ." in publish
    assert (
        "python scripts/install_local_components.py validate --root . --require-gitlinks"
        in publish
    )
    assert "sha-${SHA}" in publish
    assert ":latest" in publish
    assert "docker push" in publish
    assert "secrets.GITHUB_TOKEN" in publish
    assert "secrets." not in publish.replace("secrets.GITHUB_TOKEN", "")
    assert "app-token" not in workflow

    external_actions = {
        value
        for value in re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*(\S+)", workflow)
        if not value.startswith("./")
    }
    assert external_actions == {
        "actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd",
        "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
    }


def test_android_release_workflow_is_manual_pinned_and_composition_owned() -> None:
    """The Android store lane is dispatch-only and builds the exact submodule pin."""
    workflow = (REPOSITORY_ROOT / ".github/workflows/android-release.yml").read_text(
        encoding="utf-8"
    )
    triggers = workflow.partition("\npermissions:\n")[0]
    assert "workflow_dispatch:" in triggers
    assert "push:" not in triggers
    assert "branches:" not in triggers
    assert "pull_request" not in triggers
    assert "schedule:" not in triggers
    assert "- none" in triggers
    assert "default: none" in triggers
    assert "default: draft" in triggers

    assert (
        'EXPECTED_UPLOAD_CERT_SHA256: "56:B9:C6:4F:88:49:1E:88:76:DA:F2:E7:AB:84:99:'
        'F6:E2:28:10:AE:87:0E:0F:43:BF:AE:E9:F7:D0:D4:96:7B"'
    ) in workflow
    assert "PLAY_PACKAGE_NAME: com.personalailabs.astraldeep" in workflow
    assert "working-directory: components/AstralProjection/android-client" in workflow

    job = _workflow_job(workflow, "release-bundle")
    assert (
        "if: ${{ (github.event_name != 'workflow_dispatch' || "
        "github.ref == 'refs/heads/main') }}"
    ) in job
    assert "submodules: recursive" in job
    assert "keytool -list -v" in job
    assert 'test "$actual" = "$EXPECTED_UPLOAD_CERT_SHA256"' in job
    assert 'jarsigner -verify "$AAB" | tee' in job
    assert "grep -q '^jar verified'" in job
    assert "keytool -printcert -jarfile" in job
    assert ":app:bundleRelease" in job
    assert "git check-ignore -q keystore.properties" in job
    assert 'rm -f keystore.properties "$RUNNER_TEMP/upload-keystore.jks"' in job
    assert "if: ${{ inputs.play_track != 'none' }}" in job
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", workflow)) == {
        "ANDROID_UPLOAD_KEYSTORE_BASE64",
        "ANDROID_UPLOAD_KEYSTORE_PASSWORD",
        "ANDROID_UPLOAD_KEY_ALIAS",
        "ANDROID_UPLOAD_KEY_PASSWORD",
        "PLAY_SERVICE_ACCOUNT_JSON",
    }
    assert "app-token" not in workflow
    for value in re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*(.+?)\s*$", workflow):
        assert re.fullmatch(
            r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+@[0-9a-f]{40}\s+# v\d+(?:\.\d+)*",
            value,
        ), value
