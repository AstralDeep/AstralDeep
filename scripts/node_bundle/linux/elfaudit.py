"""Reads the dynamic section of Linux ELF files and audits a staged node pack so every RUNPATH stays inside the pack and every needed library resolves inside it or is glibc or the kernel loader.
natives.py bundles with it and assemble.py runs the audit before sealing a Linux pack.
"""

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"\x7fELF"
PT_LOAD, PT_DYNAMIC = 1, 2
DT_NULL, DT_NEEDED, DT_STRTAB, DT_RPATH, DT_RUNPATH = 0, 1, 5, 15, 29
DT_VERNEED, DT_VERNEEDNUM = 0x6FFFFFFE, 0x6FFFFFFF
SYSTEM_LIBRARIES = frozenset({
    "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1", "libutil.so.1",
    "libresolv.so.2", "libanl.so.1", "ld-linux-aarch64.so.1", "ld-linux-x86-64.so.2",
})
GLIBC_PREFIX = "GLIBC_"


class ElfError(Exception):
    pass


@dataclass
class Dynamic:
    needed: list[str] = field(default_factory=list)
    runpath: list[str] = field(default_factory=list)
    rpath: list[str] = field(default_factory=list)
    glibc: tuple[int, ...] = ()


def is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == MAGIC
    except OSError:
        return False


def read_c_string(data: bytes, offset: int) -> str:
    end = data.index(b"\0", offset)
    return data[offset:end].decode()


def to_offset(loads: list[tuple[int, int, int]], address: int) -> int:
    for vaddr, offset, size in loads:
        if vaddr <= address < vaddr + size:
            return address - vaddr + offset
    raise ElfError("address is outside every loadable segment")


def parse_version(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in name[len(GLIBC_PREFIX):].split("."))


def parse_dynamic(data: bytes) -> Dynamic:
    if data[:4] != MAGIC or data[4] != 2 or data[5] != 1:
        raise ElfError("only little-endian 64-bit ELF files are supported")
    phoff, = struct.unpack_from("<Q", data, 32)
    phentsize, phnum = struct.unpack_from("<HH", data, 54)
    loads, dynamic = [], None
    for index in range(phnum):
        base = phoff + index * phentsize
        kind, _, offset, vaddr, _, filesz, _, _ = struct.unpack_from("<IIQQQQQQ", data, base)
        if kind == PT_LOAD:
            loads.append((vaddr, offset, filesz))
        elif kind == PT_DYNAMIC:
            dynamic = (offset, filesz)
    result = Dynamic()
    if dynamic is None:
        return result
    entries, position = {}, dynamic[0]
    needed_offsets, runpath_offsets, rpath_offsets = [], [], []
    while position + 16 <= dynamic[0] + dynamic[1]:
        tag, value = struct.unpack_from("<qQ", data, position)
        position += 16
        if tag == DT_NULL:
            break
        if tag == DT_NEEDED:
            needed_offsets.append(value)
        elif tag == DT_RUNPATH:
            runpath_offsets.append(value)
        elif tag == DT_RPATH:
            rpath_offsets.append(value)
        else:
            entries[tag] = value
    strtab = to_offset(loads, entries[DT_STRTAB]) if DT_STRTAB in entries else None
    if strtab is None:
        return result
    result.needed = [read_c_string(data, strtab + value) for value in needed_offsets]
    result.runpath = [item for value in runpath_offsets for item in read_c_string(data, strtab + value).split(":") if item]
    result.rpath = [item for value in rpath_offsets for item in read_c_string(data, strtab + value).split(":") if item]
    if DT_VERNEED in entries and DT_VERNEEDNUM in entries:
        result.glibc = glibc_requirements(data, to_offset(loads, entries[DT_VERNEED]), entries[DT_VERNEEDNUM], strtab)
    return result


def glibc_requirements(data: bytes, verneed: int, count: int, strtab: int) -> tuple[int, ...]:
    newest: tuple[int, ...] = ()
    position = verneed
    for _ in range(count):
        _, aux_count, file_offset, aux_offset, next_offset = struct.unpack_from("<HHIII", data, position)
        library = read_c_string(data, strtab + file_offset)
        aux = position + aux_offset
        for _ in range(aux_count):
            _, _, _, name, aux_next = struct.unpack_from("<IHHII", data, aux)
            version = read_c_string(data, strtab + name)
            if version.startswith(GLIBC_PREFIX) and library in SYSTEM_LIBRARIES:
                newest = max(newest, parse_version(version))
            aux += aux_next
        if not next_offset:
            break
        position += next_offset
    return newest


def read_dynamic(path: Path) -> Dynamic:
    try:
        return parse_dynamic(path.read_bytes())
    except (struct.error, ValueError, KeyError) as error:
        raise ElfError(f"{path.name} is not a readable ELF file: {error}") from error


def resolve_runpath(entry: str, origin: Path) -> Path | None:
    if entry == "$ORIGIN" or entry.startswith("$ORIGIN/"):
        return Path(os.path.normpath(origin / entry[len("$ORIGIN/"):])) if entry != "$ORIGIN" else origin
    if entry.startswith("${ORIGIN}"):
        return Path(os.path.normpath(origin / entry[len("${ORIGIN}/"):]))
    return None


def search_directories(path: Path, root: Path, dynamic: Dynamic) -> tuple[list[Path], list[str]]:
    relative = path.relative_to(root).as_posix()
    problems, directories = [], []
    for entry in (*dynamic.runpath, *dynamic.rpath):
        resolved = resolve_runpath(entry, path.parent)
        if resolved is None or not (resolved == root or root in resolved.parents):
            problems.append(f"{relative}: search path {entry} leaves the pack")
        else:
            directories.append(resolved)
    return directories, problems


def resolves(library: str, path: Path, directories: list[Path]) -> bool:
    if library in SYSTEM_LIBRARIES:
        return True
    if library.startswith("$ORIGIN/"):
        return Path(os.path.normpath(path.parent / library[len("$ORIGIN/"):])).is_file()
    return any((directory / library).is_file() for directory in directories)


def unresolved_needs(path: Path, root: Path) -> list[str]:
    dynamic = read_dynamic(path)
    directories, _ = search_directories(path, root, dynamic)
    return [library for library in dynamic.needed if not resolves(library, path, directories)]


def audit_file(path: Path, root: Path) -> tuple[list[str], tuple[int, ...]]:
    relative = path.relative_to(root).as_posix()
    dynamic = read_dynamic(path)
    directories, problems = search_directories(path, root, dynamic)
    problems += [
        f"{relative}: needs {library}, which is neither in the pack nor glibc"
        for library in dynamic.needed if not resolves(library, path, directories)
    ]
    return problems, dynamic.glibc


def elf_files(root: Path) -> list[Path]:
    found = []
    for current, _, names in os.walk(root, followlinks=False):
        for name in sorted(names):
            path = Path(current) / name
            if not path.is_symlink() and is_elf(path):
                found.append(path)
    return sorted(found)


def audit_tree(root: Path, floor: tuple[int, ...]) -> tuple[list[str], tuple[int, ...]]:
    root = root.resolve()
    problems: list[str] = []
    newest: tuple[int, ...] = ()
    for path in elf_files(root):
        found, glibc = audit_file(path, root)
        problems += found
        newest = max(newest, glibc)
        if glibc > floor:
            problems.append(f"{path.relative_to(root).as_posix()}: needs glibc {'.'.join(map(str, glibc))}, newer than the floor")
    return problems, newest
