#!/usr/bin/env python3
"""Build deterministic provenance for blobs selected from one Git commit.

The working tree is deliberately not consulted for source content. Every
manifest entry comes from ``git ls-tree`` over one caller-confirmed, full
commit object ID, so ignored and untracked files cannot enter the inventory.

``manifestSha256`` is the SHA-256 of compact, sorted-key UTF-8 JSON after
removing that top-level field. Excluding the field avoids an impossible
self-referential digest while leaving every provenance-bearing field bound.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

FORMAT = "astral.extraction-provenance/v1"
DIGEST_FIELD = "manifestSha256"

_FULL_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_REF_CHARACTERS = re.compile(r"^[A-Za-z0-9._/-]+$")
_RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_WINDOWS_DEVICE_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_CANONICAL_REPOSITORIES = {
    "https://github.com/AstralDeep/AstralDeep.git",
    "https://github.com/AstralDeep/AstralPlane.git",
    "https://github.com/AstralDeep/AstralProjection.git",
}
_TRACKED_BLOB_MODES = {"100644", "100755", "120000"}


class ManifestError(RuntimeError):
    """The requested manifest could not be produced unambiguously."""


@dataclass(frozen=True)
class BlobEntry:
    """One tracked Git blob before destination mapping."""

    source_path: str
    mode: str
    object_id: str
    size_bytes: int


@dataclass(frozen=True)
class Selection:
    """One exact file or recursive tree-root destination mapping."""

    source_path: str
    destination_path: str


@dataclass(frozen=True)
class LegacyBaseline:
    """Remote default state preceding the destination replacement."""

    repository: str
    source_ref: str
    commit: str
    observed_at: str


def _require_full_commit(value: str, *, field: str) -> str:
    if not isinstance(value, str) or _FULL_SHA1.fullmatch(value) is None:
        raise ManifestError(f"{field} must be a full lowercase 40-hex commit ID")
    return value


def _canonical_repository(value: str, *, field: str) -> str:
    if value not in _CANONICAL_REPOSITORIES:
        raise ManifestError(
            f"{field} must be an exact canonical AstralDeep HTTPS Git URL"
        )
    return value


def _normalized_repo_path(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{field} must be a non-empty relative POSIX path")
    if value != unicodedata.normalize("NFC", value):
        raise ManifestError(f"{field} must use Unicode NFC normalization: {value!r}")
    if len(value) > 4096:
        raise ManifestError(f"{field} exceeds the 4096-character contract bound")
    if value.startswith("/") or value.endswith("/") or "\\" in value:
        raise ManifestError(
            f"{field} must be a normalized relative POSIX path: {value!r}"
        )

    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ManifestError(f"{field} contains an unsafe path segment: {value!r}")
    for part in parts:
        folded = part.casefold()
        stem = part.split(".", 1)[0].casefold()
        if folded == ".git" or stem in _WINDOWS_DEVICE_NAMES:
            raise ManifestError(f"{field} contains a reserved path segment: {value!r}")
        if part.endswith((" ", ".")):
            raise ManifestError(f"{field} has a non-portable path segment: {value!r}")
        if any(
            character in _WINDOWS_FORBIDDEN
            or ord(character) < 32
            or ord(character) == 127
            for character in part
        ):
            raise ManifestError(f"{field} contains an unsafe character: {value!r}")
    return "/".join(parts)


def _normalized_branch(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError("selected branch must be a non-empty short branch name")
    if value.startswith("-"):
        raise ManifestError("selected branch must not begin with '-'")
    if value.startswith("refs/"):
        raise ManifestError("selected branch must be a short name, not a full ref")
    _normalized_ref(f"refs/heads/{value}", field="selected branch")
    return value


def _normalized_ref(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("refs/heads/"):
        raise ManifestError(f"{field} must be an exact refs/heads/* ref")
    if len(value) > 255:
        raise ManifestError(f"{field} exceeds the 255-character Git ref bound")
    if (
        _REF_CHARACTERS.fullmatch(value) is None
        or value.endswith(("/", "."))
        or value.startswith("/")
        or "//" in value
        or ".." in value
        or "@{" in value
        or "\\" in value
        or any(character.isspace() or character in "~^:?*[" for character in value)
    ):
        raise ManifestError(f"{field} is not a normalized Git ref: {value!r}")
    if any(part.startswith(".") or part.endswith(".lock") for part in value.split("/")):
        raise ManifestError(f"{field} contains a reserved Git ref component: {value!r}")
    return value


def _normalized_observed_at(value: str) -> str:
    if not isinstance(value, str) or _RFC3339_UTC.fullmatch(value) is None:
        raise ManifestError("legacy observedAt must be an RFC 3339 UTC timestamp")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ManifestError("legacy observedAt is not a valid timestamp") from exc
    return value


def _git(repo: Path, arguments: Sequence[str]) -> bytes:
    environment = os.environ.copy()
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(repo), *arguments],
            check=False,
            capture_output=True,
            env=environment,
        )
    except OSError as exc:
        raise ManifestError(f"could not execute git: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ManifestError(
            f"git {' '.join(arguments)} failed with exit code "
            f"{completed.returncode}: {detail or 'no error output'}"
        )
    return completed.stdout


def _exact_repository_root(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise ManifestError(
            f"source repository does not resolve: {candidate}: {exc}"
        ) from exc
    if not root.is_dir():
        raise ManifestError(f"source repository is not a directory: {root}")

    raw_git_root = _git(root, ("rev-parse", "--show-toplevel"))
    try:
        git_root = Path(os.fsdecode(raw_git_root).rstrip("\r\n")).resolve(strict=True)
    except OSError as exc:
        raise ManifestError(f"Git worktree root does not resolve: {exc}") from exc
    if git_root != root:
        raise ManifestError(
            f"exact-root mismatch: requested '{root}', Git worktree is '{git_root}'"
        )
    return root


def _verify_source_revision(repo: Path, revision: str) -> str:
    kind = _git(repo, ("cat-file", "-t", revision)).decode("ascii", "strict").strip()
    if kind != "commit":
        raise ManifestError(f"source revision is a {kind!r} object, not a commit")
    resolved = (
        _git(repo, ("rev-parse", "--verify", f"{revision}^{{commit}}"))
        .decode("ascii", "strict")
        .strip()
    )
    if resolved != revision:
        raise ManifestError(
            f"source revision mismatch: requested {revision}, Git resolved {resolved}"
        )
    tree = (
        _git(repo, ("rev-parse", "--verify", f"{revision}^{{tree}}"))
        .decode("ascii", "strict")
        .strip()
    )
    if _FULL_SHA1.fullmatch(tree) is None:
        raise ManifestError("source revision resolved to an invalid tree object ID")
    return tree


def inventory_tracked_blobs(
    source_repo: str | Path,
    *,
    source_revision: str,
    expected_source_revision: str,
) -> tuple[str, tuple[BlobEntry, ...]]:
    """Return one immutable tree ID and its sorted tracked-blob inventory."""

    requested = _require_full_commit(source_revision, field="source revision")
    expected = _require_full_commit(
        expected_source_revision, field="expected source revision"
    )
    # This comparison intentionally precedes every Git call. A caller cannot
    # accidentally inventory a different valid commit merely because it exists.
    if requested != expected:
        raise ManifestError(
            f"source revision mismatch: requested {requested}, expected {expected}"
        )

    repo = _exact_repository_root(source_repo)
    tree = _verify_source_revision(repo, requested)
    raw = _git(
        repo,
        ("ls-tree", "-r", "-z", "-l", "--full-tree", requested),
    )

    blobs: list[BlobEntry] = []
    seen_paths: set[str] = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, raw_kind, raw_object_id, raw_size = metadata.split()
            kind = raw_kind.decode("ascii", "strict")
            if kind != "blob":
                continue
            mode = raw_mode.decode("ascii", "strict")
            object_id = raw_object_id.decode("ascii", "strict")
            size_bytes = int(raw_size)
            path = raw_path.decode("utf-8", "strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ManifestError("source tree contains malformed blob metadata") from exc
        if mode not in _TRACKED_BLOB_MODES:
            raise ManifestError(f"source blob has unsupported Git mode: {mode!r}")
        if _FULL_SHA1.fullmatch(object_id) is None or size_bytes < 0:
            raise ManifestError(f"source blob has invalid identity or size: {path!r}")
        normalized = _normalized_repo_path(path, field="tracked source path")
        if normalized in seen_paths:
            raise ManifestError(f"source tree repeats tracked path: {normalized!r}")
        seen_paths.add(normalized)
        blobs.append(BlobEntry(normalized, mode, object_id, size_bytes))

    return tree, tuple(sorted(blobs, key=lambda blob: blob.source_path))


def _canonical_selections(selections: Iterable[Selection]) -> tuple[Selection, ...]:
    normalized = [
        Selection(
            _normalized_repo_path(selection.source_path, field="selection sourcePath"),
            _normalized_repo_path(
                selection.destination_path, field="selection destinationPath"
            ),
        )
        for selection in selections
    ]
    if not normalized:
        raise ManifestError("at least one tracked-path selection is required")
    if len(set(normalized)) != len(normalized):
        raise ManifestError("duplicate selection roots are not allowed")
    return tuple(
        sorted(
            normalized,
            key=lambda item: (item.source_path, item.destination_path),
        )
    )


def _mapped_entries(
    inventory: Sequence[BlobEntry], selections: Sequence[Selection]
) -> list[dict[str, object]]:
    by_path = {blob.source_path: blob for blob in inventory}
    mapped: list[dict[str, object]] = []
    selected_sources: set[str] = set()
    destinations: dict[str, str] = {}

    for selection in selections:
        exact = by_path.get(selection.source_path)
        if exact is not None:
            candidates = ((exact, selection.destination_path),)
        else:
            prefix = selection.source_path + "/"
            candidates = tuple(
                (
                    blob,
                    _normalized_repo_path(
                        selection.destination_path
                        + "/"
                        + blob.source_path.removeprefix(prefix),
                        field="mapped destinationPath",
                    ),
                )
                for blob in inventory
                if blob.source_path.startswith(prefix)
            )
        if not candidates:
            raise ManifestError(
                f"selection does not name a tracked blob or tree: "
                f"{selection.source_path!r}"
            )

        for blob, destination_path in candidates:
            if blob.source_path in selected_sources:
                raise ManifestError(
                    f"overlapping selections repeat source blob: {blob.source_path!r}"
                )
            selected_sources.add(blob.source_path)
            folded_destination = destination_path.casefold()
            collision = destinations.get(folded_destination)
            if collision is not None:
                raise ManifestError(
                    f"destination collision between {collision!r} and "
                    f"{destination_path!r}"
                )
            destinations[folded_destination] = destination_path
            mapped.append(
                {
                    "sourcePath": blob.source_path,
                    "destinationPath": destination_path,
                    "mode": blob.mode,
                    "blob": blob.object_id,
                    "bytes": blob.size_bytes,
                }
            )

    return sorted(
        mapped,
        key=lambda entry: (
            str(entry["sourcePath"]),
            str(entry["destinationPath"]),
        ),
    )


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Canonical bytes covered by ``manifestSha256``.

    Only the top-level digest field is excluded. A digest-bearing nested field
    remains covered like every other provenance value.
    """

    projected = dict(manifest)
    projected.pop(DIGEST_FIELD, None)
    try:
        return json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"manifest cannot be canonicalized: {exc}") from exc


