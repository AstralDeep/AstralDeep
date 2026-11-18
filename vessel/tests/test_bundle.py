# Tests for node pack install: a good pack installs and reinstalls, and every refusal keeps the previous bundle.

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from astralvessel import bundle
from astralvessel.bundle import BundleError, install


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_pack(out: Path, *, version="1.0.0", platform=None, extra=None, tamper=False, link=None,
              glibc=None, files=None) -> tuple[Path, str]:
    platform = platform or bundle.platform_name()
    out.mkdir(parents=True, exist_ok=True)
    files = files or {"python/bin/python3.11": (b"#!/bin/sh\n", True), "app/backend/x.py": (b"print(1)\n", False)}
    entries = [{"path": p, "sha256": sha(d), "size": len(d), "executable": x} for p, (d, x) in files.items()]
    if link:
        entries.append({"path": link[0], "link": link[1]})
    manifest = {"format": bundle.MANIFEST_FORMAT, "version": version, "platform": platform,
                "installed_bytes": sum(e.get("size", 0) for e in entries), "files": entries,
                "installed_distributions": [{"name": "httpx", "version": "0.28.1"}]}
    if glibc:
        manifest["glibc_required"] = glibc
    mtext = json.dumps(manifest).encode()
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as z, tarfile.open(fileobj=z, mode="w") as tar:
        def add(name, data, mode=0o644):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
        add("manifest.json", mtext)
        for p, (d, x) in files.items():
            add(p, (d + b"X") if tamper else d, 0o755 if x else 0o644)
        for name, data in (extra or {}).items():
            add(name, data)
        if link:
            info = tarfile.TarInfo(link[0])
            info.type, info.linkname = tarfile.SYMTYPE, link[1]
            tar.addfile(info)
    archive = buffer.getvalue()
    name = f"pack-{version}.tar.gz"
    (out / name).write_bytes(archive)
    descriptor = {"format": bundle.DESCRIPTOR_FORMAT, "version": version, "platform": platform,
                  "archive": {"file": name, "size": len(archive), "sha256": sha(archive)},
                  "installed_bytes": manifest["installed_bytes"], "manifest_sha256": sha(mtext)}
    text = json.dumps(descriptor).encode()
    (out / "node-pack.json").write_bytes(text)
    return out / "node-pack.json", sha(text)


def test_a_good_pack_installs_and_reinstalls(tmp_path):
    descriptor, digest = make_pack(tmp_path / "src")
    root = tmp_path / "bundles"
    first = install(str(descriptor), digest, root, platform=bundle.platform_name())
    assert first.version == "1.0.0" and first.python.name == "python3.11" and first.python.stat().st_mode & 0o100
    assert first.distributions == {"httpx": "0.28.1"}
    again = install(str(descriptor), digest, root, platform=bundle.platform_name())
    assert again == first


def test_a_folder_may_stand_for_its_descriptor_and_a_local_pack_needs_no_pin(tmp_path):
    make_pack(tmp_path / "src")
    assert install(str(tmp_path / "src"), None, tmp_path / "b", platform=bundle.platform_name()).version == "1.0.0"


def test_a_remote_descriptor_needs_a_pin(tmp_path):
    with pytest.raises(BundleError, match="bundle.digest"):
        install("https://example.test/node-pack.json", None, tmp_path, platform=bundle.platform_name())


def test_a_wrong_descriptor_digest_refuses(tmp_path):
    descriptor, _ = make_pack(tmp_path / "src")
    with pytest.raises(BundleError, match="pinned SHA-256"):
        install(str(descriptor), "0" * 64, tmp_path / "b", platform=bundle.platform_name())


def test_another_platform_refuses(tmp_path):
    other = next(p for p in bundle.PLATFORMS if p != bundle.platform_name())
    descriptor, digest = make_pack(tmp_path / "src", platform=other)
    with pytest.raises(BundleError, match="bundle.platform"):
        install(str(descriptor), digest, tmp_path / "b", platform=bundle.platform_name())


def previous_install(tmp_path):
    descriptor, digest = make_pack(tmp_path / "old", version="1.0.0")
    root = tmp_path / "bundles"
    old = install(str(descriptor), digest, root, platform=bundle.platform_name())
    snapshot = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    return root, old, snapshot


