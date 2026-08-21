#!/usr/bin/env python3
"""Fail-closed changed executable-line coverage across maintained languages.

The collector deliberately owns only policy and report parsing. Coverage producers
remain platform-native, while this script selects an immutable event-aware Git
comparison, maps changed source lines to their reports, unions repeated observations,
and emits one deterministic JSON decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
JAVASCRIPT_REPORT_KEYS = {
    "schema_version",
    "producer",
    "producer_version",
    "v8_to_istanbul_version",
    "espree_version",
    "coverage",
}
JAVASCRIPT_REPORT_IDENTITY = {
    "schema_version": 1,
    "producer": "astraldeep-playwright-executable-lines",
    "producer_version": 1,
    "v8_to_istanbul_version": "9.3.0",
    "espree_version": "11.2.0",
}
MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_WITNESS_PATHS = 10_000
MAX_CANDIDATE_WITNESS_BLOB_BYTES = 16 * 1024 * 1024
MAX_CANDIDATE_WITNESS_TOTAL_BYTES = 128 * 1024 * 1024
VOICE_WORKER_SOURCE_ALIASES = {
    "backend/voice_agent/streaming_egress.py": "backend/shared/streaming_egress.py",
    "backend/voice_agent/voice_transcript.py": "backend/shared/voice_transcript.py",
    "backend/voice_agent/watch_ticket.py": "backend/shared/watch_ticket.py",
}
VOICE_WORKER_OVERWRITTEN_SHIMS = frozenset(
    {
        "backend/voice_agent/voice_transcript.py",
        "backend/voice_agent/watch_ticket.py",
    }
)
HEX_SHA = re.compile(r"^[0-9a-fA-F]+$")
HUNK_HEADER = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@",
    re.MULTILINE,
)


class CoveragePolicyError(RuntimeError):
    """A stable fail-closed policy or input error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CoverageTarget:
    """One report-producing maintained-code partition."""

    key: str
    language: str
    roots: tuple[str, ...]
    report_kind: str


@dataclass(frozen=True)
class CoverageProducer:
    """One independently produced native report slot."""

    key: str
    target_key: str
    flag: str
    required_roots: tuple[str, ...]
    excluded_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoverageRepositoryProfile:
    """One repository's owned producer set and composed path namespace."""

    producer_keys: tuple[str, ...]
    source_prefix: str = ""


@dataclass(frozen=True)
class CandidateBlob:
    """One regular source blob anchored to the immutable candidate tree."""

    object_id: str
    size_bytes: int


@dataclass(frozen=True)
class BoundCoverageReport:
    """Stable report bytes plus raw and semantic content identities."""

    path: Path
    content: bytes
    sha256: str
    size_bytes: int
    semantic_sha256: str
    semantic_size_bytes: int
    native_semantic_sha256: str
    native_semantic_size_bytes: int
    coverage: CoverageData


@dataclass(frozen=True)
class RevisionSelection:
    """Unresolved event-authoritative base and candidate identities."""

    event_name: str
    base_sha: str
    candidate_sha: str
    base_source: str
    candidate_source: str


@dataclass
class CoverageData:
    """Unique source-line observations parsed from one or more reports."""

    files: set[str] = field(default_factory=set)
    observed: set[tuple[str, int]] = field(default_factory=set)
    executable: set[tuple[str, int]] = field(default_factory=set)
    covered: set[tuple[str, int]] = field(default_factory=set)

    def add(self, path: str, line: int, covered: bool) -> None:
        if line <= 0:
            raise CoveragePolicyError(
                "unparseable_report", f"non-positive source line for {path!r}"
            )
        observation = (path, line)
        self.files.add(path)
        self.observed.add(observation)
        self.executable.add(observation)
        if covered:
            self.covered.add(observation)

    def merge(self, other: CoverageData) -> None:
        self.files.update(other.files)
        self.observed.update(other.observed)
        self.executable.update(other.executable)
        self.covered.update(other.covered)


TARGETS = (
    CoverageTarget("backend_python", "python", ("backend",), "cobertura"),
    CoverageTarget("tooling_python", "python", ("scripts",), "cobertura"),
    CoverageTarget(
        "windows_python",
        "python",
        ("components/AstralProjection/windows-client",),
        "cobertura",
    ),
    CoverageTarget(
        "javascript",
        "javascript",
        (
            "components/AstralProjection/backend/webrender",
            "components/AstralProjection/tooling/web-ci",
        ),
        "javascript",
    ),
    CoverageTarget(
        "android_app",
        "kotlin",
        (
            "components/AstralProjection/android-client/app/src/main/kotlin",
            "components/AstralProjection/android-client/app/src/main/java",
        ),
        "kover",
    ),
    CoverageTarget(
        "android_core",
        "kotlin",
        (
            "components/AstralProjection/android-client/core/src/main/kotlin",
            "components/AstralProjection/android-client/core/src/main/java",
        ),
        "kover",
    ),
    CoverageTarget(
        "apple",
        "swift",
        (
            "components/AstralProjection/apple-clients/AstralApp/AstralApp",
            "components/AstralProjection/apple-clients/AstralCore/Sources",
            "components/AstralProjection/apple-clients/AstralWatch",
        ),
        "xccov",
    ),
)
TARGET_BY_KEY = {target.key: target for target in TARGETS}
COVERAGE_PRODUCERS = (
    CoverageProducer(
        "backend",
        "backend_python",
        "backend-python",
        ("backend",),
        ("backend/voice_agent",),
    ),
    CoverageProducer(
        "voice_worker",
        "backend_python",
        "voice-worker-python",
        (
            "backend/voice_agent",
            *tuple(VOICE_WORKER_SOURCE_ALIASES.values()),
        ),
    ),
    CoverageProducer("tooling", "tooling_python", "tooling-python", ("scripts",)),
    CoverageProducer(
        "windows",
        "windows_python",
        "windows-python",
        ("components/AstralProjection/windows-client",),
    ),
    CoverageProducer(
        "javascript",
        "javascript",
        "javascript",
        ("components/AstralProjection/backend/webrender/static/client.js",),
    ),
    CoverageProducer(
        "android_app",
        "android_app",
        "android-app",
        (
            "components/AstralProjection/android-client/app/src/main/kotlin",
            "components/AstralProjection/android-client/app/src/main/java",
        ),
    ),
    CoverageProducer(
        "android_core",
        "android_core",
        "android-core",
        (
            "components/AstralProjection/android-client/core/src/main/kotlin",
            "components/AstralProjection/android-client/core/src/main/java",
        ),
    ),
    CoverageProducer(
        "ios", "apple", "ios", ("components/AstralProjection/apple-clients/AstralApp/AstralApp",)
    ),
    CoverageProducer(
        "macos", "apple", "macos", ("components/AstralProjection/apple-clients/AstralApp/AstralApp",)
    ),
    CoverageProducer(
        "watchos", "apple", "watchos", ("components/AstralProjection/apple-clients/AstralWatch",)
    ),
)
PRODUCER_BY_KEY = {producer.key: producer for producer in COVERAGE_PRODUCERS}
REPOSITORY_PROFILES = {
    "monorepo": CoverageRepositoryProfile(tuple(PRODUCER_BY_KEY)),
    "deep": CoverageRepositoryProfile(("backend", "voice_worker", "tooling")),
    "projection": CoverageRepositoryProfile(
        (
            "windows",
            "javascript",
            "android_app",
            "android_core",
            "ios",
            "macos",
            "watchos",
        ),
        "components/AstralProjection",
    ),
}
REPORT_FLAGS = {
    "backend_python": "backend-python",
    "tooling_python": "tooling-python",
    "windows_python": "windows-python",
    "javascript": "javascript",
    "android_app": "android-app",
    "android_core": "android-core",
    "apple": "ios/--macos/--watchos",
}
ANCHORS = (
    "backend/",
    "scripts/",
    "components/AstralProjection/backend/webrender/",
    "components/AstralProjection/windows-client/",
    "components/AstralProjection/android-client/",
    "components/AstralProjection/apple-clients/",
    "components/AstralProjection/tooling/web-ci/",
)


def _repo_path(path: str) -> str:
    value = path.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts:
        raise CoveragePolicyError("invalid_diff_path", f"unsafe Git path {path!r}")
    return pure.as_posix()


def _is_test_or_generated(path: str) -> bool:
    parts = PurePosixPath(path).parts
    lowered = {part.lower() for part in parts}
    if lowered & {
        "tests",
        "test",
        "vendor",
        "generated",
        "node_modules",
        "build",
        "dist",
        "deriveddata",
        ".build",
    }:
        return True
    name = parts[-1].lower()
    return name.startswith("test_") or name.endswith(("_test.py", "tests.swift"))


