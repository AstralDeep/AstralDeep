"""Checks the Linux node pack recipe: the ELF audit, native bundling, the Kernel binding match, the pins and Dockerfile, and a real built pack and its clean-image relocation when one is available."""

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from node_bundle_support import BUNDLE
from node_bundle_support import assemble as ap
from node_bundle_support import astral_node_launch as launch
from node_bundle_support import relocation_check

LINUX = BUNDLE / "linux"
sys.path.insert(0, str(LINUX))

import build_linux as bl  # noqa: E402
import elfaudit  # noqa: E402
import debian_natives  # noqa: E402

REPO = BUNDLE.parents[1]
BINDING = json.loads((REPO / "config" / "astral-kernel-binding.json").read_text())
PACK_DIR = Path(os.environ.get("NODE_PACK_LINUX_DIR") or (Path(os.environ["SCRATCH"]) / "pack-linux" if "SCRATCH" in os.environ else "/nonexistent"))
PACK_REASON = "no built Linux pack: set NODE_PACK_LINUX_DIR or SCRATCH to a folder holding the pack from build_linux.py"
needs_pack = pytest.mark.skipif(not (PACK_DIR / "node-pack.json").is_file(), reason=PACK_REASON)


def docker_image_present(reference: str) -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", reference], capture_output=True, check=False).returncode == 0


def make_elf(needed=(), runpath=None, verneed=(), rpath=None) -> bytes:
    strings = bytearray(b"\0")

    def add(text: str) -> int:
        offset = len(strings)
        strings.extend(text.encode() + b"\0")
        return offset

    needed_offsets = [add(name) for name in needed]
    runpath_offset = add(runpath) if runpath is not None else None
    rpath_offset = add(rpath) if rpath is not None else None
    verneed_blob, verneed_count = b"", 0
    if verneed:
        file_offset = add("libc.so.6")
        aux = [(add(version),) for version in verneed]
        header_size, aux_size = 16, 16
        blob = bytearray(struct.pack("<HHIII", 1, len(aux), file_offset, header_size, 0))
        for index, (name,) in enumerate(aux):
            next_offset = aux_size if index < len(aux) - 1 else 0
            blob += struct.pack("<IHHII", 0, 0, 2 + index, name, next_offset)
        verneed_blob, verneed_count = bytes(blob), 1
    base = 64 + 2 * 56
    strtab_at = base
    verneed_at = strtab_at + len(strings)
    dynamic_at = verneed_at + len(verneed_blob)
    dynamic = [(elfaudit.DT_NEEDED, value) for value in needed_offsets]
    if runpath_offset is not None:
        dynamic.append((elfaudit.DT_RUNPATH, runpath_offset))
    if rpath_offset is not None:
        dynamic.append((elfaudit.DT_RPATH, rpath_offset))
    dynamic.append((elfaudit.DT_STRTAB, strtab_at))
    if verneed_blob:
        dynamic += [(elfaudit.DT_VERNEED, verneed_at), (elfaudit.DT_VERNEEDNUM, verneed_count)]
    dynamic.append((elfaudit.DT_NULL, 0))
    dynamic_blob = b"".join(struct.pack("<qQ", tag, value) for tag, value in dynamic)
    total = dynamic_at + len(dynamic_blob)
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4], header[5], header[6] = 2, 1, 1
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HH", header, 54, 56, 2)
    load = struct.pack("<IIQQQQQQ", elfaudit.PT_LOAD, 5, 0, 0, 0, total, total, 0x1000)
    dyn = struct.pack("<IIQQQQQQ", elfaudit.PT_DYNAMIC, 6, dynamic_at, dynamic_at, 0, len(dynamic_blob), len(dynamic_blob), 8)
    return bytes(header) + load + dyn + bytes(strings) + verneed_blob + dynamic_blob


def put(root: Path, relative: str, data: bytes = b"data") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_elf_reader_returns_needed_search_paths_and_the_newest_glibc(tmp_path):
    path = put(tmp_path, "lib.so", make_elf(["libz.so.1", "libc.so.6"], "$ORIGIN:$ORIGIN/../x", ["GLIBC_2.17", "GLIBC_2.36", "GLIBC_2.4"]))
    dynamic = elfaudit.read_dynamic(path)
    assert dynamic.needed == ["libz.so.1", "libc.so.6"]
    assert dynamic.runpath == ["$ORIGIN", "$ORIGIN/../x"]
    assert dynamic.glibc == (2, 36)


def test_elf_reader_handles_a_file_with_no_dynamic_section_and_rejects_other_formats(tmp_path):
    header = bytearray(make_elf())
    struct.pack_into("<I", header, 64 + 56, 0)
    assert elfaudit.read_dynamic(put(tmp_path, "static", bytes(header))).needed == []
    with pytest.raises(elfaudit.ElfError):
        elfaudit.read_dynamic(put(tmp_path, "big", b"\x7fELF\x02\x02" + b"\0" * 80))
    with pytest.raises(elfaudit.ElfError):
        elfaudit.read_dynamic(put(tmp_path, "short", b"\x7fELF\x02\x01" + b"\0" * 10))


def test_is_elf_recognises_only_elf_files(tmp_path):
    assert elfaudit.is_elf(put(tmp_path, "a", make_elf()))
    assert not elfaudit.is_elf(put(tmp_path, "b", b"#!/bin/sh\n"))
    assert not elfaudit.is_elf(tmp_path / "missing")


