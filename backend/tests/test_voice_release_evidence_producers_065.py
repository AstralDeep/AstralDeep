"""Contract tests over source for the per-client release-evidence producers: each
projects the exact stage-owned voice runtime identity from the staged projection, and
CI workflows gate the right voice/coverage suites without duplication.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import subprocess
import textwrap

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DEEP_WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PROJECTION_WORKFLOWS = (
    REPO_ROOT / "components" / "AstralProjection" / ".github" / "workflows"
)
WORKFLOW_ROOT = PROJECTION_WORKFLOWS
PRODUCERS = {
    "backend": REPO_ROOT / "backend/tests/perf/release_backend_060.py",
    "web": REPO_ROOT / "components/AstralProjection/tooling/web-ci/tests/release-060.spec.js",
    "windows": REPO_ROOT / "components/AstralProjection/windows-client/tests/release_evidence_060.py",
    "android": REPO_ROOT
    / "components/AstralProjection/android-client/app/src/androidTest/kotlin/com/personalailabs/astraldeep/app"
    / "ReleaseEvidenceInstrumentedTest.kt",
    "ios_macos": REPO_ROOT
    / "components/AstralProjection/apple-clients/AstralApp/AstralAppUITests/ReleaseEvidenceUITests.swift",
    "watchos": REPO_ROOT / "components/AstralProjection/apple-clients/AstralWatchTests/ReleaseEvidenceTests.swift",
}

if not all(path.is_file() for path in PRODUCERS.values()):
    pytest.skip(
        "repo-root producer sources are not part of the product image",
        allow_module_level=True,
    )

VOICE_IDENTITY_LITERALS = (
    "voice_runtime",
    "voice_worker_image_reference",
    "voice_worker_image_sha256",
    "livekit_image_reference",
    "livekit_image_sha256",
    "livekit_config_sha256",
    "livekit_public_url",
    "livekit_turn_domain",
    "speech_profile",
    "Systran/faster-whisper-large-v3",
    "speaches-ai/Kokoro-82M-v1.0-ONNX",
    "af_heart",
    "24000",
    "inventory_sha256",
    "profile_sha256",
)


@pytest.mark.parametrize(("platform", "path"), PRODUCERS.items())
def test_every_non_doc_producer_projects_the_exact_voice_identity(
    platform: str, path: Path
) -> None:
    source = path.read_text(encoding="utf-8")

    for literal in VOICE_IDENTITY_LITERALS:
        assert literal in source, f"{platform} producer omits {literal}"

    assert "worker_paths" in source
    assert "voice" in source


@pytest.mark.parametrize(("platform", "path"), PRODUCERS.items())
def test_voice_identity_is_taken_from_the_staged_projection(
    platform: str, path: Path
) -> None:
    source = path.read_text(encoding="utf-8")

    projection_anchor = {
        "backend": '"voice_runtime",',
        "web": '"voice_runtime",',
        "windows": '"voice_runtime",',
        "android": '"voice_runtime",',
        "ios_macos": '"voice_runtime",',
        "watchos": '"voice_runtime",',
    }[platform]
    assert projection_anchor in source

    forbidden_constructors = (
        '"voice_runtime": {',
        'put("voice_runtime", buildJsonObject',
        'projected["voice_runtime"] = [',
    )
    assert not any(item in source for item in forbidden_constructors), (
        f"{platform} producer synthesizes voice_runtime instead of projecting it"
    )


def test_windows_coverage_producer_uses_one_unambiguous_source_root() -> None:
    assert "Run full Windows source suite with coverage" not in (
        DEEP_WORKFLOWS / "build-windows-candidate.yml"
    ).read_text(encoding="utf-8")
    workflow = (DEEP_WORKFLOWS / "release-readiness.yml").read_text(encoding="utf-8")
    job = workflow.split("\n  windows-source-suite:\n", 1)[1].split("\n  windows-producer:\n", 1)[0]
    step = job.split("- name: Run full Windows source suite with coverage", 1)[1]
    step = step.split("- name:", 1)[0]

    assert "working-directory: windows-client" not in step
    assert r"-m pytest components\AstralProjection\windows-client\tests -q" in step
    assert "--cov=components/AstralProjection/windows-client `" in step
    assert "--cov=astral_client" not in step
    assert "--cov=win_agent" not in step
    assert "--cov=main" not in step


def test_projection_ci_owns_active_voice_contract_windows_and_web_checks() -> None:
    workflow = (WORKFLOW_ROOT / "ci.yml").read_text(encoding="utf-8")

    assert "if: ${{ false }}" not in workflow
    owner_jobs = ("python", "web", "windows-tests", "windows-package")
    for owner_job in owner_jobs:
        assert f"\n  {owner_job}:\n" in workflow
    assert "required:" in workflow
    assert "--require-hashes" in workflow
    windows_tests = workflow.split("\n  windows-tests:\n", 1)[1].split(
        "\n  windows-package:\n", 1
    )[0]
    assert r"python -m pytest windows-client\tests -q" in windows_tests
    assert "tests/voice-conversation-065.spec.js" in workflow
    required = workflow.split("\n  required:", 1)[1]
    assert f"needs: [{', '.join(owner_jobs)}]" in required
    for owner_job in owner_jobs:
        assert f"needs.{owner_job}.result }}}}' == 'success'" in required


def test_deep_ci_does_not_duplicate_projection_voice_or_client_jobs() -> None:
    workflow = (DEEP_WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    for stale_job in (
        "voice-contract-validator:",
        "voice-web-conformance:",
        "windows-client:",
        "test-flags-off:",
        "coverage-gate:",
    ):
        assert f"\n  {stale_job}" not in workflow
    for client in ("apple-clients", "android-client", "windows-client"):
        assert f"components/AstralProjection/{client}/" not in workflow
    assert not (DEEP_WORKFLOWS / "android-ci.yml").exists()
    assert not (DEEP_WORKFLOWS / "apple-ci.yml").exists()


def _assert_android_instrumented_route(instrumented: str, launcher: str) -> None:
    assert instrumented.count("python3 ../scripts/run_android_emulator.py") == 1
    invocation = instrumented.split("python3 ../scripts/run_android_emulator.py", 1)[1].split(
        "\n      - name:", 1
    )[0]
    assert '--root "$GITHUB_WORKSPACE" --sdk "$ANDROID_HOME"' in invocation
    assert '--output "$GITHUB_WORKSPACE/build/088/android-coverage"' in invocation
    tree = ast.parse(launcher)
    expected_bindings = {
        "coverage": '[sys.executable, str(root / "scripts/android_coverage.py")]',
        "common": '["--root", str(root), "--output", str(output)]',
        "SERIAL": '"emulator-5554"',
    }
    for name, expression in expected_bindings.items():
        values = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        ]
        assert len(values) == 1
        assert ast.dump(values[0]) == ast.dump(ast.parse(expression, mode="eval").body)
    commands = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or ast.unparse(node.func) != "subprocess.run":
            continue
        if not node.args or not isinstance(node.args[0], ast.List):
            continue
        elements = node.args[0].elts
        if not elements or ast.unparse(elements[0]) != "*coverage":
            continue
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in node.keywords}
        assert keywords == {"cwd": "root / 'android-client'", "check": "True", "env": "environment"}
        commands.append(ast.dump(node.args[0]))
    expected_commands = (
        '[*coverage, "device", *common, "--serial", SERIAL, "--lane", "fixtures"]',
        '[*coverage, "report", *common, "--lanes", "fixtures"]',
    )
    assert commands == [ast.dump(ast.parse(command, mode="eval").body) for command in expected_commands]


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ('"--lane", "fixtures"', '"--lane", "omitted"'),
        ('"--lanes", "fixtures"', '"--lanes", "omitted"'),
        ('SERIAL = "emulator-5554"', 'SERIAL = "emulator-5556"'),
        ('"report", *common', '"device", *common'),
        ('check=True,', 'check=False,'),
        ('str(root / "scripts/android_coverage.py")', 'str(root / "scripts/other.py")'),
    ),
)
def test_android_instrumented_route_refuses_incomplete_producers(old: str, new: str) -> None:
    workflow = (WORKFLOW_ROOT / "android-ci.yml").read_text(encoding="utf-8")
    instrumented = workflow.split("  instrumented:", 1)[1].split("  android-required:", 1)[0]
    launcher = (WORKFLOW_ROOT.parent.parent / "scripts/run_android_emulator.py").read_text(
        encoding="utf-8"
    )
    assert old in launcher
    with pytest.raises(AssertionError):
        _assert_android_instrumented_route(instrumented, launcher.replace(old, new))


def test_android_instrumented_route_requires_the_workflow_launcher() -> None:
    launcher = (WORKFLOW_ROOT.parent.parent / "scripts/run_android_emulator.py").read_text(
        encoding="utf-8"
    )
    with pytest.raises(AssertionError):
        _assert_android_instrumented_route("", launcher)



@pytest.mark.parametrize(
    "argument",
    (
        '--root "$GITHUB_WORKSPACE"',
        '--output "$GITHUB_WORKSPACE/build/088/android-coverage"',
    ),
)
def test_android_instrumented_route_requires_bound_launcher_arguments(argument: str) -> None:
    workflow = (WORKFLOW_ROOT / "android-ci.yml").read_text(encoding="utf-8")
    instrumented = workflow.split("  instrumented:", 1)[1].split("  android-required:", 1)[0]
    prefix, invocation = instrumented.split("python3 ../scripts/run_android_emulator.py", 1)
    launcher = (WORKFLOW_ROOT.parent.parent / "scripts/run_android_emulator.py").read_text(
        encoding="utf-8"
    )
    assert argument in invocation
    altered = prefix + "python3 ../scripts/run_android_emulator.py" + invocation.replace(argument, "", 1)
    with pytest.raises(AssertionError):
        _assert_android_instrumented_route(altered, launcher)


def test_android_ci_gates_all_voice_suites_and_kover_inputs() -> None:
    workflow = (WORKFLOW_ROOT / "android-ci.yml").read_text(encoding="utf-8")

    assert "backend/tests/fixtures/voice_065/client_conformance.json" not in workflow
    assert "./gradlew :core:test :app:testDebugUnitTest" in workflow
    assert "./gradlew :app:koverXmlReport :core:koverXmlReport" in workflow
    assert "android-client/app/build/reports/kover/report.xml" in workflow
    assert "android-client/core/build/reports/kover/report.xml" in workflow

    instrumented = workflow.split("  instrumented:", 1)[1].split(
        "  android-required:", 1
    )[0]
    assert "-PastralCoverage=true :app:prepareCoverageInputs" in instrumented
    assert "../scripts/android_coverage.py prepare" in instrumented
    launcher = (WORKFLOW_ROOT.parent.parent / "scripts/run_android_emulator.py").read_text(
        encoding="utf-8"
    )
    _assert_android_instrumented_route(instrumented, launcher)
    assert "android-app-device-and-unit-coverage" in instrumented
    assert "path: build/088/android-coverage/" in instrumented
    assert "github.event_name == 'schedule'" not in instrumented
    assert "needs.instrumented.result" in workflow
    assert "needs.build-test.result" in workflow

    assert _android_aggregate_script(workflow)


def _android_aggregate_script(workflow: str) -> str:
    aggregate = workflow.split("  android-required:", 1)[1]
    match = re.search(r"(?m)^        run: \|\n(?P<body>(?:          [^\n]*(?:\n|$))+)", aggregate)
    assert match is not None
    return textwrap.dedent(match.group("body"))


@pytest.mark.parametrize(
    ("applicable", "changes", "build", "instrumented", "passed"),
    [
        ("true", "success", "success", "success", True),
        ("true", "success", "success", "skipped", False),
        ("true", "success", "cancelled", "success", False),
        ("true", "failure", "success", "success", False),
        ("false", "success", "skipped", "skipped", True),
        ("false", "failure", "skipped", "skipped", False),
        ("false", "success", "failure", "skipped", False),
        ("", "success", "success", "success", False),
    ],
)
def test_actual_android_aggregate_enforces_changes_and_both_lane_results(
    tmp_path: Path, applicable: str, changes: str, build: str, instrumented: str, passed: bool,
) -> None:
    workflow = (WORKFLOW_ROOT / "android-ci.yml").read_text(encoding="utf-8")
    script = _android_aggregate_script(workflow)
    values = {
        "needs.changes.outputs.android": applicable,
        "needs.changes.result": changes,
        "needs.build-test.result": build,
        "needs.instrumented.result": instrumented,
    }
    for expression, value in values.items():
        script = script.replace("${{ " + expression + " }}", value)
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    summary = tmp_path / "summary.txt"
    environment["GITHUB_STEP_SUMMARY"] = str(summary)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script], cwd=WORKFLOW_ROOT.parent.parent,
        env=environment, capture_output=True, text=True, check=False,
    )
    assert (result.returncode == 0) is passed
    if passed:
        assert summary.read_text().strip() == ("passed" if applicable == "true" else "not applicable")


def test_apple_ci_gates_voice_suites_and_every_xccov_input() -> None:
    workflow = (WORKFLOW_ROOT / "apple-ci.yml").read_text(encoding="utf-8")

    assert "backend/tests/fixtures/voice_065/client_conformance.json" not in workflow
    assert "swift test --package-path apple-clients/AstralCore" in workflow
    assert "-only-testing:AstralAppTests" in workflow
    first_login_job = workflow.split("  first-login-ui:", 1)[1].split(
        "  watch-continuity:", 1
    )[0]
    assert 'selectors+=("-only-testing:AstralAppUITests/$selected")' in first_login_job
    matrix = first_login_job.split("include:", 1)[1].split("    steps:", 1)[0]
    entries = re.split(r"(?m)^          - name: ", matrix)[1:]
    assert {
        re.search(r"platform: (\w+)", entry).group(1)
        for entry in entries
        if "VoiceConversationUITests" in entry
    } == {"ios", "macos"}
    assert "-configuration Debug" in first_login_job
    assert "CODE_SIGNING_ALLOWED=NO" in first_login_job
    assert "-only-testing:AstralWatchTests" in workflow
    assert "apple-${{ matrix.slug }}-unit-xccov.json" in workflow
    assert "apple-${{ matrix.slug }}-first-login-xccov.json" in workflow
    assert "apple-watchos-xccov.json" in workflow
    assert "needs.core-tests.result" in workflow
    assert "needs.app-unit-tests.result" in workflow
    assert "needs.first-login-ui.result" in workflow
    assert "needs.watch-continuity.result" in workflow
    app_unit_job = workflow.split("  app-unit-tests:", 1)[1].split(
        "  first-login-ui:", 1
    )[0]
    app_unit_marker = app_unit_job.split("- name: Stage app unit success marker", 1)[1]
    assert "name: apple-required-app-unit-${{ matrix.slug }}" in app_unit_marker
    assert "if: always()" not in app_unit_marker
    first_login_marker = first_login_job.split(
        "- name: Stage first-login success marker", 1
    )[1]
    assert "name: apple-required-first-login-${{ matrix.slug }}" in first_login_marker
    assert "if: always()" not in first_login_marker
    for marker in (
        "apple-required-app-unit-ios",
        "apple-required-app-unit-macos",
        "apple-required-first-login-ios",
        "apple-required-first-login-macos",
    ):
        assert marker in workflow
    assert workflow.count(
        "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
    ) >= 4
