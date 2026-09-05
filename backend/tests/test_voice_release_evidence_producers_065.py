"""Feature 065 release-evidence producer projection contract tests.

The platform producers are implemented in each client's native release lane,
so this source-contract suite is the one fast, host-independent guard that can
exercise all seven targets together.  The protected schema/validator remains
authoritative; these assertions ensure no producer drops or locally invents
the stage-owned voice runtime identity before that validation runs.
"""

from __future__ import annotations

from pathlib import Path

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

    # Each producer must reject an old stage topology that lacks the voice
    # worker; adding it locally would let candidate code forge stage identity.
    assert "worker_paths" in source
    assert "voice" in source


@pytest.mark.parametrize(("platform", "path"), PRODUCERS.items())
def test_voice_identity_is_taken_from_the_staged_projection(
    platform: str, path: Path
) -> None:
    source = path.read_text(encoding="utf-8")

    # The stage-owned object belongs inside the existing projection list/map.
    # Producers validate it, then copy that original value into
    # staging_environment; they must not create a replacement voice_runtime.
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
    workflow = (DEEP_WORKFLOWS / "build-windows-candidate.yml").read_text(
        encoding="utf-8"
    )
    step = workflow.split("- name: Run full Windows source suite with coverage", 1)[1]
    step = step.split("- name:", 1)[0]

    # Coverage.py emits class filenames relative to each --cov source. Multiple
    # roots collapse astral_client/app.py and win_agent/agent.py to basenames,
    # which the fail-closed cross-language parser correctly rejects as
    # ambiguous. Running from the repository root with one Projection-owned
    # Windows source preserves both package prefixes without sweeping synthetic
    # Qt support-module filenames into the canonical Cobertura report.
    assert "working-directory: windows-client" not in step
    assert r"-m pytest components\AstralProjection\windows-client\tests -q" in step
    assert "--cov=components/AstralProjection/windows-client `" in step
    assert "--cov=astral_client" not in step
    assert "--cov=win_agent" not in step
    assert "--cov=main" not in step


def test_projection_ci_owns_active_voice_contract_windows_and_web_checks() -> None:
    workflow = (WORKFLOW_ROOT / "ci.yml").read_text(encoding="utf-8")

    assert "if: ${{ false }}" not in workflow
    assert "python:" in workflow
    assert "web:" in workflow
    assert "windows:" in workflow
    assert "required:" in workflow
    assert "--require-hashes" in workflow
    assert r"python -m pytest windows-client\tests -q" in workflow
    assert "tests/voice-conversation-065.spec.js" in workflow
    required = workflow.split("\n  required:", 1)[1]
    for owner_job in ("python", "web", "windows"):
        assert owner_job in required
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
    assert "components/AstralProjection/" not in workflow
    assert not (DEEP_WORKFLOWS / "android-ci.yml").exists()
    assert not (DEEP_WORKFLOWS / "apple-ci.yml").exists()


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
    assert "connectedDebugAndroidTest" in instrumented
    assert "github.event_name == 'schedule'" not in instrumented
    assert "needs.instrumented.result" in workflow
    assert "needs.build-test.result" in workflow

    aggregate = workflow.split("  android-required:", 1)[1]
    assert (
        "      - name: Enforce every Android conformance and coverage producer\n"
        "        working-directory: .\n"
        "        run: |"
    ) in aggregate


def test_apple_ci_gates_voice_suites_and_every_xccov_input() -> None:
    workflow = (WORKFLOW_ROOT / "apple-ci.yml").read_text(encoding="utf-8")

    assert "backend/tests/fixtures/voice_065/client_conformance.json" not in workflow
    assert "swift test --package-path apple-clients/AstralCore" in workflow
    assert "-only-testing:AstralAppTests" in workflow
    assert "-only-testing:AstralAppUITests/VoiceConversationUITests" in workflow
    first_login_job = workflow.split("  first-login-ui:", 1)[1].split(
        "  watch-continuity:", 1
    )[0]
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
