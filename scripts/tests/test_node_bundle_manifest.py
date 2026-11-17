"""Checks the pack manifest's tamper detection, the archive's determinism and the descriptor chain the node installer verifies."""

import json
import os
import tarfile

import pytest
from node_bundle_support import manifest as pm
from node_bundle_support import verify

INPUTS = {"version": "1", "platform": "macos-arm64"}


def make_tree(root):
    (root / "python" / "bin").mkdir(parents=True)
    (root / "python" / "bin" / "python3.11").write_bytes(b"binary")
    (root / "python" / "bin" / "python3.11").chmod(0o755)
    os.symlink("python3.11", root / "python" / "bin" / "python")
    (root / "app").mkdir()
    (root / "app" / "a.py").write_text("print(1)\n")
    return root


def sealed(tmp_path):
    root = make_tree(tmp_path / "pack")
    return root, pm.write_manifest(root, INPUTS)


def test_clean_tree_verifies_and_records_links_and_modes(tmp_path):
    root, manifest = sealed(tmp_path)
    assert verify.tree_problems(root, manifest) == []
    by_path = {entry["path"]: entry for entry in manifest["files"]}
    assert by_path["python/bin/python"]["link"] == "python3.11"
    assert by_path["python/bin/python3.11"]["executable"] is True
    assert manifest["installed_bytes"] == len(b"binary") + len("print(1)\n")


@pytest.mark.parametrize("tamper,expected", [
    (lambda r: (r / "app" / "a.py").write_text("print(2)\n"), "content changed app/a.py"),
    (lambda r: (r / "app" / "a.py").unlink(), "missing file app/a.py"),
    (lambda r: (r / "app" / "extra.py").write_text("x"), "unexpected file app/extra.py"),
    (lambda r: ((r / "python" / "bin" / "python").unlink(), os.symlink("/bin/sh", r / "python" / "bin" / "python")),
     "link changed python/bin/python"),
    (lambda r: ((r / "python" / "bin" / "python").unlink(), (r / "python" / "bin" / "python").write_text("x")),
     "link changed python/bin/python"),
    (lambda r: ((r / "app" / "a.py").unlink(), os.symlink("x", r / "app" / "a.py")), "not a regular file app/a.py"),
    (lambda r: (r / "app" / "a.py").chmod(0o755), "mode changed app/a.py"),
    (lambda r: (r / "python" / "bin" / "python3.11").chmod(0o644), "mode changed python/bin/python3.11"),
])
def test_tampering_is_reported(tmp_path, tamper, expected):
    root, manifest = sealed(tmp_path)
    tamper(root)
    assert expected in verify.tree_problems(root, manifest)


def test_a_link_that_leaves_the_pack_is_refused_at_seal_time(tmp_path):
    root = make_tree(tmp_path / "pack")
    os.symlink("../../outside", root / "app" / "escape")
    with pytest.raises(ValueError, match="leaves the pack"):
        pm.write_manifest(root, INPUTS)


def test_an_absolute_link_is_refused(tmp_path):
    root = make_tree(tmp_path / "pack")
    os.symlink("/etc/passwd", root / "app" / "abs")
    with pytest.raises(ValueError, match="leaves the pack"):
        pm.write_manifest(root, INPUTS)


def test_a_forged_manifest_that_allows_an_escaping_link_is_caught_by_the_verifier(tmp_path):
    root, manifest = sealed(tmp_path)
    os.symlink("../../x", root / "app" / "evil")
    manifest["files"].append({"path": "app/evil", "link": "../../x"})
    assert "link leaves the pack app/evil" in verify.tree_problems(root, manifest)


def test_unsupported_file_types_are_refused(tmp_path):
    root = make_tree(tmp_path / "pack")
    os.mkfifo(root / "app" / "pipe")
    with pytest.raises(ValueError, match="unsupported"):
        pm.write_manifest(root, INPUTS)


def test_archive_is_deterministic_and_unpacks_to_a_verifiable_tree(tmp_path):
    root, _ = sealed(tmp_path)
    first = pm.write_archive(root, tmp_path / "one.tar.gz")
    os.utime(root / "app" / "a.py", (12345, 12345))
    second = pm.write_archive(root, tmp_path / "two.tar.gz")
    assert first["sha256"] == second["sha256"] and first["size"] == (tmp_path / "one.tar.gz").stat().st_size
    unpacked = tmp_path / "unpacked"
    with tarfile.open(tmp_path / "one.tar.gz") as bundle:
        bundle.extractall(unpacked, filter="data")
    stored = json.loads((unpacked / pm.MANIFEST_NAME).read_text())
    assert verify.tree_problems(unpacked, stored) == []
    assert (unpacked / "python" / "bin" / "python3.11").stat().st_mode & 0o100


def test_descriptor_binds_archive_and_manifest_hashes(tmp_path):
    root, manifest = sealed(tmp_path)
    archive = pm.write_archive(root, tmp_path / "pack.tar.gz")
    manifest_sha = pm.sha256_file(root / pm.MANIFEST_NAME)
    descriptor = pm.descriptor(manifest, archive, manifest_sha)
    assert descriptor["format"] == pm.DESCRIPTOR_FORMAT
    assert descriptor["archive"]["sha256"] == pm.sha256_file(tmp_path / "pack.tar.gz")
    assert descriptor["manifest_sha256"] == pm.sha256_bytes((root / pm.MANIFEST_NAME).read_bytes())
    assert descriptor["installed_bytes"] == manifest["installed_bytes"]


def test_a_version_the_installer_would_refuse_is_not_sealed(tmp_path):
    _, manifest = sealed(tmp_path)
    with pytest.raises(ValueError, match="not installable"):
        pm.descriptor({**manifest, "version": "has space"}, {}, "0" * 64)
