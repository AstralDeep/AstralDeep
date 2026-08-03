"""Canonical closure-manifest tests for the Feature 065 RTC-only worker."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tooling" / "voice-worker" / "closure_manifest.py"
RTC_AUDIT_PATH = Path(
    os.environ.get(
        "ASTRAL_VOICE_RTC_AUDIT_PATH",
        REPO_ROOT
        / "specs"
        / "065-conversational-voice"
        / "dependency-audit-rtc-only-2026-07-31.md",
    )
)


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("voice_worker_closure_065", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


closure = _load_tool()

CANDIDATE_REPOSITORY = "PersonalAILabs/AstralDeep"
CANDIDATE_SHA = "a" * 40
TRIVY_ARTIFACT_IDS = {
    "linux/amd64": f"sha256:{40:064x}",
    "linux/arm64": f"sha256:{50:064x}",
}
EVIDENCE_BYTES = {
    category: {
        platform: json.dumps(
            {
                "category": category,
                "platform": platform,
                "test_fixture": True,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
        for platform in closure.PLATFORMS
    }
    for category in ("signature", "sbom", "vex")
}


def _digest(number: int) -> str:
    return f"sha256:{number:064x}"


def _write_report(
    root: Path,
    relative: str,
    severities: list[str] | None = None,
    *,
    artifact_id: str | None = None,
    image_digest: str | None = None,
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    vulnerabilities = [
        {"VulnerabilityID": f"TEST-{index}", "Severity": severity}
        for index, severity in enumerate(severities or [])
    ]
    path.write_text(
        json.dumps(
            {
                "ArtifactID": artifact_id or TRIVY_ARTIFACT_IDS["linux/amd64"],
                "ArtifactType": "container_image",
                "Metadata": {
                    "RepoDigests": [
                        "registry.example/voice-worker@" + (image_digest or _digest(4))
                    ]
                },
                "SchemaVersion": 2,
                "Results": [
                    {
                        "Class": "os-pkgs",
                        "Target": relative,
                        "Vulnerabilities": vulnerabilities,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def closure_repo(tmp_path: Path) -> Path:
    for relative in closure.SOURCE_INPUTS:
        source = REPO_ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    _write_report(
        tmp_path,
        "build/voice-worker/trivy-amd64.json",
        artifact_id=TRIVY_ARTIFACT_IDS["linux/amd64"],
        image_digest=_digest(4),
    )
    _write_report(
        tmp_path,
        "build/voice-worker/trivy-arm64.json",
        ["LOW", "MEDIUM"],
        artifact_id=TRIVY_ARTIFACT_IDS["linux/arm64"],
        image_digest=_digest(5),
    )
    for category, platforms in EVIDENCE_BYTES.items():
        for platform, data in platforms.items():
            architecture = platform.rsplit("/", 1)[1]
            path = (
                tmp_path / "build" / "voice-worker" / f"{category}-{architecture}.json"
            )
            path.write_bytes(data)
    return tmp_path


def _artifact(category: str, platform: str) -> object:
    architecture = platform.rsplit("/", 1)[1]
    data = EVIDENCE_BYTES[category][platform]
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    return closure.BoundArtifact(
        path=f"build/voice-worker/{category}-{architecture}.json",
        sha256=digest,
        immutable_reference=(
            f"artifact://voice-worker/{category}/{architecture}#{digest}"
        ),
    )


def _evidence(root: Path, **changes: object) -> object:
    values: dict[str, object] = {
        "candidate_repository": CANDIDATE_REPOSITORY,
        "candidate_sha": CANDIDATE_SHA,
        "base_reference": f"registry.example/voice-base@{_digest(1)}",
        "base_index_digest": _digest(1),
        "base_manifests": {
            "linux/amd64": _digest(2),
            "linux/arm64": _digest(3),
        },
        "image_digests": {
            "linux/amd64": _digest(4),
            "linux/arm64": _digest(5),
        },
        "trivy_artifact_ids": dict(TRIVY_ARTIFACT_IDS),
        "trivy_version": "0.72.0",
        "trivy_db_digest": _digest(6),
        "trivy_db_updated_at": "2026-07-31T20:00:00Z",
        "trivy_scanned_at": "2026-07-31T21:00:00Z",
        "trivy_reports": {
            "linux/amd64": "build/voice-worker/trivy-amd64.json",
            "linux/arm64": "build/voice-worker/trivy-arm64.json",
        },
        "signature_bundles": {
            platform: _artifact("signature", platform) for platform in closure.PLATFORMS
        },
        "sboms": {
            platform: _artifact("sbom", platform) for platform in closure.PLATFORMS
        },
        "vex": {platform: _artifact("vex", platform) for platform in closure.PLATFORMS},
        "protected_owner_approval": None,
    }
    values.update(changes)
    evidence = closure.ClosureEvidence(**values)
    if evidence.protected_owner_approval is not None:
        return evidence
    subject_digest = closure.approval_subject_digest(root, evidence)
    approval_document = {
        "approved_at": "2026-07-31T22:00:00Z",
        "candidate": {
            "repository": evidence.candidate_repository,
            "sha": evidence.candidate_sha,
        },
        "closure_subject_sha256": subject_digest,
        "decision": "approved",
        "protected_workflow": {
            "artifact_id": 456,
            "repository": evidence.candidate_repository,
            "run_id": 123,
            "workflow_ref": (
                f"{evidence.candidate_repository}/.github/workflows/"
                f"voice-worker-closure.yml@{'b' * 40}"
            ),
        },
        "reviewer": {"database_id": 789, "login": "release-owner"},
        "schema": closure.PROTECTED_APPROVAL_SCHEMA,
    }
    data = closure._canonical(approval_document)
    path = root / "build/voice-worker/protected-owner-approval.json"
    path.write_bytes(data)
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    return dataclasses.replace(
        evidence,
        protected_owner_approval=closure.BoundArtifact(
            path="build/voice-worker/protected-owner-approval.json",
            sha256=digest,
            immutable_reference=f"artifact://protected/voice-worker-approval#{digest}",
        ),
    )


def _reports() -> dict[str, str]:
    return {
        "linux/amd64": "build/voice-worker/trivy-amd64.json",
        "linux/arm64": "build/voice-worker/trivy-arm64.json",
    }


def _raw_external_paths() -> dict[str, object]:
    return {
        "signature_bundles": {
            platform: f"build/voice-worker/signature-{platform.rsplit('/', 1)[1]}.json"
            for platform in closure.PLATFORMS
        },
        "sboms": {
            platform: f"build/voice-worker/sbom-{platform.rsplit('/', 1)[1]}.json"
            for platform in closure.PLATFORMS
        },
        "vex": {
            platform: f"build/voice-worker/vex-{platform.rsplit('/', 1)[1]}.json"
            for platform in closure.PLATFORMS
        },
        "protected_owner_approval": (
            "build/voice-worker/protected-owner-approval.json"
        ),
    }


def test_manifest_is_deterministic_canonical_and_non_circular(
    closure_repo: Path,
) -> None:
    evidence = _evidence(closure_repo)
    first = closure.write_manifest(closure_repo, evidence)
    path = closure_repo / closure.MANIFEST_PATH
    first_bytes = path.read_bytes()
    second = closure.write_manifest(closure_repo, evidence)

    assert (
        first
        == second
        == f"sha256:{__import__('hashlib').sha256(first_bytes).hexdigest()}"
    )
    assert path.read_bytes() == first_bytes
    document = json.loads(first_bytes)
    assert first_bytes == closure._canonical(document)
    assert document["schema"] == closure.SCHEMA
    assert document["candidate"] == {
        "repository": CANDIDATE_REPOSITORY,
        "sha": CANDIDATE_SHA,
    }
    assert set(document["distribution_evidence"]) == {"signatures", "sboms", "vex"}
    assert document["protected_owner_approval"]["decision"] == "approved"
    assert document["protected_owner_approval"]["closure_subject_sha256"] == (
        closure.approval_subject_digest(closure_repo, evidence)
    )
    assert set(document["source_inputs"]) == set(closure.SOURCE_INPUTS)
    assert closure.MANIFEST_PATH not in document["source_inputs"]
    assert document["worker_images"]["platform_digests"] == {
        "linux/amd64": _digest(4),
        "linux/arm64": _digest(5),
    }
    assert (
        document["trivy"]["platform_results"]["linux/amd64"]["total_vulnerabilities"]
        == 0
    )
    assert document["trivy"]["platform_results"]["linux/arm64"]["severity_counts"] == {
        "CRITICAL": 0,
        "HIGH": 0,
        "LOW": 1,
        "MEDIUM": 1,
        "UNKNOWN": 0,
    }
    assert closure.verify_manifest(closure_repo) == first
    assert closure.verify_manifest(closure_repo, _reports()) == first
    assert (
        closure.verify_manifest(
            closure_repo,
            _reports(),
            **_raw_external_paths(),
        )
        == first
    )


def test_trivy_identity_binding_rejects_artifact_and_repo_digest_substitution(
    closure_repo: Path,
) -> None:
    _write_report(
        closure_repo,
        "build/voice-worker/trivy-amd64.json",
        artifact_id=_digest(999),
        image_digest=_digest(4),
    )
    with pytest.raises(closure.ClosureError, match="ArtifactID does not match"):
        closure.build_manifest(closure_repo, _evidence(closure_repo))

    _write_report(
        closure_repo,
        "build/voice-worker/trivy-amd64.json",
        artifact_id=TRIVY_ARTIFACT_IDS["linux/amd64"],
        image_digest=_digest(999),
    )
    with pytest.raises(closure.ClosureError, match="RepoDigests do not match"):
        closure.build_manifest(closure_repo, _evidence(closure_repo))


def test_distribution_evidence_and_protected_approval_fail_closed_on_drift(
    closure_repo: Path,
) -> None:
    evidence = _evidence(closure_repo)
    closure.write_manifest(closure_repo, evidence)
    signature = closure_repo / "build/voice-worker/signature-amd64.json"
    signature.write_bytes(signature.read_bytes() + b"drift")
    with pytest.raises(closure.ClosureError, match="raw signatures evidence drift"):
        closure.verify_manifest(closure_repo, **_raw_external_paths())

    signature.write_bytes(EVIDENCE_BYTES["signature"]["linux/amd64"])
    approval = closure_repo / "build/voice-worker/protected-owner-approval.json"
    document = json.loads(approval.read_bytes())
    document["closure_subject_sha256"] = _digest(999)
    approval.write_bytes(closure._canonical(document))
    with pytest.raises(closure.ClosureError, match="approval evidence drift"):
        closure.verify_manifest(closure_repo, **_raw_external_paths())


def test_unapproved_snapshot_is_canonical_explicit_and_never_final(
    closure_repo: Path,
) -> None:
    first = closure.write_unapproved_manifest(closure_repo)
    path = closure_repo / closure.MANIFEST_PATH
    first_bytes = path.read_bytes()
    second = closure.write_unapproved_manifest(closure_repo)

    assert first == second
    assert path.read_bytes() == first_bytes
    document = json.loads(first_bytes)
    assert first_bytes == closure._canonical(document)
    assert document["schema"] == closure.UNAPPROVED_SCHEMA
    assert document["architecture"] == "rtc-only"
    assert document["approval"] == closure.UNAPPROVED_APPROVAL
    assert document["base_image"]["fallback"] == closure.FALLBACK_BASE
    assert document["base_image"]["fallback_distribution_approved"] is False
    assert document["base_image"]["final_index_digest"] is None
    assert set(document["release_gates"]) == set(closure.UNAPPROVED_GATE_STATUSES)
    for gate, status in closure.UNAPPROVED_GATE_STATUSES.items():
        assert document["release_gates"][gate] == {
            "approved": False,
            "evidence_digest": None,
            "status": status,
        }
    assert document["worker_images"] == {
        "platform_digests": {"linux/amd64": None, "linux/arm64": None},
        "status": "not_produced",
    }
    assert closure.verify_unapproved_manifest(closure_repo) == first
    with pytest.raises(closure.ClosureError, match="distribution-unapproved"):
        closure.verify_manifest(closure_repo)


def test_checked_in_unapproved_snapshot_and_rtc_audit_are_current() -> None:
    digest = closure.verify_unapproved_manifest(REPO_ROOT)
    document = json.loads((REPO_ROOT / closure.MANIFEST_PATH).read_bytes())

    assert document["dependencies"]["direct_pins"] == closure.EXPECTED_DIRECT_PINS
    assert document["dependencies"]["locked_pins"] == closure.EXPECTED_LOCK_PINS
    assert RTC_AUDIT_PATH.is_file()
    audit = RTC_AUDIT_PATH.read_text(encoding="utf-8")
    assert digest in audit
    assert closure.EXPECTED_REQUIREMENTS_LOCK_SHA256 in audit
    assert closure.EXPECTED_MODEL_SHA256 in audit
    for label in (
        "DHI base verification",
        "Multi-architecture final images",
        "Signature verification",
        "SBOM verification",
        "VEX verification",
        "Protected approval",
        "T004",
        "T168",
    ):
        assert label in audit
    assert "**Distribution approved**: No" in audit


def test_unapproved_snapshot_rejects_self_approval_and_unknown_fields(
    closure_repo: Path,
) -> None:
    closure.write_unapproved_manifest(closure_repo)
    path = closure_repo / closure.MANIFEST_PATH
    document = json.loads(path.read_bytes())
    document["approval"]["distribution_approved"] = True
    path.write_bytes(closure._canonical(document))
    with pytest.raises(closure.ClosureError, match="distribution-blocked"):
        closure.verify_unapproved_manifest(closure_repo)

    closure.write_unapproved_manifest(closure_repo)
    document = json.loads(path.read_bytes())
    document["release_gates"]["signature"]["unexpected"] = "candidate claim"
    path.write_bytes(closure._canonical(document))
    with pytest.raises(closure.ClosureError, match="wrong keys"):
        closure.verify_unapproved_manifest(closure_repo)


def test_unapproved_snapshot_rejects_every_placeholder_and_identity_tamper(
    closure_repo: Path,
) -> None:
    original = closure.build_unapproved_manifest(closure_repo)
    source = closure.SOURCE_INPUTS[0]
    cases = (
        (("schema",), "unsupported", "unsupported schema"),
        (("architecture",), "agents", "architecture drifted"),
        (
            ("dependencies", "requirements_lock_sha256"),
            _digest(7),
            "dependency identities drifted",
        ),
        (("source_inputs", source, "size_bytes"), 0, "source input drift"),
        (
            ("base_image", "fallback", "reference"),
            "python:mutable",
            "fallback base identity drifted",
        ),
        (
            ("base_image", "fallback_distribution_approved"),
            True,
            "distribution-unapproved",
        ),
        (("base_image", "final_index_digest"), _digest(8), "must remain null"),
        (
            ("base_image", "final_platform_manifests", "linux/amd64"),
            _digest(9),
            "manifests must remain null",
        ),
        (
            ("release_gates", "signature", "approved"),
            True,
            "release gate drifted",
        ),
        (("worker_images", "status"), "verified", "image status drifted"),
        (
            ("worker_images", "platform_digests", "linux/arm64"),
            _digest(10),
            "image digests must remain null",
        ),
    )
    for path, value, message in cases:
        document = copy.deepcopy(original)
        target = document
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(closure.ClosureError, match=message):
            closure._validate_unapproved_manifest_document(closure_repo, document)

    closure.write_unapproved_manifest(closure_repo)
    path = closure_repo / closure.MANIFEST_PATH
    document = json.loads(path.read_bytes())
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    with pytest.raises(closure.ClosureError, match="not canonical"):
        closure.verify_unapproved_manifest(closure_repo)


def test_manifest_enforces_exact_four_pin_nine_package_worker_closure(
    closure_repo: Path,
) -> None:
    assert closure.EXPECTED_DIRECT_PINS == {
        "livekit": "1.1.14",
        "numpy": "2.4.6",
        "onnxruntime": "1.28.0",
        "websockets": "17.0.1",
    }
    assert closure.EXPECTED_LOCK_PINS == {
        "aiofiles": "25.1.0",
        "flatbuffers": "25.12.19",
        "livekit": "1.1.14",
        "numpy": "2.4.6",
        "onnxruntime": "1.28.0",
        "packaging": "26.2",
        "protobuf": "7.35.1",
        "types-protobuf": "7.34.1.20260518",
        "websockets": "17.0.1",
    }
    assert closure.EXPECTED_REQUIREMENTS_LOCK_SHA256 == (
        "sha256:fb86c9318d01ce59afaccba57842ddde1d098444e527c70b272b81af4ebc61b3"
    )
    manifest = closure.build_manifest(closure_repo, _evidence(closure_repo))
    assert (
        manifest["source_inputs"]["backend/voice_agent/requirements.lock.txt"]["sha256"]
        == closure.EXPECTED_REQUIREMENTS_LOCK_SHA256
    )

    lock = closure_repo / "backend/voice_agent/requirements.lock.txt"
    lock.write_bytes(lock.read_bytes() + b"\n")
    with pytest.raises(closure.ClosureError, match="approved closure"):
        closure.build_manifest(closure_repo, _evidence(closure_repo))


def test_verify_rejects_noncanonical_unknown_schema_and_source_drift(
    closure_repo: Path,
) -> None:
    evidence = _evidence(closure_repo)
    closure.write_manifest(closure_repo, evidence)
    path = closure_repo / closure.MANIFEST_PATH
    document = json.loads(path.read_bytes())
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    with pytest.raises(closure.ClosureError, match="not canonical"):
        closure.verify_manifest(closure_repo)

    closure.write_manifest(closure_repo, evidence)
    document = json.loads(path.read_bytes())
    document["unexpected"] = True
    path.write_bytes(closure._canonical(document))
    with pytest.raises(closure.ClosureError, match="wrong keys"):
        closure.verify_manifest(closure_repo)

    closure.write_manifest(closure_repo, evidence)
    dockerfile = closure_repo / "Dockerfile.voice"
    dockerfile.write_text(dockerfile.read_text(encoding="utf-8") + "\n# drift\n")
    with pytest.raises(closure.ClosureError, match="source input drift"):
        closure.verify_manifest(closure_repo)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"base_index_digest": "sha256:1234"}, "lowercase sha256"),
        ({"base_reference": "registry.example/mutable:latest"}, "immutable OCI"),
        (
            {"base_manifests": {"linux/amd64": _digest(2)}},
            "wrong keys",
        ),
        (
            {"trivy_version": "latest"},
            "exact semantic version",
        ),
        (
            {"trivy_scanned_at": "2026-07-31T19:59:59Z"},
            "predates",
        ),
        (
            {
                "trivy_reports": {
                    "linux/amd64": "../trivy.json",
                    "linux/arm64": "build/voice-worker/trivy-arm64.json",
                }
            },
            "repository root",
        ),
    ],
)
def test_generate_rejects_invalid_or_incomplete_evidence(
    closure_repo: Path, changes: dict[str, object], message: str
) -> None:
    with pytest.raises(closure.ClosureError, match=message):
        closure.build_manifest(closure_repo, _evidence(closure_repo, **changes))


def test_generate_rejects_high_findings_and_malformed_scan_json(
    closure_repo: Path,
) -> None:
    _write_report(
        closure_repo,
        "build/voice-worker/trivy-amd64.json",
        ["LOW", "HIGH"],
    )
    with pytest.raises(closure.ClosureError, match="HIGH/CRITICAL"):
        closure.build_manifest(closure_repo, _evidence(closure_repo))

    report = closure_repo / "build/voice-worker/trivy-amd64.json"
    report.write_text(
        '{"SchemaVersion":2,"Results":[{}],"Results":[{}]}', encoding="utf-8"
    )
    with pytest.raises(closure.ClosureError, match="duplicate JSON key"):
        closure.build_manifest(closure_repo, _evidence(closure_repo))

    report.write_text(
        json.dumps(
            {
                "ArtifactID": TRIVY_ARTIFACT_IDS["linux/amd64"],
                "ArtifactType": "container_image",
                "Metadata": {
                    "RepoDigests": [f"registry.example/voice-worker@{_digest(4)}"]
                },
                "SchemaVersion": 2,
                "Results": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(closure.ClosureError, match="must be non-empty"):
        closure.build_manifest(closure_repo, _evidence(closure_repo))


def test_verify_rehashes_raw_scan_evidence(closure_repo: Path) -> None:
    closure.write_manifest(closure_repo, _evidence(closure_repo))
    _write_report(
        closure_repo,
        "build/voice-worker/trivy-arm64.json",
        ["LOW", "MEDIUM", "MEDIUM"],
        artifact_id=TRIVY_ARTIFACT_IDS["linux/arm64"],
        image_digest=_digest(5),
    )
    with pytest.raises(closure.ClosureError, match="Trivy report drift"):
        closure.verify_manifest(closure_repo, _reports())


def test_source_and_destination_symlinks_fail_closed(closure_repo: Path) -> None:
    requirements = closure_repo / "backend/voice_agent/requirements.in"
    real_requirements = closure_repo / "requirements.real"
    requirements.replace(real_requirements)
    requirements.symlink_to(real_requirements)
    with pytest.raises(closure.ClosureError, match="symlinks are prohibited"):
        closure.build_manifest(closure_repo, _evidence(closure_repo))

    requirements.unlink()
    shutil.copyfile(real_requirements, requirements)
    manifest = closure_repo / closure.MANIFEST_PATH
    target = closure_repo / "manifest-target.json"
    target.write_text("{}\n", encoding="utf-8")
    manifest.symlink_to(target)
    with pytest.raises(closure.ClosureError, match="must not be a symlink"):
        closure.write_manifest(closure_repo, _evidence(closure_repo))


def test_verify_rejects_tampered_counts_and_platform_binding(
    closure_repo: Path,
) -> None:
    closure.write_manifest(closure_repo, _evidence(closure_repo))
    path = closure_repo / closure.MANIFEST_PATH
    document = json.loads(path.read_bytes())
    amd64 = document["trivy"]["platform_results"]["linux/amd64"]
    amd64["total_vulnerabilities"] = 1
    path.write_bytes(closure._canonical(document))
    with pytest.raises(closure.ClosureError, match="severity total mismatch"):
        closure.verify_manifest(closure_repo)

    closure.write_manifest(closure_repo, _evidence(closure_repo))
    document = json.loads(path.read_bytes())
    document["trivy"]["platform_results"]["linux/arm64"]["image_digest"] = _digest(9)
    path.write_bytes(closure._canonical(document))
    with pytest.raises(closure.ClosureError, match="not bound"):
        closure.verify_manifest(closure_repo)


def test_cli_generate_and_verify(
    closure_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = _evidence(closure_repo)
    assert evidence.protected_owner_approval is not None
    arguments = [
        "--repo-root",
        str(closure_repo),
        "generate",
        "--candidate-repository",
        evidence.candidate_repository,
        "--candidate-sha",
        evidence.candidate_sha,
        "--base-reference",
        f"registry.example/voice-base@{_digest(1)}",
        "--base-index-digest",
        _digest(1),
        "--base-amd64-manifest",
        _digest(2),
        "--base-arm64-manifest",
        _digest(3),
        "--image-amd64-digest",
        _digest(4),
        "--image-arm64-digest",
        _digest(5),
        "--trivy-amd64-artifact-id",
        TRIVY_ARTIFACT_IDS["linux/amd64"],
        "--trivy-arm64-artifact-id",
        TRIVY_ARTIFACT_IDS["linux/arm64"],
        "--trivy-version",
        "0.72.0",
        "--trivy-db-digest",
        _digest(6),
        "--trivy-db-updated-at",
        "2026-07-31T20:00:00Z",
        "--trivy-scanned-at",
        "2026-07-31T21:00:00Z",
        "--trivy-amd64-report",
        "build/voice-worker/trivy-amd64.json",
        "--trivy-arm64-report",
        "build/voice-worker/trivy-arm64.json",
    ]
    for prefix, artifacts in (
        ("signature", evidence.signature_bundles),
        ("sbom", evidence.sboms),
        ("vex", evidence.vex),
    ):
        for platform in closure.PLATFORMS:
            architecture = platform.rsplit("/", 1)[1]
            artifact = artifacts[platform]
            arguments.extend(
                [
                    f"--{prefix}-{architecture}-file",
                    artifact.path,
                    f"--{prefix}-{architecture}-sha256",
                    artifact.sha256,
                    f"--{prefix}-{architecture}-reference",
                    artifact.immutable_reference,
                ]
            )
    arguments.extend(
        [
            "--protected-owner-approval-file",
            evidence.protected_owner_approval.path,
            "--protected-owner-approval-sha256",
            evidence.protected_owner_approval.sha256,
            "--protected-owner-approval-reference",
            evidence.protected_owner_approval.immutable_reference,
        ]
    )
    assert closure.main(arguments) == 0
    assert "wrote backend/voice_agent/CLOSURE.json" in capsys.readouterr().out
    assert closure.main(["--repo-root", str(closure_repo), "verify"]) == 0
    assert "verified backend/voice_agent/CLOSURE.json" in capsys.readouterr().out


def test_cli_writes_and_verifies_unapproved_snapshot(
    closure_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root_arguments = ["--repo-root", str(closure_repo)]
    assert closure.main([*root_arguments, "snapshot-unapproved"]) == 0
    assert "wrote distribution-unapproved" in capsys.readouterr().out
    assert closure.main([*root_arguments, "verify-unapproved"]) == 0
    assert "verified distribution-unapproved" in capsys.readouterr().out


def test_low_level_parsers_reject_ambiguous_inputs(tmp_path: Path) -> None:
    for data, message in (
        (b'{"value":NaN}', "non-finite"),
        (b"{", "invalid JSON"),
        (b"\xff", "invalid JSON"),
    ):
        with pytest.raises(closure.ClosureError, match=message):
            closure._strict_json(data, label="test")

    for value, message in (
        ("", "non-empty"),
        ("build\\scan.json", "POSIX"),
        ("build//scan.json", "not normalized"),
        ("/build/scan.json", "repository root"),
    ):
        with pytest.raises(closure.ClosureError, match=message):
            closure._safe_relative(value, label="test")

    with pytest.raises(closure.ClosureError, match="real directory"):
        closure._repo_root(tmp_path / "missing")
    with pytest.raises(closure.ClosureError, match="required file is missing"):
        closure._safe_file(tmp_path, "missing", label="test")
    (tmp_path / "directory").mkdir()
    with pytest.raises(closure.ClosureError, match="not a regular file"):
        closure._safe_file(tmp_path, "directory", label="test")
    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"xx")
    with pytest.raises(closure.ClosureError, match="exceeds"):
        closure._read_file(tmp_path, "oversized", label="test", limit=1)

    for value, message in (
        ("2026-07-31T21:00:00+00:00", "canonical UTC"),
        ("2026-02-30T21:00:00Z", "invalid UTC"),
    ):
        with pytest.raises(closure.ClosureError, match=message):
            closure._timestamp(value, label="test")
    with pytest.raises(closure.ClosureError, match="expected an object"):
        closure._exact_keys([], {"value"}, label="test")


def test_requirement_parser_fails_closed() -> None:
    with pytest.raises(closure.ClosureError, match="not UTF-8"):
        closure._requirement_records(b"\xff", require_hashes=False)
    with pytest.raises(closure.ClosureError, match="non-exact pin"):
        closure._requirement_records(b"package>=1\n", require_hashes=False)
    with pytest.raises(closure.ClosureError, match="repeats package"):
        closure._requirement_records(b"package==1\npackage==1\n", require_hashes=False)
    with pytest.raises(closure.ClosureError, match="no SHA-256"):
        closure._requirement_records(b"package==1\n", require_hashes=True)


def test_approved_source_content_checks_fail_closed(closure_repo: Path) -> None:
    dockerignore = closure_repo / "Dockerfile.voice.dockerignore"
    original_ignore = dockerignore.read_bytes()
    dockerignore.write_bytes(original_ignore + b"\n!backend/voice_agent/CLOSURE.json\n")
    with pytest.raises(closure.ClosureError, match="must not be an image input"):
        closure.build_manifest(closure_repo, _evidence(closure_repo))

    dockerignore.write_bytes(original_ignore)
    provenance = (
        closure_repo / "backend/voice_agent/licenses/SILERO_VAD_PROVENANCE.json"
    )
    original_provenance = provenance.read_bytes()
    document = json.loads(original_provenance)
    document["tag"] = "v6.1"
    provenance.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(closure.ClosureError, match="provenance"):
        closure.build_manifest(closure_repo, _evidence(closure_repo))

    provenance.write_bytes(original_provenance)
    model = closure_repo / "backend/voice_agent/models/silero_vad.onnx"
    with model.open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(closure.ClosureError, match="model bytes"):
        closure.build_manifest(closure_repo, _evidence(closure_repo))


@pytest.mark.parametrize(
    ("report", "message"),
    [
        ({"SchemaVersion": 1, "Results": [{}]}, "SchemaVersion"),
        ({"SchemaVersion": 2, "Results": ["bad"]}, "not an object"),
        (
            {"SchemaVersion": 2, "Results": [{"Vulnerabilities": {}}]},
            "vulnerabilities are invalid",
        ),
        (
            {"SchemaVersion": 2, "Results": [{"Vulnerabilities": ["bad"]}]},
            "vulnerability is not an object",
        ),
        (
            {
                "SchemaVersion": 2,
                "Results": [{"Vulnerabilities": [{"Severity": "EXTREME"}]}],
            },
            "unknown severity",
        ),
    ],
)
def test_scan_report_shape_is_strict(
    closure_repo: Path, report: dict[str, object], message: str
) -> None:
    path = closure_repo / "build/voice-worker/trivy-amd64.json"
    if report.get("SchemaVersion") == 2:
        report.setdefault("ArtifactID", TRIVY_ARTIFACT_IDS["linux/amd64"])
        report.setdefault("ArtifactType", "container_image")
        report.setdefault(
            "Metadata",
            {"RepoDigests": [f"registry.example/voice-worker@{_digest(4)}"]},
        )
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(closure.ClosureError, match=message):
        closure.build_manifest(closure_repo, _evidence(closure_repo))
