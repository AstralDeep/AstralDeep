#!/usr/bin/env python3
"""Export bounded, repository-normalized Swift line coverage from an xcresult.

``xccov view --archive --json`` is not a stable producer contract across Xcode
versions.  This exporter uses the documented per-file archive interface, validates
every observation, and writes the exact mapping consumed by
``check_changed_coverage.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


MAX_FILE_LIST_BYTES = 4 * 1024 * 1024
MAX_FILE_JSON_BYTES = 16 * 1024 * 1024
MAX_TOTAL_XCCOV_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILES = 10_000
MAX_PATH_BYTES = 16 * 1024
MAX_SOURCE_LINES = 1_000_000
MAX_TOTAL_OBSERVATIONS = 1_000_000
MAX_SUBRANGES_PER_LINE = 10_000
MAX_EXECUTION_COUNT = (1 << 63) - 1
COMMAND_TIMEOUT_SECONDS = 120
COMMAND_STOP_TIMEOUT_SECONDS = 2
EXPORT_TIMEOUT_SECONDS = 15 * 60

APPLE_ROOT = "apple-clients/"
PLATFORM_ROOTS = {
    "ios": (
        "apple-clients/AstralApp/AstralApp",
        "apple-clients/AstralCore/Sources",
    ),
    "macos": (
        "apple-clients/AstralApp/AstralApp",
        "apple-clients/AstralCore/Sources",
    ),
    "watchos": (
        "apple-clients/AstralWatch",
        "apple-clients/AstralCore/Sources",
    ),
}


class ExportError(RuntimeError):
    """Stable fail-closed producer error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _bounded_command(
    command: Sequence[str],
    *,
    cwd: Path,
    max_stdout_bytes: int,
    export_deadline: float | None = None,
) -> bytes:
    """Run one fixed-argument command and reject failures or oversized output."""

    if export_deadline is not None and time.monotonic() >= export_deadline:
        raise ExportError(
            "export_timeout", "coverage export exceeded its overall deadline"
        )
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ExportError("producer_unavailable", "coverage producer did not run") from exc
    assert process.stdout is not None and process.stderr is not None
    stream_limits = {
        process.stdout: max_stdout_bytes,
        process.stderr: MAX_FILE_LIST_BYTES,
    }
    chunks: dict[Any, list[bytes]] = {stream: [] for stream in stream_limits}
    totals = dict.fromkeys(stream_limits, 0)
    selector = selectors.DefaultSelector()
    for stream in stream_limits:
        selector.register(stream, selectors.EVENT_READ)
    command_deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    deadline = (
        min(command_deadline, export_deadline)
        if export_deadline is not None
        else command_deadline
    )
    timeout_code = (
        "export_timeout"
        if export_deadline is not None and export_deadline <= command_deadline
        else "producer_timeout"
    )
    timeout_message = (
        "coverage export exceeded its overall deadline"
        if timeout_code == "export_timeout"
        else "coverage producer timed out"
    )

    def stop() -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=COMMAND_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop()
                raise ExportError(timeout_code, timeout_message)
            for key, _events in selector.select(remaining):
                stream = key.fileobj
                limit = stream_limits[stream]
                chunk = os.read(stream.fileno(), min(64 * 1024, limit - totals[stream] + 1))
                if not chunk:
                    selector.unregister(stream)
                    continue
                if totals[stream] + len(chunk) > limit:
                    stop()
                    raise ExportError(
                        "producer_output_too_large", "coverage producer exceeded its byte bound"
                    )
                chunks[stream].append(chunk)
                totals[stream] += len(chunk)
        remaining = max(0.0, deadline - time.monotonic())
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            stop()
            raise ExportError(timeout_code, timeout_message) from exc
    except OSError as exc:
        stop()
        raise ExportError("producer_unavailable", "coverage producer could not be read") from exc
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    if returncode != 0:
        raise ExportError("producer_failed", "coverage producer returned a failure")
    stdout = b"".join(chunks[process.stdout])
    if not stdout:
        raise ExportError("producer_output_too_large", "coverage producer stdout is empty")
    return stdout


