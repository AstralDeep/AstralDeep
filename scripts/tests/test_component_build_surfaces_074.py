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
    assert "uv_build==0.11.21" in makefile
    assert "-r backend/requirements.txt" in makefile
    assert "composition-preflight:" in makefile
    assert "scripts/verify_composition.py" in makefile
    assert "validate --root . --require-gitlinks" in makefile
    assert "up: composition-preflight" in makefile
    assert "build: composition-preflight" in makefile
    assert "sync-components: composition-preflight" in makefile
    assert "sync: sync-components" in makefile


def test_public_ci_validates_declarations_without_private_bytes_or_image_export() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    build = _workflow_job(workflow, "build")
    publish = _workflow_job(workflow, "publish")
    assert "submodules: false" in build
    assert "--declarations-only --require-gitlinks" in build
    assert "docker/build-push-action" not in build
    assert "docker/setup-buildx-action" not in build
    assert "cache-to:" not in build
    assert "outputs: type=docker" not in build
    assert "name: image" not in build
    assert "submodules: recursive" not in workflow
    for projection_path in (
        "tooling/web-ci",
        "windows-client",
        "android-client",
        "apple-clients",
    ):
        assert f"components/AstralProjection/{projection_path}" in workflow
    assert "working-directory: tooling/web-ci" not in workflow
    assert '$PWD/windows-client' not in workflow
    assert '$PWD/android-client' not in workflow
    assert '$PWD/apple-clients' not in workflow
    gates = _workflow_job(workflow, "gates")
    assert "Composed qualification unavailable" in gates
    assert "exit 1" in gates
    assert "packages: write" not in publish
    assert "docker push" not in publish
    assert "docker/login-action" not in publish
    assert "if: ${{ false }}" in publish
    for job in (
        "javascript-lint",
        "voice-web-conformance",
        "release-tooling-tests",
        "windows-client",
        "test",
        "test-flags-off",
        "coverage-gate",
        "smoke",
    ):
        job_body = _workflow_job(workflow, job)
        assert "if: ${{ false }}" in job_body
    component_tests = _workflow_job(workflow, "component-contract-tests")
    assert "submodules: false" in component_tests
    assert "test_install_local_components.py" in component_tests
    assert "test_component_build_surfaces_074.py" in component_tests
