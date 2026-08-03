#!/usr/bin/env python3
"""Fail closed when a shipped ELF object declares a libgomp dependency."""

from __future__ import annotations

import argparse
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


ELF_MAGIC = b"\x7fELF"
PT_LOAD = 1
PT_DYNAMIC = 2
DT_NULL = 0
DT_NEEDED = 1
DT_STRTAB = 5
DT_STRSZ = 10


class AuditError(RuntimeError):
    """An ELF object or audit root could not be validated safely."""


@dataclass(frozen=True)
class _Segment:
    kind: int
    offset: int
    virtual_address: int
    file_size: int


def _bounded_end(path: Path, data: bytes, offset: int, size: int, label: str) -> int:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise AuditError(f"malformed ELF {path}: out-of-bounds {label}")
    return offset + size


def _elf_layout(path: Path, data: bytes) -> tuple[str, int, int, int, str, int]:
    if len(data) < 16:
        raise AuditError(f"malformed ELF {path}: truncated identification")
    elf_class = data[4]
    byte_order = data[5]
    if data[6] != 1:
        raise AuditError(f"malformed ELF {path}: unsupported ELF version")
    endian = "<" if byte_order == 1 else ">" if byte_order == 2 else ""
    if not endian:
        raise AuditError(f"malformed ELF {path}: unsupported byte order {byte_order}")
    if elf_class == 1:
        header_format = endian + "16sHHIIIIIHHHHHH"
        program_format = endian + "IIIIIIII"
        dynamic_format = endian + "iI"
    elif elf_class == 2:
        header_format = endian + "16sHHIQQQIHHHHHH"
        program_format = endian + "IIQQQQQQ"
        dynamic_format = endian + "qQ"
    else:
        raise AuditError(f"malformed ELF {path}: unsupported class {elf_class}")

    header_size = struct.calcsize(header_format)
    _bounded_end(path, data, 0, header_size, "ELF header")
    header = struct.unpack_from(header_format, data)
    program_offset = int(header[5])
    program_entry_size = int(header[9])
    program_count = int(header[10])
    minimum_program_size = struct.calcsize(program_format)
    if program_count == 0xFFFF:
        raise AuditError(f"malformed ELF {path}: extended program count unsupported")
    if program_count and program_entry_size < minimum_program_size:
        raise AuditError(f"malformed ELF {path}: short program header entry")
    _bounded_end(
        path,
        data,
        program_offset,
        program_entry_size * program_count,
        "program header table",
    )
    return (
        program_format,
        program_offset,
        program_entry_size,
        program_count,
        dynamic_format,
        elf_class,
    )