def compute_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Compute the non-self-referential canonical manifest digest."""

    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def build_extraction_manifest(
    *,
    source_repo: str | Path,
    source_repository: str,
    source_revision: str,
    expected_source_revision: str,
    destination_repository: str,
    legacy_baseline: LegacyBaseline,
    selected_branch: str,
    selections: Iterable[Selection],
) -> dict[str, object]:
    """Build a canonical extraction manifest from an immutable Git tree."""

    # Preserve the mismatch-before-Git guarantee even if another input is bad.
    requested = _require_full_commit(source_revision, field="source revision")
    expected = _require_full_commit(
        expected_source_revision, field="expected source revision"
    )
    if requested != expected:
        raise ManifestError(
            f"source revision mismatch: requested {requested}, expected {expected}"
        )

    source_identity = _canonical_repository(
        source_repository, field="source repository"
    )
    if source_identity != "https://github.com/AstralDeep/AstralDeep.git":
        raise ManifestError("extraction source repository must be AstralDeep")
    destination_identity = _canonical_repository(
        destination_repository, field="destination repository"
    )
    if destination_identity == source_identity:
        raise ManifestError("destination repository must be Plane or Projection")
    baseline_repository = _canonical_repository(
        legacy_baseline.repository, field="legacy baseline repository"
    )
    if baseline_repository != destination_identity:
        raise ManifestError(
            "legacy baseline repository must equal the destination repository"
        )
    baseline_commit = _require_full_commit(
        legacy_baseline.commit, field="legacy baseline commit"
    )
    source_ref = _normalized_ref(
        legacy_baseline.source_ref, field="legacy baseline sourceRef"
    )
    observed_at = _normalized_observed_at(legacy_baseline.observed_at)
    branch = _normalized_branch(selected_branch)
    canonical_selections = _canonical_selections(selections)

    tree, inventory = inventory_tracked_blobs(
        source_repo,
        source_revision=requested,
        expected_source_revision=expected,
    )
    entries = _mapped_entries(inventory, canonical_selections)
    manifest: dict[str, object] = {
        "format": FORMAT,
        "source": {
            "repository": source_identity,
            "commit": requested,
            "tree": tree,
        },
        "destination": {
            "repository": destination_identity,
            "branch": branch,
            "legacyBaseline": {
                "sourceRef": source_ref,
                "commit": baseline_commit,
                "observedAt": observed_at,
            },
        },
        "digestAlgorithm": "sha256",
        "selectionRoots": [selection.source_path for selection in canonical_selections],
        "entries": entries,
    }
    manifest[DIGEST_FIELD] = compute_manifest_sha256(manifest)
    return manifest


def manifest_document_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Return deterministic human-readable manifest bytes for a tracked file."""

    try:
        document = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"manifest cannot be serialized: {exc}") from exc
    return (document + "\n").encode("utf-8")