def classify_path(path: str) -> CoverageTarget | None:
    """Return the explicit maintained-code coverage target for a repo path.

    Tests, generated/build output, vendored JavaScript, and declarative build files
    are intentionally excluded by concrete path and suffix rules.
    """

    path = _repo_path(path)
    if _is_test_or_generated(path):
        return None
    if path.endswith(".py"):
        if path.startswith("backend/"):
            return TARGET_BY_KEY["backend_python"]
        if path.startswith("scripts/"):
            return TARGET_BY_KEY["tooling_python"]
        if path.startswith("components/AstralProjection/windows-client/"):
            return TARGET_BY_KEY["windows_python"]
    if path.endswith((".js", ".mjs")):
        if path.startswith("components/AstralProjection/backend/webrender/") and "/static/vendor/" not in path:
            return TARGET_BY_KEY["javascript"]
        if path.startswith("components/AstralProjection/tooling/web-ci/"):
            return TARGET_BY_KEY["javascript"]
    if path.endswith(".kt"):
        if any(
            path.startswith(f"{root}/") for root in TARGET_BY_KEY["android_app"].roots
        ):
            return TARGET_BY_KEY["android_app"]
        if any(
            path.startswith(f"{root}/") for root in TARGET_BY_KEY["android_core"].roots
        ):
            return TARGET_BY_KEY["android_core"]
    if path.endswith(".swift"):
        if any(path.startswith(f"{root}/") for root in TARGET_BY_KEY["apple"].roots):
            return TARGET_BY_KEY["apple"]
    return None


def _payload_string(payload: Mapping[str, Any], *keys: str) -> str | None:
    value: Any = payload
    for key in keys:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value if isinstance(value, str) and value else None


def _event_identity(
    explicit: str | None,
    event_value: str | None,
    *,
    field: str,
) -> str:
    if event_value is None:
        raise CoveragePolicyError(
            "invalid_event", f"event payload does not contain {field}"
        )
    if explicit is not None and explicit.lower() != event_value.lower():
        raise CoveragePolicyError(
            "event_identity_mismatch",
            f"explicit {field} does not match the immutable event value",
        )
    return event_value


def select_revisions(
    *,
    event_name: str | None,
    event_payload: Mapping[str, Any] | None,
    base_sha: str | None,
    candidate_sha: str | None,
    main_ref: str = "refs/heads/main",
) -> RevisionSelection:
    """Select immutable revision inputs from a PR, main push, or manual run.

    Pull requests and main pushes are event-authoritative: explicit values may only
    repeat, never replace, the event identities. Manual runs require explicit SHAs
    (CLI values or workflow-dispatch inputs) and are later ancestry-verified.
    """

    event = (event_name or "manual").strip()
    payload = event_payload or {}
    if event == "pull_request":
        base = _event_identity(
            base_sha,
            _payload_string(payload, "pull_request", "base", "sha"),
            field="pull_request.base.sha",
        )
        candidate = _event_identity(
            candidate_sha,
            _payload_string(payload, "pull_request", "head", "sha"),
            field="pull_request.head.sha",
        )
        return RevisionSelection(
            event, base, candidate, "pull_request.base.sha", "pull_request.head.sha"
        )
    if event == "push":
        if _payload_string(payload, "ref") != main_ref:
            raise CoveragePolicyError(
                "invalid_event", f"coverage push must target {main_ref}"
            )
        base = _event_identity(
            base_sha, _payload_string(payload, "before"), field="push.before"
        )
        candidate = _event_identity(
            candidate_sha, _payload_string(payload, "after"), field="push.after"
        )
        return RevisionSelection(event, base, candidate, "push.before", "push.after")
    if event not in {"manual", "workflow_dispatch"}:
        raise CoveragePolicyError("invalid_event", f"unsupported event {event!r}")
    inputs = payload.get("inputs") if isinstance(payload, Mapping) else None
    event_base = inputs.get("base_sha") if isinstance(inputs, Mapping) else None
    event_candidate = (
        inputs.get("candidate_sha") if isinstance(inputs, Mapping) else None
    )
    if base_sha and event_base and base_sha.lower() != str(event_base).lower():
        raise CoveragePolicyError(
            "event_identity_mismatch", "explicit base_sha disagrees with manual input"
        )
    if (
        candidate_sha
        and event_candidate
        and candidate_sha.lower() != str(event_candidate).lower()
    ):
        raise CoveragePolicyError(
            "event_identity_mismatch",
            "explicit candidate_sha disagrees with manual input",
        )
    base = base_sha or (event_base if isinstance(event_base, str) else None)
    candidate = candidate_sha or (
        event_candidate if isinstance(event_candidate, str) else None
    )
    if not base or not candidate:
        raise CoveragePolicyError(
            "missing_revision",
            "manual coverage requires explicit base and candidate SHAs",
        )
    return RevisionSelection(
        event, base, candidate, "manual.base_sha", "manual.candidate_sha"
    )


def _git(
    repo: Path,
    arguments: Sequence[str],
    *,
    allow_ancestor_false: bool = False,
) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if allow_ancestor_false and process.returncode == 1:
        return b""
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise CoveragePolicyError(
            "git_error", f"git {' '.join(arguments[:2])} failed{suffix}"
        )
    return process.stdout


def _validate_sha_text(value: str, label: str) -> str:
    if len(value) not in {40, 64} or not HEX_SHA.fullmatch(value):
        raise CoveragePolicyError(
            "invalid_revision", f"{label} must be a full hexadecimal commit SHA"
        )
    lowered = value.lower()
    if set(lowered) == {"0"}:
        raise CoveragePolicyError("zero_revision", f"{label} cannot be the zero SHA")
    return lowered


def validate_revisions(repo: Path, selection: RevisionSelection) -> RevisionSelection:
    """Resolve exact commits and prove a non-zero base ancestor of the candidate."""

    repo = repo.resolve()
    base_input = _validate_sha_text(selection.base_sha, "base_sha")
    candidate_input = _validate_sha_text(selection.candidate_sha, "candidate_sha")

    def resolve(value: str, label: str) -> str:
        output = _git(repo, ["rev-parse", "--verify", f"{value}^{{commit}}"])
        resolved = output.decode("ascii", "strict").strip().lower()
        if resolved != value:
            raise CoveragePolicyError(
                "revision_mismatch",
                f"{label} did not resolve to the exact supplied SHA",
            )
        return resolved

    base = resolve(base_input, "base_sha")
    candidate = resolve(candidate_input, "candidate_sha")
    if base == candidate:
        raise CoveragePolicyError(
            "empty_revision_range", "base and candidate SHAs must be distinct"
        )
    process = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", base, candidate],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode == 1:
        raise CoveragePolicyError(
            "non_ancestor_base", "base SHA is not an ancestor of candidate SHA"
        )
    if process.returncode != 0:
        raise CoveragePolicyError("git_error", "git merge-base ancestry check failed")
    return RevisionSelection(
        selection.event_name,
        base,
        candidate,
        selection.base_source,
        selection.candidate_source,
    )


def read_changed_lines(
    repo: Path,
    base_sha: str,
    candidate_sha: str,
    *,
    source_prefix: str = "",
) -> dict[str, set[int]]:
    """Read added/modified candidate lines from a NUL-delimited immutable Git diff."""

    raw_paths = _git(
        repo,
        [
            "diff",
            "--name-only",
            "-z",
            "--diff-filter=AM",
            "--no-renames",
            base_sha,
            candidate_sha,
            "--",
        ],
    )
    if raw_paths and not raw_paths.endswith(b"\0"):
        raise CoveragePolicyError(
            "invalid_git_diff", "NUL-delimited Git path output was truncated"
        )
    try:
        decoded = [
            item.decode("utf-8", "strict") for item in raw_paths.split(b"\0") if item
        ]
    except UnicodeDecodeError as exc:
        raise CoveragePolicyError(
            "invalid_git_diff", "Git diff contains a non-UTF-8 path"
        ) from exc
    prefix = _repo_path(source_prefix) if source_prefix else ""
    changed: dict[str, set[int]] = {}
    for raw_path in sorted(set(decoded)):
        git_path = _repo_path(raw_path)
        path = _repo_path(f"{prefix}/{git_path}") if prefix else git_path
        maintained = classify_path(path) is not None
        text_override = ["--text"] if maintained else []
        patch = _git(
            repo,
            [
                "diff",
                "--unified=0",
                "--no-color",
                "--no-ext-diff",
                "--no-renames",
                *text_override,
                base_sha,
                candidate_sha,
                "--",
                git_path,
            ],
        ).decode("utf-8", "strict")
        lines: set[int] = set()
        hunks = list(HUNK_HEADER.finditer(patch))
        if maintained and not hunks:
            raise CoveragePolicyError(
                "invalid_git_diff",
                f"maintained source path {path!r} has no textual diff hunks",
            )
        for match in hunks:
            start = int(match.group("start"))
            count = int(match.group("count") or "1")
            lines.update(range(start, start + count))
        changed[path] = lines
    return changed


