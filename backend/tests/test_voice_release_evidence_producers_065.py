"""Feature 065 release-evidence producer projection contract tests.

The platform producers are implemented in each client's native release lane,
so this source-contract suite is the one fast, host-independent guard that can
exercise all seven targets together.  The protected schema/validator remains
authoritative; these assertions ensure no producer drops or locally invents
the stage-owned voice runtime identity before that validation runs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = Path(
    os.environ.get(
        "ASTRAL_VOICE_WORKFLOW_ROOT",
        REPO_ROOT / ".github" / "workflows",
    )
)
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
    workflow = (WORKFLOW_ROOT / "build-windows-candidate.yml").read_text(
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


def test_standard_ci_gates_contract_validation_windows_and_web_coverage() -> None:
    workflow = (WORKFLOW_ROOT / "ci.yml").read_text(encoding="utf-8")

    assert "voice-contract-validator:" in workflow
    assert "tooling/contract-ci/requirements.lock.txt" in workflow
    assert "tooling/contract-ci/validate_voice_contracts.py" in workflow
    assert "--require-hashes" in workflow
    assert "python -m pytest components/AstralProjection/windows-client/tests -q" in workflow
    assert "voice-web-conformance:" in workflow
    assert "tests/voice-conversation-065.spec.js" in workflow
    assert "ASTRAL_VOICE_COVERAGE_ISTANBUL_OUTPUT" in workflow
    assert "build/065/coverage/web-istanbul.json" in workflow
    assert "name: web-voice-istanbul" in workflow

    web_job = workflow.split("\n  voice-web-conformance:", 1)[1].split(
        "\n  release-tooling-tests:", 1
    )[0]
    conformance_step = web_job.split(
        "- name: Run C0-C6 in the digest-pinned Playwright image", 1
    )[1].split("- name:", 1)[0]
    assert "tests/voice-conversation-065.spec.js" in conformance_step
    assert "ASTRAL_VOICE_COVERAGE_ISTANBUL_OUTPUT" in conformance_step
    assert "chmod 0644 /work/build/065/coverage/web-istanbul.json" in conformance_step

    # Publication depends on these gates TRANSITIVELY through the
    # unprivileged `gates` aggregation (publish's own guard must stay inside
    # the draft-bootstrap push-only grammar; a skipped direct need would skip
    # publish silently — see test_release_workflows_060 for the full chain).
    publish_needs = workflow.split("  publish:", 1)[1].split("    if:", 1)[0]
    assert "- gates" in publish_needs
    gates_job = workflow.split("  gates:", 1)[1].split("\n  publish:", 1)[0]
    for required_job in (
        "voice-contract-validator",
        "voice-web-conformance",
        "windows-client",
    ):
        assert f"- {required_job}" in gates_job
    assert "needs.voice-contract-validator.result }}' == 'success'" in gates_job
    assert "needs.voice-web-conformance.result }}' == 'skipped'" in gates_job
    assert "needs.windows-client.result }}' == 'skipped'" in gates_job


def test_backend_root_jobs_mount_only_required_voice_contract_inputs_read_only() -> None:
    workflow = (WORKFLOW_ROOT / "ci.yml").read_text(encoding="utf-8")
    test_job = workflow.split("  test:\n", 1)[1].split("  test-flags-off:\n", 1)[0]
    flags_off_job = workflow.split("  test-flags-off:\n", 1)[1].split(
        "  coverage-gate:\n", 1
    )[0]

    required_mounts = (
        (
            "components/AstralProjection/android-client:"
            '/app/components/AstralProjection/android-client:ro"'
        ),
        (
            "components/AstralProjection/apple-clients:"
            '/app/components/AstralProjection/apple-clients:ro"'
        ),
        (
            "components/AstralProjection/windows-client:"
            '/app/components/AstralProjection/windows-client:ro"'
        ),
        'deploy/livekit:/app/deploy/livekit:ro"',
        (
            "tooling/contract-ci/validate_voice_contracts.py:"
            '/app/tooling/contract-ci/validate_voice_contracts.py:ro"'
        ),
        (
            "tooling/evaluate_voice_recap_matrix_065.py:"
            '/app/tooling/evaluate_voice_recap_matrix_065.py:ro"'
        ),
        (
            "components/AstralProjection/tooling/web-ci/tests/release-060.spec.js:"
            '/app/components/AstralProjection/tooling/web-ci/tests/release-060.spec.js:ro"'
        ),
        (
            "tooling/voice-worker/closure_manifest.py:"
            '/app/tooling/voice-worker/closure_manifest.py:ro"'
        ),
        (
            "tooling/voice-worker/run_livekit_integration.py:"
            '/app/tooling/voice-worker/run_livekit_integration.py:ro"'
        ),
        (
            "scripts/validate_release_evidence.py:"
            '/ci/voice/validate_release_evidence.py:ro"'
        ),
        (
            ".github/workflows/build-windows-candidate.yml:"
            '/ci/voice/workflows/build-windows-candidate.yml:ro"'
        ),
        (
            ".github/workflows/ci.yml:"
            '/ci/voice/workflows/ci.yml:ro"'
        ),
        (
            ".github/workflows/android-ci.yml:"
            '/ci/voice/workflows/android-ci.yml:ro"'
        ),
        (
            ".github/workflows/apple-ci.yml:"
            '/ci/voice/workflows/apple-ci.yml:ro"'
        ),
        (
            "specs/065-conversational-voice/contracts/voice-control.schema.json:"
            '/ci/voice/voice-control.schema.json:ro"'
        ),
        (
            "specs/065-conversational-voice/"
            "dependency-audit-rtc-only-2026-07-31.md:"
            '/ci/voice/dependency-audit-rtc-only-2026-07-31.md:ro"'
        ),
        (
            "specs/065-conversational-voice/dependency-approval.md:"
            '/ci/voice/dependency-approval.md:ro"'
        ),
        'Dockerfile.voice:/app/Dockerfile.voice:ro"',
        'Dockerfile.voice.dockerignore:/app/Dockerfile.voice.dockerignore:ro"',
        'docker-compose.yml:/app/docker-compose.yml:ro"',
        'docker-compose.staging.yml:/app/docker-compose.staging.yml:ro"',
        (
            "docker-compose.voice-integration.yml:"
            '/app/docker-compose.voice-integration.yml:ro"'
        ),
    )
    required_paths = (
        "ASTRAL_VOICE_SCHEMA_ENGINE_PATH=/ci/voice/validate_release_evidence.py",
        "ASTRAL_VOICE_SCHEMA_PATH=/ci/voice/voice-control.schema.json",
        (
            "ASTRAL_VOICE_RTC_AUDIT_PATH="
            "/ci/voice/dependency-audit-rtc-only-2026-07-31.md"
        ),
        "ASTRAL_VOICE_WORKFLOW_ROOT=/ci/voice/workflows",
        (
            "ASTRAL_VOICE_DEPENDENCY_APPROVAL_PATH="
            "/ci/voice/dependency-approval.md"
        ),
    )
    for job in (test_job, flags_off_job):
        for mount in required_mounts:
            assert mount in job
        for path in required_paths:
            assert path in job
        assert '-v "$PWD:/app' not in job
        assert '.env:/app' not in job


def test_android_ci_gates_all_voice_suites_and_kover_inputs() -> None:
    workflow = (WORKFLOW_ROOT / "android-ci.yml").read_text(encoding="utf-8")

    assert "backend/tests/fixtures/voice_065/client_conformance.json" not in workflow
    assert "components/AstralProjection" in workflow
    assert "gradle :core:test :app:testDebugUnitTest" in workflow
    assert "gradle :app:koverXmlReport :core:koverXmlReport" in workflow
    assert "components/AstralProjection/android-client/app/build/reports/kover/report.xml" in workflow
    assert "components/AstralProjection/android-client/core/build/reports/kover/report.xml" in workflow

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
    assert "swift test --package-path components/AstralProjection/apple-clients/AstralCore" in workflow
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
