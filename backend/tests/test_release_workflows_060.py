"""Contract tests for Spec 060's release-trust workflow set (T103/T119).

The six release workflows asserted here are authored by later waves (T107,
T119, T120) AGAINST these tests; until they land, each workflow-file test
fails with a message naming the missing workflow. The policy tests at the
bottom drive the already-landed ``scripts/validate_release_evidence.py``
and pass today.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
if not (
    (REPO_ROOT / ".github").is_dir() and (REPO_ROOT / "scripts").is_dir()
):  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SPEC_ROOT = REPO_ROOT / "specs" / "060-runtime-reliability-hardening"
CONTRACT_ROOT = SPEC_ROOT / "contracts"
SCRIPT_PATH = REPO_ROOT / "scripts" / "validate_release_evidence.py"
CONTRACT_TEST_PATH = REPO_ROOT / "backend" / "tests" / "test_release_contract_schemas.py"
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
READINESS = WORKFLOWS / "release-readiness.yml"
TRUSTED_BUILDER = WORKFLOWS / "release-trusted-builder.yml"
EXCEPTION = WORKFLOWS / "release-evidence-exception.yml"
BRIDGE = WORKFLOWS / "release-windows.yml"
CONTROLLER = WORKFLOWS / "release-windows-publisher-controller.yml"
PUBLISHER = WORKFLOWS / "release-windows-publisher.yml"
RELEASE_WORKFLOW_FILES = (
    READINESS,
    TRUSTED_BUILDER,
    EXCEPTION,
    BRIDGE,
    CONTROLLER,
    PUBLISHER,
)

# Producer jobs inside release-readiness.yml. windows-candidate reuses the
# feature-068 build-once workflow; the other eight upload evidence-<platform>.
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
PRODUCER_JOBS = (*EVIDENCE_PRODUCER_JOBS, "windows-candidate")

# The one action new to this repository; the design doc pins the exact line.
ATTEST_ACTION = (
    "actions/attest-build-provenance@0f67c3f4856b2e3261c31976d6725780e5e4c373"
    " # v4.1.1"
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
    """The sibling validator-test module, reused for its evidence-set builders."""

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


def _permission_lines(scope: str) -> set[str]:
    """Every ``<scope-key>: read|write`` grant appearing in the given text."""

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


# ---------------------------------------------------------------------------
# release-readiness.yml
# ---------------------------------------------------------------------------


def test_release_readiness_identity_triggers_and_read_only_top_level() -> None:
    workflow = _workflow_text(READINESS)
    head = _workflow_head(workflow)

    assert re.search(r"(?m)^name: release-readiness$", head)
    assert "run-name: release-readiness ${{ inputs.request_id }}" in head
    assert "workflow_call:" in head
    assert "workflow_dispatch:" in head
    # Both triggers declare the same three inputs.
    for input_name in ("candidate_sha", "base_sha", "request_id"):
        assert head.count(f"{input_name}:") >= 2, f"both triggers need {input_name}"
    assert "required: true" in head
    assert _top_permissions(workflow) == {"contents: read"}
    assert "release-readiness-${{ inputs.candidate_sha }}" in head
    assert "cancel-in-progress: true" not in head


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
    # The self-hosted staging runner was retired ("won't set up"); the job runs
    # on a hosted runner and targets an external persistent staging host at
    # ASTRAL_STAGING_ENDPOINT (documented inactive until that host exists).
    assert "ubuntu-latest" in stage
    assert "self-hosted" not in stage and "astral-staging" not in stage
    assert "ASTRAL_STAGING_ENDPOINT" in stage
    assert "persist-credentials: false" in stage
    assert "--leave-running" in stage
    assert "--trusted-manifest" in stage
    assert "trusted-stage-deploy.json" in stage
    assert "stage-outputs-" in stage

    for producer in PRODUCER_JOBS:
        body = _workflow_job(workflow, producer)
        assert "needs:" in body, f"{producer} must depend on stage-deploy"
        assert "stage-deploy" in body, f"{producer} must depend on stage-deploy"
    for producer in EVIDENCE_PRODUCER_JOBS:
        body = _workflow_job(workflow, producer)
        platform = producer.removesuffix("-producer")
        assert f"evidence-{platform}" in body
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
    assert "web-istanbul.json" in web
    windows = _workflow_job(workflow, "windows-producer")
    assert "windows-candidate" in windows
    assert "release_evidence_060.py" in windows
    assert "executable_sha256" in windows
    android = _workflow_job(workflow, "android-producer")
    assert "connectedDebugAndroidTest" in android
    assert "ReleaseEvidenceInstrumentedTest" in android
    for slug in ("macos", "ios"):
        assert "ReleaseEvidenceUITests" in _workflow_job(workflow, f"{slug}-producer")
    watch = _workflow_job(workflow, "watchos-producer")
    assert "AstralWatchTests/ReleaseEvidenceTests" in watch
    docs = _workflow_job(workflow, "docs-producer")
    assert "check_doc_links.py" in docs

    builder = _workflow_job(workflow, "trusted-builder")
    assert "uses: ./.github/workflows/release-trusted-builder.yml" in builder
    assert "always()" in builder
    for producer in ("stage-deploy", *EVIDENCE_PRODUCER_JOBS):
        assert producer in builder, f"trusted-builder must wait on {producer}"

    cleanup = _workflow_job(workflow, "stage-cleanup")
    assert "always()" in cleanup
    assert "ubuntu-latest" in cleanup
    assert "self-hosted" not in cleanup and "astral-staging" not in cleanup
    assert "cleanup" in cleanup


def test_release_readiness_candidate_jobs_never_carry_write_authority() -> None:
    workflow = _workflow_text(READINESS)
    for job_id in ("stage-deploy", *PRODUCER_JOBS, "protected-decision", "stage-cleanup"):
        grants = _write_grants(_workflow_job(workflow, job_id))
        assert not grants, f"candidate-facing job {job_id} must stay read-only: {sorted(grants)}"
    # Only the trusted-builder call gets attest/OIDC authority, exactly as designed.
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
    assert "release-trusted-builder.yml" in decision
    # Debt-ledger head is read before AND after policy evaluation.
    assert "release-evidence-debt" in decision
    # The policy copy is extracted from the pinned builder commit, never the
    # candidate checkout.
    assert "git archive" in decision
    assert "RELEASE_TRUSTED_BUILDER_SHA" in decision
    assert "vars.RELEASE_TRUSTED_BUILDER_SHA" in workflow
    assert "check_changed_coverage.py" in decision
    assert "validate_release_evidence.py" in decision
    assert "--decision-output" in decision
    assert "trusted-release-decision.json" in decision
    assert "--protected-workflow-ref" in decision
    assert "--protected-policy-sha" in decision
    assert "name: release-evidence" in decision
    assert "name: trusted-release-decision" in decision


# ---------------------------------------------------------------------------
# release-trusted-builder.yml
# ---------------------------------------------------------------------------


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
    # Identities are reconstructed from the SHARED run's API state, never from
    # producer-uploaded bytes.
    assert "github.run_id" in body
    assert "/jobs" in body
    assert "/artifacts" in body
    assert ATTEST_ACTION in workflow
    assert "trusted-manifests" in body
    assert "trusted_workflow_provenance" in workflow


# ---------------------------------------------------------------------------
# release-evidence-exception.yml
# ---------------------------------------------------------------------------


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
    # Self-approval is structurally refused: the recorded requester must differ
    # from the dispatching approver.
    assert "requester_login" in workflow
    assert "github.actor" in workflow
    # Bounded debt lifetime and create-only append on the protected ledger branch.
    assert "expires_at" in workflow
    assert "release-evidence-debt" in workflow
    assert "debts/" in workflow
    assert "resolutions/" in workflow
    assert "release_evidence_debt" in workflow
    assert "trusted_exception_approval" in workflow
    assert "trusted_debt_resolution" in workflow
    assert ATTEST_ACTION in workflow
    assert "release-evidence-exception-" in register
    # Non-waivable checks are enforced in-job, mirroring the validator policy.
    assert "apple_first_login_llm" in workflow
    assert "candidate_staging" in workflow


# ---------------------------------------------------------------------------
# release-windows.yml — the exact-byte-pinned v0.3.0-compatible bridge signer
# ---------------------------------------------------------------------------


def test_release_windows_bridge_keeps_pinned_identity_with_no_write_authority() -> None:
    workflow = _workflow_text(BRIDGE)
    head = _workflow_head(workflow)

    # integrity.py's SAN pins this workflow path AND this name stays stable.
    assert re.search(r"(?m)^name: Release Windows client$", head)
    assert "tags:" in head and "v*" in head
    assert "workflow_dispatch:" in head
    assert re.search(r"(?m)^\s*tag:", head), "dispatch rehearsal needs a tag input"
    assert _top_permissions(workflow) == {
        "contents: read",
        "actions: read",
        "id-token: write",
    }
    # No job anywhere in the bridge may widen that grant.
    assert _permission_lines(workflow) == {
        "contents: read",
        "actions: read",
        "id-token: write",
    }
    assert _job_ids(workflow) == ["bridge-sign"]


def test_release_windows_bridge_never_rebuilds_and_never_mutates_releases() -> None:
    workflow = _workflow_text(BRIDGE)
    lower = workflow.lower()

    # No rebuild: the bridge signs the exact archived build-once bytes.
    assert "pyinstaller" not in lower
    assert "astraldeep.spec" not in lower
    assert re.search(r"pip install[^\n]*requirements", workflow) is None
    assert "- name: Build the exe" not in workflow
    # Consumption is by exact artifact id recorded in the trusted decision.
    assert "trusted-release-decision" in workflow
    assert re.search(r"gh api[^\n]*artifacts", workflow)
    assert "/zip" in workflow
    assert "executable_sha256" in workflow
    # Detached sigstore signature under the legacy v0.3.0 identity policy.
    assert "sigstore" in lower
    assert "cosign.bundle" in workflow
    assert "token.actions.githubusercontent.com" in workflow
    assert "rebuild_performed" in workflow
    assert "executable_bytes_modified" in workflow
    # Output is ONLY a run artifact; the bridge never touches releases.
    assert "windows-bridge-signing-" in workflow
    assert "softprops" not in lower
    assert "gh release" not in workflow
    assert "/releases" not in workflow


# ---------------------------------------------------------------------------
# release-windows-publisher-controller.yml
# ---------------------------------------------------------------------------


def test_release_windows_publisher_controller_verifies_decision_read_only() -> None:
    workflow = _workflow_text(CONTROLLER)
    head = _workflow_head(workflow)

    assert re.search(r"(?m)^name: release-windows-publisher-controller$", head)
    assert "workflow_dispatch:" in head
    for input_name in ("candidate_sha", "release_version", "mode", "readiness_run_id"):
        assert f"{input_name}:" in head, f"dispatch input missing: {input_name}"
    assert "disposable" in head and "official" in head
    assert "default: disposable" in head
    assert _top_permissions(workflow) == {"contents: read", "actions: read"}

    job_ids = _job_ids(workflow)
    assert "verify-decision" in job_ids
    assert "publish" in job_ids

    verify = _workflow_job(workflow, "verify-decision")
    assert not _write_grants(verify), "verify-decision must be read-only"
    assert "gh attestation verify" in verify
    assert "valid_until" in verify
    assert "bridge_workflow_sha256" in verify

    publish = _workflow_job(workflow, "publish")
    assert "uses: ./.github/workflows/release-windows-publisher.yml" in publish
    assert "secrets: inherit" in publish


# ---------------------------------------------------------------------------
# release-windows-publisher.yml — the ONLY write authority in the release path
# ---------------------------------------------------------------------------


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
    assert _permission_lines(body) == {"contents: write", "actions: read"}
    # Built-in short-lived token only — no App/installation/broker credential.
    secret_refs = set(re.findall(r"secrets\.([A-Za-z_0-9]+)", body))
    assert secret_refs <= {"GITHUB_TOKEN"}, f"unexpected secrets: {sorted(secret_refs)}"

    # Defense in depth: the publisher re-verifies the decision itself.
    assert "gh attestation verify" in body
    # Create-only tag at the decision SHA via the git data API.
    assert re.search(r"git/refs", body)
    # The signed bytes come from the bridge run artifact, never a rebuild.
    assert "windows-bridge-signing-" in body
    assert "pyinstaller" not in body.lower()
    # Exactly the three assets, uploaded create-only to a DRAFT release.
    for asset in ("AstralDeep.exe", "SHA256SUMS", "cosign.bundle"):
        assert asset in body, f"missing draft asset {asset}"
    assert re.search(r"(?m)\S+  AstralDeep\.exe", body), "SHA256SUMS line format"
    assert re.search(r"draft=true|--draft\b|draft:\s*true", body)
    assert "prerelease" in body
    assert "--clobber" not in body
    assert "softprops" not in workflow.lower()
    # Re-download all three by their numeric asset database ids.
    assert "assets/" in body
    # /releases/latest confirmation with the shipped updater parser runs ONLY
    # in official mode; disposable mode force-cleans and never publishes.
    assert "releases/latest" in body
    assert re.search(r"mode\s*==\s*'official'", body)
    assert re.search(r"mode\s*==\s*'disposable'", body)
    assert "always()" in body
    assert "delete" in body.lower()
    # Draft provenance record with the schema-pinned publisher constants.
    assert "windows_draft_verification_provenance" in body
    assert "windows-draft-provenance" in body
    assert "make_latest_on_publish" in body
    assert "token_broker_policy_sha256" in body
    assert "bridge_workflow_sha256" in body


# ---------------------------------------------------------------------------
# Supply-chain pinning across the whole release workflow set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", RELEASE_WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_third_party_action_is_sha_pinned_with_version_comment(path: Path) -> None:
    workflow = _workflow_text(path)
    for value in re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*(.+?)\s*$", workflow):
        if value.startswith("./"):
            continue  # Local reusable workflows are pinned by the repo commit.
        assert "app-token" not in value, (
            f"{path.name} must not mint App/installation tokens: {value}"
        )
        assert re.fullmatch(
            r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+@[0-9a-f]{40}\s+# v\d+(?:\.\d+)*",
            value,
        ), f"{path.name} action is not SHA-pinned with a version comment: {value}"


# ---------------------------------------------------------------------------
# ci.yml — caller job and release-tooling test coverage
# ---------------------------------------------------------------------------


def test_ci_release_tooling_lane_covers_the_new_release_test_files() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    job = _workflow_job(workflow, "release-tooling-tests")
    for test_path in (
        "backend/tests/test_prepare_release_evidence_060.py",
        "backend/tests/test_release_evidence_bootstrap.py",
        "backend/tests/test_release_workflows_060.py",
        "backend/tests/test_release_evidence_producers.py",
    ):
        assert test_path in job, f"RELEASE_TOOL_TESTS must include {test_path}"


def test_ci_caller_job_is_release_readiness_guarded_by_activation_variable() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    # Job id EXACTLY release-readiness so the required check-run name is
    # "release-readiness / protected-decision".
    job = _workflow_job(workflow, "release-readiness")
    assert "vars.RELEASE_READINESS_ACTIVE == 'true'" in job
    assert "github.event_name != 'pull_request'" in job
    assert "github.event.pull_request.draft == false" in job
    assert "uses: ./.github/workflows/release-readiness.yml" in job
    assert "candidate_sha:" in job
    assert "github.event.pull_request.head.sha" in job
    assert "base_sha:" in job
    assert "github.event.before" in job
    assert "request_id: ci-${{ github.run_id }}" in job
    assert "secrets: inherit" in job


def test_privileged_manual_dispatch_jobs_refuse_candidate_refs() -> None:
    guard = (
        "github.event_name != 'workflow_dispatch' || "
        "github.ref == 'refs/heads/main'"
    )
    workflows = (
        WORKFLOWS / "apple-release.yml",
        WORKFLOWS / "build-windows-candidate.yml",
        EXCEPTION,
        READINESS,
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


# ---------------------------------------------------------------------------
# Policy: local parsing is diagnostic-only; CI never trusts a local verdict
# ---------------------------------------------------------------------------


def _diagnostic_argv(
    validator: Any,
    evidence_set: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> list[str]:
    """Install the same seams the sibling validator test uses for main()."""

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    for name in ("provenance", "approvals", "resolutions", "attestations"):
        (tmp_path / name).mkdir()
    stage = copy.deepcopy(evidence_set["evidence"][0]["staging_environment"])
    stage.update(
        request_namespace="astral060-request",
        capability_manifest_sha256="7" * 64,
        service_identity_sha256="8" * 64,
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
    # The substituted local verdict: the pushed set already claims "passed".
    assert evidence_set["decision"] == "passed"
    argv = _diagnostic_argv(validator, evidence_set, monkeypatch, tmp_path)

    assert validator.main(argv) == 0
    diagnostic = json.loads(capsys.readouterr().out)
    assert diagnostic["decision"] == "diagnostic_policy_passed"
    assert diagnostic["protected_release_authorization"] is False

    # Asking the same CLI for a trusted decision outside the protected job is
    # refused fail-closed and writes nothing.
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
    assert "protected-decision" in capsys.readouterr().err
    assert not decision_path.exists()

    # A same-name check in CI but from any job other than protected-decision is
    # equally refused.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_JOB", "release-tooling-tests")
    assert validator.main(protected_argv) == 2
    assert "protected-decision" in capsys.readouterr().err
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

    # A draft asset whose digest differs from the matrix-tested EXE is a rebuild.
    mutated = copy.deepcopy(document)
    mutated["draft_executable"]["sha256"] = "0" * 64
    with pytest.raises(validator.PolicyError, match="rebuilt or modified"):
        validator.validate_windows_draft_provenance(
            mutated, trusted_decision=decision, now=now, resolver=resolver
        )

    # The publisher must record the SAME bridge workflow byte hash the signer saw.
    moved_bridge = copy.deepcopy(document)
    moved_bridge["signing"]["bridge_workflow_sha256"] = "e" * 64
    with pytest.raises(validator.PolicyError, match="bridge bytes differ"):
        validator.validate_windows_draft_provenance(
            moved_bridge, trusted_decision=decision, now=now, resolver=resolver
        )

    # A re-downloaded SHA256SUMS that does not bind the EXE bytes is refused.
    substituted = copy.deepcopy(document)
    substituted["draft_checksum_manifest"] = {
        "immutable_reference": "gh://AstralDeep/AstralDeep/releases/10/assets/14",
        "sha256": hashlib.sha256(wrong_checksums.read_bytes()).hexdigest(),
    }
    with pytest.raises(validator.PolicyError, match="does not bind"):
        validator.validate_windows_draft_provenance(
            substituted, trusted_decision=decision, now=now, resolver=resolver
        )

    # The signing record's schema consts pin rebuild_performed and
    # executable_bytes_modified to false — a true value never validates.
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