def _read_report(path: Path) -> bytes:
    """Read one unchanged regular file through a stable descriptor."""

    try:
        before_path = path.lstat()
    except OSError as exc:
        raise CoveragePolicyError(
            "missing_report", f"coverage report is unavailable: {path}"
        ) from exc
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise CoveragePolicyError(
            "unparseable_report",
            f"coverage report must be a regular non-symlink file: {path}",
        )
    # Python's Windows CRT opens descriptors in text mode unless O_BINARY is
    # explicit.  ``os.read`` would then translate CRLF to LF while ``fstat``
    # continues to report the physical byte length, producing a false
    # ``invalid size`` failure and hashing bytes other than the artifact that
    # was bound.  O_BINARY is zero/absent on POSIX, so Linux behavior is
    # unchanged.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CoveragePolicyError(
            "missing_report", f"coverage report is unreadable: {path}"
        ) from exc
    try:
        before_read = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_REPORT_BYTES:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_REPORT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after_read = os.fstat(descriptor)
    except OSError as exc:
        raise CoveragePolicyError(
            "missing_report", f"coverage report is unreadable: {path}"
        ) from exc
    finally:
        os.close(descriptor)
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise CoveragePolicyError(
            "report_changed", f"coverage report changed while being read: {path}"
        ) from exc
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (
        any(getattr(before_read, field) != getattr(after_read, field) for field in stable_fields)
        or before_path.st_dev != before_read.st_dev
        or before_path.st_ino != before_read.st_ino
        or after_path.st_dev != before_read.st_dev
        or after_path.st_ino != before_read.st_ino
    ):
        raise CoveragePolicyError(
            "report_changed", f"coverage report changed while being read: {path}"
        )
    content = b"".join(chunks)
    if not content or len(content) > MAX_REPORT_BYTES or len(content) != after_read.st_size:
        raise CoveragePolicyError(
            "unparseable_report", f"coverage report has invalid size: {path}"
        )
    return content


def _semantic_report_content(coverage: CoverageData) -> bytes:
    """Serialize only normalized source observations, never producer metadata."""

    value = {
        "files": sorted(coverage.files),
        "observed": sorted([path, line] for path, line in coverage.observed),
        "executable": sorted([path, line] for path, line in coverage.executable),
        "covered": sorted([path, line] for path, line in coverage.covered),
    }
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _native_report_content(content: bytes, target_key: str) -> bytes:
    """Canonicalize native observations without applying target path filters."""

    target = TARGET_BY_KEY[target_key]
    if target.report_kind == "cobertura":
        root = ET.fromstring(content)
        value: Any = {
            "kind": "cobertura",
            "sources": sorted(
                (node.text or "").strip().replace("\\", "/")
                for node in root.iter()
                if _local_name(node) == "source" and (node.text or "").strip()
            ),
            "classes": sorted(
                [
                    (node.get("filename") or "").replace("\\", "/"),
                    sorted(
                        [
                            int(line.get("number", "-1")),
                            int(line.get("hits", "-1")),
                        ]
                        for line in node.iter()
                        if _local_name(line) == "line"
                    ),
                ]
                for node in root.iter()
                if _local_name(node) == "class"
            ),
        }
    elif target.report_kind == "kover":
        root = ET.fromstring(content)
        value = {
            "kind": "kover",
            "sources": sorted(
                [
                    package.get("name") or "",
                    source.get("name") or "",
                    sorted(
                        [
                            int(line.get("nr", "-1")),
                            int(line.get("mi", "-1")),
                            int(line.get("ci", "-1")),
                        ]
                        for line in source
                        if _local_name(line) == "line"
                    ),
                ]
                for package in root.iter()
                if _local_name(package) == "package"
                for source in package
                if _local_name(source) == "sourcefile"
            ),
        }
    elif target.report_kind == "javascript":
        document = _strict_json(content)
        coverage = document.get("coverage", {}) if isinstance(document, Mapping) else {}
        value = {
            "kind": "javascript",
            "sources": sorted(
                [
                    str(record.get("path", raw_path)).replace("\\", "/"),
                    sorted(
                        [
                            int(location.get("start", {}).get("line", -1)),
                            int(hits.get(statement_id, -1)),
                        ]
                        for statement_id, location in statements.items()
                    ),
                ]
                for raw_path, record in coverage.items()
                if isinstance(record, Mapping)
                for statements, hits in [
                    (record.get("statementMap", {}), record.get("s", {}))
                ]
                if isinstance(statements, Mapping) and isinstance(hits, Mapping)
            ),
        }
    else:
        document = _strict_json(content)
        value = {
            "kind": "xccov",
            "sources": sorted(
                [
                    raw_path.replace("\\", "/"),
                    sorted(
                        [
                            int(item.get("line", -1)),
                            bool(item.get("isExecutable")),
                            int(item.get("executionCount", 0)),
                        ]
                        for item in observations
                        if isinstance(item, Mapping)
                    ),
                ]
                for raw_path, observations in document.items()
                if isinstance(raw_path, str)
                and raw_path.endswith(".swift")
                and isinstance(observations, list)
            ),
        }
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _apply_producer_source_aliases(
    coverage: CoverageData, producer_key: str | None
) -> CoverageData:
    """Map worker-runtime copies back to the candidate sources that built them."""

    aliases = VOICE_WORKER_SOURCE_ALIASES if producer_key == "voice_worker" else {}
    if not aliases:
        return coverage

    def remap(observations: set[tuple[str, int]]) -> set[tuple[str, int]]:
        return {(aliases.get(path, path), line) for path, line in observations}

    return CoverageData(
        files={aliases.get(path, path) for path in coverage.files},
        observed=remap(coverage.observed),
        executable=remap(coverage.executable),
        covered=remap(coverage.covered),
    )


def _coverage_report_binding(
    content: bytes,
    target_key: str,
    *,
    source: Path,
    producer_key: str | None = None,
) -> tuple[dict[str, Any], CoverageData]:
    coverage = _apply_producer_source_aliases(
        _parse_coverage_content(content, target_key, source=source), producer_key
    )
    semantic = _semantic_report_content(coverage)
    native_semantic = _native_report_content(content, target_key)
    return (
        {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "semantic_sha256": hashlib.sha256(semantic).hexdigest(),
            "semantic_size_bytes": len(semantic),
            "native_semantic_sha256": hashlib.sha256(native_semantic).hexdigest(),
            "native_semantic_size_bytes": len(native_semantic),
        },
        coverage,
    )


def coverage_report_identity(
    content: bytes, target_key: str, *, producer_key: str | None = None
) -> dict[str, Any]:
    """Return raw and semantic identities for already-bound report bytes."""

    identity, _coverage = _coverage_report_binding(
        content,
        target_key,
        source=Path("<bound-coverage-report>"),
        producer_key=producer_key,
    )
    return identity


