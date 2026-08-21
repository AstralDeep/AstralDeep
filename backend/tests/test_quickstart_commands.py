"""Executable setup-command contracts for feature 060.

These tests intentionally inspect the tracked runbook and Makefile instead of
starting Docker.  They keep the quickstart's copy/paste commands aligned with
real targets and make an empty focused pytest selection a hard failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE_PATH = REPO_ROOT / "Makefile"
QUICKSTART_PATH = (
    REPO_ROOT / "specs" / "060-runtime-reliability-hardening" / "quickstart.md"
)

if not (
    MAKEFILE_PATH.is_file() and (REPO_ROOT / "specs").is_dir()
):  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )

FOCUSED_060_TESTS = {
    "tests/test_release_contract_schemas.py",
    "tests/test_staging_fixtures_060.py",
    "tests/test_ui_protocol_manifest.py",
    "tests/test_quickstart_commands.py",
    "tests/test_ci_javascript_lint.py",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _make_targets(makefile: str) -> set[str]:
    return set(re.findall(r"(?m)^([A-Za-z0-9_.-]+):(?:[^=]|$)", makefile))


def _make_rule(makefile: str, target: str) -> tuple[str, str]:
    match = re.search(
        rf"(?ms)^{re.escape(target)}:(?P<header>[^\n]*)\n"
        rf"(?P<body>(?:\t[^\n]*(?:\n|$))*)",
        makefile,
    )
    assert match, f"Makefile target is missing: {target}"
    return match.group("header"), match.group("body")


def test_every_quickstart_make_command_resolves_to_a_tracked_target() -> None:
    makefile = _read(MAKEFILE_PATH)
    quickstart = _read(QUICKSTART_PATH)
    invoked = set(re.findall(r"(?m)^\s*make\s+([A-Za-z0-9_.-]+)\s*$", quickstart))
    assert invoked, "feature-060 quickstart did not expose any Make commands"
    assert invoked <= _make_targets(makefile), (
        f"quickstart references missing Make targets: "
        f"{sorted(invoked - _make_targets(makefile))}"
    )


def test_feature_060_focused_target_fails_closed_on_empty_collection() -> None:
    makefile = _read(MAKEFILE_PATH)
    selection_header, selection_body = _make_rule(makefile, "check-060-selection")
    test_header, test_body = _make_rule(makefile, "test-060")

    assert "check-060-selection" in test_header.split(), (
        "test-060 must run the non-empty collection guard first"
    )
    assert "--collect-only" in selection_body
    assert "python -m pytest" in selection_body
    assert "python -m pytest" in test_body
    assert "--collect-only" not in test_body
    assert "|| true" not in selection_body + test_body
    assert "--continue-on-collection-errors" not in selection_body + test_body

    for test_path in FOCUSED_060_TESTS:
        assert test_path in makefile, f"focused 060 suite omitted {test_path}"


def test_apply_config_recreates_service_and_reports_only_safe_effective_state() -> None:
    makefile = _read(MAKEFILE_PATH)
    _, body = _make_rule(makefile, "apply-config")
    quickstart = _read(QUICKSTART_PATH)

    assert "make apply-config" in quickstart
    assert "docker compose up" in body
    assert "--force-recreate" in body
    assert "astraldeep" in body
    assert "docker compose restart" not in body
    assert "docker compose exec -T astraldeep" in body
    assert "FF_BYO_AGENTS" in body

    lowered = body.lower()
    assert "printenv" not in lowered
    assert re.search(r"(?:^|\s)env(?:\s|$)", lowered) is None
    assert "cat .env" not in lowered
    assert "docker inspect" not in lowered


def test_quickstart_uses_only_the_isolated_lock_and_digest_pinned_browser() -> None:
    quickstart = _read(QUICKSTART_PATH)

    assert 'corepack npm --version)" = "11.16.0"' in quickstart
    assert "corepack npm ci --ignore-scripts" in quickstart
    assert "corepack npm run check:package-manager" in quickstart
    assert "corepack npm run check:product-isolation" in quickstart
    assert "corepack npm run lint" in quickstart
    assert "corepack npm run test:coverage-conversion" in quickstart
    assert "corepack npm run test:coverage-conversion:node" in quickstart
    assert "NODE_V8_COVERAGE" in quickstart
    assert quickstart.count("corepack npm run coverage:node") >= 2
    assert "tooling-javascript.json" in quickstart
    assert (
        "PLAYWRIGHT_IMAGE=\"$(tr -d '\\n' < components/AstralProjection/tooling/web-ci/playwright-image.txt)\""
        in quickstart
    )
    assert 'test "${PLAYWRIGHT_IMAGE#*@sha256:}" != "$PLAYWRIGHT_IMAGE"' in quickstart
    assert 'docker pull "$PLAYWRIGHT_IMAGE"' in quickstart
    assert '"$PLAYWRIGHT_IMAGE" sh -lc \'test "$(corepack npm --version)"' in quickstart
    assert "corepack npm run test:coverage-conversion:browser" in quickstart


def test_quickstart_exports_bounded_per_file_xccov_observations() -> None:
    quickstart = _read(QUICKSTART_PATH)

    assert quickstart.count("python3 scripts/export_xccov_line_coverage.py") == 3
    assert "xcrun xccov view --archive --json" not in quickstart
    for platform in ("ios", "macos", "watchos"):
        assert f"--platform {platform}" in quickstart
        assert f"apple-{platform}-xccov.json" in quickstart
    assert "owner-pinned protected-policy" in quickstart


def test_normative_strict_commands_use_the_deep_owner_profile() -> None:
    quickstart = _read(QUICKSTART_PATH)
    prepare = re.search(
        r"# equivalent direct invocation\n(?P<command>python3 "
        r"scripts/prepare_release_evidence\.py .*?)\n```",
        quickstart,
        re.DOTALL,
    )
    collector = re.search(
        r"(?P<command>python3 scripts/check_changed_coverage\.py .*?)\n```",
        quickstart,
        re.DOTALL,
    )
    assert prepare is not None
    assert collector is not None

    owner_reports = {
        "--backend-python",
        "--voice-worker-python",
        "--tooling-python",
    }
    external_reports = {
        "--projection-python",
        "--windows-python",
        "--javascript",
        "--android-app",
        "--android-core",
        "--ios",
        "--macos",
        "--watchos",
    }
    for command in (prepare.group("command"), collector.group("command")):
        assert "--repository-profile deep" in command
        assert {flag for flag in owner_reports if flag in command} == owner_reports
        assert not any(flag in command for flag in external_reports)
