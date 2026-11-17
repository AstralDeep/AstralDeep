"""Checks the Python installer check refuses every descriptor, archive and tree problem the Mac app's installer refuses and unpacks a good pack."""

import io
import json
import os
import tarfile

import pytest
from node_bundle_support import manifest as pm
from node_bundle_support import verify


def make_pack(tmp_path, version="3.11.18-aaaabbbb-1234567", platform="macos-arm64"):
    stage = tmp_path / "stage"
    (stage / "python" / "bin").mkdir(parents=True)
    (stage / "python" / "bin" / "python3.11").write_bytes(b"interpreter")
    (stage / "python" / "bin" / "python3.11").chmod(0o755)
    os.symlink("python3.11", stage / "python" / "bin" / "python")
    (stage / "app").mkdir()
    (stage / "app" / "a.py").write_text("print(1)\n")
    manifest = pm.write_manifest(stage, {"version": version, "platform": platform})
    out = tmp_path / "out"
    out.mkdir()
    archive = pm.write_archive(stage, out / f"pack-{version}.tar.gz")
    descriptor = pm.descriptor(manifest, archive, pm.sha256_file(stage / pm.MANIFEST_NAME))
    path = out / "node-pack.json"
    path.write_text(json.dumps(descriptor))
    return path, descriptor


def rewrite(path, **changes):
    descriptor = json.loads(path.read_text())
    for key, value in changes.items():
        descriptor[key] = value
    path.write_text(json.dumps(descriptor))


def test_a_good_pack_installs_and_reports_its_identity(tmp_path):
    path, descriptor = make_pack(tmp_path)
    digest = pm.sha256_file(path)
    result = verify.install(str(path), digest, tmp_path / "home" / "pack")
    assert result["version"] == descriptor["version"] and result["files"] == 3
    assert (tmp_path / "home" / "pack" / "python" / "bin" / "python3.11").stat().st_mode & 0o100
    assert verify.tree_problems(tmp_path / "home" / "pack", json.loads(
        (tmp_path / "home" / "pack" / "manifest.json").read_text())) == []
    assert not [p for p in (tmp_path / "home").iterdir() if p.name != "pack"]


def test_the_command_line_prints_the_result_and_reports_failures(tmp_path, capsys):
    path, _ = make_pack(tmp_path)
    verify.main([str(path), "--into", str(tmp_path / "into")])
    assert json.loads(capsys.readouterr().out)["version"].startswith("3.11.18")
    with pytest.raises(SystemExit, match="node pack: .*not empty"):
        verify.main([str(path), "--into", str(tmp_path / "into")])


def test_a_pinned_digest_that_does_not_match_is_refused(tmp_path):
    path, _ = make_pack(tmp_path)
    with pytest.raises(verify.VerifyError, match="pinned"):
        verify.install(str(path), "0" * 64, tmp_path / "into")
    assert not (tmp_path / "into").exists()


@pytest.mark.parametrize("change", [
    {"format": "astral.nodepack-descriptor/v2"}, {"platform": "windows"}, {"version": "has space"},
    {"version": "v" * 65}, {"installed_bytes": 0}, {"manifest_sha256": "A" * 64}, {"manifest_sha256": "abc"},
    {"archive": {"file": "../x", "size": 1, "sha256": "0" * 64}},
    {"archive": {"file": "a/b", "size": 1, "sha256": "0" * 64}},
    {"archive": {"file": "pack", "size": 0, "sha256": "0" * 64}},
    {"archive": {"file": "pack", "size": 1, "sha256": "G" * 64}},
])
def test_descriptor_fields_out_of_range_are_refused(tmp_path, change):
    path, _ = make_pack(tmp_path)
    rewrite(path, **change)
    with pytest.raises(verify.VerifyError):
        verify.install(str(path), None, tmp_path / "into")


def test_a_descriptor_that_is_not_json_or_lacks_fields_is_refused(tmp_path):
    path, _ = make_pack(tmp_path)
    path.write_text("not json")
    with pytest.raises(verify.VerifyError, match="unreadable"):
        verify.install(str(path), None, tmp_path / "into")
    path.write_text("{}")
    with pytest.raises(verify.VerifyError, match="unreadable"):
        verify.install(str(path), None, tmp_path / "into")


def test_a_descriptor_over_the_size_limit_is_refused(tmp_path):
    path, _ = make_pack(tmp_path)
    path.write_bytes(b" " * (verify.MAX_DESCRIPTOR_BYTES + 1))
    with pytest.raises(verify.VerifyError, match="larger than"):
        verify.install(str(path), None, tmp_path / "into")


def test_an_archive_with_the_wrong_size_or_digest_is_refused(tmp_path):
    path, descriptor = make_pack(tmp_path)
    archive = path.parent / descriptor["archive"]["file"]
    original = archive.read_bytes()
    archive.write_bytes(original + b"x")
    with pytest.raises(verify.VerifyError, match="size"):
        verify.install(str(path), None, tmp_path / "one")
    archive.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(verify.VerifyError, match="SHA-256"):
        verify.install(str(path), None, tmp_path / "two")


