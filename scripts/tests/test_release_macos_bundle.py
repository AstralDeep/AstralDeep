"""Checks the release publisher's --node-bundle-dir input accepts three sealed packs of the release commit and refuses unsigned, misplaced, foreign-source or tampered ones.
Packs are cc-built fixtures signed ad hoc, and the Deep checkout is a real temporary repository with gitlinks standing in for components.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from node_bundle_fixtures import linux_pack, needs_macos, unsigned_mac_pack
from node_bundle_support import sign_macos as sm

SCRIPT = Path(__file__).resolve().parents[1] / "release_macos.py"
SPEC = importlib.util.spec_from_file_location("release_macos_bundle_under_test", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)

pytestmark = needs_macos

NAMES = {"astral-projection": "components/AstralProjection", "astral-kernel": "components/AstralKernel",
         "astral-primitives": "components/AstralPrimitives", "lets": "components/LETS"}
PROJECTION = "b" * 40


def git(repo, *argv):
    return subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True, text=True).stdout.strip()


def make_deep(tmp_path):
    repo = tmp_path / "deep"
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "user.name", "Pack Test")
    git(repo, "config", "user.email", "pack@example.test")
    components = {}
    for index, (name, path) in enumerate(NAMES.items()):
        commit = f"{index + 1:x}" * 40
        components[name] = {"repository": f"https://example.invalid/{name}.git", "path": path, "commit": commit}
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},{path}")
    (repo / "config").mkdir()
    (repo / "config/astral-composition.json").write_text(json.dumps({"components": components}))
    git(repo, "add", "config")
    git(repo, "commit", "--quiet", "-m", "Release")
    return repo, components, git(repo, "rev-parse", "HEAD")


def pack_inputs(components):
    return {"components": {name: {"repository": entry["repository"], "commit": entry["commit"]}
                           for name, entry in components.items()},
            "kernel": {"repository": "k", "commit": components["astral-kernel"]["commit"]}}


class Bundle:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.repo, self.components, self.sha = make_deep(tmp_path)
        self.root = tmp_path / "bundle"
        self.root.mkdir()
        self.build()

    def inputs(self):
        return pack_inputs(self.components)

    def build(self, deep=None, mac_sign=True):
        deep = deep or self.sha
        scratch = self.tmp / "scratch"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir()
        unsigned = unsigned_mac_pack(scratch, deep, self.inputs())
        shutil.rmtree(self.root / "macos-arm64", ignore_errors=True)
        if mac_sign:
            sm.sign_pack(unsigned, "-", self.root / "macos-arm64")
        else:
            shutil.copytree(unsigned.parent, self.root / "macos-arm64")
        for platform in ("linux-x86_64",):
            shutil.rmtree(self.root / platform, ignore_errors=True)
            shutil.copytree(linux_pack(scratch, platform, deep, self.inputs()).parent, self.root / platform)

    def release(self, *extra):
        notes = self.tmp / "notes.md"
        notes.write_text("notes\n")
        args = release.build_parser().parse_args([
            "--projection-commit", PROJECTION, "--version", "2.0.0", "--build", "71", "--notes", str(notes),
            "--node-bundle-dir", str(self.root), "--staging-dir", str(self.tmp / "stage"), *extra,
        ])
        subject = release.Release(args, release.Runner(dry_run=False, publish=False, out=lambda _: None), deep=self.repo)
        subject.deep_sha = self.sha
        return subject


@pytest.fixture(scope="module")
def good(tmp_path_factory):
    return Bundle(tmp_path_factory.mktemp("bundle"))


def test_the_sealed_packs_of_the_release_commit_are_accepted(good):
    subject = good.release()
    pack = subject.verify_node_bundle()
    assert pack["signing"]["identity"] == "-" and pack["signing"]["signedFiles"] == 5
    assert pack["source_equivalence"]["releaseDeep"] == good.sha
    assert set(pack["linux"]) == {"linux-x86_64"}
    names = [name for _, name, _ in subject.pack_sources]
    assert len(set(names)) == len(names)
    assert {"node-pack.json", "node-pack-manifest.json", "node-pack-linux-x86_64.json",
            "node-pack-linux-x86_64-manifest.json"} <= set(names)
    assert not any("linux-aarch64" in name for name in names)
    assert all(source.is_file() and release.sha256_file(source) == digest for source, _, digest in subject.pack_sources)
    assert pack["descriptor_sha256"] == release.sha256_file(good.root / "macos-arm64/node-pack.json")


def test_publisher_lists_every_pack_asset_and_stages_them_without_overwriting(good):
    subject = good.release()
    subject.node_pack = subject.verify_node_bundle()
    listed = [path.name for path in subject.release_assets()]
    assert all(name in listed for _, name, _ in subject.pack_sources)
    subject.staging.mkdir(parents=True, exist_ok=True)
    subject.stage_node_pack()
    assert all(release.sha256_file(subject.staging / name) == digest for _, name, digest in subject.pack_sources)
    (subject.staging / "node-pack-linux-x86_64.json").write_text("immutable")
    (subject.staging / "node-pack.json").unlink()
    with pytest.raises(release.ReleaseError, match="never overwritten"):
        subject.stage_node_pack()
    assert (subject.staging / "node-pack-linux-x86_64.json").read_text() == "immutable"
    shutil.rmtree(subject.staging)


def test_a_manifest_that_predates_signing_is_refused(tmp_path):
    bundle = Bundle(tmp_path)
    bundle.build(mac_sign=False)
    with pytest.raises(release.ReleaseError, match="predates signing"):
        bundle.release().verify_node_bundle()


def test_signed_code_changed_after_sealing_is_refused(tmp_path):
    bundle = Bundle(tmp_path)
    archive = next((bundle.root / "macos-arm64").glob("*.tar.gz"))
    archive.write_bytes(archive.read_bytes()[:-8])
    with pytest.raises(release.ReleaseError, match="disagree"):
        bundle.release().verify_node_bundle()


def test_a_forged_signing_record_is_refused_by_the_signature_check(tmp_path):
    bundle = Bundle(tmp_path)
    scratch = tmp_path / "forged"
    scratch.mkdir()
    unsigned = unsigned_mac_pack(scratch, bundle.sha, {**bundle.inputs(), "signing": {
        "identity": "-", "sealed_from_manifest_sha256": "1" * 64, "files": ["python/bin/python3.11"]}})
    shutil.rmtree(bundle.root / "macos-arm64")
    shutil.copytree(unsigned.parent, bundle.root / "macos-arm64")
    with pytest.raises(release.ReleaseError, match="signature does not verify"):
        bundle.release().verify_node_bundle()


@pytest.mark.parametrize("folder,source", [("linux-x86_64", "macos-arm64"), ("macos-arm64", "linux-x86_64")])
def test_a_pack_with_the_wrong_platform_is_refused(tmp_path, folder, source):
    bundle = Bundle(tmp_path)
    shutil.rmtree(bundle.root / folder)
    shutil.copytree(bundle.root / source, bundle.root / folder)
    with pytest.raises(release.ReleaseError, match="(descriptor format or platform|archive name) is invalid"):
        bundle.release().verify_node_bundle()


def test_a_source_commit_other_than_the_release_commit_is_refused(tmp_path):
    bundle = Bundle(tmp_path)
    bundle.build(deep="e" * 40)
    with pytest.raises(release.ReleaseError, match="source commit other than the release commit"):
        bundle.release().verify_node_bundle()


def test_composition_drift_is_refused(tmp_path):
    bundle = Bundle(tmp_path)
    bundle.components["astral-primitives"]["commit"] = "9" * 40
    bundle.build()
    with pytest.raises(release.ReleaseError, match="astral-primitives differs from the release composition"):
        bundle.release().verify_node_bundle()
    bundle.components["astral-primitives"]["commit"] = "3" * 40
    del bundle.components["lets"]
    bundle.build()
    with pytest.raises(release.ReleaseError, match="component inventory differs"):
        bundle.release().verify_node_bundle()


def test_a_release_gitlink_that_differs_from_the_composition_is_refused(tmp_path):
    bundle = Bundle(tmp_path)
    git(bundle.repo, "update-index", "--cacheinfo", f"160000,{'8' * 40},components/LETS")
    git(bundle.repo, "commit", "--quiet", "-m", "Move a component")
    bundle.sha = git(bundle.repo, "rev-parse", "HEAD")
    bundle.build()
    with pytest.raises(release.ReleaseError, match="lets gitlink differs from composition"):
        bundle.release().verify_node_bundle()


def test_the_bundle_folder_must_hold_exactly_the_three_packs(tmp_path):
    bundle = Bundle(tmp_path)
    shutil.rmtree(bundle.root / "linux-x86_64")
    with pytest.raises(release.ReleaseError, match="exactly the macos-arm64"):
        bundle.release().verify_node_bundle()


def test_either_pack_input_is_required_and_never_both(tmp_path):
    bundle = Bundle(tmp_path)
    with pytest.raises(release.ReleaseError, match="not both"):
        bundle.release("--node-pack-dir", str(tmp_path)).validate()
    subject = bundle.release()
    subject.args.node_bundle_dir = None
    with pytest.raises(release.ReleaseError, match="--node-pack-dir is required"):
        subject.validate()


def test_a_dry_run_names_the_pack_assets_without_reading_them(tmp_path):
    bundle = Bundle(tmp_path)
    subject = bundle.release()
    subject.dry = True
    shutil.rmtree(bundle.root)
    pack = subject.verify_node_bundle()
    assert pack["archive_name"].endswith("macos-arm64.tar.gz")
    assert len(subject.pack_sources) == 3
    assert json.dumps(pack)


def test_an_ad_hoc_pack_is_refused_when_a_team_is_required(good):
    with pytest.raises(release.ReleaseError, match="signature does not verify"):
        good.release("--node-team-id", "ABCDE12345").verify_node_bundle()


def test_a_real_run_without_a_team_is_refused_before_anything_is_staged(good):
    subject = good.release()
    with pytest.raises(release.ReleaseError, match="--node-team-id is required"):
        subject.validate()
    assert not subject.staging.exists()
    dry = good.release("--dry-run")
    dry.dry = True
    dry.args.notes = good.tmp / "notes.md"
    dry.validate()


def test_a_staged_copy_that_differs_from_the_verified_bytes_is_refused(good):
    subject = good.release()
    subject.node_pack = subject.verify_node_bundle()
    subject.pack_sources[0] = (subject.pack_sources[0][0], subject.pack_sources[0][1], "0" * 64)
    subject.staging.mkdir(parents=True, exist_ok=True)
    with pytest.raises(release.ReleaseError, match="differs from the verified bytes"):
        subject.stage_node_pack()
    shutil.rmtree(subject.staging)


def build_receipt(platform="linux-x86_64", deep=None, **changes):
    receipt = {
        "platform": platform, "deep_commit": deep, "kernel_artifacts": {"wheel": "k.whl", "mailbox": "mb"},
        "builder_base": "debian:bookworm", "relocation_image": "debian:bookworm-slim", "apt_snapshot": "20260101T000000Z",
        "builder_image_id": "sha256:" + "a" * 64,
    }
    receipt.update(changes)
    return receipt


def linux_folder(good, receipt=None, raw=None):
    folder = good.root / "linux-x86_64"
    (folder / "node-pack-build.json").unlink(missing_ok=True)
    if raw is not None:
        (folder / "node-pack-build.json").write_text(raw)
    elif receipt is not None:
        (folder / "node-pack-build.json").write_text(json.dumps(receipt))
    return folder


def test_a_linux_pack_of_three_files_or_with_its_valid_receipt_is_accepted(good):
    subject = good.release()
    folder = linux_folder(good)
    assert subject.read_pack(folder, "linux-x86_64")[3].name.endswith("linux-x86_64.tar.gz")
    linux_folder(good, build_receipt(deep=good.sha))
    assert subject.read_pack(folder, "linux-x86_64")[2]["deep"]["commit"] == good.sha
    assert subject.verify_node_bundle()["source_equivalence"]["releaseDeep"] == good.sha
    linux_folder(good)


@pytest.mark.parametrize("make", [
    lambda sha: build_receipt(deep="e" * 40),
    lambda sha: build_receipt(platform="linux-aarch64", deep=sha),
    lambda sha: {**build_receipt(deep=sha), "extra": "field"},
    lambda sha: {key: value for key, value in build_receipt(deep=sha).items() if key != "apt_snapshot"},
    lambda sha: build_receipt(deep=sha, kernel_artifacts={"wheel": "k.whl"}),
    lambda sha: build_receipt(deep=sha, kernel_artifacts={"wheel": "k.whl", "mailbox": 3}),
    lambda sha: build_receipt(deep=sha, builder_base=""),
    lambda sha: ["not", "an", "object"],
])
def test_an_invalid_or_mismatching_receipt_is_refused(good, make):
    folder = linux_folder(good, make(good.sha))
    try:
        with pytest.raises(release.ReleaseError, match="build receipt"):
            good.release().read_pack(folder, "linux-x86_64")
    finally:
        linux_folder(good)


def test_an_unreadable_receipt_is_refused(good):
    folder = linux_folder(good, raw="{not json")
    try:
        with pytest.raises(release.ReleaseError, match="unreadable"):
            good.release().read_pack(folder, "linux-x86_64")
    finally:
        linux_folder(good)


def test_any_other_extra_file_is_still_refused(good):
    folder = linux_folder(good, build_receipt(deep=good.sha))
    stray = folder / "notes.txt"
    stray.write_text("x")
    try:
        with pytest.raises(release.ReleaseError, match="exactly its descriptor"):
            good.release().read_pack(folder, "linux-x86_64")
        stray.unlink()
        mac = good.root / "macos-arm64"
        (mac / "node-pack-build.json").write_text(json.dumps(build_receipt("macos-arm64", good.sha)))
        with pytest.raises(release.ReleaseError, match="exactly its descriptor"):
            good.release().read_pack(mac, "macos-arm64")
    finally:
        stray.unlink(missing_ok=True)
        (good.root / "macos-arm64" / "node-pack-build.json").unlink(missing_ok=True)
        linux_folder(good)
