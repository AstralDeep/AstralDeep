#!/usr/bin/env python3
"""Generate and verify the canonical Feature 065 voice-worker closure.

The manifest binds build inputs and independently produced image/scan evidence.
It is deliberately excluded from ``SOURCE_INPUTS`` so copying it into the image
cannot create a self-referential image-digest cycle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


SCHEMA = "astraldeep.voice-worker-closure.v2"
APPROVAL_SUBJECT_SCHEMA = "astraldeep.voice-worker-closure.approval-subject.v1"
PROTECTED_APPROVAL_SCHEMA = "astraldeep.voice-worker-closure.protected-approval.v1"
UNAPPROVED_SCHEMA = "astraldeep.voice-worker-closure.inventory.v1"
MANIFEST_PATH = "backend/voice_agent/CLOSURE.json"
PLATFORMS = ("linux/amd64", "linux/arm64")
SOURCE_INPUTS = (
    "backend/voice_agent/requirements.in",
    "backend/voice_agent/requirements.lock.txt",
    "backend/shared/voice_transcript.py",
    "Dockerfile.voice",
    "Dockerfile.voice.dockerignore",
    "backend/voice_agent/models/silero_vad.onnx",
    "backend/voice_agent/licenses/SILERO_VAD_LICENSE",
    "backend/voice_agent/licenses/SILERO_VAD_PROVENANCE.json",
)
SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?\Z"
)
REVIEWER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
IMMUTABLE_REFERENCE_RE = re.compile(
    r"[a-z][a-z0-9+.-]*://[^\s#]+#sha256:[0-9a-f]{64}\Z"
)
WORKFLOW_REF_RE = re.compile(
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/"
    r"[A-Za-z0-9_.-]+\.ya?ml@[0-9a-f]{40}\Z"
)
MAX_JSON_BYTES = 64 * 1024 * 1024
EVIDENCE_MEDIA_TYPES = {
    "signatures": "application/vnd.dev.sigstore.bundle+json;version=0.3",
    "sboms": "application/spdx+json",
    "vex": "application/openvex+json",
}
EXPECTED_DIRECT_PINS = {
    "livekit": "1.1.14",
    "numpy": "2.4.6",
    "onnxruntime": "1.28.0",
    "websockets": "17.0.1",
}
EXPECTED_LOCK_PINS = {
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
EXPECTED_REQUIREMENTS_LOCK_SHA256 = (
    "sha256:fb86c9318d01ce59afaccba57842ddde1d098444e527c70b272b81af4ebc61b3"
)
EXPECTED_MODEL_SHA256 = (
    "sha256:597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004"
)
EXPECTED_MODEL_BYTES = 2_327_524
EXPECTED_UPSTREAM_LICENSE_SHA256 = (
    "2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b"
)
EXPECTED_SILERO_COMMIT = "fba061dc5559f696e62171e9a0741782b0fdc23c"
FALLBACK_BASE = {
    "index_digest": (
        "sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba"
    ),
    "platform_manifests": {
        "linux/amd64": (
            "sha256:28255a3ace7eb4c48bc1b57b90af29e1bc82b4fd6c60614a8e3dce61b87ff941"
        ),
        "linux/arm64": (
            "sha256:3df1d95e3529533d0b646640edb63a0fde8a68597c0e7c62d34c4176678bb7d1"
        ),
    },
    "reference": (
        "python:3.11.15-slim-bookworm@"
        "sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba"
    ),
}
UNAPPROVED_GATE_STATUSES = {
    "dhi_base": "not_verified",
    "multi_architecture_runtime": "not_verified",
    "protected_policy": "not_approved",
    "sbom": "not_produced",
    "signature": "not_produced",
    "vex": "not_produced",
    "vulnerability_scan": "not_run_against_final_images",
}
UNAPPROVED_APPROVAL = {
    "artifact_export_authorized": False,
    "dhi_base_verified": False,
    "distribution_approved": False,
    "multi_architecture_runtime_verified": False,
    "owner_reviewed_fingerprint": False,
    "protected_policy_bound": False,
    "sbom_verified": False,
    "signatures_verified": False,
    "status": "blocked",
    "t004_complete": False,
    "t168_complete": False,
    "vex_verified": False,
    "zero_high_critical_scans_verified": False,
}


class ClosureError(RuntimeError):
    """The closure or one of its evidence inputs is invalid."""


@dataclass(frozen=True)
class BoundArtifact:
    """Raw evidence bytes plus their independently supplied immutable identity."""

    path: str
    sha256: str
    immutable_reference: str


@dataclass(frozen=True)
class ClosureEvidence:
    """Immutable identities supplied by the multi-platform build and scans."""

    candidate_repository: str
    candidate_sha: str
    base_reference: str
    base_index_digest: str
    base_manifests: Mapping[str, str]
    image_digests: Mapping[str, str]
    trivy_artifact_ids: Mapping[str, str]
    trivy_version: str
    trivy_db_digest: str
    trivy_db_updated_at: str
    trivy_scanned_at: str
    trivy_reports: Mapping[str, str]
    signature_bundles: Mapping[str, BoundArtifact]
    sboms: Mapping[str, BoundArtifact]
    vex: Mapping[str, BoundArtifact]
    protected_owner_approval: BoundArtifact | None = None


def _canonical(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _strict_json(data: bytes, *, label: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ClosureError(f"{label}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    def nonfinite(value: str) -> None:
        raise ClosureError(f"{label}: non-finite JSON value {value}")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClosureError(f"{label}: invalid JSON") from exc


def _safe_relative(value: str, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ClosureError(f"{label}: path must be a non-empty string")
    if "\\" in value:
        raise ClosureError(f"{label}: path must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ClosureError(f"{label}: path must stay beneath the repository root")
    if path.as_posix() != value:
        raise ClosureError(f"{label}: path is not normalized")
    return path


def _repo_root(root: Path) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise ClosureError("repository root must be a real directory, not a symlink")
    return root.resolve(strict=True)


def _safe_file(root: Path, relative: str, *, label: str) -> Path:
    pure = _safe_relative(relative, label=label)
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise ClosureError(f"{label}: symlinks are prohibited: {relative}")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise ClosureError(f"{label}: required file is missing: {relative}") from exc
    if resolved.parent != root and root not in resolved.parents:
        raise ClosureError(f"{label}: path escaped the repository root")
    if not resolved.is_file():
        raise ClosureError(f"{label}: path is not a regular file: {relative}")
    return resolved


def _read_file(
    root: Path, relative: str, *, label: str, limit: int | None = None
) -> bytes:
    path = _safe_file(root, relative, label=label)
    size = path.stat().st_size
    if limit is not None and size > limit:
        raise ClosureError(f"{label}: file exceeds {limit} bytes")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ClosureError(f"{label}: cannot read {relative}") from exc
    if len(data) != size:
        raise ClosureError(f"{label}: file changed while it was read")
    return data


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _require_digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ClosureError(f"{label}: expected a lowercase sha256:<64 hex> digest")
    return value


def _candidate(repository: Any, commit_sha: Any) -> dict[str, str]:
    if not isinstance(repository, str) or REPOSITORY_RE.fullmatch(repository) is None:
        raise ClosureError("candidate repository must be an exact owner/name identity")
    if not isinstance(commit_sha, str) or GIT_SHA_RE.fullmatch(commit_sha) is None:
        raise ClosureError(
            "candidate SHA must be one exact lowercase 40-character commit"
        )
    return {"repository": repository, "sha": commit_sha}


def _immutable_reference(value: Any, digest: str, *, label: str) -> str:
    if not isinstance(value, str) or IMMUTABLE_REFERENCE_RE.fullmatch(value) is None:
        raise ClosureError(
            f"{label}: immutable reference must be scheme://identity#sha256:<64 hex>"
        )
    if not value.endswith(f"#{digest}"):
        raise ClosureError(
            f"{label}: immutable reference does not bind the evidence bytes"
        )
    return value


def _positive_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ClosureError(f"{label}: expected a positive integer")
    return value


def _artifact_record(
    root: Path,
    artifact: Any,
    *,
    label: str,
    media_type: str,
    subject_digest: str,
) -> dict[str, Any]:
    if not isinstance(artifact, BoundArtifact):
        raise ClosureError(f"{label}: expected BoundArtifact evidence")
    expected = _require_digest(artifact.sha256, label=f"{label} bytes")
    reference = _immutable_reference(
        artifact.immutable_reference,
        expected,
        label=label,
    )
    data = _read_file(root, artifact.path, label=label, limit=MAX_JSON_BYTES)
    if _digest(data) != expected:
        raise ClosureError(f"{label}: raw evidence bytes do not match supplied digest")
    if not data:
        raise ClosureError(f"{label}: raw evidence must not be empty")
    return {
        "immutable_reference": reference,
        "media_type": media_type,
        "sha256": expected,
        "size_bytes": len(data),
        "subject_digest": subject_digest,
    }


def _artifact_records(
    root: Path,
    artifacts: Any,
    *,
    label: str,
    media_type: str,
    image_digests: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    records = _platform_map(artifacts, label=label)
    return {
        platform: _artifact_record(
            root,
            records[platform],
            label=f"{label} for {platform}",
            media_type=media_type,
            subject_digest=image_digests[platform],
        )
        for platform in PLATFORMS
    }


def _timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or UTC_RE.fullmatch(value) is None:
        raise ClosureError(f"{label}: expected canonical UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ClosureError(f"{label}: invalid UTC timestamp") from exc


def _exact_keys(value: Any, expected: set[str], *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ClosureError(f"{label}: expected an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ClosureError(f"{label}: wrong keys (missing={missing}, extra={extra})")
    return value


def _platform_map(value: Any, *, label: str) -> Mapping[str, Any]:
    return _exact_keys(value, set(PLATFORMS), label=label)


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_records(data: bytes, *, require_hashes: bool) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClosureError("dependency manifest is not UTF-8") from exc
    records = text.replace("\\\n", " ").splitlines()
    pins: dict[str, str] = {}
    for raw in records:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("--only-binary"):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)(.*)$", line)
        if match is None:
            raise ClosureError(f"dependency manifest contains a non-exact pin: {line}")
        name = _normalized_name(match.group(1))
        if name in pins:
            raise ClosureError(f"dependency manifest repeats {name}")
        if require_hashes and not re.search(
            r"(?:^|\s)--hash=sha256:[0-9a-f]{64}(?:\s|$)", match.group(3)
        ):
            raise ClosureError(f"locked dependency {name} has no SHA-256 hash")
        pins[name] = match.group(2)
    return pins


def _validate_approved_sources(root: Path, blobs: Mapping[str, bytes]) -> None:
    if (
        _requirement_records(
            blobs["backend/voice_agent/requirements.in"], require_hashes=False
        )
        != EXPECTED_DIRECT_PINS
    ):
        raise ClosureError(
            "requirements.in is not the approved four-pin RTC-only input"
        )
    lock = blobs["backend/voice_agent/requirements.lock.txt"]
    if _digest(lock) != EXPECTED_REQUIREMENTS_LOCK_SHA256:
        raise ClosureError(
            "requirements.lock.txt bytes do not match the approved closure"
        )
    if b"--only-binary :all:" not in lock:
        raise ClosureError("requirements.lock.txt is not binary-only")
    if _requirement_records(lock, require_hashes=True) != EXPECTED_LOCK_PINS:
        raise ClosureError(
            "requirements.lock.txt is not the approved nine-package closure"
        )

    model = blobs["backend/voice_agent/models/silero_vad.onnx"]
    if len(model) != EXPECTED_MODEL_BYTES or _digest(model) != EXPECTED_MODEL_SHA256:
        raise ClosureError(
            "Silero VAD model bytes do not match the approved v6.0 artifact"
        )
    license_bytes = blobs["backend/voice_agent/licenses/SILERO_VAD_LICENSE"]
    provenance = _strict_json(
        blobs["backend/voice_agent/licenses/SILERO_VAD_PROVENANCE.json"],
        label="Silero provenance",
    )
    provenance = _exact_keys(
        provenance,
        {
            "commit",
            "license_path",
            "model_path",
            "model_sha256",
            "model_size_bytes",
            "repository",
            "tag",
            "upstream_license_sha256",
            "vendored_license_sha256",
        },
        label="Silero provenance",
    )
    expected = {
        "commit": EXPECTED_SILERO_COMMIT,
        "license_path": "LICENSE",
        "model_path": "src/silero_vad/data/silero_vad.onnx",
        "model_sha256": EXPECTED_MODEL_SHA256.removeprefix("sha256:"),
        "model_size_bytes": EXPECTED_MODEL_BYTES,
        "repository": "https://github.com/snakers4/silero-vad",
        "tag": "v6.0",
        "upstream_license_sha256": EXPECTED_UPSTREAM_LICENSE_SHA256,
        "vendored_license_sha256": _digest(license_bytes).removeprefix("sha256:"),
    }
    if provenance != expected:
        raise ClosureError("Silero provenance does not match the approved artifacts")

    dockerignore = blobs["Dockerfile.voice.dockerignore"].decode("utf-8")
    if "CLOSURE.json" in dockerignore:
        raise ClosureError("CLOSURE.json must not be an image input")
    del root  # The validated root is intentionally not serialized.


def _source_records(root: Path) -> dict[str, dict[str, Any]]:
    blobs = {
        relative: _read_file(root, relative, label=f"source input {relative}")
        for relative in SOURCE_INPUTS
    }
    _validate_approved_sources(root, blobs)
    return {
        relative: {"path": relative, "sha256": _digest(data), "size_bytes": len(data)}
        for relative, data in blobs.items()
    }


def build_unapproved_manifest(repo_root: Path) -> dict[str, Any]:
    """Build the deterministic local snapshot used while release gates are open.

    This document intentionally contains no substitutable image, signature, SBOM,
    VEX, scan, or protected-policy evidence.  It records the exact locally
    reviewable closure and makes every unresolved distribution gate explicit.
    ``verify_manifest`` never accepts this schema as a final closure.
    """

    root = _repo_root(repo_root)
    manifest: dict[str, Any] = {
        "approval": dict(UNAPPROVED_APPROVAL),
        "architecture": "rtc-only",
        "base_image": {
            "fallback": FALLBACK_BASE,
            "fallback_distribution_approved": False,
            "final_index_digest": None,
            "final_platform_manifests": dict.fromkeys(PLATFORMS),
            "final_reference": None,
        },
        "dependencies": {
            "direct_pins": dict(EXPECTED_DIRECT_PINS),
            "locked_pins": dict(EXPECTED_LOCK_PINS),
            "requirements_lock_sha256": EXPECTED_REQUIREMENTS_LOCK_SHA256,
        },
        "release_gates": {
            gate: {
                "approved": False,
                "evidence_digest": None,
                "status": status,
            }
            for gate, status in UNAPPROVED_GATE_STATUSES.items()
        },
        "schema": UNAPPROVED_SCHEMA,
        "source_inputs": _source_records(root),
        "worker_images": {
            "platform_digests": dict.fromkeys(PLATFORMS),
            "status": "not_produced",
        },
    }
    _validate_unapproved_manifest_document(root, manifest)
    return manifest


def _validate_unapproved_manifest_document(
    root: Path, document: Any
) -> Mapping[str, Any]:
    manifest = _exact_keys(
        document,
        {
            "approval",
            "architecture",
            "base_image",
            "dependencies",
            "release_gates",
            "schema",
            "source_inputs",
            "worker_images",
        },
        label="unapproved closure manifest",
    )
    if manifest["schema"] != UNAPPROVED_SCHEMA:
        raise ClosureError("unapproved closure manifest: unsupported schema")
    if manifest["architecture"] != "rtc-only":
        raise ClosureError("unapproved closure manifest: architecture drifted")

    approval = _exact_keys(
        manifest["approval"],
        set(UNAPPROVED_APPROVAL),
        label="approval",
    )
    if approval != UNAPPROVED_APPROVAL:
        raise ClosureError(
            "unapproved closure manifest must remain distribution-blocked"
        )

    dependencies = _exact_keys(
        manifest["dependencies"],
        {"direct_pins", "locked_pins", "requirements_lock_sha256"},
        label="dependencies",
    )
    if dependencies != {
        "direct_pins": EXPECTED_DIRECT_PINS,
        "locked_pins": EXPECTED_LOCK_PINS,
        "requirements_lock_sha256": EXPECTED_REQUIREMENTS_LOCK_SHA256,
    }:
        raise ClosureError("unapproved closure dependency identities drifted")

    sources = _exact_keys(
        manifest["source_inputs"], set(SOURCE_INPUTS), label="source_inputs"
    )
    actual_sources = _source_records(root)
    for relative in SOURCE_INPUTS:
        record = _exact_keys(
            sources[relative], {"path", "sha256", "size_bytes"}, label=relative
        )
        if record != actual_sources[relative]:
            raise ClosureError(f"source input drift: {relative}")

    base = _exact_keys(
        manifest["base_image"],
        {
            "fallback",
            "fallback_distribution_approved",
            "final_index_digest",
            "final_platform_manifests",
            "final_reference",
        },
        label="base_image",
    )
    if base["fallback"] != FALLBACK_BASE:
        raise ClosureError("unapproved closure fallback base identity drifted")
    if base["fallback_distribution_approved"] is not False:
        raise ClosureError("fallback base must remain distribution-unapproved")
    if base["final_index_digest"] is not None or base["final_reference"] is not None:
        raise ClosureError("unverified final base identity must remain null")
    final_platforms = _platform_map(
        base["final_platform_manifests"], label="final base platform manifests"
    )
    if any(final_platforms[platform] is not None for platform in PLATFORMS):
        raise ClosureError("unverified final base manifests must remain null")

    gates = _exact_keys(
        manifest["release_gates"],
        set(UNAPPROVED_GATE_STATUSES),
        label="release_gates",
    )
    for gate, status in UNAPPROVED_GATE_STATUSES.items():
        record = _exact_keys(
            gates[gate], {"approved", "evidence_digest", "status"}, label=gate
        )
        if record != {
            "approved": False,
            "evidence_digest": None,
            "status": status,
        }:
            raise ClosureError(f"unapproved release gate drifted: {gate}")

    worker = _exact_keys(
        manifest["worker_images"],
        {"platform_digests", "status"},
        label="worker_images",
    )
    if worker["status"] != "not_produced":
        raise ClosureError("unapproved worker image status drifted")
    image_platforms = _platform_map(
        worker["platform_digests"], label="worker image platform digests"
    )
    if any(image_platforms[platform] is not None for platform in PLATFORMS):
        raise ClosureError("unverified worker image digests must remain null")
    return manifest


def _scan_report(
    root: Path,
    relative: str,
    *,
    platform: str,
    artifact_id: str,
    image_digest: str,
) -> dict[str, Any]:
    data = _read_file(
        root,
        relative,
        label=f"Trivy report for {platform}",
        limit=MAX_JSON_BYTES,
    )
    report = _strict_json(data, label=f"Trivy report for {platform}")
    if not isinstance(report, dict) or report.get("SchemaVersion") != 2:
        raise ClosureError(f"Trivy report for {platform}: SchemaVersion must be 2")
    if report.get("ArtifactType") != "container_image":
        raise ClosureError(
            f"Trivy report for {platform}: ArtifactType must be container_image"
        )
    report_artifact_id = _require_digest(
        report.get("ArtifactID"), label=f"Trivy ArtifactID for {platform}"
    )
    if report_artifact_id != artifact_id:
        raise ClosureError(
            f"Trivy report for {platform}: ArtifactID does not match supplied identity"
        )
    metadata = report.get("Metadata")
    if not isinstance(metadata, dict):
        raise ClosureError(f"Trivy report for {platform}: Metadata must be an object")
    repo_digests = metadata.get("RepoDigests")
    if not isinstance(repo_digests, list) or not repo_digests:
        raise ClosureError(
            f"Trivy report for {platform}: RepoDigests must be non-empty"
        )
    observed_repo_digests: set[str] = set()
    for index, reference in enumerate(repo_digests):
        if (
            not isinstance(reference, str)
            or reference.count("@") != 1
            or any(character.isspace() for character in reference)
        ):
            raise ClosureError(
                f"Trivy report for {platform}: RepoDigests[{index}] is not immutable"
            )
        name, digest = reference.rsplit("@", 1)
        if not name:
            raise ClosureError(
                f"Trivy report for {platform}: RepoDigests[{index}] has no repository"
            )
        observed_repo_digests.add(
            _require_digest(digest, label=f"Trivy RepoDigest for {platform}")
        )
    if observed_repo_digests != {image_digest}:
        raise ClosureError(
            f"Trivy report for {platform}: RepoDigests do not match supplied image digest"
        )
    results = report.get("Results")
    if not isinstance(results, list) or not results:
        raise ClosureError(f"Trivy report for {platform}: Results must be non-empty")
    counts = dict.fromkeys(SEVERITIES, 0)
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ClosureError(
                f"Trivy report for {platform}: result {index} is not an object"
            )
        vulnerabilities = result.get("Vulnerabilities", [])
        if vulnerabilities is None:
            vulnerabilities = []
        if not isinstance(vulnerabilities, list):
            raise ClosureError(
                f"Trivy report for {platform}: result {index} vulnerabilities are invalid"
            )
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise ClosureError(
                    f"Trivy report for {platform}: vulnerability is not an object"
                )
            severity = vulnerability.get("Severity", "UNKNOWN")
            if severity not in counts:
                raise ClosureError(
                    f"Trivy report for {platform}: unknown severity {severity!r}"
                )
            counts[severity] += 1
    if counts["HIGH"] or counts["CRITICAL"]:
        raise ClosureError(
            f"Trivy report for {platform}: HIGH/CRITICAL findings block closure"
        )
    return {
        "artifact_id": report_artifact_id,
        "image_digest": image_digest,
        "report_sha256": _digest(data),
        "report_size_bytes": len(data),
        "result_count": len(results),
        "severity_counts": counts,
        "total_vulnerabilities": sum(counts.values()),
    }


def _validate_evidence(evidence: ClosureEvidence) -> None:
    _candidate(evidence.candidate_repository, evidence.candidate_sha)
    _require_digest(evidence.base_index_digest, label="base OCI index")
    suffix = f"@{evidence.base_index_digest}"
    if (
        not evidence.base_reference.endswith(suffix)
        or evidence.base_reference == suffix
    ):
        raise ClosureError("base reference must end in its immutable OCI index digest")
    if any(character.isspace() for character in evidence.base_reference):
        raise ClosureError("base reference must not contain whitespace")
    for label, values in (
        ("base platform manifests", evidence.base_manifests),
        ("worker image digests", evidence.image_digests),
        ("Trivy artifact identities", evidence.trivy_artifact_ids),
        ("Trivy reports", evidence.trivy_reports),
        ("signature bundles", evidence.signature_bundles),
        ("SBOMs", evidence.sboms),
        ("VEX documents", evidence.vex),
    ):
        _platform_map(values, label=label)
    for platform in PLATFORMS:
        _require_digest(
            evidence.base_manifests[platform], label=f"base manifest {platform}"
        )
        _require_digest(
            evidence.image_digests[platform], label=f"worker image {platform}"
        )
        _require_digest(
            evidence.trivy_artifact_ids[platform],
            label=f"Trivy ArtifactID {platform}",
        )
        _safe_relative(
            evidence.trivy_reports[platform], label=f"Trivy report {platform}"
        )
    if VERSION_RE.fullmatch(evidence.trivy_version) is None:
        raise ClosureError("Trivy version must be an exact semantic version")
    _require_digest(evidence.trivy_db_digest, label="Trivy database")
    updated = _timestamp(
        evidence.trivy_db_updated_at, label="Trivy database updated_at"
    )
    scanned = _timestamp(evidence.trivy_scanned_at, label="Trivy scanned_at")
    if scanned < updated:
        raise ClosureError("Trivy scan predates its vulnerability database")


def _build_approval_subject(root: Path, evidence: ClosureEvidence) -> dict[str, Any]:
    """Build the non-circular bytes that protected policy must approve."""

    _validate_evidence(evidence)
    images = dict(evidence.image_digests)
    scans = {
        platform: {
            **_scan_report(
                root,
                evidence.trivy_reports[platform],
                platform=platform,
                artifact_id=evidence.trivy_artifact_ids[platform],
                image_digest=images[platform],
            ),
        }
        for platform in PLATFORMS
    }
    distribution_evidence = {
        category: _artifact_records(
            root,
            getattr(
                evidence, "signature_bundles" if category == "signatures" else category
            ),
            label=category,
            media_type=media_type,
            image_digests=images,
        )
        for category, media_type in EVIDENCE_MEDIA_TYPES.items()
    }
    return {
        "base_image": {
            "index_digest": evidence.base_index_digest,
            "platform_manifests": dict(evidence.base_manifests),
            "reference": evidence.base_reference,
        },
        "candidate": _candidate(evidence.candidate_repository, evidence.candidate_sha),
        "distribution_evidence": distribution_evidence,
        "schema": APPROVAL_SUBJECT_SCHEMA,
        "source_inputs": _source_records(root),
        "trivy": {
            "database": {
                "digest": evidence.trivy_db_digest,
                "updated_at": evidence.trivy_db_updated_at,
            },
            "platform_results": scans,
            "scanned_at": evidence.trivy_scanned_at,
            "version": evidence.trivy_version,
        },
        "worker_images": {"platform_digests": images},
    }


def approval_subject_digest(repo_root: Path, evidence: ClosureEvidence) -> str:
    """Return the exact non-circular digest a protected owner must approve."""

    root = _repo_root(repo_root)
    return _digest(_canonical(_build_approval_subject(root, evidence)))


def _protected_approval(
    root: Path,
    artifact: BoundArtifact | None,
    *,
    candidate: Mapping[str, str],
    subject_digest: str,
) -> dict[str, Any]:
    if artifact is None:
        raise ClosureError(
            "protected owner approval evidence is required for final closure"
        )
    record = _artifact_record(
        root,
        artifact,
        label="protected owner approval",
        media_type="application/vnd.astraldeep.protected-approval+json;version=1",
        subject_digest=subject_digest,
    )
    data = _read_file(
        root, artifact.path, label="protected owner approval", limit=MAX_JSON_BYTES
    )
    document = _strict_json(data, label="protected owner approval")
    approval = _exact_keys(
        document,
        {
            "approved_at",
            "candidate",
            "closure_subject_sha256",
            "decision",
            "protected_workflow",
            "reviewer",
            "schema",
        },
        label="protected owner approval",
    )
    if approval["schema"] != PROTECTED_APPROVAL_SCHEMA:
        raise ClosureError("protected owner approval: unsupported schema")
    if approval["candidate"] != candidate:
        raise ClosureError("protected owner approval: candidate identity mismatch")
    if approval["closure_subject_sha256"] != subject_digest:
        raise ClosureError("protected owner approval: closure subject mismatch")
    if approval["decision"] != "approved":
        raise ClosureError("protected owner approval: decision is not approved")
    _timestamp(approval["approved_at"], label="protected owner approval approved_at")
    reviewer = _exact_keys(
        approval["reviewer"], {"database_id", "login"}, label="approval reviewer"
    )
    if (
        not isinstance(reviewer["login"], str)
        or REVIEWER_RE.fullmatch(reviewer["login"]) is None
    ):
        raise ClosureError("approval reviewer.login is malformed")
    _positive_integer(reviewer["database_id"], label="approval reviewer.database_id")
    workflow = _exact_keys(
        approval["protected_workflow"],
        {"artifact_id", "repository", "run_id", "workflow_ref"},
        label="protected approval workflow",
    )
    if workflow["repository"] != candidate["repository"]:
        raise ClosureError("protected approval workflow repository mismatch")
    if (
        not isinstance(workflow["workflow_ref"], str)
        or WORKFLOW_REF_RE.fullmatch(workflow["workflow_ref"]) is None
        or not workflow["workflow_ref"].startswith(
            f"{candidate['repository']}/.github/workflows/"
        )
    ):
        raise ClosureError("protected approval workflow_ref is not commit-pinned")
    _positive_integer(workflow["run_id"], label="protected approval workflow.run_id")
    _positive_integer(
        workflow["artifact_id"], label="protected approval workflow.artifact_id"
    )
    return {
        "approved_at": approval["approved_at"],
        "artifact": record,
        "closure_subject_sha256": subject_digest,
        "decision": "approved",
        "protected_workflow": dict(workflow),
        "reviewer": dict(reviewer),
    }


def build_manifest(repo_root: Path, evidence: ClosureEvidence) -> dict[str, Any]:
    """Build a validated canonical final manifest without writing it."""

    root = _repo_root(repo_root)
    subject = _build_approval_subject(root, evidence)
    subject_digest = _digest(_canonical(subject))
    manifest = dict(subject)
    manifest["schema"] = SCHEMA
    manifest["protected_owner_approval"] = _protected_approval(
        root,
        evidence.protected_owner_approval,
        candidate=manifest["candidate"],
        subject_digest=subject_digest,
    )
    _validate_manifest_document(root, manifest)
    return manifest


def _validate_artifact_manifest_record(
    value: Any,
    *,
    label: str,
    media_type: str,
    subject_digest: str,
) -> Mapping[str, Any]:
    record = _exact_keys(
        value,
        {
            "immutable_reference",
            "media_type",
            "sha256",
            "size_bytes",
            "subject_digest",
        },
        label=label,
    )
    digest = _require_digest(record["sha256"], label=f"{label}.sha256")
    _immutable_reference(record["immutable_reference"], digest, label=label)
    if record["media_type"] != media_type:
        raise ClosureError(f"{label}: media type mismatch")
    if record["subject_digest"] != subject_digest:
        raise ClosureError(f"{label}: subject digest mismatch")
    _positive_integer(record["size_bytes"], label=f"{label}.size_bytes")
    return record


def _validate_manifest_document(root: Path, document: Any) -> Mapping[str, Any]:
    manifest = _exact_keys(
        document,
        {
            "base_image",
            "candidate",
            "distribution_evidence",
            "protected_owner_approval",
            "schema",
            "source_inputs",
            "trivy",
            "worker_images",
        },
        label="closure manifest",
    )
    if manifest["schema"] != SCHEMA:
        raise ClosureError("closure manifest: unsupported schema")

    candidate = _exact_keys(
        manifest["candidate"], {"repository", "sha"}, label="candidate"
    )
    if dict(candidate) != _candidate(candidate["repository"], candidate["sha"]):
        raise ClosureError("candidate identity is not canonical")

    sources = _exact_keys(
        manifest["source_inputs"], set(SOURCE_INPUTS), label="source_inputs"
    )
    actual_sources = _source_records(root)
    for relative in SOURCE_INPUTS:
        record = _exact_keys(
            sources[relative], {"path", "sha256", "size_bytes"}, label=relative
        )
        if record != actual_sources[relative]:
            raise ClosureError(f"source input drift: {relative}")

    base = _exact_keys(
        manifest["base_image"],
        {"index_digest", "platform_manifests", "reference"},
        label="base_image",
    )
    index = _require_digest(base["index_digest"], label="base image index")
    if not isinstance(base["reference"], str) or not base["reference"].endswith(
        f"@{index}"
    ):
        raise ClosureError("base image reference is not bound to the index digest")
    base_platforms = _platform_map(
        base["platform_manifests"], label="base image platform manifests"
    )
    for platform in PLATFORMS:
        _require_digest(base_platforms[platform], label=f"base manifest {platform}")

    worker = _exact_keys(
        manifest["worker_images"], {"platform_digests"}, label="worker_images"
    )
    image_platforms = _platform_map(
        worker["platform_digests"], label="worker image platform digests"
    )
    for platform in PLATFORMS:
        _require_digest(image_platforms[platform], label=f"worker image {platform}")

    distribution = _exact_keys(
        manifest["distribution_evidence"],
        set(EVIDENCE_MEDIA_TYPES),
        label="distribution_evidence",
    )
    for category, media_type in EVIDENCE_MEDIA_TYPES.items():
        platform_records = _platform_map(
            distribution[category], label=f"distribution_evidence.{category}"
        )
        for platform in PLATFORMS:
            _validate_artifact_manifest_record(
                platform_records[platform],
                label=f"distribution_evidence.{category}.{platform}",
                media_type=media_type,
                subject_digest=image_platforms[platform],
            )

    trivy = _exact_keys(
        manifest["trivy"],
        {"database", "platform_results", "scanned_at", "version"},
        label="trivy",
    )
    if (
        not isinstance(trivy["version"], str)
        or VERSION_RE.fullmatch(trivy["version"]) is None
    ):
        raise ClosureError("trivy.version is not exact")
    database = _exact_keys(
        trivy["database"], {"digest", "updated_at"}, label="trivy.database"
    )
    _require_digest(database["digest"], label="trivy database")
    updated = _timestamp(database["updated_at"], label="trivy.database.updated_at")
    scanned = _timestamp(trivy["scanned_at"], label="trivy.scanned_at")
    if scanned < updated:
        raise ClosureError("trivy scan predates its vulnerability database")
    scan_platforms = _platform_map(
        trivy["platform_results"], label="trivy.platform_results"
    )
    for platform in PLATFORMS:
        result = _exact_keys(
            scan_platforms[platform],
            {
                "artifact_id",
                "image_digest",
                "report_sha256",
                "report_size_bytes",
                "result_count",
                "severity_counts",
                "total_vulnerabilities",
            },
            label=f"trivy result {platform}",
        )
        _require_digest(result["artifact_id"], label=f"Trivy ArtifactID {platform}")
        if result["image_digest"] != image_platforms[platform]:
            raise ClosureError(f"Trivy result is not bound to worker image {platform}")
        _require_digest(result["report_sha256"], label=f"Trivy report {platform}")
        for key in ("report_size_bytes", "result_count", "total_vulnerabilities"):
            if (
                isinstance(result[key], bool)
                or not isinstance(result[key], int)
                or result[key] < 0
            ):
                raise ClosureError(
                    f"trivy result {platform}.{key} must be non-negative"
                )
        if result["report_size_bytes"] == 0 or result["result_count"] == 0:
            raise ClosureError(f"trivy result {platform} cannot be empty")
        counts = _exact_keys(
            result["severity_counts"], set(SEVERITIES), label=f"severities {platform}"
        )
        for severity in SEVERITIES:
            count = counts[severity]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ClosureError(f"invalid {severity} count for {platform}")
        if sum(counts.values()) != result["total_vulnerabilities"]:
            raise ClosureError(f"severity total mismatch for {platform}")
        if counts["HIGH"] or counts["CRITICAL"]:
            raise ClosureError(f"HIGH/CRITICAL findings block closure for {platform}")

    protected = _exact_keys(
        manifest["protected_owner_approval"],
        {
            "approved_at",
            "artifact",
            "closure_subject_sha256",
            "decision",
            "protected_workflow",
            "reviewer",
        },
        label="protected_owner_approval",
    )
    if protected["decision"] != "approved":
        raise ClosureError("protected owner approval decision is not approved")
    _timestamp(protected["approved_at"], label="protected owner approval approved_at")
    subject = {
        key: value
        for key, value in manifest.items()
        if key != "protected_owner_approval"
    }
    subject["schema"] = APPROVAL_SUBJECT_SCHEMA
    subject_digest = _digest(_canonical(subject))
    if protected["closure_subject_sha256"] != subject_digest:
        raise ClosureError("protected owner approval subject digest mismatch")
    _validate_artifact_manifest_record(
        protected["artifact"],
        label="protected owner approval artifact",
        media_type="application/vnd.astraldeep.protected-approval+json;version=1",
        subject_digest=subject_digest,
    )
    reviewer = _exact_keys(
        protected["reviewer"], {"database_id", "login"}, label="approval reviewer"
    )
    if (
        not isinstance(reviewer["login"], str)
        or REVIEWER_RE.fullmatch(reviewer["login"]) is None
    ):
        raise ClosureError("approval reviewer.login is malformed")
    _positive_integer(reviewer["database_id"], label="approval reviewer.database_id")
    workflow = _exact_keys(
        protected["protected_workflow"],
        {"artifact_id", "repository", "run_id", "workflow_ref"},
        label="protected approval workflow",
    )
    if workflow["repository"] != candidate["repository"]:
        raise ClosureError("protected approval workflow repository mismatch")
    if (
        not isinstance(workflow["workflow_ref"], str)
        or WORKFLOW_REF_RE.fullmatch(workflow["workflow_ref"]) is None
        or not workflow["workflow_ref"].startswith(
            f"{candidate['repository']}/.github/workflows/"
        )
    ):
        raise ClosureError("protected approval workflow_ref is not commit-pinned")
    _positive_integer(workflow["run_id"], label="protected approval workflow.run_id")
    _positive_integer(
        workflow["artifact_id"], label="protected approval workflow.artifact_id"
    )
    return manifest


def _write_document(root: Path, document: Mapping[str, Any]) -> str:
    data = _canonical(document)
    destination = root / MANIFEST_PATH
    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ClosureError("closure manifest parent must be a real directory")
    if destination.is_symlink():
        raise ClosureError("closure manifest destination must not be a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".CLOSURE.", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return _digest(data)


def write_manifest(repo_root: Path, evidence: ClosureEvidence) -> str:
    """Atomically write a final ``CLOSURE.json`` and return its digest."""

    root = _repo_root(repo_root)
    return _write_document(root, build_manifest(root, evidence))


def write_unapproved_manifest(repo_root: Path) -> str:
    """Atomically write the canonical distribution-blocked local snapshot."""

    root = _repo_root(repo_root)
    return _write_document(root, build_unapproved_manifest(root))


def verify_manifest(
    repo_root: Path,
    trivy_reports: Mapping[str, str] | None = None,
    *,
    signature_bundles: Mapping[str, str] | None = None,
    sboms: Mapping[str, str] | None = None,
    vex: Mapping[str, str] | None = None,
    protected_owner_approval: str | None = None,
) -> str:
    """Verify canonical bytes, current inputs, and optional raw external evidence."""

    root = _repo_root(repo_root)
    manifest_data = _read_file(
        root, MANIFEST_PATH, label="closure manifest", limit=MAX_JSON_BYTES
    )
    document = _strict_json(manifest_data, label="closure manifest")
    if not isinstance(document, dict) or manifest_data != _canonical(document):
        raise ClosureError("closure manifest is not canonical JSON")
    if document.get("schema") == UNAPPROVED_SCHEMA:
        raise ClosureError(
            "distribution-unapproved candidate; final closure evidence is absent"
        )
    manifest = _validate_manifest_document(root, document)
    if trivy_reports is not None:
        reports = _platform_map(trivy_reports, label="verification Trivy reports")
        platform_results = manifest["trivy"]["platform_results"]
        for platform in PLATFORMS:
            expected = platform_results[platform]
            actual = _scan_report(
                root,
                reports[platform],
                platform=platform,
                artifact_id=expected["artifact_id"],
                image_digest=expected["image_digest"],
            )
            if actual != expected:
                raise ClosureError(f"Trivy report drift: {platform}")
    external_groups = {
        "signatures": signature_bundles,
        "sboms": sboms,
        "vex": vex,
    }
    supplied = [
        value is not None
        for value in (*external_groups.values(), protected_owner_approval)
    ]
    if any(supplied) and not all(supplied):
        raise ClosureError(
            "signature, SBOM, VEX, and protected-owner raw evidence must be supplied together"
        )
    if all(supplied):
        distribution = manifest["distribution_evidence"]
        for category, paths in external_groups.items():
            assert paths is not None
            platform_paths = _platform_map(paths, label=f"verification {category}")
            for platform in PLATFORMS:
                data = _read_file(
                    root,
                    platform_paths[platform],
                    label=f"verification {category} for {platform}",
                    limit=MAX_JSON_BYTES,
                )
                expected = distribution[category][platform]
                if (
                    _digest(data) != expected["sha256"]
                    or len(data) != expected["size_bytes"]
                ):
                    raise ClosureError(f"raw {category} evidence drift: {platform}")
        assert protected_owner_approval is not None
        approval_data = _read_file(
            root,
            protected_owner_approval,
            label="verification protected owner approval",
            limit=MAX_JSON_BYTES,
        )
        expected_approval = manifest["protected_owner_approval"]
        artifact = expected_approval["artifact"]
        if (
            _digest(approval_data) != artifact["sha256"]
            or len(approval_data) != artifact["size_bytes"]
        ):
            raise ClosureError("raw protected owner approval evidence drift")
        approval_document = _strict_json(
            approval_data, label="verification protected owner approval"
        )
        approval_document = _exact_keys(
            approval_document,
            {
                "approved_at",
                "candidate",
                "closure_subject_sha256",
                "decision",
                "protected_workflow",
                "reviewer",
                "schema",
            },
            label="verification protected owner approval",
        )
        expected_document = {
            "approved_at": expected_approval["approved_at"],
            "candidate": manifest["candidate"],
            "closure_subject_sha256": expected_approval["closure_subject_sha256"],
            "decision": "approved",
            "protected_workflow": expected_approval["protected_workflow"],
            "reviewer": expected_approval["reviewer"],
            "schema": PROTECTED_APPROVAL_SCHEMA,
        }
        if approval_document != expected_document:
            raise ClosureError(
                "verification protected owner approval: receipt mismatch"
            )
    return _digest(manifest_data)


def verify_unapproved_manifest(repo_root: Path) -> str:
    """Verify the canonical local snapshot without granting distribution approval."""

    root = _repo_root(repo_root)
    data = _read_file(
        root, MANIFEST_PATH, label="closure manifest", limit=MAX_JSON_BYTES
    )
    document = _strict_json(data, label="closure manifest")
    if not isinstance(document, dict) or data != _canonical(document):
        raise ClosureError("closure manifest is not canonical JSON")
    _validate_unapproved_manifest_document(root, document)
    return _digest(data)


def _repo_default() -> Path:
    return Path(__file__).resolve().parents[2]


def _add_report_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument("--trivy-amd64-report", required=required)
    parser.add_argument("--trivy-arm64-report", required=required)


def _reports(args: argparse.Namespace) -> dict[str, str] | None:
    values = (
        getattr(args, "trivy_amd64_report", None),
        getattr(args, "trivy_arm64_report", None),
    )
    if values == (None, None):
        return None
    if None in values:
        raise ClosureError("both platform Trivy reports must be supplied together")
    return {"linux/amd64": values[0], "linux/arm64": values[1]}


def _add_bound_artifact_arguments(parser: argparse.ArgumentParser, prefix: str) -> None:
    for architecture in ("amd64", "arm64"):
        parser.add_argument(f"--{prefix}-{architecture}-file", required=True)
        parser.add_argument(f"--{prefix}-{architecture}-sha256", required=True)
        parser.add_argument(f"--{prefix}-{architecture}-reference", required=True)


def _bound_artifacts(args: argparse.Namespace, prefix: str) -> dict[str, BoundArtifact]:
    normalized = prefix.replace("-", "_")
    return {
        f"linux/{architecture}": BoundArtifact(
            path=getattr(args, f"{normalized}_{architecture}_file"),
            sha256=getattr(args, f"{normalized}_{architecture}_sha256"),
            immutable_reference=getattr(args, f"{normalized}_{architecture}_reference"),
        )
        for architecture in ("amd64", "arm64")
    }


def _add_final_evidence_arguments(
    parser: argparse.ArgumentParser, *, include_approval: bool
) -> None:
    parser.add_argument("--candidate-repository", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--base-reference", required=True)
    parser.add_argument("--base-index-digest", required=True)
    parser.add_argument("--base-amd64-manifest", required=True)
    parser.add_argument("--base-arm64-manifest", required=True)
    parser.add_argument("--image-amd64-digest", required=True)
    parser.add_argument("--image-arm64-digest", required=True)
    parser.add_argument("--trivy-amd64-artifact-id", required=True)
    parser.add_argument("--trivy-arm64-artifact-id", required=True)
    parser.add_argument("--trivy-version", required=True)
    parser.add_argument("--trivy-db-digest", required=True)
    parser.add_argument("--trivy-db-updated-at", required=True)
    parser.add_argument("--trivy-scanned-at", required=True)
    _add_report_arguments(parser, required=True)
    for prefix in ("signature", "sbom", "vex"):
        _add_bound_artifact_arguments(parser, prefix)
    if include_approval:
        parser.add_argument("--protected-owner-approval-file", required=True)
        parser.add_argument("--protected-owner-approval-sha256", required=True)
        parser.add_argument("--protected-owner-approval-reference", required=True)


def _closure_evidence(
    args: argparse.Namespace,
    reports: Mapping[str, str],
    *,
    include_approval: bool,
) -> ClosureEvidence:
    approval = None
    if include_approval:
        approval = BoundArtifact(
            path=args.protected_owner_approval_file,
            sha256=args.protected_owner_approval_sha256,
            immutable_reference=args.protected_owner_approval_reference,
        )
    return ClosureEvidence(
        candidate_repository=args.candidate_repository,
        candidate_sha=args.candidate_sha,
        base_reference=args.base_reference,
        base_index_digest=args.base_index_digest,
        base_manifests={
            "linux/amd64": args.base_amd64_manifest,
            "linux/arm64": args.base_arm64_manifest,
        },
        image_digests={
            "linux/amd64": args.image_amd64_digest,
            "linux/arm64": args.image_arm64_digest,
        },
        trivy_artifact_ids={
            "linux/amd64": args.trivy_amd64_artifact_id,
            "linux/arm64": args.trivy_arm64_artifact_id,
        },
        trivy_version=args.trivy_version,
        trivy_db_digest=args.trivy_db_digest,
        trivy_db_updated_at=args.trivy_db_updated_at,
        trivy_scanned_at=args.trivy_scanned_at,
        trivy_reports=reports,
        signature_bundles=_bound_artifacts(args, "signature"),
        sboms=_bound_artifacts(args, "sbom"),
        vex=_bound_artifacts(args, "vex"),
        protected_owner_approval=approval,
    )


def _add_raw_evidence_arguments(parser: argparse.ArgumentParser) -> None:
    for prefix in ("signature", "sbom", "vex"):
        parser.add_argument(f"--{prefix}-amd64-file")
        parser.add_argument(f"--{prefix}-arm64-file")
    parser.add_argument("--protected-owner-approval-file")


def _raw_platform_paths(args: argparse.Namespace, prefix: str) -> dict[str, str] | None:
    values = (
        getattr(args, f"{prefix}_amd64_file", None),
        getattr(args, f"{prefix}_arm64_file", None),
    )
    if values == (None, None):
        return None
    if None in values:
        raise ClosureError(f"both platform {prefix} files must be supplied together")
    return {"linux/amd64": values[0], "linux/arm64": values[1]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_repo_default())
    commands = parser.add_subparsers(dest="command", required=True)

    subject = commands.add_parser(
        "subject", help="print the exact non-circular protected-approval subject digest"
    )
    _add_final_evidence_arguments(subject, include_approval=False)

    generate = commands.add_parser("generate", help="generate canonical CLOSURE.json")
    _add_final_evidence_arguments(generate, include_approval=True)

    verify = commands.add_parser("verify", help="verify canonical CLOSURE.json")
    _add_report_arguments(verify, required=False)
    _add_raw_evidence_arguments(verify)

    commands.add_parser(
        "snapshot-unapproved",
        help="write a canonical snapshot with every distribution gate unresolved",
    )
    commands.add_parser(
        "verify-unapproved",
        help="verify the canonical snapshot without treating it as release approval",
    )

    args = parser.parse_args(argv)
    try:
        reports = _reports(args)
        if args.command in {"generate", "subject"}:
            assert reports is not None
            evidence = _closure_evidence(
                args, reports, include_approval=args.command == "generate"
            )
            if args.command == "subject":
                digest = approval_subject_digest(args.repo_root, evidence)
                print(f"protected approval subject: {digest}")
            else:
                digest = write_manifest(args.repo_root, evidence)
                print(f"wrote {MANIFEST_PATH} ({digest})")
        elif args.command == "verify":
            signatures = _raw_platform_paths(args, "signature")
            sboms = _raw_platform_paths(args, "sbom")
            vex = _raw_platform_paths(args, "vex")
            approval = args.protected_owner_approval_file
            digest = verify_manifest(
                args.repo_root,
                reports,
                signature_bundles=signatures,
                sboms=sboms,
                vex=vex,
                protected_owner_approval=approval,
            )
            detail = " with supplied raw evidence" if reports is not None else ""
            print(f"verified {MANIFEST_PATH} ({digest}){detail}")
        elif args.command == "snapshot-unapproved":
            digest = write_unapproved_manifest(args.repo_root)
            print(f"wrote distribution-unapproved {MANIFEST_PATH} ({digest})")
        else:
            digest = verify_unapproved_manifest(args.repo_root)
            print(f"verified distribution-unapproved {MANIFEST_PATH} ({digest})")
    except ClosureError as exc:
        parser.exit(1, f"voice-worker closure failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