def test_audit_accepts_libraries_found_in_the_pack_and_glibc(tmp_path):
    put(tmp_path, "python/lib/libpython3.11.so.1.0", make_elf(["libc.so.6", "libm.so.6"], "$ORIGIN", ["GLIBC_2.34"]))
    put(tmp_path, "python/bin/python3.11", make_elf(["libpython3.11.so.1.0", "libc.so.6", "ld-linux-aarch64.so.1"], "$ORIGIN/../lib"))
    problems, newest = elfaudit.audit_tree(tmp_path, (2, 36))
    assert problems == []
    assert newest == (2, 34)


@pytest.mark.parametrize("runpath", ["/usr/lib", "$ORIGIN/../../../../etc", "$ORIGIN/../../..", "relative/dir", "/opt/other"])
def test_audit_refuses_a_search_path_outside_the_pack(tmp_path, runpath):
    put(tmp_path, "python/bin/tool", make_elf(["libc.so.6"], runpath))
    problems, _ = elfaudit.audit_tree(tmp_path, (2, 36))
    assert any("leaves the pack" in problem for problem in problems)


def test_audit_refuses_a_legacy_rpath_outside_the_pack(tmp_path):
    put(tmp_path, "bin/tool", make_elf([], rpath="/usr/local/lib"))
    assert any("leaves the pack" in problem for problem in elfaudit.audit_tree(tmp_path, (2, 36))[0])


def test_audit_refuses_a_needed_library_that_resolves_nowhere_in_the_pack(tmp_path):
    put(tmp_path, "python/bin/tool", make_elf(["libstdc++.so.6", "libc.so.6"], "$ORIGIN/../lib"))
    problems, _ = elfaudit.audit_tree(tmp_path, (2, 36))
    assert problems == ["python/bin/tool: needs libstdc++.so.6, which is neither in the pack nor glibc"]


def test_audit_refuses_a_library_that_only_exists_next_to_a_search_path_it_lacks(tmp_path):
    put(tmp_path, "lib/libfoo.so.1", make_elf(["libc.so.6"], "$ORIGIN"))
    put(tmp_path, "bin/tool", make_elf(["libfoo.so.1"]))
    assert elfaudit.audit_tree(tmp_path, (2, 36))[0]


def test_audit_refuses_a_file_that_needs_a_newer_glibc_than_the_floor(tmp_path):
    put(tmp_path, "lib/new.so", make_elf(["libc.so.6"], "$ORIGIN", ["GLIBC_2.38"]))
    problems, newest = elfaudit.audit_tree(tmp_path, (2, 36))
    assert newest == (2, 38)
    assert any("newer than the floor" in problem for problem in problems)


def test_audit_follows_dollar_brace_origin_and_ignores_non_elf_files(tmp_path):
    put(tmp_path, "lib/libfoo.so.1", make_elf())
    put(tmp_path, "bin/tool", make_elf(["libfoo.so.1"], "${ORIGIN}/../lib"))
    put(tmp_path, "readme.txt", b"text")
    assert elfaudit.audit_tree(tmp_path, (2, 36))[0] == []


def test_audit_skips_symbolic_links(tmp_path):
    put(tmp_path, "real.so", make_elf())
    (tmp_path / "link.so").symlink_to("real.so")
    assert [path.name for path in elfaudit.elf_files(tmp_path)] == ["real.so"]


def fake_image(monkeypatch, tmp_path):
    library_dir = tmp_path / "system"
    put(library_dir, "libmagic.so.1", make_elf(["libz.so.1", "libc.so.6"]))
    put(library_dir, "libz.so.1", make_elf(["libc.so.6"]))
    put(library_dir, "pdftoppm", make_elf(["libmagic.so.1", "libc.so.6"]))
    put(library_dir, "magic.mgc", b"mgc")
    put(library_dir / "fonts", "NimbusSans-Regular.otf", b"font")
    put(library_dir / "conf", "61-urw-nimbus-sans.conf", b"<conf/>")
    put(library_dir / "conf", "30-metric-aliases.conf", b"<conf/>")
    calls = []
    monkeypatch.setattr(debian_natives, "library_directories", lambda architecture: [library_dir])
    monkeypatch.setattr(debian_natives, "locate_binary", lambda name: library_dir / name)
    monkeypatch.setattr(debian_natives, "package_versions", lambda packages: [{"name": name, "version": "1"} for name in packages])
    monkeypatch.setattr(debian_natives, "set_search_path", lambda path, value: calls.append((path.name, value)))
    return library_dir, calls


def fonts_spec(library_dir):
    return {"directory": str(library_dir / "fonts"), "conf_directory": str(library_dir / "conf"),
            "patterns": ["30-metric-aliases.conf", "61-urw-*.conf"]}


def test_native_bundling_copies_the_library_closure_and_points_every_file_into_the_pack(monkeypatch, tmp_path):
    library_dir, calls = fake_image(monkeypatch, tmp_path)
    spec = {"packages": ["poppler-utils"], "binaries": ["pdftoppm"], "libraries": ["libmagic.so.1"],
            "data": {"magic.mgc": str(library_dir / "magic.mgc")}, "fonts": fonts_spec(library_dir)}
    stage = tmp_path / "stage"
    stage.mkdir()
    bundled = debian_natives.bundle(stage, spec, "aarch64")
    assert sorted(path.name for path in (stage / "native" / "lib").iterdir()) == ["libmagic.so.1", "libz.so.1"]
    assert (stage / "native" / "bin" / "pdftoppm").is_file()
    assert (stage / "native" / "share" / "magic.mgc").read_bytes() == b"mgc"
    assert ("pdftoppm", "$ORIGIN/../lib") in calls
    assert {name for name, value in calls if value == "$ORIGIN"} == {"libmagic.so.1", "libz.so.1"}
    assert bundled == {"packages": [{"name": "poppler-utils", "version": "1"}], "libraries": ["libmagic.so.1", "libz.so.1"]}