@pytest.mark.parametrize("case", ["tamper", "extra", "link"])
def test_a_bad_pack_refuses_and_keeps_the_previous_bundle(tmp_path, case):
    root, old, snapshot = previous_install(tmp_path)
    before = (old.root / "app/backend/x.py").read_bytes()
    kwargs = {"tamper": {"tamper": True}, "extra": {"extra": {"app/evil.py": b"x"}},
              "link": {"link": ("app/escape", "../../../etc/passwd")}}[case]
    descriptor, digest = make_pack(tmp_path / "new", version="2.0.0", **kwargs)
    with pytest.raises(BundleError):
        install(str(descriptor), digest, root, platform=bundle.platform_name())
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == snapshot
    assert (old.root / "app/backend/x.py").read_bytes() == before


def test_a_tampered_archive_refuses(tmp_path):
    descriptor, digest = make_pack(tmp_path / "src")
    archive = tmp_path / "src" / "pack-1.0.0.tar.gz"
    data = bytearray(archive.read_bytes())
    data[-12] ^= 1
    archive.write_bytes(bytes(data))
    with pytest.raises(BundleError, match="archive"):
        install(str(descriptor), digest, tmp_path / "b", platform=bundle.platform_name())
    assert not any((tmp_path / "b").glob("1.0.0"))


def test_glibc_floor_refuses_an_older_system(tmp_path):
    descriptor, digest = make_pack(tmp_path / "src", platform="linux-x86_64", glibc="2.36")
    with pytest.raises(BundleError, match="glibc 2.36 or newer"):
        install(str(descriptor), digest, tmp_path / "b", platform="linux-x86_64", glibc=(2, 31))
    assert install(str(descriptor), digest, tmp_path / "b", platform="linux-x86_64", glibc=(2, 39)).version == "1.0.0"
    with pytest.raises(BundleError, match="not glibc"):
        install(str(descriptor), digest, tmp_path / "c", platform="linux-x86_64", glibc=None)


def test_no_pin_and_no_bundle_gives_a_pointed_error(tmp_path):
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"platforms": {"macos-arm64": {"descriptor": "", "sha256": ""}}}))
    with pytest.raises(BundleError, match="--bundle"):
        bundle.resolve(None, None, "macos-arm64", pins)
    pins.write_text(json.dumps({"platforms": {"macos-arm64": {"descriptor": "https://x.test/n.json",
                                                              "sha256": "a" * 64}}}))
    assert bundle.resolve(None, None, "macos-arm64", pins) == ("https://x.test/n.json", "a" * 64)
    assert bundle.resolve("/p", "b" * 64, "macos-arm64", pins) == ("/p", "b" * 64)
    assert bundle.pinned("macos-arm64", tmp_path / "missing.json") is None


def test_shipped_pins_are_empty_until_a_release_fills_them():
    assert bundle.pinned("macos-arm64") is None


def test_install_roots_follow_the_platform(monkeypatch):
    monkeypatch.setattr(bundle.sys, "platform", "darwin")
    assert str(bundle.install_root()).endswith("Library/Application Support/AstralVessel/bundles")
    monkeypatch.setattr(bundle.sys, "platform", "linux")
    assert str(bundle.install_root()).endswith(".local/share/astralvessel/bundles")


def test_platform_name_and_unsupported(monkeypatch):
    monkeypatch.setattr(bundle.sys, "platform", "darwin")
    monkeypatch.setattr(bundle.os, "uname", lambda: os.uname_result(("", "", "", "", "arm64")))
    assert bundle.platform_name() == "macos-arm64"
    monkeypatch.setattr(bundle.sys, "platform", "win32")
    with pytest.raises(BundleError, match="platform_unsupported"):
        bundle.platform_name()


def test_addresses_are_limited(tmp_path):
    with pytest.raises(BundleError, match="bundle.address"):
        install("http://evil.test/node-pack.json", "a" * 64, tmp_path, platform=bundle.platform_name())
    with pytest.raises(BundleError, match="bundle.unreachable"):
        install(str(tmp_path / "none.json"), None, tmp_path, platform=bundle.platform_name())
