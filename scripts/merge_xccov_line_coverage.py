#!/usr/bin/env python3
"""Merge bounded normalized xccov reports into one platform coverage mapping."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

try:
    from scripts.export_xccov_line_coverage import (
        MAX_EXECUTION_COUNT,
        MAX_OUTPUT_BYTES,
        MAX_SOURCE_LINES,
        MAX_TOTAL_OBSERVATIONS,
        PLATFORM_ROOTS,
        ExportError,
        _read_source_line_count,
        _safe_repo_path,
        _tracked_swift_sources,
        _validate_output,
        _validate_path,
        _write_new_output,
    )
except ModuleNotFoundError:  # Also supports protected ``python -I`` execution.
    import importlib.util

    # Only this policy file's sibling exporter is executable. Never search the
    # candidate checkout or accept a caller-selected exporter/module path.
    spec = importlib.util.spec_from_file_location(
        "_protected_xccov_exporter",
        Path(__file__).resolve().with_name("export_xccov_line_coverage.py"),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("xccov policy exporter unavailable")
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    for name in (
        "MAX_EXECUTION_COUNT",
        "MAX_OUTPUT_BYTES",
        "MAX_SOURCE_LINES",
        "MAX_TOTAL_OBSERVATIONS",
        "PLATFORM_ROOTS",
        "ExportError",
        "_read_source_line_count",
        "_safe_repo_path",
        "_tracked_swift_sources",
        "_validate_output",
        "_validate_path",
        "_write_new_output",
    ):
        globals()[name] = getattr(exporter, name)


MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 128 * 1024 * 1024
MAX_FILES = 10_000
REQUIRED_PRODUCERS = ("unit", "ui")
PROFILES = {
    ("macos", "ci"): REQUIRED_PRODUCERS,
    ("ios", "ci"): ("core", "unit", "ui"),
    ("ios", "release"): ("core", "unit", "ui", "staging"),
}
SUPPORTED_PLATFORMS = frozenset({"ios", "macos"})


class MergeError(RuntimeError):
    """Stable fail-closed normalized-coverage merge error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _raise_export_error(exc: ExportError) -> None:
    raise MergeError(exc.code, exc.message) from exc


def _read_stable_input(path: Path, *, repo: Path) -> tuple[Path, bytes]:
    try:
        requested = _validate_path(path, repo=repo, kind="input")
        absolute = requested.resolve(strict=True)
    except ExportError as exc:
        _raise_export_error(exc)
    except OSError as exc:
        raise MergeError("missing_input", "coverage input is unavailable") from exc
    try:
        before = requested.lstat()
    except OSError as exc:
        raise MergeError("missing_input", "coverage input is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise MergeError(
            "unsafe_input", "coverage input must be a regular non-symlink file"
        )
    if before.st_size <= 0 or before.st_size > MAX_INPUT_BYTES:
        raise MergeError("input_too_large", "coverage input size is out of bounds")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(requested, flags)
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_INPUT_BYTES:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_INPUT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise MergeError(
            "input_unavailable", "coverage input could not be read"
        ) from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (
        total > MAX_INPUT_BYTES
        or total != opened.st_size
        or any(getattr(before, field) != getattr(opened, field) for field in stable)
        or any(getattr(opened, field) != getattr(after, field) for field in stable)
    ):
        raise MergeError("input_changed", "coverage input changed while it was read")
    return absolute, b"".join(chunks)


def _strict_document(content: bytes) -> Mapping[str, Any]:
    try:
        text = content.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise MergeError("invalid_json", "coverage input is not UTF-8") from exc

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise MergeError(
                    "duplicate_key", "coverage input contains a duplicate key"
                )
            document[key] = value
        return document

    try:
        document = json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                MergeError(
                    "invalid_json", "coverage input contains a non-finite number"
                )
            ),
        )
    except MergeError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise MergeError("invalid_json", "coverage input is not valid JSON") from exc
    if not isinstance(document, Mapping) or not document or len(document) > MAX_FILES:
        raise MergeError(
            "invalid_document", "coverage input must be a bounded non-empty mapping"
        )
    return document


def _under_roots(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)


def _normalized_observations(
    value: Any,
    *,
    source_lines: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_SOURCE_LINES:
        raise MergeError(
            "invalid_observations", "coverage observations are empty or too large"
        )
    observations: list[dict[str, Any]] = []
    for expected_line, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise MergeError(
                "invalid_observation", "coverage observation must be an object"
            )
        line = item.get("line")
        executable = item.get("isExecutable")
        if isinstance(line, bool) or not isinstance(line, int) or line != expected_line:
            raise MergeError(
                "source_line_mismatch", "coverage lines must be positive and contiguous"
            )
        if line > source_lines or not isinstance(executable, bool):
            raise MergeError(
                "source_line_mismatch", "coverage observations do not match source"
            )
        if executable:
            if set(item) != {"line", "isExecutable", "executionCount"}:
                raise MergeError(
                    "invalid_observation", "executable coverage has the wrong shape"
                )
            count = item.get("executionCount")
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or count > MAX_EXECUTION_COUNT
            ):
                raise MergeError(
                    "invalid_observation", "execution count is out of bounds"
                )
            observations.append(
                {"line": line, "isExecutable": True, "executionCount": count}
            )
        else:
            if set(item) != {"line", "isExecutable"}:
                raise MergeError(
                    "invalid_observation", "non-executable coverage has the wrong shape"
                )
            observations.append({"line": line, "isExecutable": False})
    return observations