def test_native_bundling_is_repeatable_and_copies_package_notices(monkeypatch, tmp_path):
    library_dir, _ = fake_image(monkeypatch, tmp_path)
    spec = {"packages": ["definitely-not-a-package"], "binaries": ["pdftoppm"], "libraries": [], "data": {},
            "fonts": fonts_spec(library_dir)}
    stage = tmp_path / "stage"
    stage.mkdir()
    first = debian_natives.bundle(stage, spec, "aarch64")
    assert debian_natives.bundle(stage, spec, "aarch64") == first
    assert list((stage / "native" / "licenses").iterdir()) == []


def test_native_bundling_fails_when_the_image_lacks_a_library(monkeypatch, tmp_path):
    fake_image(monkeypatch, tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(debian_natives.NativeError):
        debian_natives.bundle(stage, {"packages": [], "binaries": [], "libraries": ["libmissing.so.1"], "data": {}, "fonts": {}}, "aarch64")
    monkeypatch.undo()
    with pytest.raises(debian_natives.NativeError):
        debian_natives.locate_binary("definitely-not-a-binary")


def test_package_versions_refuse_a_package_the_image_lacks():
    if shutil.which("dpkg-query") is None:
        with pytest.raises(FileNotFoundError):
            debian_natives.package_versions(["x"])
    else:
        with pytest.raises(debian_natives.NativeError):
            debian_natives.package_versions(["definitely-not-a-package"])


def test_library_directories_name_the_debian_multiarch_paths():
    assert Path("/usr/lib/aarch64-linux-gnu") in debian_natives.library_directories("aarch64")
    assert Path("/lib/x86_64-linux-gnu") in debian_natives.library_directories("x86_64")


def test_the_launcher_puts_the_native_tools_first_on_the_path_and_finds_the_magic_database(tmp_path, monkeypatch):
    env = tmp_path / "node.env"
    env.write_text("ASTRAL_NODE_PROFILE=personal\n")
    root = tmp_path / "pack"
    (root / "native" / "bin").mkdir(parents=True)
    (root / "native" / "share").mkdir()
    (root / "native" / "share" / "magic.mgc").write_bytes(b"x")
    monkeypatch.setenv("PATH", "/usr/bin")
    import argparse

    arguments = argparse.Namespace(host="127.0.0.1", port=8001, env_file=env, allow_remote=False)
    values = launch.configure(arguments, root)
    assert values["PATH"] == f"{root / 'native' / 'bin'}{os.pathsep}/usr/bin"
    assert values["MAGIC"] == str(root / "native" / "share" / "magic.mgc")


def test_the_launcher_leaves_the_path_alone_without_native_tools(tmp_path):
    import argparse

    env = tmp_path / "node.env"
    env.write_text("ASTRAL_NODE_PROFILE=personal\n")
    values = launch.configure(argparse.Namespace(host="127.0.0.1", port=1, env_file=env, allow_remote=False), tmp_path)
    assert "PATH" not in values and "MAGIC" not in values


def test_the_launcher_preloads_libmagic_by_its_pack_path(tmp_path, monkeypatch):
    loaded = []
    monkeypatch.setattr(launch.ctypes, "CDLL", lambda path: loaded.append(path))
    launch.preload_native_libraries(tmp_path)
    assert loaded == []
    (tmp_path / "native" / "lib").mkdir(parents=True)
    library = tmp_path / "native" / "lib" / "libmagic.so.1"
    library.write_bytes(b"x")
    launch.preload_native_libraries(tmp_path)
    assert loaded == [str(library)]


def test_linux_pins_fix_python_locks_binding_and_the_glibc_floor():
    pins = ap.load_pins()
    for platform, arch in (("linux-aarch64", "aarch64"), ("linux-x86_64", "x86_64")):
        target = pins["platforms"][platform]
        assert target["python"]["url"].endswith(f"{arch}-unknown-linux-gnu-install_only_stripped.tar.gz")
        assert target["linux"]["glibc_floor"] == "2.36"
        spec = target["kernel_artifacts"]
        assert spec["binding"] == platform and spec["kind"] == "linux"
        assert spec["wheel_pattern"] == f"astral_kernel-*-manylinux_2_36_{arch}.whl"
        assert BINDING["artifacts"][platform]["wheel"]["file"].endswith(f"manylinux_2_36_{arch}.whl")
        assert (BUNDLE / target["node_lock"]).is_file()
        assert "allowed_packages" not in target
        assert target["linux"]["natives"]["source_image"] == bl.dockerfile_args()["BOOKWORM"]


def test_every_lock_carries_the_native_bindings():
    for arch in ("aarch64", "x86_64"):
        text = (BUNDLE / f"node-requirements.linux-{arch}.lock").read_text()
        assert "\npdf2image==" in text and "\npython-magic==" in text
        assert f"--python-platform {arch}-unknown-linux-gnu" in text.splitlines()[0]
    assert "\npdf2image==" in (BUNDLE / "node-requirements.macos-arm64.lock").read_text()


def test_the_dockerfile_pins_both_bookworm_images_by_digest_and_apt_by_snapshot():
    args = bl.dockerfile_args()
    assert re.fullmatch(r"debian:bookworm@sha256:[0-9a-f]{64}", args["BOOKWORM"])
    assert re.fullmatch(r"debian:bookworm-slim@sha256:[0-9a-f]{64}", args["BOOKWORM_SLIM"])
    assert re.fullmatch(r"\d{8}T\d{6}Z", args["SNAPSHOT"])
    text = bl.DOCKERFILE.read_text()
    assert "snapshot.debian.org" in text and "poppler-utils=" in text and "libmagic1=" in text


def test_the_pack_does_not_exclude_what_linux_bundles():
    pins = ap.load_pins()
    names = {entry["name"] for entry in ap.effective_exclusions(pins, pins["platforms"]["linux-aarch64"])}
    assert "python-magic" not in names and "pdf2image" not in names and "scipy" in names
    mac = {entry["name"] for entry in ap.effective_exclusions(pins, pins["platforms"]["macos-arm64"])}
    assert not {"python-magic", "pdf2image"} & mac


def test_the_kernel_source_comes_from_the_binding_for_linux_and_the_pins_otherwise(tmp_path):
    pins = ap.load_pins()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "astral-kernel-binding.json").write_text(json.dumps(BINDING))
    commit, entries = ap.kernel_source(tmp_path, pins, pins["platforms"]["linux-aarch64"]["kernel_artifacts"])
    assert commit == BINDING["source"]["commit"] and entries == BINDING["artifacts"]["linux-aarch64"]
    mac = ap.kernel_source(tmp_path, pins, pins["platforms"]["macos-arm64"]["kernel_artifacts"])
    assert mac == (pins["kernel"]["commit"], None)


