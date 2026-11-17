"""Checks the Mach-O reader on synthesized headers: deployment target, host and @rpath library paths, fat containers and non-Mach-O files."""

import struct

import pytest
from node_bundle_support import macho

MAGIC_64 = b"\xcf\xfa\xed\xfe"


def pad(text: str) -> bytes:
    raw = text.encode() + b"\0"
    return raw + b"\0" * (-len(raw) % 8)


def dylib_command(command: int, name: str) -> bytes:
    body = pad(name)
    return struct.pack("<IIIIII", command, 24 + len(body), 24, 0, 0, 0) + body


def rpath_command(name: str) -> bytes:
    body = pad(name)
    return struct.pack("<III", macho.LC_RPATH, 12 + len(body), 12) + body


def build_version(platform: int, minos: tuple[int, int, int]) -> bytes:
    packed = (minos[0] << 16) | (minos[1] << 8) | minos[2]
    return struct.pack("<IIIIII", macho.LC_BUILD_VERSION, 24, platform, packed, packed, 0)


def legacy_version(minos: tuple[int, int, int]) -> bytes:
    packed = (minos[0] << 16) | (minos[1] << 8) | minos[2]
    return struct.pack("<IIII", macho.LC_VERSION_MIN_MACOSX, 16, packed, packed)


def thin(commands: list[bytes], file_type: int = 6) -> bytes:
    body = b"".join(commands)
    header = MAGIC_64 + struct.pack("<7I", 0x0100000C, 0, file_type, len(commands), len(body), 0, 0)
    return header + body


def fat(*slices: bytes) -> bytes:
    table, offset = b"", 8 + 20 * len(slices)
    for item in slices:
        table += struct.pack(">IIIII", 0x0100000C, 0, offset, len(item), 14)
        offset += len(item)
    return struct.pack(">II", macho.FAT_MAGIC, len(slices)) + table + b"".join(slices)


def write(tmp_path, name, data):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_a_clean_slice_reads_its_target_and_libraries():
    data = thin([dylib_command(macho.LC_ID_DYLIB, "@loader_path/liba.dylib"),
                 dylib_command(macho.LC_LOAD_DYLIB, "/usr/lib/libSystem.B.dylib"), build_version(1, (14, 0, 0))])
    [parsed] = macho.parse(data)
    assert parsed.minos == (14, 0, 0) and parsed.install_id == "@loader_path/liba.dylib"
    assert parsed.loads == ["/usr/lib/libSystem.B.dylib"]
    assert macho.slice_problems(parsed) == []


def test_a_newer_deployment_target_is_a_problem():
    [parsed] = macho.parse(thin([build_version(1, (15, 0, 0))]))
    assert macho.slice_problems(parsed) == ["minos 15.0.0 is newer than 14.0"]


def test_a_binary_for_another_platform_is_a_problem():
    [parsed] = macho.parse(thin([build_version(2, (12, 0, 0))]))
    assert "not macOS" in macho.slice_problems(parsed)[0]


def test_a_slice_without_any_deployment_target_is_a_problem():
    [parsed] = macho.parse(thin([dylib_command(macho.LC_LOAD_DYLIB, "/usr/lib/libz.1.dylib")]))
    assert macho.slice_problems(parsed) == ["names no deployment target"]


def test_the_legacy_version_command_is_read_as_a_macos_target():
    [parsed] = macho.parse(thin([legacy_version((10, 15, 0))], file_type=2))
    assert parsed.platform == macho.PLATFORM_MACOS and parsed.minos == (10, 15, 0)
    assert macho.slice_problems(parsed) == []


@pytest.mark.parametrize("name", ["/opt/homebrew/lib/libx.dylib", "/usr/local/lib/libx.dylib", "@rpath/libx.dylib"])
def test_host_and_rpath_load_commands_are_problems(name):
    [parsed] = macho.parse(thin([dylib_command(macho.LC_LOAD_WEAK_DYLIB, name), rpath_command(name),
                                 build_version(1, (11, 0, 0))]))
    assert macho.slice_problems(parsed) == [f"load command {name}", f"rpath {name}"]


def test_an_rpath_install_name_is_only_checked_when_asked():
    [parsed] = macho.parse(thin([dylib_command(macho.LC_ID_DYLIB, "@rpath/libx.dylib"), build_version(1, (11, 0, 0))]))
    assert macho.slice_problems(parsed) == ["install name @rpath/libx.dylib"]
    assert macho.slice_problems(parsed, check_install_name=False) == []


def test_every_slice_of_a_fat_file_is_read():
    data = fat(thin([build_version(1, (11, 0, 0))]), thin([build_version(1, (16, 0, 0))]))
    assert [item.minos for item in macho.parse(data)] == [(11, 0, 0), (16, 0, 0)]


def test_files_that_are_not_mach_o_have_no_slices():
    assert macho.parse(b"#!/bin/sh\n") == [] and macho.parse(b"abc") == []
    assert macho.parse(thin([build_version(1, (11, 0, 0))], file_type=1)) == []
    assert macho.parse(struct.pack(">I", 0xFEEDFACF) + b"\0" * 28) == []


def test_a_load_command_shorter_than_its_header_is_refused():
    with pytest.raises(ValueError, match="shorter"):
        macho.parse(thin([struct.pack("<II", 0x99, 4)]))


def test_pack_scan_reports_problems_per_file_and_scopes_install_names(tmp_path):
    ours = thin([dylib_command(macho.LC_ID_DYLIB, "@rpath/libours.dylib"), build_version(1, (14, 0, 0))])
    theirs = thin([dylib_command(macho.LC_ID_DYLIB, "@rpath/libtheirs.dylib"), build_version(1, (14, 0, 0))])
    write(tmp_path, "lib/libours.dylib", ours)
    write(tmp_path, "python/lib/libtheirs.dylib", theirs)
    write(tmp_path, "lib/python3.11/site-packages/x/ext.so", theirs)
    write(tmp_path, "bin/tool", thin([build_version(1, (26, 0, 0))], file_type=2))
    write(tmp_path, "app/readme.txt", b"text")
    (tmp_path / "bin" / "link").symlink_to("tool")
    problems, scanned = macho.pack_problems(tmp_path)
    assert scanned == 4
    assert problems == ["bin/tool: minos 26.0.0 is newer than 14.0",
                        "lib/libours.dylib: install name @rpath/libours.dylib",
                        "lib/python3.11/site-packages/x/ext.so: install name @rpath/libtheirs.dylib"]


def test_pack_scan_reports_an_unreadable_header(tmp_path):
    write(tmp_path, "broken.so", MAGIC_64 + b"\0" * 8)
    problems, scanned = macho.pack_problems(tmp_path)
    assert scanned == 0 and "unreadable Mach-O header" in problems[0]