def merge_xccov_reports(
    *,
    repo: Path,
    inputs: Mapping[str, Path],
    output: Path,
    platform: str,
    profile: str = "ci",
) -> dict[str, list[dict[str, Any]]]:
    """Validate and add normalized unit/UI observations for one Apple platform."""

    try:
        repo = repo.resolve(strict=True)
    except OSError as exc:
        raise MergeError("missing_repo", "repository root is unavailable") from exc
    if not repo.is_dir():
        raise MergeError("invalid_repo", "repository root is not a directory")
    if platform not in SUPPORTED_PLATFORMS:
        raise MergeError("invalid_platform", "unsupported Apple coverage platform")
    required = PROFILES.get((platform, profile))
    if required is None:
        raise MergeError("invalid_profile", "unsupported platform coverage profile")
    if not isinstance(inputs, Mapping) or set(inputs) != set(required):
        raise MergeError(
            "invalid_producer_set",
            "coverage inputs do not match the mandatory platform/profile producer lanes",
        )
    try:
        tracked = _tracked_swift_sources(repo, platform)
        destination = _validate_output(output, repo=repo)
    except ExportError as exc:
        _raise_export_error(exc)

    documents: list[Mapping[str, Any]] = []
    seen_inputs: set[Path] = set()
    total_input_bytes = 0
    for producer in required:
        input_path = inputs[producer]
        if not isinstance(input_path, Path):
            raise MergeError(
                "invalid_producer_input",
                f"{producer} coverage input must be a filesystem path",
            )
        absolute, content = _read_stable_input(input_path, repo=repo)
        if absolute in seen_inputs:
            raise MergeError("duplicate_input", "coverage input path is duplicated")
        seen_inputs.add(absolute)
        total_input_bytes += len(content)
        if total_input_bytes > MAX_TOTAL_INPUT_BYTES:
            raise MergeError(
                "input_budget_exceeded", "coverage inputs exceed their byte bound"
            )
        document = _strict_document(content)
        if platform == "ios":
            expected_root = PLATFORM_ROOTS[platform][1 if producer == "core" else 0]
            if (
                not any(path.startswith(expected_root + "/") for path in document)
                or producer == "core"
                and any(not path.startswith(expected_root + "/") for path in document)
            ):
                raise MergeError(
                    "producer_scope_mismatch",
                    "coverage lane lacks its required source domain",
                )
        documents.append(document)

    merged: dict[str, list[dict[str, Any]]] = {}
    total_observations = 0
    roots = PLATFORM_ROOTS[platform]
    for document in documents:
        for raw_path, raw_observations in document.items():
            try:
                path = _safe_repo_path(raw_path)
            except ExportError as exc:
                _raise_export_error(exc)
            if (
                path != raw_path
                or not path.endswith(".swift")
                or not _under_roots(path, roots)
                or path not in tracked
            ):
                raise MergeError(
                    "invalid_source_path", "coverage source is not tracked for platform"
                )
            try:
                source_lines = _read_source_line_count(repo, path)
            except ExportError as exc:
                _raise_export_error(exc)
            observations = _normalized_observations(
                raw_observations,
                source_lines=source_lines,
            )
            total_observations += len(observations)
            if total_observations > MAX_TOTAL_OBSERVATIONS:
                raise MergeError(
                    "observation_budget_exceeded",
                    "coverage observations exceed their cumulative bound",
                )
            existing = merged.get(path)
            if existing is None:
                merged[path] = observations
                continue
            if len(existing) != len(observations) or any(
                left["line"] != right["line"]
                or left["isExecutable"] != right["isExecutable"]
                for left, right in zip(existing, observations, strict=True)
            ):
                raise MergeError(
                    "incompatible_executable_mask",
                    "coverage inputs disagree on executable source lines",
                )
            for left, right in zip(existing, observations, strict=True):
                if not left["isExecutable"]:
                    continue
                count = left["executionCount"] + right["executionCount"]
                if count > MAX_EXECUTION_COUNT:
                    raise MergeError(
                        "execution_count_overflow", "execution count overflow"
                    )
                left["executionCount"] = count

    if not merged:
        raise MergeError("empty_union", "coverage inputs contain no platform sources")
    ordered = {path: merged[path] for path in sorted(merged)}
    rendered = (
        json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    if len(rendered) > MAX_OUTPUT_BYTES:
        raise MergeError("output_too_large", "merged coverage exceeds its output bound")
    try:
        _write_new_output(destination, rendered)
    except ExportError as exc:
        _raise_export_error(exc)
    return ordered


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--unit-input", type=Path, action="append", required=True)
    parser.add_argument("--ui-input", type=Path, action="append", required=True)
    parser.add_argument("--core-input", type=Path, action="append", default=[])
    parser.add_argument("--staging-input", type=Path, action="append", default=[])
    parser.add_argument("--profile", choices=("ci", "release"), default="ci")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--platform", choices=sorted(SUPPORTED_PLATFORMS), required=True
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    lanes = {
        key: getattr(args, key + "_input") for key in ("core", "unit", "ui", "staging")
    }
    if any(len(paths) > 1 for paths in lanes.values()):
        print(
            "xccov merge failed [invalid_producer_set]: "
            "exactly one unit, ui, and each other required lane input is permitted",
            file=sys.stderr,
        )
        return 2
    try:
        merge_xccov_reports(
            repo=args.repo,
            inputs={key: paths[0] for key, paths in lanes.items() if paths},
            output=args.output,
            platform=args.platform,
            profile=args.profile,
        )
    except (MergeError, OSError) as exc:
        code = exc.code if isinstance(exc, MergeError) else "filesystem_error"
        message = (
            exc.message
            if isinstance(exc, MergeError)
            else "filesystem operation failed"
        )
        print(f"xccov merge failed [{code}]: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