def binding_fixture(tmp_path):
    kernel = tmp_path / "kernel"
    kernel.mkdir()
    wheel = put(kernel, "astral_kernel-0.2.0-py3-none-manylinux_2_36_aarch64.whl", b"wheel")
    mailbox = put(kernel, "astral-mailbox-linux-aarch64", b"mailbox")
    entries = {
        "wheel": {"file": wheel.name, "sha256": hashlib.sha256(b"wheel").hexdigest()},
        "mailbox": {"file": mailbox.name, "sha256": hashlib.sha256(b"mailbox").hexdigest()},
    }
    return kernel, wheel, mailbox, entries


def test_the_binding_check_accepts_the_bound_wheel_and_mailbox_and_defaults_the_mailbox(tmp_path):
    _, wheel, mailbox, entries = binding_fixture(tmp_path)
    assert ap.check_binding({"wheel": wheel, "mailbox": mailbox}, entries, None) == mailbox
    assert ap.check_binding({"wheel": wheel}, entries, mailbox) == mailbox


def test_the_binding_check_refuses_a_changed_wheel_a_changed_mailbox_and_a_missing_mailbox(tmp_path):
    _, wheel, mailbox, entries = binding_fixture(tmp_path)
    wheel.write_bytes(b"changed")
    with pytest.raises(ap.PackError, match="wheel"):
        ap.check_binding({"wheel": wheel}, entries, mailbox)
    wheel.write_bytes(b"wheel")
    mailbox.write_bytes(b"changed")
    with pytest.raises(ap.PackError, match="mailbox"):
        ap.check_binding({"wheel": wheel}, entries, mailbox)
    with pytest.raises(ap.PackError, match="needs the Kernel mailbox"):
        ap.check_binding({"wheel": wheel}, entries, None)


def test_the_builder_verifies_kernel_artifacts_against_the_repository_binding(tmp_path):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    kernel, wheel, mailbox, entries = binding_fixture(tmp_path)
    manifest = {**BINDING, "artifacts": {"linux-aarch64": entries}}
    (repo / "config" / "astral-kernel-binding.json").write_text(json.dumps(manifest))
    assert bl.verify_kernel_artifacts(kernel, "linux-aarch64", repo) == {"wheel": wheel.name, "mailbox": mailbox.name}
    wheel.write_bytes(b"other")
    with pytest.raises(bl.BuildError, match="do not match config/astral-kernel-binding.json"):
        bl.verify_kernel_artifacts(kernel, "linux-aarch64", repo)
    with pytest.raises(bl.BuildError):
        bl.verify_kernel_artifacts(tmp_path / "empty", "linux-aarch64", repo)


