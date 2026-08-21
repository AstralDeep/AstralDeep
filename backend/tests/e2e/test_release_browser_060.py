"""Pinned real-browser release-lane contracts and opt-in staging orchestration."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
PROJECTION_ROOT = REPO_ROOT / "components" / "AstralProjection"
TOOL_ROOT = PROJECTION_ROOT / "tooling" / "web-ci"
if not (TOOL_ROOT.is_dir() and (REPO_ROOT / "scripts").is_dir()):
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )
PACKAGE = TOOL_ROOT / "package.json"
LOCK = TOOL_ROOT / "package-lock.json"
IMAGE = TOOL_ROOT / "playwright-image.txt"
RUNNER = TOOL_ROOT / "release-runner.mjs"
RELEASE_SPEC = TOOL_ROOT / "tests" / "release-060.spec.js"
CONTRACT_SPEC = TOOL_ROOT / "tests" / "continuity-contract-060.spec.js"
COVERAGE_PRODUCER = TOOL_ROOT / "coverage-conversion.mjs"
COVERAGE_UNION = TOOL_ROOT / "coverage-union.mjs"
EVIDENCE_SCHEMA = (
    REPO_ROOT
    / "specs/060-runtime-reliability-hardening/contracts/release-evidence.schema.json"
)
VALIDATOR = REPO_ROOT / "scripts" / "validate_release_evidence.py"
COVERAGE_GATE = REPO_ROOT / "scripts" / "check_changed_coverage.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_validator() -> Any:
    return _load_module("release_validator_060_e2e", VALIDATOR)


def test_playwright_package_lock_core_and_image_versions_are_identical() -> None:
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    image = IMAGE.read_text(encoding="utf-8").strip()
    version = package["devDependencies"]["@playwright/test"]
    assert lock["packages"]["node_modules/@playwright/test"]["version"] == version
    assert lock["packages"]["node_modules/playwright-core"]["version"] == version
    assert f"playwright:v{version}-" in image
    assert re.fullmatch(r"mcr\.microsoft\.com/playwright:v[0-9.]+-noble@sha256:[0-9a-f]{64}", image)
    assert package["packageManager"].startswith("npm@11.16.0+")
    assert package["engines"] == {"node": ">=24 <25"}


def test_release_runner_is_container_only_and_fail_closed() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    assert package["scripts"]["browser:release"] == "node release-runner.mjs"
    assert package["scripts"]["browser:contract"].endswith(
        "tests/continuity-contract-060.spec.js"
    )
    for required in (
        'existsSync("/ms-playwright")',
        'process.platform !== "linux"',
        "playwright-image.txt",
        "package-lock.json",
        "npm_config_user_agent",
        "ASTRAL_RELEASE_USERNAME",
        "ASTRAL_RELEASE_PASSWORD",
        "ASTRAL_RELEASE_STAGING_FILE",
        "coverage-istanbul-output",
        "ASTRAL_RELEASE_COVERAGE_ISTANBUL_OUTPUT",
    ):
        assert required in source
    assert "playwright test" not in package["scripts"]["browser:release"]
    assert "chromium.launchExecutablePath" not in source
    assert "executablePath" not in source


def test_release_spec_uses_real_auth_transport_and_candidate_ui() -> None:
    source = RELEASE_SPEC.read_text(encoding="utf-8")
    for forbidden in (
        "page.route(",
        "route.fulfill(",
        "FakeWebSocket",
        "storageState",
        "addCookies(",
        "__ASTRAL_TOKEN__",
        "alg: \"none\"",
        "addScriptTag",
    ):
        assert forbidden not in source
    for required in (
        'input[name="username"]',
        'input[name="password"]',
        "page.coverage.startJSCoverage",
        "page.coverage.stopJSCoverage",
        "conversation_snapshot",
        "seedPriorPrincipalDecoy",
        "verifyCurrentSessionOwnsWebSocket",
        "sharedTabStaleTokenRejected",
        "websocketPrincipalMatchesCookieSession",
        "operation_status",
        "agent_lifecycle",
        "runResumeTrials",
        "for (let trial = 0; trial < 20; trial += 1)",
        # T108 quantitative floors and screenshot-grade raw evidence.
        "resume_success_rate",
        "resume_latency_max_ms",
        "unnamed_visible_controls",
        "authoring_operations_failed",
        "credential_storage_leaks",
        '"screenshot"',
        "bundle://web-raw/",
    ):
        assert required in source
    assert CONTRACT_SPEC.is_file(), "the synthetic reducer suite must remain non-qualifying"


def test_release_lane_wires_the_lock_pinned_coverage_producer() -> None:
    """The browser lane must emit exactly the envelope the coverage gate parses."""

    spec_source = RELEASE_SPEC.read_text(encoding="utf-8")
    assert 'from "../coverage-conversion.mjs"' in spec_source
    assert "convertPlaywrightV8Coverage" in spec_source
    assert "ASTRAL_RELEASE_COVERAGE_OUTPUT" in spec_source
    assert "ASTRAL_RELEASE_COVERAGE_ISTANBUL_OUTPUT" in spec_source
    # Executable-syntax filtering rides the pinned producer, never a re-write.
    assert "v8ToIstanbul" not in spec_source
    assert "espree" not in spec_source
    assert "backend/webrender/static/client.js" in spec_source

    runner_source = RUNNER.read_text(encoding="utf-8")
    assert "web-istanbul.json" in runner_source

    producer_source = COVERAGE_PRODUCER.read_text(encoding="utf-8")
    assert "export const BROWSER_COVERAGE_PRODUCER" in producer_source
    assert 'producer: "astralprojection-browser-v8-executable-lines"' in producer_source
    assert 'coverage_lane: "browser-v8"' in producer_source

    union_source = COVERAGE_UNION.read_text(encoding="utf-8")
    match = re.search(
        r"export const UNION_COVERAGE_PRODUCER = Object\.freeze\(\{(?P<body>.*?)\}\);",
        union_source,
        flags=re.DOTALL,
    )
    assert match, "coverage-union.mjs no longer exports UNION_COVERAGE_PRODUCER"
    body = match.group("body")
    gate = _load_module("coverage_gate_060_e2e", COVERAGE_GATE)
    assert gate.JAVASCRIPT_REPORT_KEYS == set(gate.JAVASCRIPT_REPORT_IDENTITY) | {
        "coverage"
    }
    for key, expected in gate.JAVASCRIPT_REPORT_IDENTITY.items():
        if isinstance(expected, str):
            assert f'{key}: "{expected}"' in body, f"producer field drifted: {key}"
        else:
            assert re.search(rf"{key}:\s*{expected}\b", body), (
                f"producer field drifted: {key}"
            )


def test_release_report_shape_is_validated_by_the_production_schema_engine() -> None:
    # The browser producer writes this exact top-level shape; schema validation
    # remains in Python so the JavaScript lane cannot declare itself trusted.
    source = RELEASE_SPEC.read_text(encoding="utf-8")
    for field in (
        "document_type",
        "evidence_id",
        "candidate_sha",
        "release_id",
        "release_version",
        "staging_environment",
        "unavailability_observation",
        "checks",
    ):
        assert re.search(rf"\b{field}\s*:", source)
    validator = _load_validator()
    schema = validator.load_json_document(EVIDENCE_SCHEMA)
    validator.validate_schema_document(schema)


def test_real_browser_release_lane_against_trusted_staging(tmp_path: Path) -> None:
    """Run only when the protected producer explicitly opts into live staging."""

    if os.environ.get("ASTRAL_RELEASE_E2E") != "true":
        pytest.skip("set ASTRAL_RELEASE_E2E=true only on the trusted staging producer")
    required = (
        "ASTRAL_PLAYWRIGHT_IMAGE",
        "ASTRAL_RELEASE_CANDIDATE_SHA",
        "ASTRAL_RELEASE_ID",
        "ASTRAL_RELEASE_LIFECYCLE_AGENT_ID",
        "ASTRAL_RELEASE_LIFECYCLE_STATES",
        "ASTRAL_RELEASE_PASSWORD",
        "ASTRAL_RELEASE_STAGING_FILE",
        "ASTRAL_RELEASE_USERNAME",
        "ASTRAL_RELEASE_VERSION",
        "ASTRAL_RUNNER_ENVIRONMENT",
        "GITHUB_JOB",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_RUN_ID",
        "GITHUB_WORKFLOW",
        "RUNNER_ARCH",
        "RUNNER_NAME",
        "RUNNER_OS",
        "STAGING_URL",
    )
    missing = [name for name in required if not os.environ.get(name)]
    assert not missing, f"trusted browser environment is incomplete: {missing}"
    pinned = IMAGE.read_text(encoding="utf-8").strip()
    assert os.environ["ASTRAL_PLAYWRIGHT_IMAGE"] == pinned
    output = tmp_path / "web.json"
    coverage = tmp_path / "web-v8.json"
    environment_flags = [flag for name in required for flag in ("-e", name)]
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{REPO_ROOT}:/work",
            "-v",
            f"{tmp_path}:/evidence",
            "-w",
            "/work/components/AstralProjection/tooling/web-ci",
            *environment_flags,
            pinned,
            "sh",
            "-lc",
            (
                "set -eu\n"
                "NODE_V8_DIRECTORY=/tmp/astral-node-v8\n"
                "NODE_INTERIM=/tmp/astral-node-interim.json\n"
                "NODE_COVERAGE=/tmp/astral-node.json\n"
                "BROWSER_COVERAGE=/tmp/astral-browser.json\n"
                'test ! -e "$NODE_V8_DIRECTORY"\n'
                'mkdir "$NODE_V8_DIRECTORY"\n'
                'test "$(corepack npm --version)" = "11.16.0"\n'
                "corepack npm ci --ignore-scripts\n"
                "corepack npm run check:package-manager\n"
                'export NODE_V8_COVERAGE="$NODE_V8_DIRECTORY"\n'
                "corepack npm run check:product-isolation\n"
                "corepack npm run lint\n"
                "corepack npm run test:coverage-conversion\n"
                "corepack npm run test:coverage-conversion:node\n"
                "corepack npm run test:coverage-union\n"
                "corepack npm run test:coverage-conversion:browser\n"
                "corepack npm run browser:release -- "
                '--base-url "$STAGING_URL" '
                '--candidate-sha "$ASTRAL_RELEASE_CANDIDATE_SHA" '
                "--output /evidence/web.json "
                "--coverage-output /evidence/web-v8.json "
                '--coverage-istanbul-output "$BROWSER_COVERAGE"\n'
                "corepack npm run coverage:node -- "
                '--node-v8-directory "$NODE_V8_DIRECTORY" '
                '--repo-root ../.. --output "$NODE_INTERIM"\n'
                "unset NODE_V8_COVERAGE\n"
                "corepack npm run coverage:node -- "
                '--node-v8-directory "$NODE_V8_DIRECTORY" '
                '--repo-root ../.. --output "$NODE_COVERAGE"\n'
                "corepack npm run coverage:union -- "
                '--node "$NODE_COVERAGE" --browser "$BROWSER_COVERAGE" '
                "--repo-root ../.. --output /evidence/web-istanbul.json"
            ),
        ],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20 * 60,
    )
    assert completed.returncode == 0, completed.stdout[-8000:]
    validator = _load_validator()
    report = validator.load_json_document(output)
    schema = validator.load_json_document(EVIDENCE_SCHEMA)
    validator.validate_document(report, schema)
    assert report["candidate_sha"] == os.environ["ASTRAL_RELEASE_CANDIDATE_SHA"]
    assert report["outcome"] == "passed"
    assert coverage.is_file() and coverage.stat().st_size > 0

    # All six client checks passed with policy-canonical quantitative floors.
    checks = {check["id"]: check for check in report["checks"]}
    assert set(checks) == {
        "sign_in",
        "rendered_chat",
        "reconnect_resume",
        "agent_lifecycle",
        "accessibility_semantics",
        "personal_agent",
    }
    assert all(check["outcome"] == "passed" for check in checks.values())
    for check in checks.values():
        validator._validate_measurements(check)

    # Every raw evidence reference is bundle-bound and re-hashes exactly.
    for check in checks.values():
        assert check["evidence_artifacts"], f"{check['id']} lacks raw evidence"
        for artifact in check["evidence_artifacts"]:
            reference = artifact["immutable_reference"]
            assert reference.startswith("bundle://web-raw/")
            member = tmp_path / "web-raw" / reference.rsplit("/", 1)[1]
            digest = hashlib.sha256(member.read_bytes()).hexdigest()
            assert digest == artifact["sha256"], f"raw bytes drifted: {reference}"

    # The lock-pinned producer converted the raw V8 into the exact envelope
    # scripts/check_changed_coverage.py parses for the changed-line gate.
    gate = _load_module("coverage_gate_060_live", COVERAGE_GATE)
    istanbul = validator.load_json_document(tmp_path / "web-istanbul.json")
    assert set(istanbul) == gate.JAVASCRIPT_REPORT_KEYS
    for key, expected in gate.JAVASCRIPT_REPORT_IDENTITY.items():
        assert istanbul[key] == expected
    record = istanbul["coverage"]["backend/webrender/static/client.js"]
    assert record["path"] == "backend/webrender/static/client.js"
    assert record["statementMap"] and set(record["s"]) == set(record["statementMap"])