def _strict_json(content: bytes) -> Any:
    """Decode strict UTF-8 JSON while rejecting duplicate object keys/constants."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            content.decode("utf-8", "strict"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExportError("invalid_xccov_json", "xccov returned malformed JSON") from exc


def _safe_repo_path(value: str) -> str:
    """Return one normalized, non-control-character repository path."""

    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or len(value.encode("utf-8")) > MAX_PATH_BYTES
    ):
        raise ExportError("invalid_source_path", "coverage source path is invalid")
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts:
        raise ExportError("invalid_source_path", "coverage source path is unsafe")
    return pure.as_posix()


def _path_under_roots(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)


def _tracked_swift_sources(
    repo: Path, platform: str, *, export_deadline: float | None = None
) -> set[str]:
    roots = PLATFORM_ROOTS[platform]
    output = _bounded_command(
        ["git", "-C", str(repo), "ls-files", "-z", "--", *roots],
        cwd=repo,
        max_stdout_bytes=MAX_FILE_LIST_BYTES,
        export_deadline=export_deadline,
    )
    if not output.endswith(b"\x00"):
        raise ExportError("invalid_git_inventory", "tracked source inventory is truncated")
    try:
        raw_paths = [item.decode("utf-8", "strict") for item in output.split(b"\x00") if item]
    except UnicodeDecodeError as exc:
        raise ExportError("invalid_git_inventory", "tracked source path is not UTF-8") from exc
    sources: set[str] = set()
    for raw_path in raw_paths:
        path = _safe_repo_path(raw_path)
        if path.endswith(".swift") and _path_under_roots(path, roots):
            sources.add(path)
    if not sources:
        raise ExportError("empty_source_inventory", "platform has no tracked Swift sources")
    return sources


def _archive_file_list(
    repo: Path, xcresult: Path, *, export_deadline: float | None = None
) -> list[str]:
    output = _bounded_command(
        ["xcrun", "xccov", "view", "--archive", "--file-list", str(xcresult)],
        cwd=repo,
        max_stdout_bytes=MAX_FILE_LIST_BYTES,
        export_deadline=export_deadline,
    )
    if not output.endswith(b"\n"):
        raise ExportError("invalid_file_list", "xccov file list is truncated")
    try:
        paths = output[:-1].decode("utf-8", "strict").split("\n")
    except UnicodeDecodeError as exc:
        raise ExportError("invalid_file_list", "xccov file list is not UTF-8") from exc
    if not paths or len(paths) > MAX_ARCHIVE_FILES or any(not path for path in paths):
        raise ExportError("invalid_file_list", "xccov file list is empty or exceeds its bound")
    seen: set[str] = set()
    for path in paths:
        if (
            path in seen
            or not path.startswith("/")
            or not path.endswith(".swift")
            or "\\" in path
            or "\r" in path
            or "\x00" in path
            or len(path.encode("utf-8")) > MAX_PATH_BYTES
        ):
            raise ExportError("invalid_file_list", "xccov file list has an invalid duplicate path")
        seen.add(path)
    return paths


def _normalize_archive_path(raw_path: str) -> str | None:
    """Map from the first Apple repository anchor, never a later lookalike."""

    value = raw_path.replace("\\", "/")
    candidates: list[int] = []
    if value.startswith(APPLE_ROOT):
        candidates.append(0)
    marker = f"/{APPLE_ROOT}"
    start = 0
    while (index := value.find(marker, start)) >= 0:
        candidates.append(index + 1)
        start = index + 1
    if not candidates:
        return None
    try:
        return _safe_repo_path(value[min(candidates) :])
    except ExportError:
        return None


def _canonical_archive_repo_root(value: str | Path) -> str:
    """Validate a historical producer checkout root without resolving it locally."""

    raw = os.fspath(value)
    if (
        not raw
        or not raw.startswith("/")
        or raw.endswith("/")
        or "\\" in raw
        or "\x00" in raw
        or "\n" in raw
        or "\r" in raw
        or len(raw.encode("utf-8")) > MAX_PATH_BYTES
    ):
        raise ExportError(
            "invalid_archive_repo_root", "archive repository root is not canonical"
        )
    pure = PurePosixPath(raw)
    if pure.as_posix() != raw or any(part in {"", ".", ".."} for part in pure.parts):
        raise ExportError(
            "invalid_archive_repo_root", "archive repository root is not canonical"
        )
    return raw


def _read_source_line_count(
    repo: Path, relative_path: str, *, export_deadline: float | None = None
) -> int:
    """Count physical lines through one stable, regular, non-symlink descriptor."""

    path = _validate_path(Path(relative_path), repo=repo, kind="source")
    try:
        before = path.lstat()
    except OSError as exc:
        raise ExportError("source_unavailable", "tracked source is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ExportError("unsafe_source", "tracked source must be a regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_FILE_JSON_BYTES:
            if export_deadline is not None and time.monotonic() >= export_deadline:
                raise ExportError(
                    "export_timeout", "coverage export exceeded its overall deadline"
                )
            chunk = os.read(
                descriptor, min(1024 * 1024, MAX_FILE_JSON_BYTES + 1 - total)
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ExportError("source_unavailable", "tracked source could not be read") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (
        total > MAX_FILE_JSON_BYTES
        or total != opened.st_size
        or any(getattr(opened, field) != getattr(after, field) for field in stable)
        or before.st_dev != opened.st_dev
        or before.st_ino != opened.st_ino
    ):
        raise ExportError("source_changed", "tracked source changed or exceeded its bound")
    content = b"".join(chunks)
    lines = content.count(b"\n") + int(bool(content) and not content.endswith(b"\n"))
    if lines <= 0 or lines > MAX_SOURCE_LINES:
        raise ExportError("invalid_source_size", "tracked source line count is out of bounds")
    return lines


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExportError("invalid_observation", f"invalid integer for {label}")
    return value


def _validate_subranges(value: Any) -> None:
    if not isinstance(value, list) or len(value) > MAX_SUBRANGES_PER_LINE:
        raise ExportError("invalid_observation", "xccov subranges are invalid")
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "column",
            "executionCount",
            "length",
        }:
            raise ExportError("invalid_observation", "xccov subrange has an invalid shape")
        for key in ("column", "executionCount", "length"):
            number = _integer(item.get(key), label=f"subrange {key}")
            if number > MAX_EXECUTION_COUNT:
                raise ExportError("invalid_observation", "xccov subrange exceeds its bound")


def _normalize_observations(
    content: bytes,
    *,
    queried_path: str,
    maximum_lines: int,
    maximum_observations: int = MAX_TOTAL_OBSERVATIONS,
) -> list[dict[str, Any]]:
    document = _strict_json(content)
    if not isinstance(document, Mapping) or list(document) != [queried_path]:
        raise ExportError("invalid_observation", "per-file xccov result has the wrong source key")
    values = document[queried_path]
    if not isinstance(values, list) or not values or len(values) > MAX_SOURCE_LINES:
        raise ExportError("invalid_observation", "per-file xccov observations are invalid")
    if len(values) > maximum_observations:
        raise ExportError(
            "observation_budget_exceeded",
            "normalized coverage exceeds its cumulative observation bound",
        )
    normalized: list[dict[str, Any]] = []
    seen_lines: set[int] = set()
    for item in values:
        if not isinstance(item, Mapping) or not set(item) <= {
            "line",
            "isExecutable",
            "executionCount",
            "subranges",
        }:
            raise ExportError("invalid_observation", "xccov line observation has an invalid shape")
        line = _integer(item.get("line"), label="line")
        executable = item.get("isExecutable")
        if line <= 0 or line in seen_lines or not isinstance(executable, bool):
            raise ExportError("invalid_observation", "xccov lines must be positive and unique")
        seen_lines.add(line)
        observation: dict[str, Any] = {"line": line, "isExecutable": executable}
        if "subranges" in item:
            _validate_subranges(item["subranges"])
        if executable:
            count = _integer(item.get("executionCount"), label="executionCount")
            if count > MAX_EXECUTION_COUNT:
                raise ExportError("invalid_observation", "execution count exceeds its bound")
            observation["executionCount"] = count
        elif "executionCount" in item:
            raise ExportError("invalid_observation", "non-executable line has an execution count")
        normalized.append(observation)
    normalized.sort(key=lambda item: item["line"])
    maximum_observed = normalized[-1]["line"]
    if maximum_observed > maximum_lines or [item["line"] for item in normalized] != list(
        range(1, maximum_observed + 1)
    ):
        raise ExportError(
            "source_line_mismatch",
            "xccov observations are non-contiguous or exceed the current source",
        )
    return normalized


def _validate_path(path: Path, *, repo: Path, kind: str) -> Path:
    """Resolve an existing input beneath the repo without accepting symlink components."""

    absolute = path if path.is_absolute() else repo / path
    if ".." in absolute.parts:
        raise ExportError(f"unsafe_{kind}", f"{kind} path cannot traverse parents")
    try:
        relative = absolute.relative_to(repo)
    except ValueError as exc:
        raise ExportError(f"unsafe_{kind}", f"{kind} must be inside the repository") from exc
    cursor = repo
    for part in relative.parts:
        cursor /= part
        try:
            if stat.S_ISLNK(cursor.lstat().st_mode):
                raise ExportError(f"unsafe_{kind}", f"{kind} path cannot contain symlinks")
        except OSError as exc:
            raise ExportError(f"missing_{kind}", f"{kind} path is unavailable") from exc
    return absolute


def _validate_output(output: Path, *, repo: Path) -> Path:
    absolute = output if output.is_absolute() else repo / output
    if ".." in absolute.parts:
        raise ExportError("unsafe_output", "output path cannot traverse parents")
    try:
        relative_parent = absolute.parent.relative_to(repo)
    except ValueError as exc:
        raise ExportError("unsafe_output", "output must be inside the repository") from exc
    cursor = repo
    for part in relative_parent.parts:
        cursor /= part
        try:
            mode = cursor.lstat().st_mode
        except OSError as exc:
            raise ExportError("missing_output_parent", "output parent is unavailable") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ExportError("unsafe_output", "output parent must not contain symlinks")
    if absolute.exists() or absolute.is_symlink():
        raise ExportError("output_exists", "output must not already exist")
    return absolute


def _write_new_output(output: Path, content: bytes) -> None:
    if not content or len(content) > MAX_OUTPUT_BYTES:
        raise ExportError("output_too_large", "normalized coverage exceeds its bound")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(output, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        if created:
            try:
                output.unlink()
            except OSError:
                pass
        raise ExportError("output_write_failed", "normalized coverage could not be written") from exc


def export_xccov(
    *,
    repo: Path,
    xcresult: Path,
    output: Path,
    platform: str,
    archive_repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Export one platform-filtered normalized xccov mapping."""

    repo = repo.resolve(strict=True)
    if platform not in PLATFORM_ROOTS:
        raise ExportError("invalid_platform", "unsupported Apple coverage platform")
    bundle = _validate_path(xcresult, repo=repo, kind="xcresult")
    if not bundle.is_dir() or not bundle.name.endswith(".xcresult"):
        raise ExportError("invalid_xcresult", "xcresult must be a directory bundle")
    destination = _validate_output(output, repo=repo)
    deadline = time.monotonic() + EXPORT_TIMEOUT_SECONDS
    archive_root = _canonical_archive_repo_root(
        repo.as_posix() if archive_repo_root is None else archive_repo_root
    )
    tracked = _tracked_swift_sources(repo, platform, export_deadline=deadline)
    archive_paths = _archive_file_list(repo, bundle, export_deadline=deadline)
    selected: dict[str, str] = {}
    for raw_path in archive_paths:
        archive_prefix = f"{archive_root}/"
        if raw_path.startswith(archive_prefix):
            try:
                normalized = _safe_repo_path(raw_path.removeprefix(archive_prefix))
            except ExportError:
                normalized = None
        else:
            normalized = _normalize_archive_path(raw_path)
        if normalized is None or normalized not in tracked:
            continue
        if raw_path != f"{archive_root}/{normalized}":
            raise ExportError(
                "unsafe_archive_source",
                "xccov source does not belong to the candidate checkout",
            )
        selected[normalized] = raw_path
    if not selected:
        raise ExportError("empty_platform_coverage", "xccov archive has no platform Swift sources")

    report: dict[str, Any] = {}
    total_xccov_bytes = 0
    total_observations = 0
    rendered_size = len(b"{}\n")
    for relative_path in sorted(selected):
        if time.monotonic() >= deadline:
            raise ExportError(
                "export_timeout", "coverage export exceeded its overall deadline"
            )
        if total_xccov_bytes >= MAX_TOTAL_XCCOV_BYTES:
            raise ExportError(
                "input_budget_exceeded",
                "xccov output exceeds its cumulative byte bound",
            )
        if total_observations >= MAX_TOTAL_OBSERVATIONS:
            raise ExportError(
                "observation_budget_exceeded",
                "normalized coverage exceeds its cumulative observation bound",
            )
        raw_path = selected[relative_path]
        source_lines = _read_source_line_count(
            repo, relative_path, export_deadline=deadline
        )
        remaining_input = MAX_TOTAL_XCCOV_BYTES - total_xccov_bytes
        content = _bounded_command(
            [
                "xcrun",
                "xccov",
                "view",
                "--archive",
                "--file",
                raw_path,
                "--json",
                str(bundle),
            ],
            cwd=repo,
            max_stdout_bytes=min(MAX_FILE_JSON_BYTES, remaining_input),
            export_deadline=deadline,
        )
        total_xccov_bytes += len(content)
        observations = _normalize_observations(
            content,
            queried_path=raw_path,
            maximum_lines=source_lines,
            maximum_observations=MAX_TOTAL_OBSERVATIONS - total_observations,
        )
        total_observations += len(observations)
        key_bytes = json.dumps(
            relative_path, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        value_bytes = json.dumps(
            observations, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        rendered_size += len(key_bytes) + 1 + len(value_bytes) + int(bool(report))
        if rendered_size > MAX_OUTPUT_BYTES:
            raise ExportError("output_too_large", "normalized coverage exceeds its bound")
        report[relative_path] = observations
    rendered = (
        json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    if len(rendered) != rendered_size:
        raise ExportError("output_size_mismatch", "normalized coverage size is unstable")
    _write_new_output(destination, rendered)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--xcresult", type=Path, required=True)
    parser.add_argument(
        "--archive-repo-root",
        help="absolute producer checkout root recorded in the xcresult (defaults to --repo)",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform", choices=sorted(PLATFORM_ROOTS), required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fail-closed xccov export CLI."""

    args = _parser().parse_args(argv)
    try:
        export_xccov(
            repo=args.repo,
            xcresult=args.xcresult,
            output=args.output,
            platform=args.platform,
            archive_repo_root=args.archive_repo_root,
        )
    except (ExportError, OSError) as exc:
        code = exc.code if isinstance(exc, ExportError) else "filesystem_error"
        message = exc.message if isinstance(exc, ExportError) else "filesystem operation failed"
        print(f"xccov export failed [{code}]: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
