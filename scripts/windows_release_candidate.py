#!/usr/bin/env python3
"""Builds a deterministic, stdlib-only manifest binding one archived Windows executable
to its source, environment resolutions, and runtime lock hash, without signing or
publishing anything.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Optional


SCHEMA_VERSION = 1
_LOCK_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^ ;\\]+)")
_SHA = re.compile(r"^[0-9a-f]{40,64}$")


class CandidateManifestError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CandidateManifestError(f"required input is unreadable: {path.name}") from exc
    return digest.hexdigest()


def canonical_profile_sha256(path: Path) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateManifestError("deployment profile is not readable JSON") from exc
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def locked_packages(path: Path) -> dict[str, str]:
    packages: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CandidateManifestError("release lock is unreadable") from exc
    for line in lines:
        if '; sys_platform == "darwin"' in line:
            # Lets a macOS host dry-resolve this Windows-only lock
            continue
        match = _LOCK_LINE.match(line)
        if match is None:
            continue
        name = match.group(1).replace("_", "-").lower()
        version = match.group(2)
        if name in packages:
            raise CandidateManifestError(f"release lock repeats package {name}")
        packages[name] = version
    if not packages:
        raise CandidateManifestError("release lock contains no exact packages")
    return packages


def installed_versions(
    names: Iterable[str], distributions: Optional[Iterable[Any]] = None
) -> dict[str, str]:
    available: dict[str, str] = {}
    rows = importlib.metadata.distributions() if distributions is None else distributions
    for distribution in rows:
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            continue
        name = raw_name.replace("_", "-").lower()
        available[name] = distribution.version
    missing = sorted(set(names) - set(available))
    if missing:
        raise CandidateManifestError(
            "clean environment is missing locked packages: " + ",".join(missing)
        )
    return {name: available[name] for name in sorted(names)}


def environment_manifest(lock_path: Path) -> dict[str, Any]:
    expected = locked_packages(lock_path)
    installed = installed_versions(expected)
    mismatches = {
        name: {"expected": expected[name], "installed": installed[name]}
        for name in expected
        if expected[name] != installed[name]
    }
    if mismatches:
        raise CandidateManifestError("installed versions differ from the release lock")
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": "windows_release_environment",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "target_platform": "win_amd64",
        "requirements_lock_sha256": sha256_file(lock_path),
        "packages": [{"name": name, "version": installed[name]} for name in sorted(installed)],
    }


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(value), sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateManifestError(f"manifest is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise CandidateManifestError(f"manifest must be an object: {path.name}")
    return value


def compare_environment_manifests(first: Path, second: Path) -> dict[str, Any]:
    left = load_json(first)
    right = load_json(second)
    if left != right:
        raise CandidateManifestError("two clean release environments did not resolve identically")
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": "windows_release_reproducibility",
        "status": "passed",
        "environment_manifest_sha256": sha256_file(first),
        "requirements_lock_sha256": left.get("requirements_lock_sha256"),
        "package_count": len(left.get("packages") or []),
    }


def artifact_manifest(
    *,
    executable: Path,
    profile: Path,
    runtime_manifest_path: Path,
    requirements_input: Path,
    lock: Path,
    source_sha: str,
    run_id: str,
    run_attempt: str,
    artifact_name: str,
) -> dict[str, Any]:
    if _SHA.fullmatch(source_sha) is None:
        raise CandidateManifestError("source SHA is invalid")
    if not run_id.isdigit() or not run_attempt.isdigit():
        raise CandidateManifestError("workflow run identity is invalid")
    runtime = load_json(runtime_manifest_path)
    profile_digest = canonical_profile_sha256(profile)
    lock_digest = sha256_file(lock)
    input_digest = sha256_file(requirements_input)
    expected = {
        "deployment_profile_sha256": profile_digest,
        "requirements_lock_sha256": lock_digest,
        "required_runtime_lock_sha256": lock_digest,
        "requirements_input_sha256": input_digest,
    }
    if any(runtime.get(name) != value for name, value in expected.items()):
        raise CandidateManifestError("packaged runtime metadata does not bind exact release inputs")
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": "windows_unsigned_candidate",
        "release_id": runtime.get("release_id"),
        "client_version": runtime.get("client_version"),
        "source_sha": source_sha,
        "workflow_run_id": run_id,
        "workflow_run_attempt": run_attempt,
        "artifact_name": artifact_name,
        "executable_name": executable.name,
        "executable_sha256": sha256_file(executable),
        "deployment_profile_sha256": profile_digest,
        "requirements_input_sha256": input_digest,
        "requirements_lock_sha256": lock_digest,
        "required_runtime_lock_sha256": lock_digest,
        "runtime_contract_version": runtime.get("runtime_contract_version"),
    }


def artifact_reference(
    *, manifest_path: Path, artifact_id: str, artifact_digest: str
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    if not artifact_id.isdigit() or int(artifact_id) <= 0:
        raise CandidateManifestError("GitHub artifact ID is invalid")
    normalized = artifact_digest.removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
        raise CandidateManifestError("GitHub artifact digest is invalid")
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": "windows_candidate_reference",
        "source_sha": manifest.get("source_sha"),
        "workflow_run_id": manifest.get("workflow_run_id"),
        "workflow_run_attempt": manifest.get("workflow_run_attempt"),
        "artifact_name": manifest.get("artifact_name"),
        "artifact_id": artifact_id,
        "artifact_archive_sha256": normalized,
        "executable_sha256": manifest.get("executable_sha256"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    environment = commands.add_parser("environment-manifest")
    environment.add_argument("--lock", type=Path, required=True)
    environment.add_argument("--output", type=Path, required=True)

    compare = commands.add_parser("compare-environments")
    compare.add_argument("--first", type=Path, required=True)
    compare.add_argument("--second", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)

    artifact = commands.add_parser("artifact-manifest")
    artifact.add_argument("--executable", type=Path, required=True)
    artifact.add_argument("--profile", type=Path, required=True)
    artifact.add_argument("--runtime-manifest", type=Path, required=True)
    artifact.add_argument("--requirements-input", type=Path, required=True)
    artifact.add_argument("--lock", type=Path, required=True)
    artifact.add_argument("--source-sha", required=True)
    artifact.add_argument("--run-id", required=True)
    artifact.add_argument("--run-attempt", required=True)
    artifact.add_argument("--artifact-name", required=True)
    artifact.add_argument("--output", type=Path, required=True)

    reference = commands.add_parser("artifact-reference")
    reference.add_argument("--manifest", type=Path, required=True)
    reference.add_argument("--artifact-id", required=True)
    reference.add_argument("--artifact-digest", required=True)
    reference.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    options = _parser().parse_args(argv)
    try:
        if options.command == "environment-manifest":
            value = environment_manifest(options.lock)
        elif options.command == "compare-environments":
            value = compare_environment_manifests(options.first, options.second)
        elif options.command == "artifact-manifest":
            value = artifact_manifest(
                executable=options.executable,
                profile=options.profile,
                runtime_manifest_path=options.runtime_manifest,
                requirements_input=options.requirements_input,
                lock=options.lock,
                source_sha=options.source_sha,
                run_id=options.run_id,
                run_attempt=options.run_attempt,
                artifact_name=options.artifact_name,
            )
        else:
            value = artifact_reference(
                manifest_path=options.manifest,
                artifact_id=options.artifact_id,
                artifact_digest=options.artifact_digest,
            )
        write_json(options.output, value)
    except CandidateManifestError as exc:
        print(f"windows candidate manifest failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
