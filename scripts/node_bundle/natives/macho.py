"""Reads Mach-O headers in pure Python and reports the deployment target and the library paths each binary names.
build_macos.py runs it over a whole node pack, and the tests run it on synthesized headers.
"""

import mmap
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
THIN_MAGICS = {0xCEFAEDFE: 28, 0xCFFAEDFE: 32}
FILE_TYPES = {2: "executable", 6: "dylib", 8: "bundle"}
LC_REQ_DYLD = 0x80000000
LC_LOAD_DYLIB = 0xC
LC_ID_DYLIB = 0xD
LC_LOAD_WEAK_DYLIB = 0x18 | LC_REQ_DYLD
LC_REEXPORT_DYLIB = 0x1F | LC_REQ_DYLD
LC_LAZY_LOAD_DYLIB = 0x20
LC_LOAD_UPWARD_DYLIB = 0x23 | LC_REQ_DYLD
LC_RPATH = 0x1C | LC_REQ_DYLD
LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32
LOAD_COMMANDS = {LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB, LC_REEXPORT_DYLIB, LC_LAZY_LOAD_DYLIB, LC_LOAD_UPWARD_DYLIB}
PLATFORM_MACOS = 1
FORBIDDEN_PREFIXES = ("/opt/homebrew", "/usr/local", "@rpath")
MAX_MINOS = (14, 0, 0)


@dataclass
class Slice:
    file_type: str
    platform: int | None = None
    minos: tuple[int, int, int] | None = None
    install_id: str | None = None
    loads: list[str] = field(default_factory=list)
    rpaths: list[str] = field(default_factory=list)


def unpack_version(value: int) -> tuple[int, int, int]:
    return (value >> 16, (value >> 8) & 0xFF, value & 0xFF)


def c_string(data: bytes | mmap.mmap, offset: int, end: int) -> str:
    return bytes(data[offset:end]).split(b"\0", 1)[0].decode("utf-8", "replace")


def parse_thin(data: bytes | mmap.mmap, base: int) -> Slice | None:
    (magic,) = struct.unpack_from(">I", data, base)
    header = THIN_MAGICS.get(magic)
    if header is None:
        return None
    _, _, file_type, count, _, _ = struct.unpack_from("<6I", data, base + 4)
    kind = FILE_TYPES.get(file_type)
    if kind is None:
        return None
    parsed = Slice(kind)
    cursor = base + header
    for _ in range(count):
        command, length = struct.unpack_from("<II", data, cursor)
        if length < 8:
            raise ValueError("a load command is shorter than its header")
        end = cursor + length
        if command in LOAD_COMMANDS or command == LC_ID_DYLIB:
            (name_offset,) = struct.unpack_from("<I", data, cursor + 8)
            name = c_string(data, cursor + name_offset, end)
            if command == LC_ID_DYLIB:
                parsed.install_id = name
            else:
                parsed.loads.append(name)
        elif command == LC_RPATH:
            (name_offset,) = struct.unpack_from("<I", data, cursor + 8)
            parsed.rpaths.append(c_string(data, cursor + name_offset, end))
        elif command == LC_BUILD_VERSION:
            parsed.platform, minos = struct.unpack_from("<II", data, cursor + 8)
            parsed.minos = unpack_version(minos)
        elif command == LC_VERSION_MIN_MACOSX and parsed.minos is None:
            parsed.platform = PLATFORM_MACOS
            parsed.minos = unpack_version(struct.unpack_from("<I", data, cursor + 8)[0])
        cursor = end
    return parsed


def parse(data: bytes | mmap.mmap) -> list[Slice]:
    if len(data) < 8:
        return []
    (magic,) = struct.unpack_from(">I", data, 0)
    if magic in (FAT_MAGIC, FAT_MAGIC_64):
        wide = magic == FAT_MAGIC_64
        (count,) = struct.unpack_from(">I", data, 4)
        slices = []
        for index in range(count):
            entry = 8 + index * (32 if wide else 20)
            offset = struct.unpack_from(">Q" if wide else ">I", data, entry + (8 if wide else 8))[0]
            found = parse_thin(data, offset)
            if found is not None:
                slices.append(found)
        return slices
    found = parse_thin(data, 0) if magic in THIN_MAGICS else None
    return [found] if found is not None else []


def read_slices(path: Path) -> list[Slice]:
    with path.open("rb") as handle:
        head = handle.read(8)
        if len(head) < 8 or struct.unpack(">I", head[:4])[0] not in {*THIN_MAGICS, FAT_MAGIC, FAT_MAGIC_64}:
            return []
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return parse(data)


def slice_problems(parsed: Slice, check_install_name: bool = True) -> list[str]:
    problems = []
    if parsed.minos is None:
        problems.append("names no deployment target")
    elif parsed.platform != PLATFORM_MACOS:
        problems.append(f"targets platform {parsed.platform}, not macOS")
    elif parsed.minos > MAX_MINOS:
        problems.append("minos " + ".".join(map(str, parsed.minos)) + " is newer than 14.0")
    ids = [parsed.install_id] if parsed.install_id and check_install_name else []
    for label, names in (("install name", ids),
                         ("load command", parsed.loads), ("rpath", parsed.rpaths)):
        problems += [f"{label} {name}" for name in names if name.startswith(FORBIDDEN_PREFIXES)]
    return problems


def pack_problems(root: Path, own_roots: tuple[str, ...] = ("bin", "lib")) -> tuple[list[str], int]:
    problems, scanned = [], 0
    for current, directories, names in os.walk(root, followlinks=False):
        directories.sort()
        for name in sorted(names):
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                slices = read_slices(path)
            except (struct.error, ValueError) as error:
                problems.append(f"{path.relative_to(root).as_posix()}: unreadable Mach-O header ({error})")
                continue
            scanned += bool(slices)
            relative = path.relative_to(root).as_posix()
            own = relative.split("/", 1)[0] in own_roots
            for parsed in slices:
                problems += [f"{relative}: {reason}" for reason in slice_problems(parsed, own)]
    return problems, scanned