def _normalized_report_path(raw: str, target: CoverageTarget) -> str | None:
    """Map a producer path from its first repository anchor only.

    The first anchor is authoritative.  Looking for a later anchor that happens to
    match ``target`` would let an absolute ``backend/scripts/...`` path masquerade
    as a repository-root ``scripts/...`` path.
    """

    value = raw.strip().replace("\\", "/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme in {"file", "http", "https"}:
        value = urllib.parse.unquote(parsed.path)
    value = value.replace("\\", "/")
    anchored: list[tuple[int, str]] = []
    for anchor in ANCHORS:
        if value.startswith(anchor):
            anchored.append((0, value))
        marker = f"/{anchor}"
        start = 0
        while (index := value.find(marker, start)) >= 0:
            anchored.append((index + 1, value[index + 1 :]))
            start = index + 1
    if anchored:
        candidate = min(anchored, key=lambda item: item[0])[1]
    else:
        relative = value.lstrip("/")
        projection_owner_roots = (
            "backend/webrender/",
            "tooling/web-ci/",
            "windows-client/",
            "android-client/",
            "apple-clients/",
        )
        if not value.startswith("/") and relative.startswith(projection_owner_roots):
            candidate = f"components/AstralProjection/{relative}"
        elif target.key == "javascript" and relative.startswith("static/"):
            candidate = f"components/AstralProjection/backend/webrender/{relative}"
        else:
            return None
    try:
        normalized = _repo_path(candidate)
    except CoveragePolicyError:
        return None
    classified = classify_path(normalized)
    return normalized if classified and classified.key == target.key else None


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise CoveragePolicyError("unparseable_report", f"invalid integer for {label}")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        result = int(value)
    else:
        raise CoveragePolicyError("unparseable_report", f"invalid integer for {label}")
    if result < 0:
        raise CoveragePolicyError("unparseable_report", f"negative integer for {label}")
    return result


def _local_name(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _direct_children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node if _local_name(child) == name]


def _line_counter(node: ET.Element, *, label: str) -> tuple[int, int]:
    counters = [
        child
        for child in _direct_children(node, "counter")
        if child.get("type") == "LINE"
    ]
    if len(counters) != 1:
        raise CoveragePolicyError(
            "unparseable_report", f"{label} must contain exactly one LINE counter"
        )
    counter = counters[0]
    return (
        _integer(counter.get("missed"), label=f"{label} missed LINE counter"),
        _integer(counter.get("covered"), label=f"{label} covered LINE counter"),
    )


def _instruction_counter(node: ET.Element, *, label: str) -> tuple[int, int]:
    counters = [
        child
        for child in _direct_children(node, "counter")
        if child.get("type") == "INSTRUCTION"
    ]
    if len(counters) != 1:
        raise CoveragePolicyError(
            "unparseable_report",
            f"{label} must contain exactly one INSTRUCTION counter",
        )
    counter = counters[0]
    return (
        _integer(
            counter.get("missed"),
            label=f"{label} missed INSTRUCTION counter",
        ),
        _integer(
            counter.get("covered"),
            label=f"{label} covered INSTRUCTION counter",
        ),
    )


def _rate(value: str | None, *, label: str) -> Decimal:
    try:
        result = Decimal(value) if value is not None else Decimal("NaN")
    except InvalidOperation as exc:
        raise CoveragePolicyError(
            "unparseable_report", f"invalid line rate for {label}"
        ) from exc
    if not result.is_finite() or result < 0 or result > 1:
        raise CoveragePolicyError(
            "unparseable_report", f"invalid line rate for {label}"
        )
    return result


def _validate_rate(value: str | None, *, covered: int, total: int, label: str) -> None:
    if value is None:
        return
    actual = _rate(value, label=label)
    expected = Decimal(covered) / Decimal(total) if total else Decimal(1)
    if abs(actual - expected) > Decimal("0.0001"):
        raise CoveragePolicyError(
            "unparseable_report", f"{label} disagrees with its line observations"
        )


def _cobertura_sources(root: ET.Element) -> list[str]:
    sources: list[str] = []
    for container in root.iter():
        if _local_name(container) != "sources":
            continue
        for source in _direct_children(container, "source"):
            if source.text and source.text.strip():
                sources.append(source.text.strip().replace("\\", "/"))
    return sources


def _cobertura_path(
    raw: str, sources: Sequence[str], target: CoverageTarget
) -> str | None:
    """Resolve coverage.py filenames against their declared source roots."""

    candidates: set[str] = set()
    direct = _normalized_report_path(raw, target)
    if direct is not None:
        candidates.add(direct)
    parsed = urllib.parse.urlsplit(raw)
    is_relative = (
        not parsed.scheme and not PurePosixPath(raw.replace("\\", "/")).is_absolute()
    )
    if is_relative:
        for source in sources:
            combined = f"{source.rstrip('/')}/{raw.lstrip('./')}"
            mapped = _normalized_report_path(combined, target)
            if mapped is not None:
                candidates.add(mapped)
    if len(candidates) > 1:
        raise CoveragePolicyError(
            "unparseable_report", f"ambiguous Cobertura source path {raw!r}"
        )
    return next(iter(candidates), None)


def _parse_cobertura(content: bytes, target: CoverageTarget) -> CoverageData:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise CoveragePolicyError(
            "unparseable_report", "invalid Cobertura XML"
        ) from exc
    data = CoverageData()
    declared_valid = _integer(root.get("lines-valid"), label="Cobertura lines-valid")
    declared_covered = _integer(
        root.get("lines-covered"), label="Cobertura lines-covered"
    )
    if declared_covered > declared_valid:
        raise CoveragePolicyError(
            "unparseable_report", "Cobertura covered line total exceeds valid lines"
        )
    sources = _cobertura_sources(root)
    classes = [node for node in root.iter() if _local_name(node) == "class"]
    if not classes:
        raise CoveragePolicyError(
            "unparseable_report", "Cobertura report has no classes"
        )
    artifact_lines: dict[tuple[str, int], bool] = {}
    normalized_lines: set[tuple[str, int]] = set()
    for class_node in classes:
        raw = class_node.get("filename")
        if not raw:
            raise CoveragePolicyError(
                "unparseable_report", "Cobertura class lacks filename"
            )
        path = _cobertura_path(raw, sources, target)
        class_total = 0
        class_covered = 0
        for line_node in class_node.iter():
            if _local_name(line_node) != "line":
                continue
            number = _integer(line_node.get("number"), label="Cobertura line")
            if number <= 0:
                raise CoveragePolicyError(
                    "unparseable_report", "Cobertura line must be positive"
                )
            hits = _integer(line_node.get("hits"), label="Cobertura hits")
            identity = (raw.replace("\\", "/"), number)
            if identity in artifact_lines:
                raise CoveragePolicyError(
                    "unparseable_report", "duplicate Cobertura source-line observation"
                )
            covered = hits > 0
            artifact_lines[identity] = covered
            class_total += 1
            class_covered += int(covered)
            if path is not None:
                observation = (path, number)
                if observation in normalized_lines:
                    raise CoveragePolicyError(
                        "unparseable_report",
                        "duplicate normalized Cobertura source-line observation",
                    )
                normalized_lines.add(observation)
                data.add(path, number, covered)
        _validate_rate(
            class_node.get("line-rate"),
            covered=class_covered,
            total=class_total,
            label=f"Cobertura class {raw!r} line-rate",
        )
        if path is not None:
            data.files.add(path)
    parsed_covered = sum(artifact_lines.values())
    if len(artifact_lines) != declared_valid or parsed_covered != declared_covered:
        raise CoveragePolicyError(
            "unparseable_report",
            "Cobertura root totals disagree with class line observations",
        )
    _validate_rate(
        root.get("line-rate"),
        covered=parsed_covered,
        total=len(artifact_lines),
        label="Cobertura root line-rate",
    )
    return data


def _parse_kover(content: bytes, target: CoverageTarget) -> CoverageData:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise CoveragePolicyError("unparseable_report", "invalid Kover XML") from exc
    data = CoverageData()
    packages = _direct_children(root, "package")
    if not packages:
        raise CoveragePolicyError("unparseable_report", "Kover report has no packages")
    report_missed = 0
    report_covered = 0
    report_instruction_missed = 0
    report_instruction_covered = 0
    normalized_lines: set[tuple[str, int]] = set()
    for package in packages:
        package_name = (package.get("name") or "").replace(".", "/").strip("/")
        package_missed = 0
        package_covered = 0
        package_instruction_missed = 0
        package_instruction_covered = 0
        for source in _direct_children(package, "sourcefile"):
            name = source.get("name")
            if not name:
                raise CoveragePolicyError(
                    "unparseable_report", "Kover sourcefile lacks a name"
                )
            relative = f"{package_name}/{name}" if package_name else name
            relative = f"{target.roots[0]}/{relative}"
            path = _normalized_report_path(relative, target)
            source_instruction_missed = 0
            source_instruction_covered = 0
            executable_lines = 0
            seen_numbers: set[int] = set()
            for line_node in _direct_children(source, "line"):
                line = _integer(line_node.get("nr"), label="Kover line")
                if line <= 0 or line in seen_numbers:
                    raise CoveragePolicyError(
                        "unparseable_report",
                        "Kover source lines must be positive and unique",
                    )
                seen_numbers.add(line)
                covered = _integer(line_node.get("ci", 0), label="Kover ci")
                missed = _integer(line_node.get("mi", 0), label="Kover mi")
                source_instruction_covered += covered
                source_instruction_missed += missed
                if path is not None:
                    observation = (path, line)
                    if observation in normalized_lines:
                        raise CoveragePolicyError(
                            "unparseable_report",
                            "duplicate normalized Kover source-line observation",
                        )
                    normalized_lines.add(observation)
                    data.observed.add(observation)
                # Kover legitimately emits physical source-line observations
                # with no mapped JVM instructions (mi=0, ci=0), notably around
                # coroutine/inline lowering. They are not executable LINE
                # counter members and must not be counted as missed coverage.
                if covered + missed == 0:
                    continue
                executable_lines += 1
                is_covered = covered > 0
                if path is not None:
                    data.add(path, line, is_covered)
            source_label = f"Kover sourcefile {name!r}"
            source_line_counter = _line_counter(source, label=source_label)
            if sum(source_line_counter) < executable_lines:
                raise CoveragePolicyError(
                    "unparseable_report",
                    f"{source_label} LINE counter omits executable observations",
                )
            if _instruction_counter(source, label=source_label) != (
                source_instruction_missed,
                source_instruction_covered,
            ):
                raise CoveragePolicyError(
                    "unparseable_report",
                    f"{source_label} INSTRUCTION counter disagrees with lines",
                )
            package_missed += source_line_counter[0]
            package_covered += source_line_counter[1]
            package_instruction_missed += source_instruction_missed
            package_instruction_covered += source_instruction_covered
            if path is not None:
                data.files.add(path)
        if _line_counter(package, label=f"Kover package {package_name!r}") != (
            package_missed,
            package_covered,
        ):
            raise CoveragePolicyError(
                "unparseable_report",
                f"Kover package {package_name!r} LINE counter disagrees with sourcefiles",
            )
        if _instruction_counter(
            package, label=f"Kover package {package_name!r}"
        ) != (package_instruction_missed, package_instruction_covered):
            raise CoveragePolicyError(
                "unparseable_report",
                f"Kover package {package_name!r} INSTRUCTION counter "
                "disagrees with sourcefiles",
            )
        report_missed += package_missed
        report_covered += package_covered
        report_instruction_missed += package_instruction_missed
        report_instruction_covered += package_instruction_covered
    if _line_counter(root, label="Kover report") != (
        report_missed,
        report_covered,
    ):
        raise CoveragePolicyError(
            "unparseable_report",
            "Kover report LINE counter disagrees with packages",
        )
    if _instruction_counter(root, label="Kover report") != (
        report_instruction_missed,
        report_instruction_covered,
    ):
        raise CoveragePolicyError(
            "unparseable_report",
            "Kover report INSTRUCTION counter disagrees with packages",
        )
    return data


def _strict_json(content: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise CoveragePolicyError(
                    "unparseable_report", f"duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    def nonfinite(value: str) -> None:
        raise CoveragePolicyError(
            "unparseable_report", f"non-finite JSON value {value}"
        )

    try:
        return json.loads(content, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoveragePolicyError(
            "unparseable_report", "invalid coverage JSON"
        ) from exc


def _parse_istanbul(
    document: Mapping[str, Any], target: CoverageTarget
) -> CoverageData:
    data = CoverageData()
    normalized_sources: set[str] = set()
    normalized_lines: set[tuple[str, int]] = set()
    for raw_key, raw_record in document.items():
        if not isinstance(raw_record, Mapping):
            raise CoveragePolicyError(
                "unparseable_report", "Istanbul source record must be an object"
            )
        if not ({"statementMap", "s", "l"} & set(raw_record)):
            raise CoveragePolicyError(
                "unparseable_report",
                "Istanbul source record lacks canonical statementMap+s data",
            )
        if "l" in raw_record:
            raise CoveragePolicyError(
                "unparseable_report",
                "Istanbul line maps are not accepted; canonical statementMap+s is required",
            )
        raw_path = raw_record.get("path", raw_key)
        if not isinstance(raw_path, str):
            continue
        path = _normalized_report_path(raw_path, target)
        if path is None:
            continue
        if path in normalized_sources:
            raise CoveragePolicyError(
                "unparseable_report", "duplicate normalized Istanbul source record"
            )
        normalized_sources.add(path)
        data.files.add(path)
        statements = raw_record.get("statementMap")
        hits_by_id = raw_record.get("s")
        if not isinstance(statements, Mapping) or not isinstance(hits_by_id, Mapping):
            raise CoveragePolicyError(
                "unparseable_report", "Istanbul statement maps are incomplete"
            )
        if not statements or not hits_by_id:
            raise CoveragePolicyError(
                "unparseable_report", "Istanbul statement maps cannot be empty"
            )
        if set(statements) != set(hits_by_id):
            raise CoveragePolicyError(
                "unparseable_report", "Istanbul statement and hit keys differ"
            )
        for statement_id, location in statements.items():
            if not isinstance(location, Mapping) or statement_id not in hits_by_id:
                raise CoveragePolicyError(
                    "unparseable_report", "Istanbul statement entry is incomplete"
                )
            start = location.get("start")
            end = location.get("end")
            if not isinstance(start, Mapping) or not isinstance(end, Mapping):
                raise CoveragePolicyError(
                    "unparseable_report", "Istanbul statement location is invalid"
                )
            first = _integer(start.get("line"), label="Istanbul start line")
            last = _integer(end.get("line"), label="Istanbul end line")
            if last < first:
                raise CoveragePolicyError(
                    "unparseable_report", "Istanbul statement range is reversed"
                )
            covered = (
                _integer(hits_by_id[statement_id], label="Istanbul statement hits") > 0
            )
            if (path, first) in normalized_lines:
                raise CoveragePolicyError(
                    "unparseable_report",
                    "duplicate Istanbul executable-line observation",
                )
            normalized_lines.add((path, first))
            data.add(path, first, covered)
    if not data.files:
        raise CoveragePolicyError(
            "unparseable_report", "Istanbul report has no sources"
        )
    return data


def _parse_javascript(content: bytes, target: CoverageTarget) -> CoverageData:
    document = _strict_json(content)
    if not isinstance(document, Mapping) or set(document) != JAVASCRIPT_REPORT_KEYS:
        raise CoveragePolicyError(
            "unparseable_report",
            "JavaScript coverage requires the exact lock-pinned AstralDeep "
            "executable-line producer envelope",
        )
    for key, expected in JAVASCRIPT_REPORT_IDENTITY.items():
        if document.get(key) != expected:
            raise CoveragePolicyError(
                "unparseable_report",
                f"JavaScript coverage has unsupported producer field {key!r}",
            )
    coverage = document.get("coverage")
    if not isinstance(coverage, Mapping) or not coverage:
        raise CoveragePolicyError(
            "unparseable_report", "JavaScript coverage envelope is empty"
        )
    return _parse_istanbul(coverage, target)


def _add_xccov_archive_lines(data: CoverageData, path: str, values: Any) -> None:
    if not isinstance(values, list):
        raise CoveragePolicyError(
            "unparseable_report", "xccov archive file observations must be an array"
        )
    if not values:
        raise CoveragePolicyError(
            "unparseable_report", "xccov archive file observations cannot be empty"
        )
    seen_lines: set[int] = set()
    for item in values:
        if not isinstance(item, Mapping):
            raise CoveragePolicyError(
                "unparseable_report", "invalid xccov archive line observation"
            )
        executable = item.get("isExecutable")
        if not isinstance(executable, bool):
            raise CoveragePolicyError(
                "unparseable_report", "xccov archive line lacks isExecutable"
            )
        line = _integer(item.get("line"), label="xccov archive line")
        if line <= 0 or line in seen_lines:
            raise CoveragePolicyError(
                "unparseable_report",
                "xccov archive lines must be positive and unique",
            )
        seen_lines.add(line)
        data.observed.add((path, line))
        if executable:
            count = _integer(
                item.get("executionCount", 0),
                label="xccov archive execution count",
            )
            data.add(path, line, count > 0)
        elif "executionCount" in item:
            _integer(item.get("executionCount"), label="xccov archive execution count")
    if seen_lines != set(range(1, max(seen_lines) + 1)):
        raise CoveragePolicyError(
            "unparseable_report",
            "xccov archive must observe every physical line contiguously from line 1",
        )
    data.files.add(path)


def _parse_xccov(content: bytes, target: CoverageTarget) -> CoverageData:
    document = _strict_json(content)
    if not isinstance(document, Mapping):
        raise CoveragePolicyError(
            "unparseable_report", "xccov report must be an object"
        )
    if "targets" in document or "files" in document:
        raise CoveragePolicyError(
            "unsupported_xccov_report",
            "xccov summary JSON lacks per-line execution counts; export and "
            "map per-file observations with scripts/export_xccov_line_coverage.py",
        )
    data = CoverageData()
    archive_entries = [
        (raw_path, observations)
        for raw_path, observations in document.items()
        if isinstance(raw_path, str) and raw_path.endswith(".swift")
    ]
    if not archive_entries:
        raise CoveragePolicyError(
            "unparseable_report", "xccov archive has no Swift line observations"
        )
    normalized_sources: set[str] = set()
    for raw_path, observations in archive_entries:
        path = _normalized_report_path(raw_path, target)
        if path is not None:
            if path in normalized_sources:
                raise CoveragePolicyError(
                    "unparseable_report", "duplicate normalized xccov source record"
                )
            normalized_sources.add(path)
            _add_xccov_archive_lines(data, path, observations)
    if not data.files:
        raise CoveragePolicyError(
            "unparseable_report", "xccov archive has no maintained Swift sources"
        )
    return data


def _parse_coverage_content(
    content: bytes, target_key: str, *, source: Path
) -> CoverageData:
    """Parse already-bounded report bytes so identity and policy use one read."""

    try:
        target = TARGET_BY_KEY[target_key]
    except KeyError as exc:
        raise CoveragePolicyError(
            "invalid_target", f"unknown target {target_key!r}"
        ) from exc
    try:
        if target.report_kind == "cobertura":
            return _parse_cobertura(content, target)
        if target.report_kind == "kover":
            return _parse_kover(content, target)
        if target.report_kind == "javascript":
            return _parse_javascript(content, target)
        return _parse_xccov(content, target)
    except CoveragePolicyError as exc:
        if exc.code in {"missing_report", "unsupported_xccov_report"}:
            raise
        raise CoveragePolicyError(
            "unparseable_report", f"{source}: {exc.message}"
        ) from exc


def parse_coverage_report(path: Path, target_key: str) -> CoverageData:
    """Parse one Cobertura/Kover, V8/Istanbul, or xccov report."""

    return _parse_coverage_content(_read_report(path), target_key, source=path)


def _percentage(covered: int, executable: int) -> float:
    return round(covered * 100.0 / executable, 2)


def _threshold(value: float | int | str) -> Decimal:
    try:
        threshold = Decimal(str(value))
    except InvalidOperation as exc:
        raise CoveragePolicyError(
            "invalid_threshold", "fail-under is not numeric"
        ) from exc
    if not threshold.is_finite() or threshold < 0 or threshold > 100:
        raise CoveragePolicyError(
            "invalid_threshold", "fail-under must be between 0 and 100"
        )
    return threshold


def _unique_report_inputs(
    reports: Mapping[str, Sequence[Path]],
    producer_slots: Mapping[str, Path] | None = None,
) -> dict[str, list[BoundCoverageReport]]:
    """Read each producer artifact once and reject aliased evidence globally."""

    seen_paths: dict[Path, str] = {}
    seen_files: dict[tuple[int, int], str] = {}
    seen_payloads: dict[tuple[int, str], str] = {}
    seen_semantics: dict[tuple[int, str], str] = {}
    seen_native_semantics: dict[tuple[int, str], str] = {}
    slot_by_path = {
        Path(path).resolve(): slot for slot, path in (producer_slots or {}).items()
    }
    loaded: dict[str, list[BoundCoverageReport]] = {}
    for target_key in sorted(reports):
        loaded[target_key] = []
        for value in sorted((Path(path) for path in reports[target_key]), key=str):
            content = _read_report(value)
            try:
                stat_result = value.stat()
            except OSError as exc:
                raise CoveragePolicyError(
                    "missing_report",
                    f"coverage report is unavailable: {value}",
                ) from exc
            canonical_path = value.resolve()
            file_identity = (stat_result.st_dev, stat_result.st_ino)
            artifact_identity, coverage = _coverage_report_binding(
                content,
                target_key,
                source=value,
                producer_key=slot_by_path.get(canonical_path),
            )
            payload_identity = (
                artifact_identity["size_bytes"],
                artifact_identity["sha256"],
            )
            semantic_identity = (
                artifact_identity["semantic_size_bytes"],
                artifact_identity["semantic_sha256"],
            )
            native_semantic_identity = (
                artifact_identity["native_semantic_size_bytes"],
                artifact_identity["native_semantic_sha256"],
            )
            duplicate_of = (
                seen_paths.get(canonical_path)
                or seen_files.get(file_identity)
                or seen_payloads.get(payload_identity)
                or seen_semantics.get(semantic_identity)
                or seen_native_semantics.get(native_semantic_identity)
            )
            if duplicate_of is not None:
                raise CoveragePolicyError(
                    "duplicate_report",
                    f"coverage report for {target_key} duplicates {duplicate_of}",
                )
            identity = f"{target_key}:{value}"
            seen_paths[canonical_path] = identity
            seen_files[file_identity] = identity
            seen_payloads[payload_identity] = identity
            seen_semantics[semantic_identity] = identity
            seen_native_semantics[native_semantic_identity] = identity
            loaded[target_key].append(
                BoundCoverageReport(
                    path=value,
                    content=content,
                    sha256=artifact_identity["sha256"],
                    size_bytes=artifact_identity["size_bytes"],
                    semantic_sha256=artifact_identity["semantic_sha256"],
                    semantic_size_bytes=artifact_identity["semantic_size_bytes"],
                    native_semantic_sha256=artifact_identity[
                        "native_semantic_sha256"
                    ],
                    native_semantic_size_bytes=artifact_identity[
                        "native_semantic_size_bytes"
                    ],
                    coverage=coverage,
                )
            )
    return loaded


def _path_matches_root(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def _producer_applies_to_path(slot_key: str, path: str) -> bool:
    """Return whether a strict producer is responsible for one maintained path."""

    if slot_key == "backend":
        return path in VOICE_WORKER_OVERWRITTEN_SHIMS or (
            path.startswith("backend/")
            and not path.startswith("backend/voice_agent/")
        )
    if slot_key == "voice_worker":
        return (
            path.startswith("backend/voice_agent/")
            and path not in VOICE_WORKER_OVERWRITTEN_SHIMS
        ) or path in set(VOICE_WORKER_SOURCE_ALIASES.values())
    if slot_key == "ios":
        return any(
            _path_matches_root(path, root)
            for root in (
                "components/AstralProjection/apple-clients/AstralApp/AstralApp",
                "components/AstralProjection/apple-clients/AstralCore/Sources",
            )
        )
    if slot_key == "macos":
        return any(
            _path_matches_root(path, root)
            for root in (
                "components/AstralProjection/apple-clients/AstralApp/AstralApp",
                "components/AstralProjection/apple-clients/AstralCore/Sources",
            )
        )
    if slot_key == "watchos":
        return _path_matches_root(path, "components/AstralProjection/apple-clients/AstralWatch")
    target = TARGET_BY_KEY[PRODUCER_BY_KEY[slot_key].target_key]
    return any(_path_matches_root(path, root) for root in target.roots)


def _producer_owned_coverage(
    coverage: CoverageData, slot_key: str
) -> CoverageData:
    """Exclude observations outside a strict producer's owned source partition."""

    def owned(path: str) -> bool:
        return _producer_applies_to_path(slot_key, path)

    return CoverageData(
        files={path for path in coverage.files if owned(path)},
        observed={item for item in coverage.observed if owned(item[0])},
        executable={item for item in coverage.executable if owned(item[0])},
        covered={item for item in coverage.covered if owned(item[0])},
    )


def _candidate_source_blobs(
    repo: Path,
    candidate_sha: str,
    *,
    source_prefix: str = "",
) -> dict[str, CandidateBlob]:
    """Inventory regular blobs from one immutable candidate tree."""

    output = _git(repo, ["ls-tree", "-r", "-z", "-l", "--full-tree", candidate_sha])
    prefix = _repo_path(source_prefix) if source_prefix else ""
    blobs: dict[str, CandidateBlob] = {}
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, raw_object_id, raw_size = metadata.split()
            path = raw_path.decode("utf-8", "strict")
            object_id = raw_object_id.decode("ascii", "strict")
            size_bytes = int(raw_size)
        except (UnicodeDecodeError, ValueError) as exc:
            raise CoveragePolicyError(
                "invalid_candidate_tree", "candidate tree has malformed blob metadata"
            ) from exc
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            continue
        git_path = _repo_path(path)
        canonical_path = _repo_path(f"{prefix}/{git_path}") if prefix else git_path
        blobs[canonical_path] = CandidateBlob(object_id, size_bytes)
    return blobs


def _candidate_source_line_counts(
    repo: Path,
    blobs: Mapping[str, CandidateBlob],
    paths: set[str],
) -> dict[str, int]:
    """Batch-read bounded candidate blobs and count their physical source lines."""

    if len(paths) > MAX_CANDIDATE_WITNESS_PATHS:
        raise CoveragePolicyError(
            "candidate_witness_limit",
            "coverage reports cite too many candidate source paths for witness validation",
        )
    selected = {
        path: blobs[path]
        for path in sorted(paths)
        if path in blobs
        and blobs[path].size_bytes <= MAX_CANDIDATE_WITNESS_BLOB_BYTES
    }
    by_object = {blob.object_id: blob.size_bytes for blob in selected.values()}
    if sum(by_object.values()) > MAX_CANDIDATE_WITNESS_TOTAL_BYTES:
        raise CoveragePolicyError(
            "candidate_witness_limit",
            "candidate source blobs exceed the bounded witness-validation budget",
        )
    object_ids = sorted(by_object)
    if not object_ids:
        return {}
    process = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=("\n".join(object_ids) + "\n").encode("ascii"),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode != 0:
        raise CoveragePolicyError(
            "git_error", "git cat-file --batch failed during candidate witness validation"
        )
    cursor = 0
    line_counts_by_object: dict[str, int] = {}
    for object_id in object_ids:
        header_end = process.stdout.find(b"\n", cursor)
        if header_end < 0:
            raise CoveragePolicyError(
                "invalid_candidate_tree", "candidate blob batch output is truncated"
            )
        try:
            returned_id, kind, raw_size = process.stdout[cursor:header_end].split()
            size_bytes = int(raw_size)
        except ValueError as exc:
            raise CoveragePolicyError(
                "invalid_candidate_tree", "candidate blob batch header is malformed"
            ) from exc
        if (
            returned_id.decode("ascii", "strict") != object_id
            or kind != b"blob"
            or size_bytes != by_object[object_id]
        ):
            raise CoveragePolicyError(
                "invalid_candidate_tree", "candidate blob identity or size changed"
            )
        content_start = header_end + 1
        content_end = content_start + size_bytes
        if (
            content_end >= len(process.stdout)
            or process.stdout[content_end : content_end + 1] != b"\n"
        ):
            raise CoveragePolicyError(
                "invalid_candidate_tree", "candidate blob batch payload is truncated"
            )
        content = process.stdout[content_start:content_end]
        if b"\0" not in content:
            line_counts_by_object[object_id] = content.count(b"\n") + int(
                bool(content) and not content.endswith(b"\n")
            )
        cursor = content_end + 1
    if cursor != len(process.stdout):
        raise CoveragePolicyError(
            "invalid_candidate_tree", "candidate blob batch output has trailing data"
        )
    return {
        path: line_counts_by_object[blob.object_id]
        for path, blob in selected.items()
        if blob.object_id in line_counts_by_object
    }


def _strict_producer_contributions(
    repo: Path,
    candidate_sha: str,
    changed: Mapping[str, set[int]],
    maintained: Mapping[str, CoverageTarget],
    report_inputs: Mapping[str, Sequence[BoundCoverageReport]],
    producer_slots: Mapping[str, Path] | None,
    *,
    required_producer_keys: Sequence[str] | None = None,
    source_prefix: str = "",
) -> dict[str, int]:
    """Require one useful native report in every repository-owned slot."""

    expected_slots = set(required_producer_keys or PRODUCER_BY_KEY)
    unknown_slots = expected_slots - set(PRODUCER_BY_KEY)
    if unknown_slots:
        raise CoveragePolicyError(
            "invalid_report_slot",
            f"unknown required producer slot {sorted(unknown_slots)[0]!r}",
        )
    actual_slots = set(producer_slots or {})
    if actual_slots != expected_slots:
        missing = sorted(expected_slots - actual_slots)
        unexpected = sorted(actual_slots - expected_slots)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        requirement = (
            "strict coverage requires the exact ten producer slots"
            if expected_slots == set(PRODUCER_BY_KEY)
            else "strict repository coverage requires its exact producer slots"
        )
        raise CoveragePolicyError(
            "incomplete_report_matrix",
            requirement + ": " + "; ".join(details),
        )

    artifacts_by_path = {
        artifact.path.resolve(): artifact
        for artifacts in report_inputs.values()
        for artifact in artifacts
    }
    candidate_blobs = _candidate_source_blobs(
        repo,
        candidate_sha,
        source_prefix=source_prefix,
    )
    candidate_paths = set(candidate_blobs)
    witness_paths = {
        path
        for artifact in artifacts_by_path.values()
        for path, _line in artifact.coverage.executable
        if path in candidate_blobs and classify_path(path) is not None
    }
    candidate_line_counts = _candidate_source_line_counts(
        repo, candidate_blobs, witness_paths
    )
    contributions: dict[str, int] = {}
    artifacts_by_slot: dict[str, BoundCoverageReport] = {}
    for slot_key in sorted(expected_slots):
        producer = PRODUCER_BY_KEY[slot_key]
        artifact = artifacts_by_path[Path(producer_slots[slot_key]).resolve()]
        artifacts_by_slot[slot_key] = artifact
        useful = {
            observation
            for observation in artifact.coverage.executable
            if any(
                _path_matches_root(observation[0], root)
                for root in producer.required_roots
            )
            and not any(
                _path_matches_root(observation[0], root)
                for root in producer.excluded_roots
            )
            and observation[0] in candidate_paths
            and classify_path(observation[0]) is not None
            and classify_path(observation[0]).key == producer.target_key
            and observation[1] <= candidate_line_counts.get(observation[0], 0)
        }
        if not useful:
            roots = ", ".join(producer.required_roots)
            raise CoveragePolicyError(
                "unproductive_report",
                f"producer slot {slot_key!r} has no executable contribution "
                f"under its required source scope: {roots}",
            )
        if slot_key == "voice_worker" and any(
            not (
                _path_matches_root(path, "backend/voice_agent")
                or path in set(VOICE_WORKER_SOURCE_ALIASES.values())
            )
            for path in artifact.coverage.files
        ):
            raise CoveragePolicyError(
                "producer_scope_mismatch",
                "voice_worker coverage contains maintained sources outside "
                "backend/voice_agent and its audited backend/shared source aliases",
            )
        if slot_key == "ios" and any(
            _path_matches_root(path, "components/AstralProjection/apple-clients/AstralWatch")
            for path in artifact.coverage.files
        ):
            raise CoveragePolicyError(
                "producer_scope_mismatch", "ios coverage contains Watch sources"
            )
        if slot_key == "macos" and any(
            _path_matches_root(path, "components/AstralProjection/apple-clients/AstralWatch")
            for path in artifact.coverage.files
        ):
            raise CoveragePolicyError(
                "producer_scope_mismatch", "macos coverage contains Watch sources"
            )
        if slot_key == "watchos":
            watch_roots = (
                "components/AstralProjection/apple-clients/AstralWatch",
                "components/AstralProjection/apple-clients/AstralCore/Sources",
            )
            if any(
                not any(_path_matches_root(path, root) for root in watch_roots)
                for path in artifact.coverage.files
            ):
                raise CoveragePolicyError(
                    "producer_scope_mismatch",
                    "watchos coverage contains maintained App sources outside its "
                    "Watch and AstralCore source scope",
                )
        contributions[slot_key] = len(useful)

    for changed_path, target in maintained.items():
        applicable = [
            slot_key
            for slot_key in sorted(expected_slots)
            if _producer_applies_to_path(slot_key, changed_path)
        ]
        if target.key == "apple" and _path_matches_root(
            changed_path, "components/AstralProjection/apple-clients/AstralCore/Sources"
        ):
            core_slots = [
                slot_key
                for slot_key in applicable
                if slot_key in {"ios", "macos"}
            ]
            mapped_slots = [
                slot_key
                for slot_key in core_slots
                if changed_path in artifacts_by_slot[slot_key].coverage.files
            ]
            if not mapped_slots:
                raise CoveragePolicyError(
                    "producer_unmapped_changed_file",
                    "neither ios nor macos coverage maps changed AstralCore file "
                    f"{changed_path!r}",
                )
            complete_slots = []
            for slot_key in mapped_slots:
                observed = {
                    line
                    for path, line in artifacts_by_slot[slot_key].coverage.observed
                    if path == changed_path
                }
                if changed[changed_path] <= observed:
                    complete_slots.append(slot_key)
            if not complete_slots:
                missing = sorted(
                    set.intersection(
                        *(
                            changed[changed_path]
                            - {
                                line
                                for path, line in artifacts_by_slot[
                                    slot_key
                                ].coverage.observed
                                if path == changed_path
                            }
                            for slot_key in mapped_slots
                        )
                    )
                )
                example_line = missing[0] if missing else min(changed[changed_path])
                raise CoveragePolicyError(
                    "producer_unmapped_changed_line",
                    "neither ios nor macos coverage completely observes changed "
                    f"AstralCore lines for {changed_path!r}; example line "
                    f"{example_line}",
                )
            continue
        for slot_key in applicable:
            coverage = artifacts_by_slot[slot_key].coverage
            if changed_path not in coverage.files:
                raise CoveragePolicyError(
                    "producer_unmapped_changed_file",
                    f"producer slot {slot_key!r} does not map changed file "
                    f"{changed_path!r}",
                )
            if target.key == "apple":
                observed = {
                    line
                    for path, line in coverage.observed
                    if path == changed_path
                }
                missing = sorted(changed[changed_path] - observed)
                if missing:
                    raise CoveragePolicyError(
                        "producer_unmapped_changed_line",
                        f"producer slot {slot_key!r} does not observe changed Apple "
                        f"line {changed_path!r}:{missing[0]}",
                    )
        if target.key != "apple" and len(applicable) > 1:
            executable = set().union(
                *(
                    {
                        line
                        for path, line in artifacts_by_slot[slot_key].coverage.executable
                        if path == changed_path and line in changed[changed_path]
                    }
                    for slot_key in applicable
                )
            )
            for slot_key in applicable:
                slot_executable = {
                    line
                    for path, line in artifacts_by_slot[slot_key].coverage.executable
                    if path == changed_path
                }
                missing = sorted(executable - slot_executable)
                if missing:
                    raise CoveragePolicyError(
                        "producer_unmapped_changed_line",
                        f"producer slot {slot_key!r} does not map executable changed "
                        f"line {changed_path!r}:{missing[0]}",
                    )
    return contributions


def evaluate_changed_coverage(
    repo: Path,
    selection: RevisionSelection,
    reports: Mapping[str, Sequence[Path]],
    *,
    fail_under: float | int | str = 90,
    producer_slots: Mapping[str, Path] | None = None,
    strict_producers: bool = False,
    repository_profile: str = "monorepo",
    required_producer_keys: Sequence[str] | None = None,
    source_prefix: str = "",
) -> dict[str, Any]:
    """Evaluate changed executable lines and return a deterministic decision.

    Repeated reports are unioned by normalized ``(source path, line)``. Every
    applicable target must supply parseable coverage and map every changed source
    file before per-language and combined thresholds are evaluated.
    """

    threshold = _threshold(fail_under)
    report_inputs = _unique_report_inputs(reports, producer_slots)
    slot_by_path: dict[Path, str] = {}
    if producer_slots is not None:
        supplied_paths = {
            report.path.resolve()
            for artifacts in report_inputs.values()
            for report in artifacts
        }
        for slot_key, path in producer_slots.items():
            try:
                producer = PRODUCER_BY_KEY[slot_key]
            except KeyError as exc:
                raise CoveragePolicyError(
                    "invalid_report_slot", f"unknown producer slot {slot_key!r}"
                ) from exc
            canonical_path = Path(path).resolve()
            target_paths = {
                report.path.resolve()
                for report in report_inputs.get(producer.target_key, [])
            }
            if canonical_path not in target_paths:
                raise CoveragePolicyError(
                    "invalid_report_slot",
                    f"producer slot {slot_key!r} is not bound to {producer.target_key}",
                )
            if canonical_path in slot_by_path:
                raise CoveragePolicyError(
                    "duplicate_report",
                    f"producer slot {slot_key!r} reuses another slot's report",
                )
            slot_by_path[canonical_path] = slot_key
        if set(slot_by_path) != supplied_paths:
            raise CoveragePolicyError(
                "invalid_report_slot",
                "every supplied report must have exactly one producer slot",
            )
    producer_summary: dict[str, dict[str, Any]] = {}
    for artifacts in report_inputs.values():
        for artifact in artifacts:
            slot_key = slot_by_path.get(artifact.path.resolve())
            if slot_key is None:
                continue
            producer_summary[slot_key] = {
                "path": str(artifact.path).replace("\\", "/"),
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "semantic_sha256": artifact.semantic_sha256,
                "semantic_size_bytes": artifact.semantic_size_bytes,
                "native_semantic_sha256": artifact.native_semantic_sha256,
                "native_semantic_size_bytes": artifact.native_semantic_size_bytes,
                "producer_slot": slot_key,
            }
    changed = read_changed_lines(
        repo,
        selection.base_sha,
        selection.candidate_sha,
        source_prefix=source_prefix,
    )
    maintained: dict[str, CoverageTarget] = {}
    for path in sorted(changed):
        target = classify_path(path)
        if target is not None:
            maintained[path] = target
    if not maintained:
        raise CoveragePolicyError(
            "unexpected_empty_executable_diff",
            "immutable comparison contains no maintained executable source paths",
        )
    producer_contributions = (
        _strict_producer_contributions(
            repo,
            selection.candidate_sha,
            changed,
            maintained,
            report_inputs,
            producer_slots,
            required_producer_keys=required_producer_keys,
            source_prefix=source_prefix,
        )
        if strict_producers
        else {}
    )

    target_data: dict[str, CoverageData] = {}
    report_summary: dict[str, Any] = {}
    for target_key in sorted({target.key for target in maintained.values()}):
        target = TARGET_BY_KEY[target_key]
        artifacts = report_inputs.get(target_key, [])
        if not artifacts:
            raise CoveragePolicyError(
                "missing_report",
                f"changed {target_key} code requires --{REPORT_FLAGS[target_key]}",
            )
        merged = CoverageData()
        artifact_identities: list[dict[str, Any]] = []
        for artifact in artifacts:
            slot_key = slot_by_path.get(artifact.path.resolve())
            owned_coverage = (
                _producer_owned_coverage(artifact.coverage, slot_key)
                if slot_key is not None
                else artifact.coverage
            )
            merged.merge(owned_coverage)
            identity = {
                "path": str(artifact.path).replace("\\", "/"),
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "semantic_sha256": artifact.semantic_sha256,
                "semantic_size_bytes": artifact.semantic_size_bytes,
                "native_semantic_sha256": artifact.native_semantic_sha256,
                "native_semantic_size_bytes": artifact.native_semantic_size_bytes,
            }
            if slot_key is not None:
                identity["producer_slot"] = slot_key
            artifact_identities.append(identity)
        changed_files = sorted(
            path for path, mapped in maintained.items() if mapped.key == target_key
        )
        missing_files = sorted(set(changed_files) - merged.files)
        if missing_files:
            raise CoveragePolicyError(
                "unmapped_changed_file",
                f"{target_key} reports do not map changed file {missing_files[0]!r}",
            )
        if target_key == "apple":
            for changed_file in changed_files:
                missing_lines = sorted(
                    changed[changed_file]
                    - {
                        line
                        for observed_path, line in merged.observed
                        if observed_path == changed_file
                    }
                )
                if missing_lines:
                    raise CoveragePolicyError(
                        "unmapped_changed_line",
                        "apple reports do not observe changed physical line "
                        f"{changed_file!r}:{missing_lines[0]}",
                    )
        target_data[target_key] = merged
        report_summary[target_key] = {
            "artifacts": [
                str(report.path).replace("\\", "/") for report in artifacts
            ],
            "artifact_identities": artifact_identities,
            "changed_files": changed_files,
            "mapped_files": sorted(set(changed_files) & merged.files),
        }

    line_records: list[dict[str, Any]] = []
    language_lines: dict[str, set[tuple[str, int]]] = {}
    covered_lines: set[tuple[str, int]] = set()
    for path, target in sorted(maintained.items()):
        parsed = target_data[target.key]
        for line in sorted(changed[path]):
            observation = (path, line)
            if observation not in parsed.executable:
                continue
            language_lines.setdefault(target.language, set()).add(observation)
            covered = observation in parsed.covered
            if covered:
                covered_lines.add(observation)
            line_records.append(
                {
                    "path": path,
                    "line": line,
                    "target": target.key,
                    "language": target.language,
                    "covered": covered,
                }
            )
    if not line_records:
        raise CoveragePolicyError(
            "unexpected_empty_executable_diff",
            "coverage reports map no executable added or modified lines",
        )

    failures: list[dict[str, str]] = []
    language_summary: dict[str, Any] = {}
    for language in sorted(language_lines):
        observations = language_lines[language]
        covered = len(observations & covered_lines)
        executable = len(observations)
        percent = _percentage(covered, executable)
        language_summary[language] = {
            "covered_lines": covered,
            "executable_lines": executable,
            "percent": percent,
        }
        if Decimal(covered * 100) < threshold * executable:
            failures.append(
                {
                    "code": "coverage_below_threshold",
                    "scope": language,
                    "message": f"{language} changed-line coverage {percent:.2f}% is below {threshold}%",
                }
            )
    combined_observations = set().union(*language_lines.values())
    combined_covered = len(combined_observations & covered_lines)
    combined_total = len(combined_observations)
    combined_percent = _percentage(combined_covered, combined_total)
    if Decimal(combined_covered * 100) < threshold * combined_total:
        failures.append(
            {
                "code": "coverage_below_threshold",
                "scope": "combined",
                "message": (
                    f"combined changed-line coverage {combined_percent:.2f}% "
                    f"is below {threshold}%"
                ),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "fail" if failures else "pass",
        "repository_profile": repository_profile,
        "source_prefix": source_prefix,
        "base_sha": selection.base_sha,
        "candidate_sha": selection.candidate_sha,
        "revisions_validated": True,
        "selection": {
            "event_name": selection.event_name,
            "base_source": selection.base_source,
            "candidate_source": selection.candidate_source,
        },
        "fail_under": float(threshold),
        "diff": {
            "changed_paths": sorted(changed),
            "maintained_paths": sorted(maintained),
            "changed_maintained_lines": sum(len(changed[path]) for path in maintained),
            "executable_lines": len(line_records),
        },
        "reports": report_summary,
        "producer_slots": producer_summary,
        "producer_contributions": producer_contributions,
        "languages": language_summary,
        "combined": {
            "covered_lines": combined_covered,
            "executable_lines": combined_total,
            "percent": combined_percent,
        },
        "lines": line_records,
        "failures": failures,
    }


def _write_document(document: Mapping[str, Any], output: str) -> None:
    rendered = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if output == "-":
        sys.stdout.write(rendered)
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def _load_event(path: str | None, event_name: str | None) -> Mapping[str, Any] | None:
    if not path:
        if event_name in {"pull_request", "push"}:
            raise CoveragePolicyError(
                "invalid_event",
                "pull_request and push selection require an event payload",
            )
        return None
    content = _read_report(Path(path))
    payload = _strict_json(content)
    if not isinstance(payload, Mapping):
        raise CoveragePolicyError(
            "invalid_event", "event payload must be a JSON object"
        )
    return payload


class _SingleReportAction(argparse.Action):
    """Reject repeated producer flags instead of silently taking the last path."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} may be supplied exactly once")
        setattr(namespace, self.dest, values)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="Git repository root")
    parser.add_argument(
        "--event-name", choices=("pull_request", "push", "workflow_dispatch", "manual")
    )
    parser.add_argument("--event-path", help="GitHub event JSON path")
    parser.add_argument("--main-ref", default="refs/heads/main")
    parser.add_argument("--base-sha")
    parser.add_argument("--candidate-sha")
    for producer in COVERAGE_PRODUCERS:
        parser.add_argument(
            f"--{producer.flag}",
            dest=producer.key,
            action=_SingleReportAction,
        )
    parser.add_argument(
        "--coverage-mode",
        choices=("strict", "partial"),
        default="strict",
        help="strict requires useful native reports in every profile-owned slot",
    )
    parser.add_argument(
        "--repository-profile",
        choices=tuple(REPOSITORY_PROFILES),
        default="monorepo",
        help=(
            "select the repository-owned producer matrix; projection maps its "
            "child paths into the composed components/AstralProjection namespace"
        ),
    )
    parser.add_argument("--fail-under", default="90")
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the collector CLI, always writing deterministic pass/fail JSON."""

    args = _parser().parse_args(argv)
    event_name = args.event_name or os.environ.get("GITHUB_EVENT_NAME")
    event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")
    selected: RevisionSelection | None = None
    revisions_validated = False
    threshold: Decimal | None = None
    try:
        threshold = _threshold(args.fail_under)
        payload = _load_event(event_path, event_name)
        selected = select_revisions(
            event_name=event_name,
            event_payload=payload,
            base_sha=args.base_sha,
            candidate_sha=args.candidate_sha,
            main_ref=args.main_ref,
        )
        selected = validate_revisions(Path(args.repo), selected)
        revisions_validated = True
        profile = REPOSITORY_PROFILES[args.repository_profile]
        reports = {target.key: [] for target in TARGETS}
        producer_slots: dict[str, Path] = {}
        for producer in COVERAGE_PRODUCERS:
            path_text = getattr(args, producer.key)
            if path_text is None:
                continue
            path = Path(path_text)
            reports[producer.target_key].append(path)
            producer_slots[producer.key] = path
        decision = evaluate_changed_coverage(
            Path(args.repo),
            selected,
            reports,
            fail_under=args.fail_under,
            producer_slots=producer_slots,
            strict_producers=args.coverage_mode == "strict",
            repository_profile=args.repository_profile,
            required_producer_keys=profile.producer_keys,
            source_prefix=profile.source_prefix,
        )
    except CoveragePolicyError as exc:
        decision = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": {"code": exc.code, "message": exc.message},
        }
        if threshold is not None:
            decision["fail_under"] = float(threshold)
        if selected is not None:
            decision.update(
                {
                    "base_sha": selected.base_sha,
                    "candidate_sha": selected.candidate_sha,
                    "revisions_validated": revisions_validated,
                    "selection": {
                        "event_name": selected.event_name,
                        "base_source": selected.base_source,
                        "candidate_source": selected.candidate_source,
                    },
                }
            )
    _write_document(decision, args.output)
    return 0 if decision["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
