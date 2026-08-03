#!/usr/bin/env python3
"""Safely extract one untrusted GitHub Actions artifact archive.

Release artifacts are candidate-controlled ZIP files even when their metadata
is selected through GitHub's trusted API.  This helper applies explicit size
and member-count limits, rejects ambiguous paths and non-regular members, and
never overwrites an existing file.  Multiple independently selected artifacts
may share an existing directory, but their file members may not collide.
"""

from __future__ import annotations

import argparse
import os
import stat
import struct
import unicodedata
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_MEMBER_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_MEMBERS = 4096
DEFAULT_MAX_PATH_BYTES = 1024
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_EOCD_SIGNATURE = b"PK\x05\x06"


class ExtractionError(ValueError):
    """Raised when an archive cannot be extracted without ambiguity or risk."""


@dataclass(frozen=True)
class ExtractionLimits:
    """Resource bounds applied before and during artifact extraction."""

    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_members: int = DEFAULT_MAX_MEMBERS
    max_path_bytes: int = DEFAULT_MAX_PATH_BYTES

    def validate(self) -> None:
        """Reject non-positive limits and incoherent byte bounds."""

        values = {
            "max_archive_bytes": self.max_archive_bytes,
            "max_member_bytes": self.max_member_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_members": self.max_members,
            "max_path_bytes": self.max_path_bytes,
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ExtractionError(f"{name} must be one positive integer")
        if self.max_member_bytes > self.max_total_bytes:
            raise ExtractionError("max_member_bytes cannot exceed max_total_bytes")


@dataclass(frozen=True)
class _Member:
    info: zipfile.ZipInfo
    relative: str
    parts: tuple[str, ...]
    collision_key: tuple[str, ...]
    is_directory: bool


def _collision_part(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _member_path(info: zipfile.ZipInfo, limits: ExtractionLimits) -> _Member:
    raw = getattr(info, "orig_filename", info.filename)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ExtractionError("archive member has an empty or NUL-containing path")
    if raw.startswith("/") or "\\" in raw:
        raise ExtractionError(f"archive member path is absolute or ambiguous: {raw!r}")
    is_directory = info.is_dir()
    if raw.endswith("/") != is_directory:
        raise ExtractionError(f"archive member directory marker is inconsistent: {raw!r}")
    path_text = raw[:-1] if is_directory else raw
    parts = tuple(path_text.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ExtractionError(f"archive member path is not canonical: {raw!r}")
    if any(part.endswith((" ", ".")) for part in parts):
        raise ExtractionError(f"archive member path has an ambiguous suffix: {raw!r}")
    if any(any(ord(character) < 32 for character in part) for part in parts):
        raise ExtractionError(f"archive member path contains a control character: {raw!r}")
    normalized = tuple(unicodedata.normalize("NFC", part) for part in parts)
    if normalized != parts:
        raise ExtractionError(f"archive member path is not NFC-normalized: {raw!r}")
    relative = "/".join(parts)
    if len(relative.encode("utf-8")) > limits.max_path_bytes:
        raise ExtractionError(f"archive member path exceeds the byte limit: {raw!r}")
    if any(len(part.encode("utf-8")) > 255 for part in parts):
        raise ExtractionError(f"archive member segment exceeds the byte limit: {raw!r}")

    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    allowed_type = stat.S_IFDIR if is_directory else stat.S_IFREG
    if file_type not in {0, allowed_type}:
        raise ExtractionError(f"archive member is not a regular file or directory: {raw!r}")
    if info.flag_bits & 0x1:
        raise ExtractionError(f"encrypted archive members are forbidden: {raw!r}")
    if info.compress_type not in _ALLOWED_COMPRESSION:
        raise ExtractionError(f"archive member uses unsupported compression: {raw!r}")
    if info.file_size < 0 or info.compress_size < 0:
        raise ExtractionError(f"archive member has an invalid declared size: {raw!r}")
    if is_directory and (info.file_size != 0 or info.compress_size != 0):
        raise ExtractionError(f"archive directory member has a payload: {raw!r}")
    if info.file_size > limits.max_member_bytes:
        raise ExtractionError(f"archive member exceeds the byte limit: {raw!r}")

    return _Member(
        info=info,
        relative=relative,
        parts=parts,
        collision_key=tuple(_collision_part(part) for part in parts),
        is_directory=is_directory,
    )


def _assert_safe_existing_tree(root: Path) -> dict[tuple[str, ...], tuple[str, bool]]:
    existing: dict[tuple[str, ...], tuple[str, bool]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ExtractionError(f"extraction target contains a symlink: {path}")
        if not (path.is_file() or path.is_dir()):
            raise ExtractionError(f"extraction target contains a special file: {path}")
        relative = path.relative_to(root).as_posix()
        parts = tuple(relative.split("/"))
        key = tuple(_collision_part(part) for part in parts)
        prior = existing.get(key)
        record = (relative, path.is_dir())
        if prior is not None and prior != record:
            raise ExtractionError(f"extraction target has a case-folded collision: {relative!r}")
        existing[key] = record
    return existing


def _assert_safe_target_ancestors(target: Path) -> None:
    current = target.absolute()
    while True:
        if current.is_symlink():
            raise ExtractionError(f"extraction target traverses a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise ExtractionError(f"extraction target ancestor is not a directory: {current}")
        if current.parent == current:
            return
        current = current.parent


def _preflight_directory_count(
    archive_path: Path,
    archive_size: int,
    limits: ExtractionLimits,
) -> int:
    """Read the bounded end records before ``ZipFile`` allocates member objects."""

    tail_size = min(archive_size, 22 + 65535)
    with archive_path.open("rb") as handle:
        handle.seek(archive_size - tail_size)
        tail = handle.read(tail_size)
        search_end = len(tail)
        while True:
            position = tail.rfind(_EOCD_SIGNATURE, 0, search_end)
            if position < 0:
                raise ExtractionError("artifact archive lacks a canonical end record")
            if position + 22 <= len(tail):
                comment_length = struct.unpack_from("<H", tail, position + 20)[0]
                if position + 22 + comment_length == len(tail):
                    break
            search_end = position

        (
            _signature,
            disk_number,
            directory_disk,
            disk_members,
            total_members,
            directory_size,
            directory_offset,
            _comment_length,
        ) = struct.unpack_from("<4s4H2LH", tail, position)
        eocd_offset = archive_size - tail_size + position
        sentinel = (
            disk_members == 0xFFFF
            or total_members == 0xFFFF
            or directory_size == 0xFFFFFFFF
            or directory_offset == 0xFFFFFFFF
        )
        if sentinel:
            # The enforced archive/member/count bounds are all below ZIP64's
            # thresholds, so a ZIP64 end record is unnecessary and only adds
            # a second parser surface at this trust boundary.
            raise ExtractionError("ZIP64 artifact archives are forbidden by the size bounds")

    if disk_number != 0 or directory_disk != 0 or disk_members != total_members:
        raise ExtractionError("multi-disk artifact archives are forbidden")
    if total_members <= 0:
        raise ExtractionError("artifact archive is empty")
    if total_members > limits.max_members:
        raise ExtractionError("artifact archive exceeds the member-count limit")
    if directory_offset + directory_size > eocd_offset:
        raise ExtractionError("artifact central directory lies outside the archive")
    return total_members


def _validate_members(
    archive: zipfile.ZipFile,
    existing: dict[tuple[str, ...], tuple[str, bool]],
    limits: ExtractionLimits,
    expected_members: int,
    exact_members: frozenset[str] | None = None,
) -> list[_Member]:
    infos = archive.infolist()
    if len(infos) != expected_members:
        raise ExtractionError("artifact member count differs from its end record")

    members: list[_Member] = []
    archive_keys: dict[tuple[str, ...], _Member] = {}
    total_bytes = 0
    for info in infos:
        member = _member_path(info, limits)
        if member.collision_key in archive_keys:
            raise ExtractionError(
                f"artifact archive has a duplicate or case-folded collision: {member.relative!r}"
            )
        archive_keys[member.collision_key] = member
        total_bytes += info.file_size
        if total_bytes > limits.max_total_bytes:
            raise ExtractionError("artifact archive exceeds the total expanded-byte limit")
        members.append(member)

    if exact_members is not None:
        regular_members = {member.relative for member in members if not member.is_directory}
        all_members = {member.relative for member in members}
        missing = sorted(exact_members - regular_members)
        extra = sorted(all_members - exact_members)
        if missing or extra:
            raise ExtractionError(
                "artifact members differ from the exact expected regular-file set: "
                f"missing={missing!r}, extra={extra!r}"
            )

    combined: dict[tuple[str, ...], tuple[str, bool]] = dict(existing)
    for member in members:
        prior = combined.get(member.collision_key)
        if prior is not None:
            if not (
                member.is_directory
                and existing.get(member.collision_key) == (member.relative, True)
            ):
                raise ExtractionError(f"artifact member collides with target: {member.relative!r}")
        for depth in range(1, len(member.collision_key)):
            ancestor = combined.get(member.collision_key[:depth])
            if ancestor is not None and not ancestor[1]:
                raise ExtractionError(
                    f"artifact member descends through a file: {member.relative!r}"
                )
        if not member.is_directory and any(
            key[: len(member.collision_key)] == member.collision_key
            for key in combined
            if len(key) > len(member.collision_key)
        ):
            raise ExtractionError(
                f"artifact file would replace an existing directory: {member.relative!r}"
            )
        combined[member.collision_key] = (member.relative, member.is_directory)
    return members


def _expected_member_set(
    values: Iterable[str] | None,
    limits: ExtractionLimits,
) -> frozenset[str] | None:
    """Validate an optional exact set of canonical regular-file member paths."""

    if values is None:
        return None
    if isinstance(values, str):
        raise ExtractionError("expected_members must be an iterable of member paths")
    requested = list(values)
    if not requested:
        raise ExtractionError("expected_members must contain at least one member")
    expected: dict[tuple[str, ...], str] = {}
    for value in requested:
        if not isinstance(value, str) or not value:
            raise ExtractionError("expected member paths must be non-empty strings")
        member = _member_path(zipfile.ZipInfo(value), limits)
        if member.is_directory:
            raise ExtractionError(
                f"expected member must name a regular file, not a directory: {value!r}"
            )
        prior = expected.get(member.collision_key)
        if prior is not None:
            raise ExtractionError(
                "expected members contain a duplicate or case-folded collision: "
                f"{value!r} collides with {prior!r}"
            )
        expected[member.collision_key] = member.relative
    return frozenset(expected.values())


def _ensure_directory(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ExtractionError(f"extraction path became a symlink: {current}")
        if current.exists():
            if not current.is_dir():
                raise ExtractionError(f"extraction path is not a directory: {current}")
        else:
            current.mkdir(mode=0o700)
    return current


def extract_artifact(
    archive_path: Path,
    target: Path,
    *,
    limits: ExtractionLimits | None = None,
    expected_members: Iterable[str] | None = None,
) -> list[str]:
    """Extract an artifact without links, traversal, overwrite, or unbounded output.

    Args:
        archive_path: ZIP archive downloaded by exact immutable artifact ID.
        target: Destination directory. Existing directories may be shared by
            multiple artifacts, but existing file members are never replaced.
        limits: Optional resource limits, primarily useful for focused tests.
        expected_members: Optional exact set of canonical regular-file paths.
            When supplied, every listed member must be present and every
            archive member must be listed; validation completes before any
            archive bytes are extracted.

    Returns:
        Canonical relative member paths written or accepted as directories.

    Raises:
        ExtractionError: If the archive or target violates a safety invariant.
        OSError: If a filesystem operation fails.
        zipfile.BadZipFile: If the input is not a valid ZIP archive.
    """

    effective_limits = limits or ExtractionLimits()
    effective_limits.validate()
    exact_members = _expected_member_set(expected_members, effective_limits)
    archive_path = archive_path.absolute()
    target = target.absolute()
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ExtractionError("artifact archive must be one regular, non-symlink file")
    archive_size = archive_path.stat().st_size
    if archive_size <= 0 or archive_size > effective_limits.max_archive_bytes:
        raise ExtractionError("artifact archive exceeds the compressed-byte limit")
    directory_member_count = _preflight_directory_count(
        archive_path, archive_size, effective_limits
    )

    _assert_safe_target_ancestors(target)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = _assert_safe_existing_tree(target)
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        members = _validate_members(
            archive,
            existing,
            effective_limits,
            directory_member_count,
            exact_members,
        )
        for member in members:
            destination = target.joinpath(*member.parts)
            if member.is_directory:
                _ensure_directory(target, member.parts)
                continue
            _ensure_directory(target, member.parts[:-1])
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(destination, flags, 0o600)
            written = 0
            try:
                with os.fdopen(descriptor, "wb") as output, archive.open(
                    member.info, mode="r"
                ) as source:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > effective_limits.max_member_bytes:
                            raise ExtractionError(
                                f"expanded member exceeds the byte limit: {member.relative!r}"
                            )
                        output.write(chunk)
                if written != member.info.file_size:
                    raise ExtractionError(
                        f"expanded member size differs from its declaration: {member.relative!r}"
                    )
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
    return [member.relative for member in members]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument(
        "--expected-member",
        action="append",
        dest="expected_members",
        metavar="PATH",
        help=(
            "require this exact regular-file member (repeat for every allowed member); "
            "missing and extra members are rejected before extraction"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the bounded extractor CLI."""

    options = _parser().parse_args(argv)
    try:
        members = extract_artifact(
            options.archive,
            options.target,
            expected_members=options.expected_members,
        )
    except (ExtractionError, OSError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"artifact extraction refused: {exc}") from None
    print(f"safely extracted {len(members)} artifact member(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