def test_the_builder_refuses_an_unknown_platform_and_a_dirty_checkout(tmp_path):
    with pytest.raises(bl.BuildError, match="not a Linux pack platform"):
        bl.build("macos-arm64", tmp_path, tmp_path, "", 1)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "file").write_text("x")
    with pytest.raises(bl.BuildError, match="clean and committed"):
        bl.clean_commit(repo)
    subprocess.run(["git", "-C", str(repo), "add", "file"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
    commit = bl.clean_commit(repo)
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    clone = tmp_path / "clone"
    bl.clone_source(repo, commit, clone)
    assert (clone / "file").read_text() == "x"


def test_source_overrides_are_mounted_read_only_and_renamed_for_the_container(tmp_path):
    mounts, variables = bl.source_mounts({"NODE_BUNDLE_SOURCE_LETS": str(tmp_path), "OTHER": "x", "NODE_BUNDLE_SOURCE_EMPTY": ""})
    assert mounts == ["-v", f"{tmp_path.resolve()}:{bl.BUILD_ROOT}/sources/lets:ro"]
    assert variables == ["-e", f"NODE_BUNDLE_SOURCE_LETS={bl.BUILD_ROOT}/sources/lets"]


def test_the_relocation_script_refuses_a_clean_image_that_has_python_on_its_path():
    assert "command -v" in bl.RELOCATION_SCRIPT and "exit 1" in bl.RELOCATION_SCRIPT
    assert "--network" not in bl.RELOCATION_SCRIPT


def extracted(tmp_path_factory) -> Path:
    destination = tmp_path_factory.mktemp("linux-pack")
    descriptor = json.loads((PACK_DIR / "node-pack.json").read_text())
    with tarfile.open(PACK_DIR / descriptor["archive"]["file"]) as bundle:
        bundle.extractall(destination, filter="data")
    return destination


@pytest.fixture(scope="module")
def pack(tmp_path_factory):
    return extracted(tmp_path_factory)


@needs_pack
def test_the_built_descriptor_names_a_linux_pack_with_the_pinned_python():
    descriptor = json.loads((PACK_DIR / "node-pack.json").read_text())
    manifest = json.loads((PACK_DIR / "node-pack-manifest.json").read_text())
    assert descriptor["platform"].startswith("linux-")
    assert manifest["python"]["url"].endswith("unknown-linux-gnu-install_only_stripped.tar.gz")
    assert hashlib.sha256((PACK_DIR / "node-pack-manifest.json").read_bytes()).hexdigest() == descriptor["manifest_sha256"]


@needs_pack
def test_the_built_manifest_records_the_glibc_floor_and_nothing_needs_more():
    manifest = json.loads((PACK_DIR / "node-pack-manifest.json").read_text())
    assert manifest["glibc_floor"] == "2.36"
    required = tuple(int(part) for part in manifest["glibc_required"].split("."))
    assert required <= (2, 36)


def assemble_kernel_files():
    return ap.KERNEL_FILES


@needs_pack
def test_the_built_pack_records_the_bound_kernel_wheel_and_mailbox_and_every_search_path_patch(pack):
    manifest = json.loads((PACK_DIR / "node-pack-manifest.json").read_text())
    platform = manifest["platform"]
    bound = BINDING["artifacts"][platform]
    assert manifest["kernel"]["commit"] == BINDING["source"]["commit"]
    assert manifest["kernel"]["wheel"] == bound["wheel"]
    assert manifest["kernel"]["mailbox"] == bound["mailbox"]
    patches = manifest["kernel_search_path_patch"]
    assert set(patches) == set(assemble_kernel_files())
    assert patches["bin/astral-mailbox"]["bound_sha256"] == bound["mailbox"]["sha256"]
    folder = os.environ.get(bl.KERNEL_ARTIFACTS_ENV)
    if folder and (Path(folder) / bound["wheel"]["file"]).is_file():
        with zipfile.ZipFile(Path(folder) / bound["wheel"]["file"]) as archive:
            member = hashlib.sha256(archive.read(ap.KERNEL_LIBRARY)).hexdigest()
        assert patches[ap.KERNEL_FILES[1]]["bound_sha256"] == member
    for name, record in patches.items():
        assert hashlib.sha256((pack / name).read_bytes()).hexdigest() == record["shipped_sha256"]
        assert (record["bound_sha256"] != record["shipped_sha256"]) == bool(elfaudit.read_dynamic(pack / name).runpath)
    assert any(entry["name"] == "astral-kernel" for entry in manifest["installed_distributions"])


@needs_pack
def test_every_elf_in_the_built_pack_stays_inside_it_except_for_glibc(pack):
    problems, newest = elfaudit.audit_tree(pack, (2, 36))
    assert problems == []
    assert newest <= (2, 36)
    assert len(elfaudit.elf_files(pack)) > 50


@needs_pack
def test_the_built_pack_bundles_poppler_and_libmagic_with_their_libraries_and_licenses(pack):
    manifest = json.loads((PACK_DIR / "node-pack-manifest.json").read_text())
    assert {"poppler-utils", "libmagic1", "libmagic-mgc", "libgcc-s1"} <= {entry["name"] for entry in manifest["natives"]["packages"]}
    assert (pack / "native" / "bin" / "pdftoppm").is_file() and (pack / "native" / "bin" / "pdfinfo").is_file()
    assert (pack / "native" / "lib" / "libmagic.so.1").is_file() and (pack / "native" / "share" / "magic.mgc").is_file()
    assert (pack / "native" / "licenses" / "poppler-utils.copyright").is_file()
    assert manifest["natives"]["libraries"] == sorted(path.name for path in (pack / "native" / "lib").iterdir())


@needs_pack
def test_the_built_pack_keeps_python_magic_and_pdf2image(pack):
    manifest = json.loads((PACK_DIR / "node-pack-manifest.json").read_text())
    installed = {entry["name"].lower() for entry in manifest["installed_distributions"]}
    assert {"python-magic", "pdf2image"} <= installed
    assert not {"python-magic", "pdf2image"} & {entry["name"] for entry in manifest["excluded_packages"]}


@needs_pack
def test_the_build_receipt_names_the_pinned_images():
    receipt = json.loads((PACK_DIR / bl.RECEIPT_NAME).read_text())
    args = bl.dockerfile_args()
    assert receipt["builder_base"] == args["BOOKWORM"] and receipt["relocation_image"] == args["BOOKWORM_SLIM"]
    assert receipt["kernel_artifacts"].keys() == {"wheel", "mailbox"}


@needs_pack
def test_patchelf_reports_only_pack_relative_search_paths_for_the_built_pack(pack):
    receipt = json.loads((PACK_DIR / bl.RECEIPT_NAME).read_text())
    prefix = os.environ.get(bl.NAME_PREFIX_ENV, "node-pack-")
    candidates = [receipt["builder_image_id"], f"{prefix}builder:{receipt['platform'].removeprefix('linux-')}"]
    tag = next((candidate for candidate in candidates if docker_image_present(candidate)), None)
    if tag is None:
        pytest.skip("the builder image that has patchelf is not present locally")
    script = (
        "cd /pack && find . -type f -print0 | while IFS= read -r -d '' f; do "
        "if head -c 4 \"$f\" | grep -q ELF; then printf '%s\\t%s\\n' \"$f\" \"$(patchelf --print-rpath \"$f\" 2>/dev/null)\"; fi; done"
    )
    output = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--name", "p4-12-patchelf-check", "-v", f"{pack}:/pack:ro", tag, "bash", "-c", script],
        capture_output=True, text=True, check=True,
    ).stdout
    assert output
    for line in output.splitlines():
        path, _, search = line.partition("\t")
        for entry in filter(None, search.split(":")):
            assert entry.startswith("$ORIGIN"), (path, entry)
            resolved = os.path.normpath(os.path.join(os.path.dirname(path), entry[len("$ORIGIN/"):]))
            assert not resolved.startswith(".."), (path, entry)


@needs_pack
def test_the_pack_relocates_in_a_clean_bookworm_slim_image_with_no_python_on_the_path(tmp_path):
    slim = bl.dockerfile_args()["BOOKWORM_SLIM"]
    if not docker_image_present(slim):
        pytest.skip(f"the clean image {slim} is not present locally and the test must not pull it")
    bl.check_relocation(json.loads((PACK_DIR / "node-pack-manifest.json").read_text())["platform"], os.environ.get(bl.NAME_PREFIX_ENV, "node-pack-test-"), slim, PACK_DIR, 900)


def tar_with(path: Path, members: list[tuple[str, bytes | None, str | None]]) -> tarfile.TarFile:
    import io

    with tarfile.open(path, "w") as bundle:
        for name, data, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link
                bundle.addfile(info)
            elif data is None:
                info.type = tarfile.DIRTYPE
                bundle.addfile(info)
            else:
                info.size = len(data)
                bundle.addfile(info, io.BytesIO(data))
    return tarfile.open(path)


@pytest.mark.parametrize("filtered", [True, False])
def test_extraction_unpacks_contained_members_with_or_without_the_tar_data_filter(tmp_path, monkeypatch, filtered):
    if not filtered:
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
    bundle = tar_with(tmp_path / "ok.tar", [("dir", None, None), ("dir/file", b"x", None), ("dir/link", None, "file")])
    destination = tmp_path / "out"
    destination.mkdir()
    ap.extract_members(bundle, destination)
    assert (destination / "dir" / "file").read_bytes() == b"x" and (destination / "dir" / "link").is_symlink()


@pytest.mark.parametrize("member", [
    ("../escape", b"x", None), ("/absolute", b"x", None), ("a/link", None, "/etc/passwd"), ("a/link", None, "../../outside"),
])
def test_extraction_without_the_data_filter_refuses_members_that_leave_the_destination(tmp_path, monkeypatch, member):
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    bundle = tar_with(tmp_path / "bad.tar", [member])
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(ap.PackError, match="not a contained"):
        ap.extract_members(bundle, destination)
    assert not (tmp_path / "escape").exists()


def test_extraction_without_the_data_filter_refuses_device_nodes(tmp_path, monkeypatch):
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    path = tmp_path / "dev.tar"
    with tarfile.open(path, "w") as bundle:
        info = tarfile.TarInfo("node")
        info.type = tarfile.CHRTYPE
        bundle.addfile(info)
    with pytest.raises(ap.PackError):
        ap.extract_members(tarfile.open(path), tmp_path)


def test_audit_resolves_a_needed_entry_written_as_an_origin_relative_path(tmp_path):
    put(tmp_path, "python/lib/libpython3.11.so.1.0", make_elf())
    put(tmp_path, "python/lib/libpython3.so", make_elf(["$ORIGIN/../lib/libpython3.11.so.1.0"]))
    assert elfaudit.audit_tree(tmp_path, (2, 36))[0] == []
    (tmp_path / "python/lib/libpython3.11.so.1.0").unlink()
    assert elfaudit.audit_tree(tmp_path, (2, 36))[0]


def test_system_libraries_that_pack_files_need_are_bundled_and_reachable_from_each_needing_file(monkeypatch, tmp_path):
    library_dir, calls = fake_image(monkeypatch, tmp_path)
    put(library_dir, "libgcc_s.so.1", make_elf(["libc.so.6"]))
    stage = tmp_path / "stage"
    put(stage, "python/lib/python3.11/site-packages/pkg/ext.so", make_elf(["libgcc_s.so.1", "libc.so.6"], "$ORIGIN/pkg.libs"))
    put(stage, "python/lib/python3.11/site-packages/pkg/pkg.libs/other.so", make_elf())
    put(stage, "bin/astral-mailbox", make_elf(["libgcc_s.so.1"]))
    put(stage, "bin/already", make_elf(["libz.so.1"], "$ORIGIN/../native/lib"))
    put(stage, "native/lib/libz.so.1", make_elf())
    debian_natives.link_system_needs(stage, stage / "native", [library_dir])
    assert (stage / "native" / "lib" / "libgcc_s.so.1").is_file()
    assert ("ext.so", "$ORIGIN/pkg.libs:$ORIGIN/../../../../../native/lib") in calls
    assert ("astral-mailbox", "$ORIGIN/../native/lib") in calls
    assert ("libgcc_s.so.1", "$ORIGIN") in calls
    assert not any(name == "already" for name, _ in calls)


def test_a_library_missing_from_the_image_is_left_for_the_audit_to_refuse(monkeypatch, tmp_path):
    library_dir, calls = fake_image(monkeypatch, tmp_path)
    stage = tmp_path / "stage"
    put(stage, "bin/tool", make_elf(["libnowhere.so.9"]))
    debian_natives.link_system_needs(stage, stage / "native", [library_dir])
    assert calls == []
    assert elfaudit.audit_tree(stage, (2, 36))[0]


def test_container_build_paths_are_distinctive_enough_for_the_leak_scan_not_to_match_source_text():
    assert bl.BUILD_ROOT == "/astral-node-build"


def test_loaded_library_trace_flags_only_existing_libraries_from_outside_the_pack(tmp_path):
    inside = tmp_path / "python" / "lib" / "libgcc_s.so.1"
    outside = tmp_path / "elsewhere" / "libz.so.1"
    system = tmp_path / "lib" / "libc.so.6"
    for path in (inside, outside, system, tmp_path / "lib" / "ld-linux-aarch64.so.1"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    pack = tmp_path / "python"
    trace = "\n".join([
        f"  1: calling init: {inside}", f"  1: calling init: {system}", f"  1: trying file={tmp_path}/lib/ld-linux-aarch64.so.1",
        f"  1: trying file={outside}", "  1: trying file=/does/not/exist/libfoo.so.1",
    ])
    assert relocation_check.foreign_libraries(trace, pack) == [str(outside)]
    assert relocation_check.foreign_libraries("", pack) == []
    spaced = tmp_path / "relocated copy" / "python" / "lib" / "libz.so.1"
    spaced.parent.mkdir(parents=True)
    spaced.write_bytes(b"x")
    assert relocation_check.foreign_libraries(f"  9: trying file={spaced}\n", tmp_path / "relocated copy") == []


def test_the_check_environment_matches_what_the_launcher_sets_for_native_tools(tmp_path):
    fonts = tmp_path / "native" / "etc" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "fonts.conf").write_text("<fontconfig/>")
    environment = relocation_check.native_environment(tmp_path)
    assert environment["FONTCONFIG_FILE"] == str(fonts / "fonts.conf") and environment["FONTCONFIG_PATH"] == str(fonts)
    assert environment["PATH"].startswith(str(tmp_path / "native" / "bin"))
    assert "FONTCONFIG_FILE" not in relocation_check.native_environment(tmp_path / "none")


def test_font_bundling_ships_the_fonts_and_a_configuration_that_points_only_inside_the_pack(monkeypatch, tmp_path):
    library_dir, _ = fake_image(monkeypatch, tmp_path)
    root = tmp_path / "stage" / "native"
    debian_natives.bundle_fonts(root, fonts_spec(library_dir))
    configuration = (root / "etc" / "fonts" / "fonts.conf").read_text()
    assert '<dir prefix="relative">../../fonts</dir>' in configuration
    assert not re.search(r"<dir>|<dir [^>]*>/|<include>/", configuration)
    assert (root / "fonts" / "NimbusSans-Regular.otf").is_file()
    assert sorted(path.name for path in (root / "etc" / "fonts" / "conf.d").iterdir()) == ["30-metric-aliases.conf", "61-urw-nimbus-sans.conf"]


def test_font_bundling_refuses_a_missing_font_directory_and_missing_configuration(monkeypatch, tmp_path):
    library_dir, _ = fake_image(monkeypatch, tmp_path)
    with pytest.raises(debian_natives.NativeError, match="no fonts"):
        debian_natives.bundle_fonts(tmp_path / "a", {**fonts_spec(library_dir), "directory": str(tmp_path / "none")})
    with pytest.raises(debian_natives.NativeError, match="configuration"):
        debian_natives.bundle_fonts(tmp_path / "b", {**fonts_spec(library_dir), "patterns": ["nothing-*.conf"]})


def test_the_launcher_points_fontconfig_at_the_pack_when_it_ships_fonts(tmp_path):
    import argparse

    env = tmp_path / "node.env"
    env.write_text("ASTRAL_NODE_PROFILE=personal\n")
    fonts = tmp_path / "native" / "etc" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "fonts.conf").write_text("<fontconfig/>")
    values = launch.configure(argparse.Namespace(host="127.0.0.1", port=1, env_file=env, allow_remote=False), tmp_path)
    assert values["FONTCONFIG_FILE"] == str(fonts / "fonts.conf") and values["FONTCONFIG_PATH"] == str(fonts)


