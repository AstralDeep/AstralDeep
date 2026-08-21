"""Focused safety tests for protected release-artifact extraction."""

from __future__ import annotations

import importlib.util
import os
import stat
import struct
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "extract_release_artifact.py"
if not SCRIPT.is_file():  # repo-root tooling is absent from the product image
    pytest.skip("release tooling is not part of the product image", allow_module_level=True)


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("extract_release_artifact_060", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


extractor = _load_module()


def _archive(path: Path, members: list[tuple[str | zipfile.ZipInfo, bytes]]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members:
            archive.writestr(name, value)
    return path


def test_extracts_regular_members_and_allows_shared_directories(tmp_path: Path) -> None:
    target = tmp_path / "output"
    first = _archive(tmp_path / "first.zip", [("platform/backend.json", b"backend")])
    directory = zipfile.ZipInfo("platform/")
    directory.create_system = 3
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    second = _archive(
        tmp_path / "second.zip",
        [(directory, b""), ("platform/web.json", b"web")],
    )

    assert extractor.extract_artifact(first, target) == ["platform/backend.json"]
    assert extractor.extract_artifact(second, target) == ["platform", "platform/web.json"]
    assert (target / "platform" / "backend.json").read_bytes() == b"backend"
    assert (target / "platform" / "web.json").read_bytes() == b"web"


@pytest.mark.parametrize(
    "member",
    (
        "../escape.json",
        "/absolute.json",
        "./relative.json",
        "nested//empty.json",
        "trailing-dot./report.json",
        "control\x01/report.json",
        "not-nfc-e\u0301/report.json",
    ),
)
def test_rejects_noncanonical_or_escaping_paths(tmp_path: Path, member: str) -> None:
    archive = _archive(tmp_path / "bad.zip", [(member, b"bad")])

    with pytest.raises(extractor.ExtractionError, match="path|suffix|control|NFC"):
        extractor.extract_artifact(archive, tmp_path / "output")
    assert not (tmp_path / "escape.json").exists()


def test_rejects_symlink_and_special_file_members(tmp_path: Path) -> None:
    for filename, file_type in (("link", stat.S_IFLNK), ("socket", stat.S_IFSOCK)):
        member = zipfile.ZipInfo(filename)
        member.create_system = 3
        member.external_attr = (file_type | 0o777) << 16
        archive = _archive(tmp_path / f"{filename}.zip", [(member, b"destination")])

        with pytest.raises(extractor.ExtractionError, match="regular file or directory"):
            extractor.extract_artifact(archive, tmp_path / f"output-{filename}")


def test_rejects_duplicates_case_collisions_and_existing_files(tmp_path: Path) -> None:
    target = tmp_path / "output"
    first = _archive(tmp_path / "first.zip", [("report.json", b"one")])
    duplicate = _archive(
        tmp_path / "duplicate.zip",
        [("nested/Report.json", b"one"), ("nested/report.json", b"two")],
    )
    replacement = _archive(tmp_path / "replacement.zip", [("REPORT.JSON", b"two")])
    extractor.extract_artifact(first, target)

    with pytest.raises(extractor.ExtractionError, match="duplicate|collision"):
        extractor.extract_artifact(duplicate, target)
    with pytest.raises(extractor.ExtractionError, match="collides"):
        extractor.extract_artifact(replacement, target)
    assert (target / "report.json").read_bytes() == b"one"


def test_rejects_file_directory_topology_collisions(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "collision.zip",
        [("parent", b"file"), ("parent/child.json", b"child")],
    )

    with pytest.raises(extractor.ExtractionError, match="descends through a file"):
        extractor.extract_artifact(archive, tmp_path / "output")


def test_rejects_member_count_and_expanded_size_over_limits(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "bounded.zip",
        [("one", b"1234"), ("two", b"5678")],
    )
    member_limit = extractor.ExtractionLimits(
        max_archive_bytes=1024,
        max_member_bytes=8,
        max_total_bytes=16,
        max_members=1,
        max_path_bytes=128,
    )
    size_limit = extractor.ExtractionLimits(
        max_archive_bytes=1024,
        max_member_bytes=3,
        max_total_bytes=16,
        max_members=8,
        max_path_bytes=128,
    )

    with pytest.raises(extractor.ExtractionError, match="member-count"):
        extractor.extract_artifact(archive, tmp_path / "count", limits=member_limit)
    with pytest.raises(extractor.ExtractionError, match="member exceeds"):
        extractor.extract_artifact(archive, tmp_path / "size", limits=size_limit)


def test_rejects_oversized_central_directory_count_before_zipfile_allocation(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "count.zip", [("one", b"one")])
    document = bytearray(archive.read_bytes())
    end = document.rfind(b"PK\x05\x06")
    assert end >= 0
    struct.pack_into("<H", document, end + 8, 5000)
    struct.pack_into("<H", document, end + 10, 5000)
    archive.write_bytes(document)

    with pytest.raises(extractor.ExtractionError, match="member-count"):
        extractor.extract_artifact(archive, tmp_path / "output")


def test_rejects_trailing_bytes_after_the_zip_end_record(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "trailing.zip", [("one", b"one")])
    archive.write_bytes(archive.read_bytes() + b"candidate-controlled-trailer")

    with pytest.raises(extractor.ExtractionError, match="canonical end record"):
        extractor.extract_artifact(archive, tmp_path / "output")


def test_rejects_symlinked_archive_target_and_existing_target_member(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "artifact.zip", [("report.json", b"report")])
    archive_link = tmp_path / "artifact-link.zip"
    archive_link.symlink_to(archive)
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    target_link = tmp_path / "target-link"
    target_link.symlink_to(real_target, target_is_directory=True)

    with pytest.raises(extractor.ExtractionError, match="non-symlink"):
        extractor.extract_artifact(archive_link, tmp_path / "archive-output")
    with pytest.raises(extractor.ExtractionError, match="traverses a symlink"):
        extractor.extract_artifact(archive, target_link)

    (real_target / "report.json").write_bytes(b"existing")
    with pytest.raises(extractor.ExtractionError, match="collides"):
        extractor.extract_artifact(archive, real_target)
    assert (real_target / "report.json").read_bytes() == b"existing"


def test_rejects_invalid_or_incoherent_limits() -> None:
    with pytest.raises(extractor.ExtractionError, match="positive integer"):
        extractor.ExtractionLimits(max_members=0).validate()
    with pytest.raises(extractor.ExtractionError, match="cannot exceed"):
        extractor.ExtractionLimits(max_member_bytes=2, max_total_bytes=1).validate()


def test_member_validation_covers_path_size_flags_and_directory_payloads() -> None:
    limits = extractor.ExtractionLimits()
    empty = zipfile.ZipInfo("")
    with pytest.raises(extractor.ExtractionError, match="empty"):
        extractor._member_path(empty, limits)

    long_path = zipfile.ZipInfo("long-name")
    with pytest.raises(extractor.ExtractionError, match="path exceeds"):
        extractor._member_path(
            long_path,
            extractor.ExtractionLimits(max_path_bytes=3),
        )
    long_segment = zipfile.ZipInfo("x" * 256)
    with pytest.raises(extractor.ExtractionError, match="segment exceeds"):
        extractor._member_path(long_segment, limits)

    # ``zipfile`` normalizes a backslash in ``filename`` when writing an
    # archive on Windows, so drive the validator against its preserved raw
    # member name instead of relying on a platform-dependent archive fixture.
    ambiguous = zipfile.ZipInfo("safe")
    ambiguous.orig_filename = "nested\\windows.json"
    with pytest.raises(extractor.ExtractionError, match="absolute or ambiguous"):
        extractor._member_path(ambiguous, limits)

    encrypted = zipfile.ZipInfo("encrypted")
    encrypted.flag_bits = 1
    with pytest.raises(extractor.ExtractionError, match="encrypted"):
        extractor._member_path(encrypted, limits)
    unsupported = zipfile.ZipInfo("unsupported")
    unsupported.compress_type = zipfile.ZIP_BZIP2
    with pytest.raises(extractor.ExtractionError, match="unsupported compression"):
        extractor._member_path(unsupported, limits)
    negative = zipfile.ZipInfo("negative")
    negative.file_size = -1
    with pytest.raises(extractor.ExtractionError, match="invalid declared size"):
        extractor._member_path(negative, limits)
    directory = zipfile.ZipInfo("directory/")
    directory.create_system = 3
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    directory.file_size = 1
    directory.compress_size = 1
    with pytest.raises(extractor.ExtractionError, match="directory member has a payload"):
        extractor._member_path(directory, limits)


def test_rejects_nested_target_links_special_files_and_file_ancestors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _archive(tmp_path / "artifact.zip", [("safe", b"safe")])
    linked_target = tmp_path / "linked-target"
    linked_target.mkdir()
    (linked_target / "nested-link").symlink_to(tmp_path)
    with pytest.raises(extractor.ExtractionError, match="contains a symlink"):
        extractor.extract_artifact(archive, linked_target)

    special_target = tmp_path / "special-target"
    special_target.mkdir()
    special_file = special_target / "pipe"
    mkfifo = getattr(os, "mkfifo", None)
    if mkfifo is not None:
        mkfifo(special_file)
    else:
        # Windows has no filesystem FIFO constructor. Keep this branch as a
        # real traversal test and substitute only the entry's file-kind probe.
        special_file.write_bytes(b"placeholder")
        path_type = type(special_file)
        original_is_file = path_type.is_file
        original_is_dir = path_type.is_dir
        monkeypatch.setattr(
            path_type,
            "is_file",
            lambda path: False
            if path == special_file
            else original_is_file(path),
        )
        monkeypatch.setattr(
            path_type,
            "is_dir",
            lambda path: False
            if path == special_file
            else original_is_dir(path),
        )
    with pytest.raises(extractor.ExtractionError, match="special file"):
        extractor.extract_artifact(archive, special_target)

    ancestor = tmp_path / "not-a-directory"
    ancestor.write_bytes(b"file")
    with pytest.raises(extractor.ExtractionError, match="ancestor is not a directory"):
        extractor.extract_artifact(archive, ancestor / "output")


def test_rejects_zip64_multidisk_empty_and_out_of_range_directories(
    tmp_path: Path,
) -> None:
    base = _archive(tmp_path / "base.zip", [("one", b"one")]).read_bytes()
    end = base.rfind(b"PK\x05\x06")
    assert end >= 0

    zip64 = bytearray(base)
    struct.pack_into("<H", zip64, end + 8, 0xFFFF)
    struct.pack_into("<H", zip64, end + 10, 0xFFFF)
    zip64_path = tmp_path / "zip64.zip"
    zip64_path.write_bytes(zip64)
    with pytest.raises(extractor.ExtractionError, match="ZIP64"):
        extractor.extract_artifact(zip64_path, tmp_path / "zip64-output")

    multidisk = bytearray(base)
    struct.pack_into("<H", multidisk, end + 4, 1)
    multidisk_path = tmp_path / "multidisk.zip"
    multidisk_path.write_bytes(multidisk)
    with pytest.raises(extractor.ExtractionError, match="multi-disk"):
        extractor.extract_artifact(multidisk_path, tmp_path / "multidisk-output")

    outside = bytearray(base)
    struct.pack_into("<L", outside, end + 16, 0xFFFFFF00)
    outside_path = tmp_path / "outside.zip"
    outside_path.write_bytes(outside)
    with pytest.raises(extractor.ExtractionError, match="outside"):
        extractor.extract_artifact(outside_path, tmp_path / "outside-output")

    empty_path = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_path, "w"):
        pass
    with pytest.raises(extractor.ExtractionError, match="empty"):
        extractor.extract_artifact(empty_path, tmp_path / "empty-output")


def test_rejects_total_expansion_and_member_count_mismatch(tmp_path: Path) -> None:
    archive_path = _archive(
        tmp_path / "total.zip",
        [("one", b"1234"), ("two", b"5678")],
    )
    limits = extractor.ExtractionLimits(
        max_archive_bytes=1024,
        max_member_bytes=4,
        max_total_bytes=7,
        max_members=8,
        max_path_bytes=128,
    )
    with pytest.raises(extractor.ExtractionError, match="total expanded"):
        extractor.extract_artifact(archive_path, tmp_path / "total", limits=limits)

    with zipfile.ZipFile(archive_path) as archive:
        with pytest.raises(extractor.ExtractionError, match="member count differs"):
            extractor._validate_members(
                archive,
                {},
                extractor.ExtractionLimits(),
                expected_members=3,
            )


def test_rejects_reverse_file_directory_collision_and_unsafe_directory_parts(
    tmp_path: Path,
) -> None:
    archive = _archive(
        tmp_path / "reverse.zip",
        [("parent/child", b"child"), ("parent", b"file")],
    )
    with pytest.raises(extractor.ExtractionError, match="replace an existing directory"):
        extractor.extract_artifact(archive, tmp_path / "reverse-output")

    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(extractor.ExtractionError, match="became a symlink"):
        extractor._ensure_directory(root, ("link",))
    (root / "file").write_bytes(b"file")
    with pytest.raises(extractor.ExtractionError, match="not a directory"):
        extractor._ensure_directory(root, ("file",))


def test_rejects_compressed_size_bound_and_removes_partial_crc_failure(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "bounded.zip", [("payload", b"payload")])
    with pytest.raises(extractor.ExtractionError, match="compressed-byte"):
        extractor.extract_artifact(
            archive,
            tmp_path / "bounded-output",
            limits=extractor.ExtractionLimits(max_archive_bytes=1),
        )

    stored = tmp_path / "stored.zip"
    with zipfile.ZipFile(stored, "w", compression=zipfile.ZIP_STORED) as document:
        document.writestr("payload", b"payload")
    damaged = bytearray(stored.read_bytes())
    occurrences = [
        offset
        for offset in range(len(damaged))
        if damaged.startswith(b"payload", offset)
    ]
    assert len(occurrences) == 3
    payload_offset = occurrences[1]
    damaged[payload_offset] ^= 0xFF
    damaged_path = tmp_path / "damaged.zip"
    damaged_path.write_bytes(damaged)
    damaged_target = tmp_path / "damaged-output"
    with pytest.raises(zipfile.BadZipFile, match="CRC"):
        extractor.extract_artifact(damaged_path, damaged_target)
    assert not (damaged_target / "payload").exists()


def test_cli_reports_success_and_refuses_invalid_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = _archive(tmp_path / "cli.zip", [("report", b"report")])
    assert extractor.main(["--archive", str(archive), "--target", str(tmp_path / "out")]) == 0
    assert "safely extracted 1" in capsys.readouterr().out

    with pytest.raises(SystemExit, match="artifact extraction refused"):
        extractor.main(
            ["--archive", str(tmp_path / "missing.zip"), "--target", str(tmp_path / "bad")]
        )


def test_exact_members_reject_missing_extra_and_path_collisions_before_extraction(
    tmp_path: Path,
) -> None:
    expected = ["trusted-release-decision.json"]
    extra_archive = _archive(
        tmp_path / "extra.zip",
        [
            ("trusted-release-decision.json", b"decision"),
            ("sitecustomize.py", b"raise SystemExit('candidate code ran')"),
        ],
    )
    missing_archive = _archive(
        tmp_path / "missing.zip",
        [("different.json", b"not the decision")],
    )

    for archive, target in (
        (extra_archive, tmp_path / "extra-output"),
        (missing_archive, tmp_path / "missing-output"),
    ):
        with pytest.raises(extractor.ExtractionError, match="exact expected"):
            extractor.extract_artifact(
                archive,
                target,
                expected_members=expected,
            )
        assert not any(target.rglob("*"))

    with pytest.raises(extractor.ExtractionError, match="path"):
        extractor.extract_artifact(
            missing_archive,
            tmp_path / "bad-expected-path",
            expected_members=["../trusted-release-decision.json"],
        )
    with pytest.raises(extractor.ExtractionError, match="case-folded collision"):
        extractor.extract_artifact(
            extra_archive,
            tmp_path / "expected-collision",
            expected_members=[
                "trusted-release-decision.json",
                "TRUSTED-RELEASE-DECISION.JSON",
            ],
        )
    with pytest.raises(extractor.ExtractionError, match="iterable of member paths"):
        extractor.extract_artifact(
            missing_archive,
            tmp_path / "bare-string",
            expected_members="trusted-release-decision.json",
        )


def test_exact_members_reject_symlink_member_and_cli_accepts_repeated_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    link = zipfile.ZipInfo("trusted-release-decision.json")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    symlink_archive = _archive(tmp_path / "symlink.zip", [(link, b"target")])
    with pytest.raises(extractor.ExtractionError, match="regular file or directory"):
        extractor.extract_artifact(
            symlink_archive,
            tmp_path / "symlink-output",
            expected_members=["trusted-release-decision.json"],
        )

    archive = _archive(
        tmp_path / "exact.zip",
        [("one.json", b"one"), ("nested/two.json", b"two")],
    )
    target = tmp_path / "exact-output"
    assert (
        extractor.main(
            [
                "--archive",
                str(archive),
                "--target",
                str(target),
                "--expected-member",
                "one.json",
                "--expected-member",
                "nested/two.json",
            ]
        )
        == 0
    )
    assert "safely extracted 2" in capsys.readouterr().out