def needed_libraries(path: Path) -> tuple[str, ...]:
    """Return the DT_NEEDED values from one ELF file, rejecting malformed data."""

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AuditError(f"cannot read ELF {path}: {exc}") from exc
    if not data.startswith(ELF_MAGIC):
        raise AuditError(f"not an ELF object: {path}")

    (
        program_format,
        program_offset,
        program_entry_size,
        program_count,
        dynamic_format,
        elf_class,
    ) = _elf_layout(path, data)
    load_segments: list[_Segment] = []
    dynamic_segments: list[_Segment] = []
    for index in range(program_count):
        values = struct.unpack_from(
            program_format, data, program_offset + index * program_entry_size
        )
        if elf_class == 1:
            kind, offset, virtual_address, _physical, file_size = values[:5]
        else:
            kind, _flags, offset, virtual_address, _physical, file_size = values[:6]
        segment = _Segment(
            kind=int(kind),
            offset=int(offset),
            virtual_address=int(virtual_address),
            file_size=int(file_size),
        )
        _bounded_end(path, data, segment.offset, segment.file_size, "segment")
        if segment.kind == PT_LOAD:
            load_segments.append(segment)
        elif segment.kind == PT_DYNAMIC:
            dynamic_segments.append(segment)

    dynamic_entry_size = struct.calcsize(dynamic_format)
    needed_offsets: list[int] = []
    string_table_address: int | None = None
    string_table_size: int | None = None
    for segment in dynamic_segments:
        if segment.file_size % dynamic_entry_size:
            raise AuditError(f"malformed ELF {path}: misaligned dynamic table")
        terminated = False
        for offset in range(
            segment.offset,
            segment.offset + segment.file_size,
            dynamic_entry_size,
        ):
            tag, value = struct.unpack_from(dynamic_format, data, offset)
            if tag == DT_NULL:
                terminated = True
                break
            if tag == DT_NEEDED:
                needed_offsets.append(int(value))
            elif tag == DT_STRTAB:
                string_table_address = int(value)
            elif tag == DT_STRSZ:
                string_table_size = int(value)
        if not terminated:
            raise AuditError(f"malformed ELF {path}: unterminated dynamic table")

    if not needed_offsets:
        return ()
    if string_table_address is None or string_table_size is None:
        raise AuditError(f"malformed ELF {path}: missing dynamic string table")
    string_table_offset: int | None = None
    for segment in load_segments:
        relative = string_table_address - segment.virtual_address
        if 0 <= relative <= segment.file_size:
            candidate = segment.offset + relative
            if string_table_size <= segment.file_size - relative:
                string_table_offset = candidate
                break
    if string_table_offset is None:
        raise AuditError(f"malformed ELF {path}: unmapped dynamic string table")
    string_table_end = _bounded_end(
        path, data, string_table_offset, string_table_size, "dynamic string table"
    )

    libraries: list[str] = []
    for needed_offset in needed_offsets:
        if needed_offset < 0 or needed_offset >= string_table_size:
            raise AuditError(f"malformed ELF {path}: invalid DT_NEEDED offset")
        start = string_table_offset + needed_offset
        end = data.find(b"\0", start, string_table_end)
        if end < 0:
            raise AuditError(f"malformed ELF {path}: unterminated DT_NEEDED value")
        try:
            library = data[start:end].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise AuditError(f"malformed ELF {path}: invalid DT_NEEDED text") from exc
        if not library:
            raise AuditError(f"malformed ELF {path}: empty DT_NEEDED value")
        libraries.append(library)
    return tuple(libraries)


def _regular_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        raise AuditError(f"audit root does not exist: {root}")
    if root.is_symlink():
        raise AuditError(f"audit root must not be a symlink: {root}")
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise AuditError(f"audit root is not a regular file or directory: {root}")
    try:
        candidates = sorted(root.rglob("*"))
    except OSError as exc:
        raise AuditError(f"cannot enumerate audit root {root}: {exc}") from exc
    for candidate in candidates:
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
        except OSError as exc:
            raise AuditError(f"cannot inspect audit path {candidate}: {exc}") from exc
        yield candidate


def audit_roots(roots: Sequence[Path]) -> tuple[int, tuple[tuple[Path, str], ...]]:
    """Scan roots and return the ELF count plus any libgomp dependencies."""

    scanned = 0
    offenders: list[tuple[Path, str]] = []
    for root in roots:
        for path in _regular_files(root):
            try:
                with path.open("rb") as stream:
                    magic = stream.read(len(ELF_MAGIC))
            except OSError as exc:
                raise AuditError(f"cannot inspect audit path {path}: {exc}") from exc
            if magic != ELF_MAGIC:
                continue
            scanned += 1
            for library in needed_libraries(path):
                if library == "libgomp.so" or library.startswith("libgomp.so."):
                    offenders.append((path, library))
    if scanned == 0:
        raise AuditError("no ELF objects were scanned; refusing a vacuous pass")
    return scanned, tuple(offenders)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        action="append",
        required=True,
        type=Path,
        help="Regular file or directory tree to scan; may be repeated.",
    )
    args = parser.parse_args(argv)
    try:
        scanned, offenders = audit_roots(args.root)
    except AuditError as exc:
        parser.exit(1, f"native dependency audit failed: {exc}\n")
    if offenders:
        details = "\n".join(f"  {path}: DT_NEEDED {name}" for path, name in offenders)
        parser.exit(1, f"forbidden libgomp dependency detected:\n{details}\n")
    noun = "object" if scanned == 1 else "objects"
    print(f"scanned {scanned} ELF {noun}; no DT_NEEDED libgomp entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
