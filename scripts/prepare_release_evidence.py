#!/usr/bin/env python3
"""Collect, normalize, and parse local feature-060 release evidence.

This is the deterministic local pre-push half of T107.  The command
inventories the evidence directory, canonicalizes and SHA-256-digests every
recognized document, assembles one canonical ``release_evidence_set`` when the
directory holds only platform reports, and delegates schema plus same-candidate
policy validation to the sibling ``validate_release_evidence.py`` module so the
logic stays single-sourced.  The result is ALWAYS diagnostic: the emitted JSON
states ``protected_release_authorization: false``, there is no decision-output
mode, and only the protected-decision GitHub job can emit a trusted release
decision.  Output is deterministic for identical inputs and ``--now``:
assembled evidence-set identity uses content-derived UUIDv5, never random or
time-dependent values beyond the ``--now`` default of the current UTC time.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = REPO_ROOT / "specs" / "060-runtime-reliability-hardening" / "contracts"
EVIDENCE_DOCUMENT_TYPES = {
    "platform_evidence",
    "evidence_exception_request",
    "release_evidence_set",
    "windows_draft_verification_provenance",
}


def _load_validator() -> Any:
    """Import the sibling validator so schema/policy logic stays single-sourced."""

    path = Path(__file__).resolve().parent / "validate_release_evidence.py"
    spec = importlib.util.spec_from_file_location(
        "astral_release_evidence_validator", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import release-evidence validator at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", default="build/060/release-evidence")
    parser.add_argument("--coverage-dir", default="build/060/coverage")
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument(
        "--schema", default=str(CONTRACT_ROOT / "release-evidence.schema.json")
    )
    parser.add_argument(
        "--trust-schema", default=str(CONTRACT_ROOT / "release-trust.schema.json")
    )
    parser.add_argument(
        "--deployment-profile-schema",
        default=str(CONTRACT_ROOT / "windows-deployment-profile.schema.json"),
    )
    parser.add_argument("--staging-outputs")
    parser.add_argument(
        "--output", default="build/060/release-evidence/local-diagnostic.json"
    )
    parser.add_argument(
        "--failure-output",
        help="Optional machine-readable, non-authorizing handled-failure receipt",
    )
    parser.add_argument("--now")
    return parser


def _inventory(
    root: Path, schema: Mapping[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """Load, schema-validate, and order every recognized evidence document."""

    if not root.is_dir():
        raise VALIDATOR.DocumentError(f"evidence directory does not exist: {root}")
    entries: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(root.glob("*.json")):
        document = VALIDATOR.load_json_document(path)
        if document.get("document_type") not in EVIDENCE_DOCUMENT_TYPES:
            continue
        VALIDATOR.validate_document(document, schema)
        entries.append((path.name, document))
    if not entries:
        raise VALIDATOR.PolicyError(
            f"evidence directory contains no release evidence documents: {root}"
        )
    return entries


def assemble_evidence_set(
    reports: Sequence[Mapping[str, Any]],
    requests: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Assemble one canonical, deterministic release_evidence_set from reports.

    The evidence-set identity is a UUIDv5 over the canonical JSON digests of the
    sorted member documents, so re-running the command over identical inputs
    always yields byte-identical output for the same ``--now``.  The assembled
    ``decision`` is a claim only; policy evaluation still verifies it.
    """

    if not reports:
        raise VALIDATOR.PolicyError(
            "no platform evidence reports to assemble into an evidence set"
        )
    identities = {
        (
            report.get("candidate_sha"),
            report.get("release_id"),
            report.get("release_version"),
        )
        for report in reports
    }
    if len(identities) != 1:
        raise VALIDATOR.PolicyError(
            "platform reports disagree on candidate/release identity"
        )
    candidate_sha, release_id, release_version = identities.pop()
    evidence = sorted(reports, key=lambda report: str(report.get("platform")))
    exception_requests = sorted(
        requests, key=lambda request: str(request.get("exception_id"))
    )
    seed = VALIDATOR.canonical_json_sha256(
        {
            "candidate_sha": candidate_sha,
            "release_id": release_id,
            "release_version": release_version,
            "evidence": [VALIDATOR.canonical_json_sha256(item) for item in evidence],
            "exception_requests": [
                VALIDATOR.canonical_json_sha256(item) for item in exception_requests
            ],
        }
    )
    return {
        "document_type": "release_evidence_set",
        "schema_version": 1,
        "policy_revision": "060-v1",
        "evidence_set_id": str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"astraldeep:060:release-evidence-set:{seed}"
            )
        ),
        "candidate_sha": candidate_sha,
        "release_id": release_id,
        "release_version": release_version,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "required_targets": list(VALIDATOR.REQUIRED_TARGETS),
        "evidence": list(evidence),
        "exception_requests": list(exception_requests),
        "decision": "passed",
    }


def _coverage_reports(root: Path) -> list[dict[str, str]]:
    """Digest raw coverage report bytes; an absent directory is an empty list."""

    if not root.is_dir():
        return []
    entries = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".xml"}
    ]
    return sorted(entries, key=lambda entry: entry["path"])