def forged_archive(tmp_path, tamper):
    path, descriptor = make_pack(tmp_path)
    archive = path.parent / descriptor["archive"]["file"]
    with tarfile.open(archive) as bundle:
        members = [(m, bundle.extractfile(m).read() if m.isfile() else None) for m in bundle.getmembers()]
    with tarfile.open(archive, "w:gz") as bundle:
        for member, data in members:
            tamper(bundle, member, data)
    rewrite(path, archive={"file": archive.name, "size": archive.stat().st_size, "sha256": pm.sha256_file(archive)})
    return path


def test_a_file_changed_inside_a_correctly_hashed_archive_is_caught_by_the_manifest(tmp_path):
    def tamper(bundle, member, data):
        if member.name == "app/a.py":
            data = b"print(2)\n"
            member.size = len(data)
        bundle.addfile(member, io.BytesIO(data) if data is not None else None)

    path = forged_archive(tmp_path, tamper)
    with pytest.raises(verify.VerifyError, match="content changed app/a.py"):
        verify.install(str(path), None, tmp_path / "into")
    assert not (tmp_path / "into").exists()


def test_a_changed_mode_and_an_extra_file_are_caught(tmp_path):
    def tamper(bundle, member, data):
        if member.name == "app/a.py":
            member.mode = 0o755
        bundle.addfile(member, io.BytesIO(data) if data is not None else None)
        if member.name == "app/a.py":
            extra = tarfile.TarInfo("app/extra.py")
            extra.size = 1
            bundle.addfile(extra, io.BytesIO(b"x"))

    path = forged_archive(tmp_path, tamper)
    with pytest.raises(verify.VerifyError) as raised:
        verify.install(str(path), None, tmp_path / "into")
    assert "mode changed app/a.py" in str(raised.value) and "unexpected file app/extra.py" in str(raised.value)


def test_an_archive_that_escapes_its_folder_is_refused(tmp_path):
    def tamper(bundle, member, data):
        bundle.addfile(member, io.BytesIO(data) if data is not None else None)
        if member.name == "app/a.py":
            evil = tarfile.TarInfo("../escape.txt")
            evil.size = 1
            bundle.addfile(evil, io.BytesIO(b"x"))

    path = forged_archive(tmp_path, tamper)
    with pytest.raises(verify.VerifyError, match="cannot be unpacked"):
        verify.install(str(path), None, tmp_path / "into")
    assert not (tmp_path / "escape.txt").exists()


def test_a_manifest_that_does_not_match_the_descriptor_is_refused(tmp_path):
    path, _ = make_pack(tmp_path)
    rewrite(path, manifest_sha256="1" * 64)
    with pytest.raises(verify.VerifyError, match="manifest does not match"):
        verify.install(str(path), None, tmp_path / "into")


def test_a_manifest_naming_another_version_is_refused(tmp_path):
    path, _ = make_pack(tmp_path)
    other, _ = make_pack(tmp_path / "other", version="9.9.9")
    descriptor = json.loads(path.read_text())
    wrong = json.loads(other.read_text())
    descriptor["manifest_sha256"] = wrong["manifest_sha256"]
    descriptor["archive"] = {**wrong["archive"]}
    (path.parent / wrong["archive"]["file"]).write_bytes((other.parent / wrong["archive"]["file"]).read_bytes())
    path.write_text(json.dumps(descriptor))
    with pytest.raises(verify.VerifyError, match="does not match the descriptor"):
        verify.install(str(path), None, tmp_path / "into")


def test_an_archive_without_a_manifest_is_refused(tmp_path):
    def tamper(bundle, member, data):
        if member.name != "manifest.json":
            bundle.addfile(member, io.BytesIO(data) if data is not None else None)

    path = forged_archive(tmp_path, tamper)
    with pytest.raises(verify.VerifyError, match="no readable manifest"):
        verify.install(str(path), None, tmp_path / "into")


def test_a_corrupt_manifest_with_the_right_digest_is_refused(tmp_path):
    def tamper(bundle, member, data):
        if member.name == "manifest.json":
            data = b"[1, 2"
            member.size = len(data)
        bundle.addfile(member, io.BytesIO(data) if data is not None else None)

    path = forged_archive(tmp_path, tamper)
    rewrite(path, manifest_sha256=pm.sha256_bytes(b"[1, 2"))
    with pytest.raises(verify.VerifyError, match="manifest is unreadable"):
        verify.install(str(path), None, tmp_path / "into")


def test_a_symlink_that_leaves_the_pack_is_reported(tmp_path):
    stage = tmp_path / "pack"
    (stage / "app").mkdir(parents=True)
    os.symlink("../../x", stage / "app" / "link")
    manifest = {"files": [{"path": "app/link", "link": "../../x"}]}
    assert verify.tree_problems(stage, manifest) == ["link leaves the pack app/link"]


def test_remote_addresses_follow_the_installer_url_policy(tmp_path):
    for address in ("http://example.com/node-pack.json", "ftp://127.0.0.1/node-pack.json"):
        with pytest.raises(verify.VerifyError, match="unsupported address"):
            verify.fetch(address)
    assert verify.sibling("https://host/a/node-pack.json", "pack.tar.gz") == "https://host/a/pack.tar.gz"


def test_a_descriptor_can_be_read_through_a_file_address(tmp_path):
    path, descriptor = make_pack(tmp_path)
    result = verify.install(path.as_uri(), None, tmp_path / "into")
    assert result["version"] == descriptor["version"]
