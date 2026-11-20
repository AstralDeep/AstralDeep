"""Selects an available, qualified AstralKernel Apple archive for the pinned source.
The macOS release workflow rebuilds unavailable inputs from source and verifies the archive before packaging.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

if __package__:
    from .extract_release_artifact import DEFAULT_MAX_ARCHIVE_BYTES, ExtractionLimits, extract_artifact
else:
    from extract_release_artifact import DEFAULT_MAX_ARCHIVE_BYTES, ExtractionLimits, extract_artifact

REPO = "AstralDeep/AstralKernel"
REPO_ID = 1455890728
TARGETS = ("aarch64-apple-darwin", "x86_64-apple-darwin", "aarch64-apple-ios", "aarch64-apple-ios-sim", "x86_64-apple-ios")


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", f"repos/{REPO}/actions/{path}"], text=True))


def select(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid Kernel gitlink")
    query = f"workflows/ci.yml/runs?head_sha={sha}&event=push&branch=main&status=success&per_page=100"
    for run in api(query)["workflow_runs"]:
        if (run["event"] != "push" or run["head_sha"] != sha or run["head_branch"] != "main"
                or run["status"] != "completed" or run["conclusion"] != "success"
                or run["path"] != ".github/workflows/ci.yml" or run["name"] != "Kernel qualification"
                or any(run[key]["id"] != REPO_ID or run[key]["full_name"] != REPO
                       for key in ("repository", "head_repository"))):
            raise ValueError("unexpected Kernel qualification identity")
        candidates = [a for a in api(f"runs/{int(run['id'])}/artifacts?name=kernel-apple&per_page=100")["artifacts"]
                      if a["name"] == "kernel-apple" and a["expired"] is False]
        if not candidates:
            continue
        if len(candidates) != 1:
            raise ValueError("ambiguous Kernel archive")
        artifact = candidates[0]
        owner = artifact["workflow_run"]
        if (owner["id"] != run["id"] or owner["head_sha"] != sha or owner["head_branch"] != "main"
                or owner["repository_id"] != REPO_ID or owner["head_repository_id"] != REPO_ID
                or type(artifact["id"]) is not int or artifact["id"] <= 0
                or type(artifact["size_in_bytes"]) is not int or not 0 < artifact["size_in_bytes"] <= DEFAULT_MAX_ARCHIVE_BYTES
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", artifact["digest"])):
            raise ValueError("unexpected Kernel archive identity, size or digest")
        endpoint = f"repos/{REPO}/actions/artifacts/{artifact['id']}/zip"
        subprocess.run(["gh", "api", "--method", "HEAD", "--silent", endpoint], check=True)
        print(f"Using Kernel qualification {run['id']}, artifact {artifact['id']}, {artifact['digest']}")
        return artifact["id"], artifact["digest"], artifact["size_in_bytes"]
    return None


def plan(sha, output):
    try:
        selected = select(sha)
    except subprocess.CalledProcessError:
        selected = None
    artifact, digest, size = selected or ("", "", "")
    if selected is None:
        print("No accessible qualified Apple archive. Building the pinned source targets.")
    with output.open("a", encoding="utf-8") as stream:
        stream.write(f"apple_artifact={artifact}\napple_digest={digest}\napple_bytes={size}\n")
        stream.write("apple_inputs=" + json.dumps(["qualified"] if selected else TARGETS) + "\n")


def restore(archive, destination, size, digest):
    if not 0 < size <= DEFAULT_MAX_ARCHIVE_BYTES or archive.stat().st_size != size:
        raise ValueError("incorrect Kernel ZIP size")
    checksum = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    if "sha256:" + checksum.hexdigest() != digest:
        raise ValueError("incorrect Kernel ZIP digest")
    extract_artifact(archive, destination, limits=ExtractionLimits(max_member_bytes=768 * 1024 * 1024, max_members=32))
    data = json.loads((destination / "apple-build-manifest.json").read_text())
    if (data["target"] != "universal" or data["toolchain"]["xcode"] != "Xcode 26.6\nBuild version 17F113"
            or not data["toolchain"]["rustc"].startswith("rustc 1.99.1 ")):
        raise ValueError("qualified Kernel archive has an unexpected target or compiler")


def main(args):
    if len(args) == 3 and args[0] == "plan":
        plan(args[1], Path(args[2]))
    elif len(args) == 5 and args[0] == "restore":
        restore(Path(args[1]), Path(args[2]), int(args[3]), args[4])
    else:
        raise SystemExit("expected plan or restore")


if __name__ == "__main__":
    main(sys.argv[1:])
