"""Derives the next AstralDeep Mac release version and build from the published macos-v* releases and the Projection source version.
The release-macos workflow runs it before building and passes its outputs to release_macos.py and macos_release_notes.py.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

VERSION = r"[1-9]\d*\.\d+(?:\.\d+)?"
TAG = re.compile(rf"macos-v({VERSION})-([1-9]\d*)")
COMMIT = re.compile(r"[a-f0-9]{40}")
FIRST_BUILD_FLOOR = 80
MAX_BUILD_NUMBER = 2_147_483_647
CONFIGURATION = "DirectDownload"
BLOCK = re.compile(r"(?ms)^\t\t[0-9A-F]+ /\* [^*]* \*/ = \{\n(.*?)^\t\t\};")
TAG_DEPTH = 5


class NextReleaseError(RuntimeError):
    pass


def parse_tag(tag: str) -> tuple[str, int] | None:
    match = TAG.fullmatch(tag)
    return (match.group(1), int(match.group(2))) if match else None


def source_version(pbxproj_text: str) -> str:
    versions = set()
    for block in BLOCK.findall(pbxproj_text):
        if "isa = XCBuildConfiguration;" not in block or f"\n\t\t\tname = {CONFIGURATION};" not in f"\n{block}":
            continue
        for value in re.findall(r"\bMARKETING_VERSION = (.+?);", block):
            version = value.strip('"')
            if not re.fullmatch(VERSION, version):
                raise ValueError(f"MARKETING_VERSION {value} is not a release version")
            versions.add(version)
    if not versions:
        raise ValueError(f"The project has no {CONFIGURATION} MARKETING_VERSION")
    if len(versions) > 1:
        raise ValueError(f"{CONFIGURATION} configurations disagree on MARKETING_VERSION: {sorted(versions)}")
    return versions.pop()


def source_build(pbxproj_text: str) -> int:
    builds = set()
    for block in BLOCK.findall(pbxproj_text):
        if "isa = XCBuildConfiguration;" not in block or f"\n\t\t\tname = {CONFIGURATION};" not in f"\n{block}":
            continue
        for value in re.findall(r"\bCURRENT_PROJECT_VERSION = (.+?);", block):
            match = re.fullmatch(r'(?:([1-9]\d*)|"([1-9]\d*)")', value.strip())
            if not match:
                raise ValueError(f"CURRENT_PROJECT_VERSION {value} is not a positive integer")
            digits = match.group(1) or match.group(2)
            if len(digits) > 10 or (len(digits) == 10 and digits > str(MAX_BUILD_NUMBER)):
                raise ValueError(f"CURRENT_PROJECT_VERSION {value} exceeds the bounded integer range")
            builds.add(int(digits))
    if not builds:
        raise ValueError(f"The project has no {CONFIGURATION} CURRENT_PROJECT_VERSION")
    if len(builds) > 1:
        raise ValueError(f"{CONFIGURATION} configurations disagree on CURRENT_PROJECT_VERSION: {sorted(builds)}")
    return builds.pop()


def numbers(version: str) -> tuple[int, int, int]:
    parts = [int(part) for part in version.split(".")]
    return parts[0], parts[1], parts[2] if len(parts) == 3 else 0


def decide(
    source: str,
    newest: dict | None,
    head_sha: str,
    descends: bool = True,
    *,
    source_build: int = FIRST_BUILD_FLOOR,
) -> dict:
    if type(source_build) is not int or not 1 <= source_build <= MAX_BUILD_NUMBER:
        raise ValueError(f"source_build must be an integer from 1 to {MAX_BUILD_NUMBER}")
    major, minor, _ = numbers(source)
    if newest is None or (major, minor) > numbers(newest["version"])[:2]:
        version = source if source.count(".") == 2 else f"{major}.{minor}.0"
    else:
        old_major, old_minor, old_patch = numbers(newest["version"])
        version = f"{old_major}.{old_minor}.{old_patch + 1}"
    build_base = max(newest["build"] if newest else FIRST_BUILD_FLOOR, FIRST_BUILD_FLOOR, source_build)
    if build_base >= MAX_BUILD_NUMBER:
        raise ValueError(f"next build would exceed the bounded integer range ending at {MAX_BUILD_NUMBER}")
    build = build_base + 1
    reason = f"release macos-v{version}-{build}"
    skip = False
    if newest is not None and newest["target"] == head_sha:
        skip, reason = True, f"head is already released as {newest['tag']}"
    elif newest is not None and not descends:
        skip, reason = True, f"head is older than {newest['tag']} ({newest['target']}); nothing to release"
    return {
        "skip": skip, "reason": reason, "version": version, "build": build,
        "previous_tag": newest["tag"] if newest else None, "previous_target": newest["target"] if newest else None,
    }


def gh(*args: str) -> object:
    done = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)
    if done.returncode:
        raise NextReleaseError(f"gh api {' '.join(args)} failed: {done.stderr.strip()[-500:]}")
    try:
        return json.loads(done.stdout)
    except ValueError as error:
        raise NextReleaseError(f"gh api {' '.join(args)} returned unreadable JSON") from error


def resolve_tag(repo: str, tag: str) -> str:
    reference = gh(f"repos/{repo}/git/ref/tags/{tag}")
    try:
        target = reference["object"]
        for _ in range(TAG_DEPTH):
            if target["type"] != "tag" or not COMMIT.fullmatch(str(target["sha"])):
                break
            target = gh(f"repos/{repo}/git/tags/{target['sha']}")["object"]
        named = reference["ref"] == f"refs/tags/{tag}" and target["type"] == "commit"
        sha = str(target["sha"])
    except (KeyError, TypeError) as error:
        raise NextReleaseError(f"Tag {tag} is unreadable") from error
    if not named or not COMMIT.fullmatch(sha):
        raise NextReleaseError(f"Tag {tag} does not name a commit")
    return sha


def newest_release(repo: str) -> dict | None:
    pages = gh("--paginate", "--slurp", f"repos/{repo}/releases?per_page=100")
    best: tuple[str, str, int] | None = None
    try:
        for entry in (entry for page in pages for entry in page):
            parsed = parse_tag(str(entry["tag_name"]))
            if entry["draft"] or parsed is None:
                continue
            if best is None or parsed[1] > best[2]:
                best = (str(entry["tag_name"]), *parsed)
    except (KeyError, TypeError, AttributeError) as error:
        raise NextReleaseError(f"GitHub release list is unreadable: {error}") from error
    if best is None:
        return None
    tag, version, build = best
    return {"tag": tag, "version": version, "build": build, "target": resolve_tag(repo, tag)}


def descends(checkout: Path, ancestor: str, head: str) -> bool:
    done = subprocess.run(
        ["git", "-C", str(checkout), "merge-base", "--is-ancestor", ancestor, head],
        capture_output=True, text=True, check=False,
    )
    if done.returncode not in (0, 1):
        raise NextReleaseError(f"git cannot compare {ancestor} with {head}: {done.stderr.strip()[-500:]}")
    return done.returncode == 0


def run(args: argparse.Namespace) -> dict:
    if not COMMIT.fullmatch(args.head):
        raise NextReleaseError("--head must be a full lowercase commit hash")
    try:
        project_text = Path(args.pbxproj).read_text(encoding="utf-8")
        source = source_version(project_text)
        build_floor = source_build(project_text)
    except OSError as error:
        raise NextReleaseError(f"cannot read {args.pbxproj}: {error}") from error
    except ValueError as error:
        raise NextReleaseError(str(error)) from error
    newest = newest_release(args.repo)
    checkout = Path(args.checkout)
    later = newest is None or newest["target"] == args.head or descends(checkout, newest["target"], args.head)
    if not later and not descends(checkout, args.head, newest["target"]):
        raise NextReleaseError(f"head and {newest['tag']} ({newest['target']}) have diverged; release from main's history")
    return decide(source, newest, args.head, later, source_build=build_floor)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--pbxproj", required=True)
    parser.add_argument("--checkout", default=".")
    parser.add_argument("--github-output")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except NextReleaseError as error:
        print(f"Next Mac release failed: {error}", file=sys.stderr)
        return 1
    if result["skip"]:
        print(f"::notice::{result['reason']}")
    print(json.dumps(result, sort_keys=True))
    if args.github_output:
        lines = {
            "version": result["version"], "build": result["build"], "skip": "true" if result["skip"] else "false",
            "previous_tag": result["previous_tag"] or "", "previous_target": result["previous_target"] or "",
        }
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.writelines(f"{key}={value}\n" for key, value in lines.items())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