def _staging_outputs_summary(
    path_text: str | None, staging_environment_id: str
) -> dict[str, str] | None:
    """Bind optional local staging outputs to the matrix staging identity."""

    if not path_text:
        return None
    path = Path(path_text)
    document = VALIDATOR.load_json_document(path)
    environment_id = document.get("environment_id")
    if environment_id != staging_environment_id:
        raise VALIDATOR.PolicyError(
            f"staging outputs environment {environment_id!r} differs from "
            f"evidence matrix {staging_environment_id!r}"
        )
    return {
        "path": path.as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "environment_id": str(environment_id),
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".release-diagnostic-", dir=path.parent
    ) as temp:
        temporary = Path(temp) / "diagnostic.json"
        temporary.write_bytes(
            json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        )
        os.replace(temporary, path)


def _failure_document(
    exc: Exception, *, base_sha: str, candidate_sha: str
) -> dict[str, Any]:
    message = str(exc)
    missing_provider_inputs = (
        message.startswith("evidence directory does not exist:")
        or message.startswith("evidence directory contains no release evidence documents:")
    )
    return {
        "document_type": "release_evidence_local_failure",
        "schema_version": 1,
        "error_code": (
            "missing_provider_inputs"
            if missing_provider_inputs
            else "release_evidence_rejected"
        ),
        "error_message_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "base_sha": base_sha,
        "candidate_sha": candidate_sha,
        "protected_release_authorization": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the deterministic, non-authorizing local pre-push evidence parse."""

    args = _parser().parse_args(argv)
    try:
        if not VALIDATOR.GIT_SHA_RE.fullmatch(args.base_sha) or not (
            VALIDATOR.GIT_SHA_RE.fullmatch(args.candidate_sha)
        ):
            raise VALIDATOR.PolicyError(
                "base-sha and candidate-sha must be exact lowercase Git SHAs"
            )
        if args.base_sha == args.candidate_sha:
            raise VALIDATOR.PolicyError("base-sha must differ from candidate-sha")
        # Reuse the validator's strict RFC 3339 semantics for --now.
        now = (
            VALIDATOR._parse_timestamp(args.now, field="now")
            if args.now
            else datetime.now(UTC)
        )
        evidence_schema = VALIDATOR.load_json_document(args.schema)
        trust_schema = VALIDATOR.load_json_document(args.trust_schema)
        profile_schema = VALIDATOR.load_json_document(args.deployment_profile_schema)
        for schema in (evidence_schema, trust_schema, profile_schema):
            VALIDATOR.validate_schema_document(schema)
        documents = _inventory(Path(args.evidence_dir), evidence_schema)
        sets = [
            document
            for _, document in documents
            if document.get("document_type") == "release_evidence_set"
        ]
        if len(sets) > 1:
            raise VALIDATOR.PolicyError(
                "evidence directory must contain exactly one release_evidence_set"
            )
        assembled = not sets
        if sets:
            evidence_set = sets[0]
        else:
            evidence_set = assemble_evidence_set(
                [
                    document
                    for _, document in documents
                    if document.get("document_type") == "platform_evidence"
                ],
                [
                    document
                    for _, document in documents
                    if document.get("document_type") == "evidence_exception_request"
                ],
                now=now,
            )
            VALIDATOR.validate_document(evidence_set, evidence_schema)
        if evidence_set.get("candidate_sha") != args.candidate_sha:
            raise VALIDATOR.PolicyError(
                "evidence-set candidate SHA differs from CLI candidate"
            )
        result = VALIDATOR.evaluate_evidence_set(
            evidence_set, now=now, trusted_approvals=()
        )
        staging_outputs = _staging_outputs_summary(
            args.staging_outputs, result.staging_environment_id
        )
        diagnostic = {
            "base_sha": args.base_sha,
            "candidate_sha": args.candidate_sha,
            "coverage_reports": _coverage_reports(Path(args.coverage_dir)),
            "decision": "diagnostic_policy_passed",
            "documents": [
                {
                    "path": name,
                    "document_type": document["document_type"],
                    "sha256": VALIDATOR.canonical_json_sha256(document),
                }
                for name, document in documents
            ],
            "evidence_set_assembled": assembled,
            "evidence_set_id": evidence_set["evidence_set_id"],
            "evidence_set_sha256": VALIDATOR.canonical_json_sha256(evidence_set),
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "protected_release_authorization": False,
            "required_targets": list(result.required_targets),
            "staging_environment_id": result.staging_environment_id,
            "staging_outputs": staging_outputs,
            "used_exception_ids": list(result.used_exception_ids),
        }
        _atomic_json(Path(args.output), diagnostic)
        print(json.dumps(diagnostic, sort_keys=True))
        return 0
    except VALIDATOR.ReleaseEvidenceError as exc:
        if args.failure_output:
            _atomic_json(
                Path(args.failure_output),
                _failure_document(
                    exc,
                    base_sha=args.base_sha,
                    candidate_sha=args.candidate_sha,
                ),
            )
        print(f"release evidence rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