def write_manifest(path: str | Path, manifest: Mapping[str, Any]) -> None:
    """Atomically replace one regular output path in an existing directory."""

    output = Path(path)
    if not output.name or output.name in {".", ".."}:
        raise ManifestError("manifest output must name one file")
    try:
        parent = output.parent.resolve(strict=True)
    except OSError as exc:
        raise ManifestError(f"manifest output parent does not resolve: {exc}") from exc
    if not parent.is_dir():
        raise ManifestError(f"manifest output parent is not a directory: {parent}")

    resolved_output = parent / output.name
    if resolved_output.is_symlink():
        raise ManifestError(f"refusing to replace symlink output: {resolved_output}")
    if resolved_output.exists() and not resolved_output.is_file():
        raise ManifestError(f"manifest output is not a regular file: {resolved_output}")
    document = manifest_document_bytes(manifest)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(resolved_output)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise ManifestError(
            f"could not write manifest {resolved_output}: {exc}"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", required=True)
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--expected-source-revision", required=True)
    parser.add_argument("--destination-repository", required=True)
    parser.add_argument("--legacy-source-ref", required=True)
    parser.add_argument("--legacy-commit", required=True)
    parser.add_argument("--legacy-observed-at", required=True)
    parser.add_argument("--selected-branch", required=True)
    parser.add_argument(
        "--select",
        action="append",
        nargs=2,
        required=True,
        metavar=("SOURCE_PATH", "DESTINATION_PATH"),
        help="Map one tracked file or recursive tracked tree root",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Manifest path, or '-' (the default) for standard output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_extraction_manifest(
            source_repo=args.source_repo,
            source_repository=args.source_repository,
            source_revision=args.source_revision,
            expected_source_revision=args.expected_source_revision,
            destination_repository=args.destination_repository,
            legacy_baseline=LegacyBaseline(
                repository=args.destination_repository,
                source_ref=args.legacy_source_ref,
                commit=args.legacy_commit,
                observed_at=args.legacy_observed_at,
            ),
            selected_branch=args.selected_branch,
            selections=(
                Selection(source_path, destination_path)
                for source_path, destination_path in args.select
            ),
        )
        if args.output == "-":
            sys.stdout.buffer.write(manifest_document_bytes(manifest))
        else:
            write_manifest(args.output, manifest)
    except ManifestError as exc:
        print(f"build_extraction_manifest: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
