"""Run the pinned offline scanner and validate its export with the upstream canonical validators. The isolated runtime emits a digest receipt binding canonical outputs, SARIF and the approved source snapshot."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

SOURCE = Path("/source")
SCAN = Path("/output/scan")
SCRIPTS = Path("/scanner/plugins/codex-security/scripts")
UPSTREAM_COMMIT = "f894f1ff64b1a332001f48593c1622b85701f9f3"
ARTIFACTS = (
    "scan-manifest.json",
    "findings.json",
    "coverage.json",
    "exports/results.sarif",
)
COMMAND = [
    "/usr/local/bin/node",
    "/scanner/sdk/typescript/bin/codex-security.mjs",
    "scan",
    "/source",
    "--mock",
    "--headless",
    "--format",
    "json",
    "--model",
    "gpt-5.6-sol",
    "--plugin-path",
    "/scanner/plugins/codex-security",
    "--output-dir",
    "/output/scan",
]


def load_upstream(name: str):
    specification = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    if specification is None or specification.loader is None:
        raise ValueError("pinned upstream validator is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_digest(root: Path) -> str:
    entries = []
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("approved snapshot contains a non-regular source entry")
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": metadata.st_size,
                "sha256": file_digest(path),
            }
        )
    data = json.dumps(
        entries, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(data).hexdigest()


def validate_output(scan_dir: Path, source: Path, expected_source_digest: str) -> None:
    if snapshot_digest(source) != expected_source_digest:
        raise ValueError("approved source snapshot identity changed")
    finalizer = load_upstream("finalize_scan_contract")
    expected = finalizer.build_sarif_projection(scan_dir, source)
    actual = json.loads((scan_dir / "exports/results.sarif").read_text())
    if actual != expected:
        raise ValueError("SARIF export differs from the pinned canonical projection")
    manifest = json.loads((scan_dir / "scan-manifest.json").read_text())
    target = manifest["scan"]["target"]
    targets = load_upstream("workbench_target")
    source_snapshot = targets.directory_content_digest(source, include_ignored=True)
    if (
        target["kind"] != "directory_snapshot"
        or target.get("snapshotDigest") != source_snapshot
    ):
        raise ValueError(
            "upstream scan target does not bind the approved source snapshot"
        )
    receipt = {
        "schema_version": 1,
        "status": "validated",
        "upstream_commit": UPSTREAM_COMMIT,
        "source_digest": expected_source_digest,
        "snapshot_digest": source_snapshot,
        "scan_id": manifest["scan"]["id"],
        "artifacts": {name: file_digest(scan_dir / name) for name in ARTIFACTS},
    }
    descriptor = os.open(
        scan_dir / "adapter-validation.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w") as handle:
        json.dump(receipt, handle, sort_keys=True)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-digest", required=True)
    args = parser.parse_args(argv)
    try:
        completed = subprocess.run(COMMAND, check=False)
        if completed.returncode != 0:
            return completed.returncode
        validate_output(SCAN, SOURCE, args.source_digest)
        return 0
    except (OSError, ValueError, SystemExit) as error:
        print(f"canonical artifact validation failed: {error}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
