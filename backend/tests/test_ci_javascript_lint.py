"""Contracts for feature 060's isolated JavaScript CI tooling."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECTION_ROOT = REPO_ROOT / "components" / "AstralProjection"
TOOLING_ROOT = PROJECTION_ROOT / "tooling" / "web-ci"
PACKAGE_PATH = TOOLING_ROOT / "package.json"
LOCK_PATH = TOOLING_ROOT / "package-lock.json"
ESLINT_CONFIG_PATH = TOOLING_ROOT / "eslint.config.mjs"
PLAYWRIGHT_IMAGE_PATH = TOOLING_ROOT / "playwright-image.txt"
RELEASE_RUNNER_PATH = TOOLING_ROOT / "release-runner.mjs"
WORKFLOW_PATH = PROJECTION_ROOT / ".github" / "workflows" / "ci.yml"

if not (TOOLING_ROOT.is_dir() and (REPO_ROOT / ".github").is_dir()):
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )

PLAYWRIGHT_VERSION = "1.61.1"
PLAYWRIGHT_IMAGE_DIGEST = (
    "sha256:5b8f294aff9041b7191c34a4bab3ac270157a28774d4b0660e9743297b697e48"
)
EXPECTED_DEV_DEPENDENCIES = {
    "@eslint/js": "10.0.1",
    "@playwright/test": PLAYWRIGHT_VERSION,
    "eslint": "10.7.0",
    "espree": "11.2.0",
    "globals": "17.7.0",
    "v8-to-istanbul": "9.3.0",
}


def _json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


def _workflow_job(workflow: str, job_name: str) -> str:
    jobs = workflow.partition("\njobs:\n")[2]
    assert jobs, "ci.yml does not define jobs"
    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\n(?P<body>.*?)"
        rf"(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        jobs,
    )
    assert match, f"ci.yml job is missing: {job_name}"
    return match.group("body")


def test_web_ci_manifest_is_private_exact_and_ci_only() -> None:
    package = _json(PACKAGE_PATH)

    assert package["name"] == "@astraldeep/web-ci"
    assert package["private"] is True
    assert package["type"] == "module"
    assert package["packageManager"] == (
        "npm@11.16.0+sha512."
        "03be172fc3b199c7a06433163e459be5b110a6983c1dd6305b7ac10f6b0fa12e"
        "1440755a8df6b1064ab2ccb789df0474919fb9c684e322dc57685ede21752ccb"
    )
    assert package["engines"] == {"node": ">=24 <25"}
    assert package["devDependencies"] == EXPECTED_DEV_DEPENDENCIES
    assert "dependencies" not in package
    assert "optionalDependencies" not in package
    assert "peerDependencies" not in package

    scripts = package["scripts"]
    assert set(scripts) == {
        "browser:contract",
        "browser:release",
        "check:package-manager",
        "check:product-isolation",
        "coverage:node",
        "coverage:union",
        "lint",
        "test:coverage-conversion",
        "test:coverage-conversion:browser",
        "test:coverage-conversion:node",
        "test:coverage-union",
        "test:product-isolation",
    }
    assert "eslint" in scripts["lint"]
    assert "--max-warnings=0" in scripts["lint"]
    assert "backend/webrender/static" in scripts["lint"]
    assert "continuity-contract-060.spec.js" in scripts["browser:contract"]
    assert scripts["browser:release"] == "node release-runner.mjs"
    assert '"tests/release-060.spec.js"' in RELEASE_RUNNER_PATH.read_text(
        encoding="utf-8"
    )
    assert "coverage-conversion.test.mjs" in scripts["test:coverage-conversion"]
    assert (
        "coverage-conversion.browser.test.mjs"
        in scripts["test:coverage-conversion:browser"]
    )
    assert "node-v8-cli.test.mjs" in scripts["test:coverage-conversion:node"]
    assert "coverage-conversion-cli.mjs" in scripts["coverage:node"]
    assert scripts["coverage:union"] == "node coverage-union-cli.mjs"
    assert "coverage-union.test.mjs" in scripts["test:coverage-union"]
    assert "coverage-union-cli.test.mjs" in scripts["test:coverage-union"]
    assert "npm 11.16.0" in scripts["check:package-manager"]
    assert scripts["check:product-isolation"] == "node product-isolation.mjs"


def test_package_lock_exactly_matches_manifest_and_pins_transitives() -> None:
    package = _json(PACKAGE_PATH)
    lock = _json(LOCK_PATH)

    assert lock["name"] == package["name"]
    assert lock["version"] == package["version"]
    assert lock["lockfileVersion"] == 3
    assert lock["requires"] is True
    root = lock["packages"][""]
    assert root["name"] == package["name"]
    assert root["version"] == package["version"]
    assert root["devDependencies"] == package["devDependencies"]
    assert root["engines"] == package["engines"]

    packages = lock["packages"]
    for dependency, version in EXPECTED_DEV_DEPENDENCIES.items():
        entry = packages[f"node_modules/{dependency}"]
        assert entry["version"] == version
        assert re.fullmatch(r"sha512-[A-Za-z0-9+/=]+", entry["integrity"])

    assert packages["node_modules/playwright"]["version"] == PLAYWRIGHT_VERSION
    assert packages["node_modules/playwright-core"]["version"] == PLAYWRIGHT_VERSION
    for path, entry in packages.items():
        if not path or entry.get("link"):
            continue
        assert "version" in entry, f"unversioned lock entry: {path}"
        assert re.fullmatch(r"sha512-[A-Za-z0-9+/=]+", entry.get("integrity", "")), (
            f"lock entry lacks registry integrity: {path}"
        )


def test_playwright_image_is_official_digest_pinned_and_version_matched() -> None:
    image = PLAYWRIGHT_IMAGE_PATH.read_text(encoding="utf-8")
    assert image.endswith("\n")
    assert image.count("\n") == 1
    assert image.strip() == (
        f"mcr.microsoft.com/playwright:v{PLAYWRIGHT_VERSION}-noble@"
        f"{PLAYWRIGHT_IMAGE_DIGEST}"
    )


def test_eslint_flat_config_covers_maintained_web_js_and_excludes_vendor() -> None:
    config = ESLINT_CONFIG_PATH.read_text(encoding="utf-8")

    assert 'from "@eslint/js"' in config
    assert 'from "globals"' in config
    assert "js.configs.recommended" in config
    assert "globals.browser" in config
    assert "backend/webrender/static/**/*.js" in config
    assert "tooling/web-ci/**/*.mjs" in config
    assert "backend/webrender/static/vendor/**" in config
    assert "backend/webrender/static/**/*.min.js" in config
    assert 'reportUnusedDisableDirectives: "error"' in config
    assert '"no-empty": ["error", { allowEmptyCatch: true }]' in config
    assert '"no-unused-vars": [' in config
    assert 'Plotly: "readonly"' in config
    assert "globals.node" in config


def test_projection_ci_owns_an_active_lock_pinned_web_gate() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    job = _workflow_job(workflow, "web")

    assert "if: ${{ false }}" not in job
    assert "actions/setup-node@" in job
    assert re.search(r'node-version:\s*["\']?24["\']?', job)
    assert re.search(r"cache:\s*[\"']?npm[\"']?", job)
    assert "cache-dependency-path: tooling/web-ci/package-lock.json" in job

    version = job.index("corepack npm --version")
    assert '"11.16.0"' in job or "'11.16.0'" in job
    install = job.index("corepack npm ci --ignore-scripts")
    manager = job.index("corepack npm run check:package-manager")
    isolation = job.index("corepack npm run check:product-isolation")
    lint = job.index("corepack npm run lint")
    assert version < install < manager < isolation < lint
    assert job.count("working-directory: tooling/web-ci") >= 2

    required = _workflow_job(workflow, "required")
    assert "web" in required
    assert "needs.web.result }}' == 'success'" in required


def test_web_ci_packages_cannot_enter_product_manifests_or_image() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "components/AstralProjection/tooling/web-ci" not in dockerfile
    assert re.search(r"(?mi)^\s*COPY\s+\.\s", dockerfile) is None

    tracked = subprocess.run(
        ["git", "ls-files", "--", "*package.json"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    package_manifests = {
        line
        for line in tracked.stdout.splitlines()
        if line and (REPO_ROOT / line).is_file()
    }
    assert package_manifests == set()

    projection_tracked = subprocess.run(
        ["git", "ls-files", "--", "tooling/web-ci/package.json"],
        cwd=PROJECTION_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert projection_tracked.stdout.splitlines() == ["tooling/web-ci/package.json"]

    forbidden = tuple(EXPECTED_DEV_DEPENDENCIES)
    for relative in (
        "backend/requirements.txt",
        "components/AstralProjection/windows-client/requirements.txt",
    ):
        contents = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert not any(name in contents for name in forbidden)
