"""Checks the macOS signing pass signs fixture code inside out, re-seals the manifest and descriptor, and that verify.py and the publisher's signature check accept or refuse the result.
Every binary is a tiny cc build in a temporary folder signed ad hoc with identity -; no real pack, Developer ID or Keychain identity is touched.
"""

import json
import subprocess

import pytest
from node_bundle_fixtures import linux_pack, needs_macos, unsigned_mac_pack
from node_bundle_support import manifest as pm
from node_bundle_support import sign_macos as sm
from node_bundle_support import verify

pytestmark = needs_macos

CODE = ["Applications/Fix.app/Contents/MacOS/fixapp", "python/bin/python3.11", "python/lib/lib-dynload/ext.so",
        "python/lib/libfix.dylib"]
BUNDLE = "Applications/Fix.app"


def strict(path):
    return subprocess.run(["codesign", "--verify", "--strict", str(path)], capture_output=True, text=True)


def signed_pack(tmp_path):
    descriptor = unsigned_mac_pack(tmp_path)
    return descriptor, sm.sign_pack(descriptor, "-", tmp_path / "signed")


def unpack(descriptor, into):
    verify.install(str(descriptor), pm.sha256_file(descriptor), into)
    return into


def test_fixture_code_starts_unsigned(tmp_path):
    tree = unpack(unsigned_mac_pack(tmp_path), tmp_path / "tree")
    assert all(strict(tree / path).returncode for path in CODE)


def test_ad_hoc_signing_verifies_every_file_and_reseals_the_pack(tmp_path):
    before, after = signed_pack(tmp_path)
    tree = unpack(after, tmp_path / "tree")
    for path in [*CODE, BUNDLE]:
        done = strict(tree / path)
        assert done.returncode == 0, done.stderr
    sealed = json.loads((tree / pm.MANIFEST_NAME).read_text())
    assert sealed["signing"]["identity"] == "-"
    assert sealed["signing"]["files"] == sorted([*CODE, BUNDLE])
    assert sealed["signing"]["sealed_from_manifest_sha256"] == json.loads(before.read_text())["manifest_sha256"]
    assert pm.sha256_file(after) != pm.sha256_file(before)
    assert json.loads(after.read_text())["manifest_sha256"] != json.loads(before.read_text())["manifest_sha256"]
    assert pm.sha256_file(after.parent / "node-pack-manifest.json") == json.loads(after.read_text())["manifest_sha256"]
    assert (tree / "python/bin/python3.11").stat().st_mode & 0o100
    assert any(entry["path"].startswith(f"{BUNDLE}/Contents/_CodeSignature") for entry in sealed["files"])
    assert sm.verify_signed_pack(after) == {"identity": "-", "files": 5}


def test_the_command_line_signs_and_prints_the_descriptor(tmp_path, capsys):
    descriptor = unsigned_mac_pack(tmp_path)
    sm.main([str(descriptor), "--identity", "-", "--out", str(tmp_path / "cli")])
    assert capsys.readouterr().out.strip() == str(tmp_path / "cli" / "node-pack.json")
    with pytest.raises(SystemExit, match="not empty"):
        sm.main([str(descriptor), "--identity", "-", "--out", str(tmp_path / "cli")])


def test_code_is_signed_inside_out_with_the_ad_hoc_identity(tmp_path):
    tree = unpack(unsigned_mac_pack(tmp_path), tmp_path / "tree")
    calls = []

    def recording(command, **kwargs):
        calls.append(command)
        return subprocess.run(command, **kwargs)

    signed = sm.sign_tree(tree, "-", recording)
    order = [command[-1].removeprefix(f"{tree}/") for command in calls]
    assert all(command[:4] == ["codesign", "--force", "--sign", "-"] for command in calls)
    assert order[-1] == BUNDLE and set(order[:-1]) == set(CODE)
    assert order.index("Applications/Fix.app/Contents/MacOS/fixapp") < order.index(BUNDLE)
    depths = [path.count("/") for path in order[:-1]]
    assert depths == sorted(depths, reverse=True)
    assert signed == sorted([*CODE, BUNDLE])


def test_codesign_failure_stops_the_pass(tmp_path):
    tree = unpack(unsigned_mac_pack(tmp_path), tmp_path / "tree")

    def failing(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "no identity")

    with pytest.raises(sm.PackError, match="codesign refused .*no identity"):
        sm.sign_tree(tree, "-", failing)


