"""Binds Plane upgrade qualification to the previous component consumed by an immutable Deep event.
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
from pathlib import Path

from scripts.check_changed_coverage import (
    CoveragePolicyError,
    RevisionSelection,
    select_revisions,
    validate_revisions,
)

PLANE_PATH = "components/AstralPlane"
PLANE_REPOSITORY = "https://github.com/AstralDeep/AstralPlane.git"
MANIFEST_PATH = "config/astral-composition.json"
SHA = re.compile(r"[0-9a-f]{40}\Z")


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, timeout=120, check=False
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


def read_pin(repo: Path, revision: str) -> tuple[str, str]:
    content = git(repo, "show", f"{revision}:{MANIFEST_PATH}")
    manifest = json.loads(content, object_pairs_hook=unique_object)
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("components"), dict
    ):
        raise ValueError("committed composition has no components")
    entry = manifest["components"].get("astral-plane")
    if (
        not isinstance(entry, dict)
        or entry.get("path") != PLANE_PATH
        or entry.get("repository") != PLANE_REPOSITORY
        or entry.get("contract_version") != "astralplane.contract/v1"
        or not isinstance(entry.get("commit"), str)
        or SHA.fullmatch(entry["commit"]) is None
        or entry["commit"] == "0" * 40
    ):
        raise ValueError("committed Plane composition identity is invalid")
    pin = entry["commit"]
    expected = f"160000 commit {pin}\t{PLANE_PATH}\n".encode()
    if git(repo, "ls-tree", revision, "--", PLANE_PATH) != expected:
        raise ValueError("committed Plane gitlink differs from its composition pin")
    return pin, hashlib.sha256(content).hexdigest()


def prepare(repo: Path, selected: RevisionSelection, baseline_root: Path) -> dict:
    repo, baseline_root = repo.resolve(), baseline_root.resolve()
    selected = validate_revisions(repo, selected)
    if git(repo, "rev-parse", "HEAD").decode().strip() != selected.candidate_sha:
        raise ValueError("Deep checkout differs from the immutable candidate")
    previous, base_manifest_hash = read_pin(repo, selected.base_sha)
    candidate, candidate_manifest_hash = read_pin(repo, selected.candidate_sha)
    component = repo / PLANE_PATH
    if git(component, "rev-parse", "HEAD").decode().strip() != candidate:
        raise ValueError("Plane checkout differs from the committed candidate pin")
    if git(component, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Plane qualification requires an unchanged source checkout")
    if baseline_root.exists():
        raise ValueError("isolated baseline destination already exists")
    if baseline_root == component or component in baseline_root.parents:
        raise ValueError("isolated baseline destination must be outside the component")
    available = subprocess.run(
        ["git", "-C", str(component), "cat-file", "-e", f"{previous}^{{commit}}"],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if available.returncode:
        origin = git(component, "remote", "get-url", "origin").decode().strip()
        if origin not in {
            PLANE_REPOSITORY,
            "git@github.com:AstralDeep/AstralPlane.git",
        }:
            raise ValueError("Plane fetch origin is not the canonical repository")
        git(component, "fetch", "--no-tags", "origin", previous)
    baseline_tree = git(component, "rev-parse", f"{previous}:src").decode().strip()
    candidate_tree = git(component, "rev-parse", f"{candidate}:src").decode().strip()
    baseline_root.mkdir(parents=True)
    git(baseline_root, "init", "--bare")
    git(
        baseline_root,
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        str(component),
        previous,
    )
    git(baseline_root, "update-ref", "refs/remotes/origin/main", previous)
    if (
        git(baseline_root, "rev-parse", f"{previous}:src").decode().strip()
        != baseline_tree
    ):
        raise ValueError("isolated Plane baseline source identity differs")
    return {
        "status": "prepared",
        "event_name": selected.event_name,
        "base_deep_sha": selected.base_sha,
        "candidate_deep_sha": selected.candidate_sha,
        "base_source": selected.base_source,
        "candidate_source": selected.candidate_source,
        "baseline_plane_sha": previous,
        "candidate_plane_sha": candidate,
        "baseline_manifest_sha256": base_manifest_hash,
        "candidate_manifest_sha256": candidate_manifest_hash,
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
            "PLANE_BASE_COMPONENT_SHA": report["baseline_plane_sha"],
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
