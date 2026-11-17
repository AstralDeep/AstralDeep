"""Binds Plane upgrade qualification to the previous Plane source consumed by an immutable Deep event.
It verifies committed composition and checkout identities, then isolates the exact baseline source for Plane's owning upgrade probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.check_changed_coverage import (
    CoveragePolicyError,
    RevisionSelection,
    select_revisions,
    validate_revisions,
)

PLANE_PATH = "plane"
LEGACY_PLANE_PATH = "components/AstralPlane"
PLANE_REPOSITORY = "https://github.com/AstralDeep/AstralPlane.git"
ARCHIVED_SOURCE = PLANE_REPOSITORY
MANIFEST_PATH = "config/astral-composition.json"
CONTRACT_VERSION = "astralplane.contract/v1"
SHA = re.compile(r"[0-9a-f]{40}\Z")
SYNTHETIC_IDENTITY = {
    "GIT_AUTHOR_NAME": "Plane baseline",
    "GIT_AUTHOR_EMAIL": "plane-baseline@invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "Plane baseline",
    "GIT_COMMITTER_EMAIL": "plane-baseline@invalid",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}


def git(repo: Path, *args: str, environment: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        timeout=120,
        check=False,
        env={**os.environ, **environment} if environment else None,
    )
    if result.returncode:
        raise ValueError(f"Git {args[0]} failed while binding the Plane baseline")
    return result.stdout


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


@dataclass(frozen=True)
class PlaneSource:
    pin: str | None
    manifest_sha256: str


def read_source(repo: Path, revision: str) -> PlaneSource:
    content = git(repo, "show", f"{revision}:{MANIFEST_PATH}")
    manifest = json.loads(content, object_pairs_hook=unique_object)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("components"), dict):
        raise ValueError("committed composition has no components")
    digest = hashlib.sha256(content).hexdigest()
    if manifest.get("format") == "astral.composition/v2":
        entry = (manifest.get("in_tree") or {}).get("astral-plane")
        if entry != {"path": PLANE_PATH, "contract_version": CONTRACT_VERSION}:
            raise ValueError("committed in-tree Plane identity is invalid")
        listing = git(repo, "ls-tree", revision, "--", PLANE_PATH).decode().split()
        if len(listing) != 4 or listing[:2] != ["040000", "tree"] or listing[3] != PLANE_PATH:
            raise ValueError("committed Plane folder is not a tree")
        return PlaneSource(None, digest)
    entry = manifest["components"].get("astral-plane")
    if (
        not isinstance(entry, dict)
        or entry.get("path") != LEGACY_PLANE_PATH
        or entry.get("repository") != PLANE_REPOSITORY
        or entry.get("contract_version") != CONTRACT_VERSION
        or not isinstance(entry.get("commit"), str)
        or SHA.fullmatch(entry["commit"]) is None
        or entry["commit"] == "0" * 40
    ):
        raise ValueError("committed Plane composition identity is invalid")
    pin = entry["commit"]
    expected = f"160000 commit {pin}\t{LEGACY_PLANE_PATH}\n".encode()
    if git(repo, "ls-tree", revision, "--", LEGACY_PLANE_PATH) != expected:
        raise ValueError("committed Plane gitlink differs from its composition pin")
    return PlaneSource(pin, digest)


def has_commit(repo: Path, commit: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        timeout=120,
        check=False,
    ).returncode == 0


def fetch_into(baseline_root: Path, origin: str, commit: str) -> None:
    git(baseline_root, "fetch", "--no-tags", "--no-write-fetch-head", origin, commit)


def prepare(repo: Path, selected: RevisionSelection, baseline_root: Path) -> dict:
    repo, baseline_root = repo.resolve(), baseline_root.resolve()
    selected = validate_revisions(repo, selected)
    if git(repo, "rev-parse", "HEAD").decode().strip() != selected.candidate_sha:
        raise ValueError("Deep checkout differs from the immutable candidate")
    previous = read_source(repo, selected.base_sha)
    candidate = read_source(repo, selected.candidate_sha)
    if candidate.pin is not None:
        raise ValueError("candidate must hold Plane in the repository tree")
    component = repo / PLANE_PATH
    if git(repo, "status", "--porcelain", "--untracked-files=all", "--", PLANE_PATH):
        raise ValueError("Plane qualification requires an unchanged source checkout")
    if baseline_root.exists():
        raise ValueError("isolated baseline destination already exists")
    if baseline_root == component or component in baseline_root.parents:
        raise ValueError("isolated baseline destination must be outside the component")
    baseline_root.mkdir(parents=True)
    git(baseline_root, "init", "--bare")
    if previous.pin is None:
        fetch_into(baseline_root, str(repo), selected.base_sha)
        tree = git(baseline_root, "rev-parse", f"{selected.base_sha}:{PLANE_PATH}").decode().strip()
        baseline_sha = git(
            baseline_root, "commit-tree", tree, "-m", "Plane baseline", environment=SYNTHETIC_IDENTITY
        ).decode().strip()
    else:
        baseline_sha = previous.pin
        fetch_into(baseline_root, str(repo) if has_commit(repo, baseline_sha) else ARCHIVED_SOURCE, baseline_sha)
    git(baseline_root, "update-ref", "refs/remotes/origin/main", baseline_sha)
    baseline_plane_tree = git(baseline_root, "rev-parse", f"{baseline_sha}^{{tree}}").decode().strip()
    baseline_tree = git(baseline_root, "rev-parse", f"{baseline_sha}:src").decode().strip()
    candidate_tree = git(repo, "rev-parse", f"{selected.candidate_sha}:{PLANE_PATH}/src").decode().strip()
    candidate_plane_tree = git(repo, "rev-parse", f"{selected.candidate_sha}:{PLANE_PATH}").decode().strip()
    return {
        "status": "prepared",
        "event_name": selected.event_name,
        "base_deep_sha": selected.base_sha,
        "candidate_deep_sha": selected.candidate_sha,
        "base_source": selected.base_source,
        "candidate_source": selected.candidate_source,
        "baseline_plane_sha": baseline_sha,
        "baseline_manifest_sha256": previous.manifest_sha256,
        "candidate_manifest_sha256": candidate.manifest_sha256,
        "baseline_plane_tree": baseline_plane_tree,
        "candidate_plane_tree": candidate_plane_tree,
        "baseline_source_tree": baseline_tree,
        "candidate_source_tree": candidate_tree,
        "baseline_root": str(baseline_root),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME"))
    parser.add_argument(
        "--event-path", type=Path, default=os.environ.get("GITHUB_EVENT_PATH")
    )
    parser.add_argument("--base-sha")
    parser.add_argument("--candidate-sha")
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--github-env", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = (
            json.loads(args.event_path.read_bytes(), object_pairs_hook=unique_object)
            if args.event_path
            else None
        )
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("immutable event must be a JSON object")
        selected = select_revisions(
            event_name=args.event_name,
            event_payload=payload,
            base_sha=args.base_sha,
            candidate_sha=args.candidate_sha,
        )
        report = prepare(args.repo, selected, args.baseline_root)
        environment = {
            "ASTRALPLANE_UPGRADE_BASELINE_ROOT": report["baseline_root"],
            "ASTRALPLANE_UPGRADE_BASELINE_SHA": report["baseline_plane_sha"],
            "PLANE_BASE_DEEP_SHA": report["base_deep_sha"],
            "PLANE_BASE_TREE": report["baseline_plane_tree"],
            "PLANE_CANDIDATE_TREE": report["candidate_plane_tree"],
        }
        if any("\n" in value or "\r" in value for value in environment.values()):
            raise ValueError("baseline environment value contains a line break")
        with args.github_env.open("a", encoding="utf-8") as stream:
            stream.write(
                "".join(f"{key}={value}\n" for key, value in environment.items())
            )
    except (ValueError, OSError, CoveragePolicyError, subprocess.SubprocessError):
        report = {
            "status": "failed",
            "reason": "immutable consumer baseline preparation failed",
        }
        print(report["reason"], file=sys.stderr)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if report["status"] == "prepared" else 2


if __name__ == "__main__":
    raise SystemExit(main())