def test_signing_refuses_a_busy_output_a_signed_pack_and_a_foreign_platform(tmp_path):
    descriptor = unsigned_mac_pack(tmp_path)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep").write_text("existing")
    with pytest.raises(sm.PackError, match="not empty"):
        sm.sign_pack(descriptor, "-", busy)
    assert (busy / "keep").read_text() == "existing"
    signed = sm.sign_pack(descriptor, "-", tmp_path / "signed")
    with pytest.raises(sm.PackError, match="already signed"):
        sm.sign_pack(signed, "-", tmp_path / "again")
    with pytest.raises(sm.PackError, match="only macos-arm64"):
        sm.sign_pack(linux_pack(tmp_path, "linux-x86_64"), "-", tmp_path / "linux-out")


def test_signing_refuses_a_pack_that_fails_verification_or_holds_no_code(tmp_path):
    descriptor = unsigned_mac_pack(tmp_path)
    data = json.loads(descriptor.read_text())
    data["archive"]["sha256"] = "0" * 64
    descriptor.write_text(json.dumps(data))
    with pytest.raises(sm.PackError, match="does not verify"):
        sm.sign_pack(descriptor, "-", tmp_path / "out")
    bare = linux_pack(tmp_path, "linux-aarch64")
    rewritten = json.loads(bare.read_text())
    rewritten["platform"] = "macos-arm64"
    manifest_path = bare.parent / "node-pack-manifest.json"
    sealed = json.loads(manifest_path.read_text())
    sealed["platform"] = "macos-arm64"
    manifest_path.write_text(json.dumps(sealed))
    rewritten["manifest_sha256"] = pm.sha256_file(manifest_path)
    bare.write_text(json.dumps(rewritten))
    with pytest.raises(sm.PackError, match="does not verify"):
        sm.sign_pack(bare, "-", tmp_path / "out2")


def test_a_pack_whose_manifest_predates_signing_is_refused(tmp_path):
    with pytest.raises(sm.PackError, match="predates signing"):
        sm.verify_signed_pack(unsigned_mac_pack(tmp_path))


def reseal_with(descriptor, tmp_path, name, mutate):
    tree = unpack(descriptor, tmp_path / name)
    sealed = json.loads((tree / pm.MANIFEST_NAME).read_text())
    mutate(tree, sealed)
    inputs = {key: value for key, value in sealed.items() if key not in sm.DERIVED_KEYS}
    inputs["signing"] = sealed["signing"]
    out = tmp_path / f"{name}-out"
    out.mkdir()
    pm.write_manifest(tree, inputs)
    archive = pm.write_archive(tree, out / "pack.tar.gz")
    manifest = json.loads((tree / pm.MANIFEST_NAME).read_text())
    (out / "node-pack.json").write_text(json.dumps(
        pm.descriptor(manifest, archive, pm.sha256_file(tree / pm.MANIFEST_NAME))))
    (out / "node-pack-manifest.json").write_bytes((tree / pm.MANIFEST_NAME).read_bytes())
    return out / "node-pack.json"


def test_unsigned_code_and_altered_signed_code_are_refused(tmp_path):
    _, signed = signed_pack(tmp_path)

    def drop(tree, sealed):
        sealed["signing"]["files"].remove("python/lib/libfix.dylib")

    with pytest.raises(sm.PackError, match="unsigned code in the pack: python/lib/libfix.dylib"):
        sm.verify_signed_pack(reseal_with(signed, tmp_path, "dropped", drop))

    def alter(tree, sealed):
        target = tree / "python/lib/libfix.dylib"
        data = bytearray(target.read_bytes())
        data[100] ^= 0xFF
        target.write_bytes(bytes(data))

    with pytest.raises(sm.PackError, match="signature on python/lib/libfix.dylib does not verify"):
        sm.verify_signed_pack(reseal_with(signed, tmp_path, "altered", alter))


def test_a_stale_manifest_over_changed_signed_files_is_refused(tmp_path):
    signed = sm.sign_pack(unsigned_mac_pack(tmp_path), "-", tmp_path / "signed")
    tree = unpack(signed, tmp_path / "tree")
    (tree / "python/bin/python3.11").write_bytes(b"changed after sealing")
    stale = tmp_path / "stale"
    stale.mkdir()
    archive = pm.write_archive(tree, stale / "pack.tar.gz")
    swapped = json.loads(signed.read_text())
    swapped["archive"] = archive
    (stale / "node-pack.json").write_text(json.dumps(swapped))
    (stale / "node-pack-manifest.json").write_bytes((signed.parent / "node-pack-manifest.json").read_bytes())
    with pytest.raises(sm.PackError, match="content changed python/bin/python3.11"):
        sm.verify_signed_pack(stale / "node-pack.json")
