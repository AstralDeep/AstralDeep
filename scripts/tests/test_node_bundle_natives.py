"""Checks the natives lock, the digest-first fetch, the pinned build options, the build flow with its tools faked, the receipt and the pack rules that build_macos.py enforces."""

import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from node_bundle_support import assemble as ap
from node_bundle_support import build_macos as bm
from node_bundle_support import manifest as pm
from node_bundle_support import relocation_check as rc
from test_node_bundle_macho import build_version, thin, write


def real_lock():
    return json.loads((bm.HERE / "sources.lock.json").read_text())


def archive_bytes(files: dict, top: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            info.mode = 0o755 if name == "rg" else 0o644
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_the_committed_lock_pins_every_source_with_licenses():
    lock = bm.validate_lock(real_lock())
    assert {entry["name"] for entry in lock["sources"]} == {"expat", "fontconfig", "freetype", "libjpeg-turbo", "libmagic", "poppler", "ripgrep"}
    assert all(entry["url"].startswith("https://") for entry in lock["sources"])
    assert set(lock["shipped"]) >= {"bin/pdfinfo", "bin/pdftoppm", "bin/rg", "lib/libmagic.dylib",
                                    "share/fontconfig/fonts.conf"}
    assert "CJK" in lock["known_limits"][0]


@pytest.mark.parametrize("mutation, message", [
    (lambda lock: lock.update(format="other"), "natives lock is not"),
    (lambda lock: lock.update(deployment_target="latest"), "deployment_target"),
    (lambda lock: lock.update(prefix="relative"), "absolute prefix"),
    (lambda lock: lock["sources"][0].update(url="http://example.invalid/x.tar.gz"), "https"),
    (lambda lock: lock["sources"][0].update(sha256="ABC"), "SHA-256"),
    (lambda lock: lock["sources"][0].update(size=0), "positive size"),
    (lambda lock: lock["sources"][0].update(kind="other"), "kind"),
    (lambda lock: lock["sources"][0].update(license_files=[]), "license files"),
    (lambda lock: lock["sources"][0].update(license_files=["../escape"]), "license files"),
    (lambda lock: lock["sources"][1].update(name=lock["sources"][0]["name"]), "repeated"),
    (lambda lock: lock.update(shipped=[]), "no sources or no shipped"),
])
def test_a_malformed_lock_is_refused(mutation, message):
    lock = copy.deepcopy(real_lock())
    mutation(lock)
    with pytest.raises(bm.NativesError, match=message):
        bm.validate_lock(lock)


def test_an_unreadable_lock_is_refused(tmp_path):
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(bm.NativesError, match="not a readable"):
        bm.load_lock(tmp_path / "bad.json")
    with pytest.raises(bm.NativesError, match="not a readable"):
        bm.load_lock(tmp_path / "absent.json")


def test_a_download_is_checked_before_it_is_kept_or_unpacked(tmp_path, monkeypatch):
    payload = archive_bytes({"COPYING": b"license"}, "pkg-1")
    pin = {"url": "https://example.invalid/pkg-1.tar.gz", "size": len(payload),
           "sha256": hashlib.sha256(payload).hexdigest()}
    monkeypatch.setattr(ap.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(payload))
    assert ap.fetch_pinned(pin, tmp_path / "cache").read_bytes() == payload
    for broken in ({**pin, "sha256": "0" * 64}, {**pin, "size": len(payload) + 1}):
        with pytest.raises(ap.PackError, match="pinned size and SHA-256"):
            ap.fetch_pinned(broken, tmp_path / "cache")
        assert not (tmp_path / "cache" / "pkg-1.tar.gz").exists()
    with pytest.raises(ap.PackError, match="https"):
        ap.fetch_pinned({**pin, "url": "http://example.invalid/pkg-1.tar.gz"}, tmp_path / "cache")


def test_unpacking_needs_the_named_top_folder_and_a_readable_archive(tmp_path):
    archive = tmp_path / "pkg.tar.gz"
    archive.write_bytes(archive_bytes({"COPYING": b"x"}, "pkg-1"))
    assert bm.unpack({"top": "pkg-1"}, archive, tmp_path / "one") == tmp_path / "one" / "pkg-1"
    with pytest.raises(bm.NativesError, match="does not unpack to"):
        bm.unpack({"top": "other"}, archive, tmp_path / "two")
    archive.write_bytes(b"not an archive")
    with pytest.raises(bm.NativesError, match="could not be unpacked"):
        bm.unpack({"top": "pkg-1"}, archive, tmp_path / "three")


def test_the_build_environment_is_isolated_and_pins_the_deployment_target(tmp_path):
    lock = real_lock()
    environment = bm.build_environment(lock, tmp_path / "tools", tmp_path / "deps", tmp_path / "work", "/sdk")
    assert environment["PATH"] == f"{tmp_path / 'tools'}:/usr/bin:/bin:/usr/sbin:/sbin"
    assert environment["MACOSX_DEPLOYMENT_TARGET"] == "14.0"
    assert "-mmacosx-version-min=14.0" in environment["CFLAGS"]
    assert "-Werror=unguarded-availability-new" in environment["CXXFLAGS"]
    assert f"-ffile-prefix-map={tmp_path / 'work'}=/build" in environment["CFLAGS"]
    assert environment["PKG_CONFIG_LIBDIR"] == str(tmp_path / "deps" / "lib" / "pkgconfig")
    assert not any("homebrew" in value for value in environment.values())


def test_cmake_options_ignore_host_prefixes_and_every_optional_backend_is_off(tmp_path):
    common = bm.cmake_options(real_lock(), tmp_path, "/prefix")
    assert "-DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew;/usr/local" in common
    assert "-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0" in common and "-DCMAKE_INSTALL_PREFIX=/prefix" in common
    poppler = bm.poppler_options(tmp_path)
    for name in ("ENABLE_LIBOPENJPEG", "ENABLE_LCMS", "ENABLE_LIBTIFF", "ENABLE_GLIB", "ENABLE_QT6"):
        assert f"-D{name}=OFF" in poppler
    assert "-DENABLE_LIBJPEG=ON" in poppler and "-DENABLE_LIBJPEG=OFF" not in poppler
    assert "-DFONT_CONFIGURATION=fontconfig" in poppler and "-DCMAKE_DISABLE_FIND_PACKAGE_PNG=ON" in poppler
    assert "-DFT_DISABLE_HVF=ON" in bm.freetype_options()
    assert "-DENABLE_SHARED=OFF" in bm.jpeg_options() and "-DEXPAT_SHARED_LIBS=OFF" in bm.expat_options()
    fontconfig = bm.fontconfig_options(real_lock())
    assert "--disable-shared" in fontconfig and "--with-default-fonts=/System/Library/Fonts" in fontconfig
    configuration = bm.fonts_configuration()
    assert "<dir>/System/Library/Fonts/Supplemental</dir>" in configuration and "<cachedir" in configuration
    magic = bm.libmagic_options(real_lock())
    assert "--disable-zlib" in magic and "--disable-xzlib" in magic and "ac_cv_func_pipe2=no" in magic


def test_license_files_are_copied_and_a_missing_one_is_refused(tmp_path):
    top = tmp_path / "src"
    write(top, "COPYING", b"terms")
    entry = {"name": "pkg", "version": "1", "license_files": ["COPYING"]}
    assert bm.copy_licenses(entry, top, tmp_path / "out") == ["licenses/natives/pkg-1/COPYING"]
    write(top, "EMPTY", b"")
    for missing in ("ABSENT", "EMPTY"):
        with pytest.raises(bm.NativesError, match="no license file"):
            bm.copy_licenses({**entry, "license_files": [missing]}, top, tmp_path / f"other-{missing}")


def test_ripgrep_is_taken_from_its_release_and_a_release_without_it_is_refused(tmp_path):
    entry = {"top": "rg-1"}
    archive = tmp_path / "rg.tar.gz"
    archive.write_bytes(archive_bytes({"rg": b"binary", "COPYING": b"x"}, "rg-1"))
    (tmp_path / "out" / "bin").mkdir(parents=True)
    bm.extract_ripgrep(entry, archive, tmp_path / "scratch", tmp_path / "out")
    assert (tmp_path / "out" / "bin" / "rg").read_bytes() == b"binary"
    archive.write_bytes(archive_bytes({"COPYING": b"x"}, "rg-1"))
    with pytest.raises(bm.NativesError, match="no rg binary"):
        bm.extract_ripgrep(entry, archive, tmp_path / "scratch2", tmp_path / "out")


@pytest.fixture
def fake_build(tmp_path, monkeypatch):
    lock = real_lock()
    lock_path = tmp_path / "recipe" / "sources.lock.json"
    write(lock_path.parent, "sources.lock.json", json.dumps(lock).encode())
    archives = {}
    tops = {"freetype": {"docs/FTL.TXT": b"ftl", "docs/GPLv2.TXT": b"gpl"}, "libmagic": {"COPYING": b"bsd"},
            "poppler": {"COPYING": b"gpl2", "COPYING3": b"gpl3"},
            "libjpeg-turbo": {"LICENSE.md": b"bsd", "README.ijg": b"ijg"}, "expat": {"COPYING": b"mit"},
            "fontconfig": {"COPYING": b"hpnd"},
            "ripgrep": {"rg": b"rg-binary", "COPYING": b"c", "LICENSE-MIT": b"m", "UNLICENSE": b"u"}}
    for entry in lock["sources"]:
        archive = tmp_path / "dl" / f"{entry['name']}.tar.gz"
        archive.parent.mkdir(exist_ok=True)
        archive.write_bytes(archive_bytes(tops[entry["name"]], entry["top"]))
        archives[entry["name"]] = archive
    commands = []

    def fetch(entry, cache):
        return archives[entry["name"]]

    def run(command, **kwargs):
        command = [str(part) for part in command]
        commands.append(command)
        if command[:2] == ["/usr/bin/xcrun", "--show-sdk-path"]:
            return SimpleNamespace(stdout="/sdk\n")
        if command[0] == "cmake" and "--target" in command:
            utilities = Path(command[command.index("-B") if "-B" in command else command.index("--build") + 1]) / "utils"
            utilities.mkdir(parents=True, exist_ok=True)
            for tool in bm.POPPLER_TOOLS:
                (utilities / tool).write_bytes(b"tool")
        if command[0].endswith("configure"):
            Path(kwargs["cwd"]).mkdir(exist_ok=True)
        if command[0] == "make" and command[1] == "install":
            root = Path(command[2].split("=", 1)[1]) / "astral-node-pack"
            if "fontconfig" in command[2]:
                write(root, "include/fontconfig/fontconfig.h", b"h")
                write(root, "lib/libfontconfig.a", b"a")
                write(root, "lib/pkgconfig/fontconfig.pc", b"prefix=/astral-node-pack\n")
            else:
                write(root, "lib/libmagic.1.dylib", b"dylib")
                write(root, "share/misc/magic.mgc", b"mgc")
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(ap, "fetch_pinned", fetch)
    monkeypatch.setattr(bm, "run", run)
    monkeypatch.setattr(bm, "link_tools", lambda directory: directory)
    monkeypatch.setattr(bm, "tool_versions", lambda tools: {"cmake": "cmake 1", "ninja": "1"})
    monkeypatch.setattr(ap, "scan_for_leaks", lambda *a: None)
    return SimpleNamespace(lock=lock_path, out=tmp_path / "natives", cache=tmp_path / "cache", work=tmp_path / "work",
                           commands=commands)


def test_the_build_stages_tools_library_database_licenses_and_a_receipt(fake_build):
    out = bm.build(fake_build.lock, fake_build.out, fake_build.cache, fake_build.work)
    for name in real_lock()["shipped"]:
        assert (out / name).exists(), name
    assert (out / "lib" / "libmagic.dylib").is_symlink()
    assert (out / "bin" / "rg").stat().st_mode & 0o111
    receipt = json.loads((out / bm.RECEIPT_PATH).read_text())
    assert receipt["lock_sha256"] == pm.sha256_file(fake_build.lock) and receipt["tools"]["cmake"] == "cmake 1"
    assert {item["path"] for item in receipt["files"]} == set(real_lock()["shipped"])
    assert (out / bm.LOCK_COPY_PATH).read_bytes() == fake_build.lock.read_bytes()
    assert bm.license_problems(out) == []
    assert any(command[:2] == ["install_name_tool", "-id"] for command in fake_build.commands)
    assert sum(command[0] == "codesign" for command in fake_build.commands) == 3
    assert "<dir>/Library/Fonts</dir>" in (out / "share/fontconfig/fonts.conf").read_text()
    descriptor = (fake_build.work / "deps/lib/pkgconfig/fontconfig.pc").read_text()
    assert str(fake_build.work / "deps") in descriptor and "/astral-node-pack" not in descriptor
    assert bm.summary(out)["files"] == receipt["files"]


def test_a_receipt_binds_the_folder_to_its_lock_and_its_bytes(fake_build, tmp_path):
    out = bm.build(fake_build.lock, fake_build.out, fake_build.cache, fake_build.work)
    assert bm.require_receipt_for(out, fake_build.lock)["format"] == bm.RECEIPT_FORMAT
    other = tmp_path / "other.lock"
    other.write_text(fake_build.lock.read_text() + " ")
    with pytest.raises(bm.NativesError, match="different sources lock"):
        bm.require_receipt_for(out, other)
    (out / "bin" / "rg").write_bytes(b"changed")
    with pytest.raises(bm.NativesError, match="changed after it was built: bin/rg"):
        bm.require_receipt_for(out, fake_build.lock)
    shutil.rmtree(out / "licenses")
    with pytest.raises(bm.NativesError, match="no readable natives receipt"):
        bm.read_receipt(out)


def test_a_wrong_receipt_format_is_refused(tmp_path):
    write(tmp_path, bm.RECEIPT_PATH, b'{"format": "other"}')
    with pytest.raises(bm.NativesError, match="receipt is not"):
        bm.read_receipt(tmp_path)


def test_a_build_that_misses_a_shipped_file_is_refused(fake_build, monkeypatch):
    monkeypatch.setattr(bm.shutil, "copy2", lambda source, target, **kw: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        bm.build(fake_build.lock, fake_build.out, fake_build.cache, fake_build.work)
    with pytest.raises(bm.NativesError, match="did not produce"):
        bm.file_digests(fake_build.out, ["bin/absent"])


def test_license_problems_name_missing_empty_and_unlisted_files(fake_build):
    out = bm.build(fake_build.lock, fake_build.out, fake_build.cache, fake_build.work)
    (out / "licenses/natives/poppler-26.11.0/COPYING3").write_bytes(b"")
    (out / "licenses/natives/libmagic-5.48/COPYING").unlink()
    (out / "share/magic/magic.mgc").unlink()
    problems = bm.license_problems(out)
    assert "license file licenses/natives/poppler-26.11.0/COPYING3 is missing or empty" in problems
    assert "license file licenses/natives/libmagic-5.48/COPYING is missing or empty" in problems
    assert "shipped file share/magic/magic.mgc is missing" in problems
    receipt = json.loads((out / bm.RECEIPT_PATH).read_text())
    receipt["sources"][0]["license_files"] = []
    (out / bm.RECEIPT_PATH).write_text(json.dumps(receipt))
    assert any("ships no license file" in problem for problem in bm.license_problems(out))
    shutil.rmtree(out / "licenses")
    assert "no readable natives receipt" in bm.license_problems(out)[0]


def test_pack_rules_report_binary_and_license_problems(fake_build, capsys):
    out = bm.build(fake_build.lock, fake_build.out, fake_build.cache, fake_build.work)
    assert bm.pack_problems(out) == ["the pack holds no Mach-O files"]
    write(out, "bin/tool", thin([build_version(1, (14, 0, 0))], file_type=2))
    assert bm.pack_problems(out) == []
    write(out, "bin/late", thin([build_version(1, (15, 0, 0))], file_type=2))
    assert bm.pack_problems(out) == ["bin/late: minos 15.0.0 is newer than 14.0"]
    assert "FAIL" in capsys.readouterr().out


def test_the_check_runs_the_probe_in_a_relocated_pack_and_reports_failures(fake_build, monkeypatch, capsys):
    out = bm.build(fake_build.lock, fake_build.out, fake_build.cache, fake_build.work)
    write(out, "bin/tool", thin([build_version(1, (14, 0, 0))], file_type=2))
    stage = out.parent / "stage"
    shutil.copytree(out, stage)
    (stage / "python" / "bin").mkdir(parents=True)
    (stage / "python" / "bin" / "python3.11").write_text("#!/bin/sh\n")
    manifest = pm.write_manifest(stage, {"version": "1.0", "platform": "macos-arm64"})
    monkeypatch.setattr(rc, "locate_pack", lambda source, scratch: (stage, manifest))
    outcome = {"code": 0}
    monkeypatch.setattr(bm, "run_probe", lambda root, python: SimpleNamespace(
        returncode=outcome["code"], stdout="PASS probe\n", stderr="boom"))
    assert bm.check_pack(out) == []
    outcome["code"] = 3
    assert bm.check_pack(out) == ["probe exited 3: boom"]
    (stage / "bin" / "tool").write_bytes(b"drift")
    assert any("tool" in problem for problem in bm.check_pack(out))
    assert "PASS probe" in capsys.readouterr().out


def test_the_probe_runs_with_the_pack_python_in_a_minimal_environment(tmp_path, monkeypatch):
    seen = {}

    def record(command, **kwargs):
        seen.update(command=command, **kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bm.subprocess, "run", record)
    bm.run_probe(tmp_path / "pack", tmp_path / "pack" / "python" / "bin" / "python3.11")
    assert seen["command"][1:3] == ["-I", "-B"] and seen["command"][3] == str(bm.PROBE)
    assert seen["env"] == {"PATH": "/usr/bin:/bin"}


def test_the_command_line_checks_and_builds(fake_build, monkeypatch, capsys):
    monkeypatch.setattr(bm, "check_pack", lambda source: [])
    bm.main(["--check", str(fake_build.out)])
    assert "ALL CHECKS PASSED" in capsys.readouterr().out
    monkeypatch.setattr(bm, "check_pack", lambda source: ["bad thing"])
    with pytest.raises(SystemExit, match="bad thing"):
        bm.main(["--check", str(fake_build.out)])
    monkeypatch.setattr(bm, "check_pack", lambda source: (_ for _ in ()).throw(bm.NativesError("broken")))
    with pytest.raises(SystemExit, match="natives: broken"):
        bm.main(["--check", str(fake_build.out)])
    with pytest.raises(SystemExit, match="--build needs"):
        bm.main(["--build", str(fake_build.out)])
    bm.main(["--build", str(fake_build.out), "--lock", str(fake_build.lock), "--work", str(fake_build.work),
             "--cache", str(fake_build.cache)])
    assert str(fake_build.out) in capsys.readouterr().out


def test_missing_build_tools_are_named(tmp_path, monkeypatch):
    monkeypatch.setattr(bm.shutil, "which", lambda name: None)
    with pytest.raises(bm.NativesError, match="cmake is required"):
        bm.link_tools(tmp_path / "tools")


def test_build_tools_are_linked_and_their_versions_recorded(tmp_path, monkeypatch):
    found = tmp_path / "bin"
    found.mkdir()
    for name in bm.BUILD_TOOLS:
        (found / name).write_text(f"#!/bin/sh\necho {name} 9.9\n")
        (found / name).chmod(0o755)
    monkeypatch.setattr(bm.shutil, "which", lambda name: str(found / name))
    monkeypatch.setattr(bm.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 0, stdout=f"{Path(command[0]).name} 9.9\n", stderr=""))
    tools = bm.link_tools(tmp_path / "tools")
    assert (tools / "cmake").resolve() == (found / "cmake").resolve()
    versions = bm.tool_versions(tools)
    assert versions["cmake"] == "cmake 9.9" and versions["ninja"] == "ninja 9.9"
    assert set(versions) == {"cmake", "ninja", "pkg-config", "clang", "make", "sdk"}


def test_binaries_are_stripped_and_resigned_ad_hoc(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(bm, "run", lambda command, **kwargs: commands.append([str(part) for part in command]))
    bm.seal_binary(tmp_path / "tool", {})
    assert commands == [["strip", "-x", str(tmp_path / "tool")],
                        ["codesign", "--force", "--sign", "-", str(tmp_path / "tool")]]


def test_run_raises_on_a_failing_tool():
    with pytest.raises(subprocess.CalledProcessError):
        bm.run(["/usr/bin/false"])


@pytest.mark.skipif("ASTRAL_NODE_PACK_CHECK" not in os.environ, reason="needs an assembled full pack")
def test_an_assembled_full_pack_passes_every_native_rule():
    assert bm.check_pack(Path(os.environ["ASTRAL_NODE_PACK_CHECK"])) == []


def load_probe(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("native_probe", bm.PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_probe_fails_when_a_jpeg_page_or_a_plain_font_renders_blank(tmp_path, monkeypatch, capsys):
    probe = load_probe(monkeypatch)
    renders = {}

    def fake_render(document, directory):
        return renders.get("jpeg", 500) if b"DCTDecode" in document else (
            renders.get("font", 500) if b"/BaseFont" in document else 500)

    monkeypatch.setattr(probe, "render", fake_render)
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=b"Pages:           1", stderr=b""))
    pdf2image = SimpleNamespace(convert_from_bytes=lambda data, dpi: [SimpleNamespace(size=(50, 50))])
    monkeypatch.setitem(__import__("sys").modules, "pdf2image", pdf2image)
    probe.probe_poppler(tmp_path)
    assert probe.FAILED == []
    renders.update(jpeg=0)
    probe.probe_poppler(tmp_path)
    assert probe.FAILED == ["pdftoppm renders a JPEG image page"]
    probe.FAILED.clear()
    renders.update(jpeg=500, font=0)
    probe.probe_poppler(tmp_path)
    assert len(probe.FAILED) == 3 and all("non-embedded" in label for label in probe.FAILED)
    assert "FAIL pdftoppm renders a JPEG" not in capsys.readouterr().out.split("non-embedded")[-1]


def test_a_missing_imaging_library_fails_the_probe_instead_of_skipping(monkeypatch):
    probe = load_probe(monkeypatch)
    monkeypatch.setitem(__import__("sys").modules, "PIL", None)
    with pytest.raises(ImportError):
        probe.jpeg_pdf()


def test_the_probe_fixtures_are_well_formed_pdfs(monkeypatch):
    probe = load_probe(monkeypatch)
    for document in (probe.square_pdf(), probe.flate_pdf(), probe.text_pdf("Helvetica")):
        assert document.startswith(b"%PDF-1.4") and document.rstrip().endswith(b"%%EOF")
    assert b"/BaseFont /Times-Roman" in probe.text_pdf("Times-Roman")


def test_the_shared_error_class_is_the_one_assemble_run_as_a_script_catches():
    import runpy

    fresh = runpy.run_path(str(ap.HERE / "assemble.py"), run_name="not_main")
    assert fresh["PackError"] is bm.PackError is ap.PackError
    assert issubclass(bm.NativesError, fresh["PackError"])


def test_a_natives_failure_prints_a_node_pack_message_and_exits(monkeypatch, tmp_path):
    def broken(*arguments, **keywords):
        raise bm.NativesError("natives broke")

    monkeypatch.setattr(ap, "assemble", broken)
    with pytest.raises(SystemExit, match="node pack: natives broke"):
        ap.main(["--out", str(tmp_path / "o"), "--work", str(tmp_path / "w"), "--kernel-dir", str(tmp_path)])