def test_linux_pins_ship_a_pinned_font_package_and_record_the_cjk_limit():
    pins = ap.load_pins()
    for platform in ("linux-aarch64", "linux-x86_64"):
        linux = pins["platforms"][platform]["linux"]
        assert "fonts-urw-base35" in linux["natives"]["packages"]
        assert any("CJK" in limit for limit in linux["known_limits"])
    assert "fonts-urw-base35=" in bl.DOCKERFILE.read_text()


def load_render_module():
    import importlib

    return importlib.import_module("native_render_check")


def test_the_render_check_builds_well_formed_pdfs_with_correct_cross_reference_offsets():
    render = load_render_module()
    document = render.text_pdf("Helvetica")
    assert document.startswith(b"%PDF-1.4") and b"/BaseFont /Helvetica" in document and b"FontDescriptor" not in document
    table = int(document.rsplit(b"startxref\n", 1)[1].split()[0])
    assert document[table:table + 4] == b"xref"
    for number, line in enumerate(document[table:].split(b"\n")[3:8], 1):
        offset = int(line.split()[0])
        assert document[offset:].startswith(f"{number} 0 obj".encode())


def test_the_render_check_counts_dark_pixels_and_tells_blank_from_filled():
    pytest.importorskip("PIL")
    from PIL import Image

    render = load_render_module()
    assert render.dark_fraction(Image.new("L", (10, 10), 255)) == (0, 0.0)
    assert render.dark_fraction(Image.new("L", (10, 10), 0)) == (100, 1.0)
    assert b"DCTDecode" in render.jpeg_page_pdf()


def test_the_pack_relocation_runs_the_render_check_and_it_exists():
    assert relocation_check.RENDER_CHECK.is_file()


def test_the_mailbox_must_exit_zero_and_print_its_usage_in_the_clean_image_run():
    source = (BUNDLE / "relocation_check.py").read_text()
    assert "the mailbox did not print its usage" in source and "exited {result.returncode}" in source


def test_bound_kernel_digests_come_from_the_wheel_member_and_the_mailbox(tmp_path):
    import zipfile

    wheel = tmp_path / "k.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(ap.KERNEL_LIBRARY, b"library")
    mailbox = put(tmp_path, "mb", b"mailbox")
    assert ap.bound_kernel_digests(wheel, mailbox) == {
        ap.KERNEL_FILES[0]: hashlib.sha256(b"mailbox").hexdigest(), ap.KERNEL_FILES[1]: hashlib.sha256(b"library").hexdigest(),
    }


def test_sealing_refuses_kernel_files_that_are_not_the_bound_bytes(tmp_path):
    stage = tmp_path / "stage"
    put(stage, ap.KERNEL_FILES[0], b"changed")
    put(stage, ap.KERNEL_FILES[1], b"library")
    bound = {ap.KERNEL_FILES[0]: hashlib.sha256(b"mailbox").hexdigest(), ap.KERNEL_FILES[1]: hashlib.sha256(b"library").hexdigest()}
    with pytest.raises(ap.PackError, match="bound wheel and mailbox"):
        ap.seal_linux(stage, {}, bound)


def test_the_default_kernel_directory_comes_from_the_environment_first(monkeypatch, tmp_path):
    monkeypatch.setenv(bl.KERNEL_ARTIFACTS_ENV, str(tmp_path))
    assert bl.default_kernel_dir() == tmp_path
    monkeypatch.delenv(bl.KERNEL_ARTIFACTS_ENV)
    assert bl.default_kernel_dir() in (None, bl.DEFAULT_KERNEL_DIR)


@needs_pack
def test_the_recorded_bound_library_digest_is_the_one_inside_the_bound_wheel():
    manifest = json.loads((PACK_DIR / "node-pack-manifest.json").read_text())
    folder = os.environ.get(bl.KERNEL_ARTIFACTS_ENV)
    wheel = Path(folder or "/nonexistent") / BINDING["artifacts"][manifest["platform"]]["wheel"]["file"]
    if not wheel.is_file():
        pytest.skip(f"the bound wheel is not available: set {bl.KERNEL_ARTIFACTS_ENV} to the verified Kernel artifacts folder")
    with zipfile.ZipFile(wheel) as archive:
        member = hashlib.sha256(archive.read(ap.KERNEL_LIBRARY)).hexdigest()
    assert manifest["kernel_search_path_patch"][ap.KERNEL_FILES[1]]["bound_sha256"] == member


@needs_pack
def test_the_built_pack_ships_fonts_and_a_configuration_that_stays_inside_it(pack):
    manifest = json.loads((PACK_DIR / "node-pack-manifest.json").read_text())
    assert "fonts-urw-base35" in {entry["name"] for entry in manifest["natives"]["packages"]}
    assert (pack / "native" / "licenses" / "fonts-urw-base35.copyright").is_file()
    assert list((pack / "native" / "fonts").glob("*.otf"))
    configuration = (pack / "native" / "etc" / "fonts" / "fonts.conf").read_text()
    assert 'prefix="relative"' in configuration and "/usr/share" not in configuration
    for conf in (pack / "native" / "etc" / "fonts" / "conf.d").iterdir():
        assert "/usr/share/fonts" not in conf.read_text()


@pytest.mark.parametrize("lock", sorted(BUNDLE.glob("node-requirements.*.lock")), ids=lambda path: path.name)
def test_every_platform_lock_satisfies_the_spacy_model_range(lock):
    pinned = re.search(r"^spacy==(\S+) \\$", lock.read_text(), re.MULTILINE)
    assert pinned is not None, f"{lock.name} has no spacy pin"
    model = json.loads((BUNDLE / "pins.json").read_text())["spacy_model"]
    assert ap.satisfies(pinned.group(1), model["requires_spacy"])
    assert re.search(r"^presidio-analyzer==", lock.read_text(), re.MULTILINE)
    assert re.search(r"^presidio-anonymizer==", lock.read_text(), re.MULTILINE)


@pytest.mark.parametrize("lock", sorted(BUNDLE.glob("node-requirements.*.lock")), ids=lambda path: path.name)
def test_every_platform_lock_meets_the_egress_floors(lock):
    pinned = {name.lower().replace("_", "-"): version for name, version in re.findall(r"^([A-Za-z0-9_.-]+)==(\S+) \\$", lock.read_text(), re.MULTILINE)}
    floors = re.findall(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?>=([\d.]+)\s*$", (REPO / "vessel" / "egress" / "requirements.txt").read_text(), re.MULTILINE)
    assert floors
    for name, floor in floors:
        key = name.lower().replace("_", "-")
        if key in pinned:
            assert ap.satisfies(pinned[key], f">={floor}"), f"{lock.name} pins {key} {pinned[key]} below the egress floor {floor}"
    assert "fastapi" in pinned
