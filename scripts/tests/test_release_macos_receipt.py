"""Checks the release publisher accepts a Linux node pack folder with or without its build receipt, refuses every malformed or mismatching receipt, and refuses a bundle folder that does not hold exactly the three packs.
Folders are plain files sealed by the shared fixtures, so these run on any host, unlike the macOS-gated bundle tests.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from node_bundle_fixtures import linux_pack

SCRIPT = Path(__file__).resolve().parents[1] / "release_macos.py"
SPEC = importlib.util.spec_from_file_location("release_macos_receipt_under_test", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)

DEEP = "d" * 40
RECEIPT = "node-pack-build.json"


def receipt(platform="linux-x86_64", deep=DEEP, **changes):
    value = {
        "platform": platform, "deep_commit": deep, "kernel_artifacts": {"wheel": "k.whl", "mailbox": "mb"},
        "builder_base": "debian:bookworm", "relocation_image": "debian:bookworm-slim",
        "apt_snapshot": "20260101T000000Z", "builder_image_id": "sha256:" + "a" * 64,
    }
    value.update(changes)
    return value


@pytest.fixture
def subject(tmp_path):
    args = release.build_parser().parse_args([
        "--projection-commit", "b" * 40, "--version", "2.0.0", "--build", "71", "--notes", str(tmp_path / "n.md"),
    ])
    return release.Release(args, release.Runner(dry_run=False, publish=False, out=lambda _: None), deep=tmp_path)


@pytest.fixture
def folder(tmp_path):
    return linux_pack(tmp_path / "scratch", "linux-x86_64").parent


def put(folder, value=None, raw=None):
    (folder / RECEIPT).write_text(raw if raw is not None else json.dumps(value))


def test_a_folder_of_exactly_three_files_is_accepted(subject, folder):
    assert subject.read_pack(folder, "linux-x86_64")[3].name.endswith("linux-x86_64.tar.gz")


def test_the_same_folder_with_a_valid_receipt_is_accepted(subject, folder):
    put(folder, receipt())
    assert subject.read_pack(folder, "linux-x86_64")[2]["deep"]["commit"] == DEEP


@pytest.mark.parametrize("value", [
    receipt(deep="e" * 40),
    receipt(platform="linux-aarch64"),
    {**receipt(), "extra": "field"},
    {key: item for key, item in receipt().items() if key != "apt_snapshot"},
    receipt(kernel_artifacts={"wheel": "k.whl"}),
    receipt(kernel_artifacts={"wheel": "k.whl", "mailbox": 3}),
    receipt(kernel_artifacts="wheel"),
    receipt(builder_base=""),
    ["not", "an", "object"],
])
def test_an_invalid_or_mismatching_receipt_is_refused(subject, folder, value):
    put(folder, value)
    with pytest.raises(release.ReleaseError, match="build receipt"):
        subject.read_pack(folder, "linux-x86_64")


def test_an_unreadable_receipt_is_refused(subject, folder):
    put(folder, raw="{not json")
    with pytest.raises(release.ReleaseError, match="unreadable"):
        subject.read_pack(folder, "linux-x86_64")


def test_a_receipt_that_is_not_utf8_is_refused(subject, folder):
    (folder / RECEIPT).write_bytes(b"\xff\xfe")
    with pytest.raises(release.ReleaseError, match="unreadable"):
        subject.read_pack(folder, "linux-x86_64")


def test_a_receipt_that_is_a_symlink_is_refused(subject, folder, tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(receipt()))
    (folder / RECEIPT).symlink_to(elsewhere)
    with pytest.raises(release.ReleaseError, match="regular files"):
        subject.read_pack(folder, "linux-x86_64")


def test_a_receipt_that_is_a_directory_is_refused(subject, folder):
    (folder / RECEIPT).mkdir()
    with pytest.raises(release.ReleaseError, match="regular files"):
        subject.read_pack(folder, "linux-x86_64")


def test_a_manifest_whose_deep_entry_is_not_an_object_is_refused(subject, tmp_path):
    folder = linux_pack(tmp_path / "bad", "linux-x86_64", extra={"deep": "oops"}).parent
    put(folder, receipt())
    with pytest.raises(release.ReleaseError, match="another platform or source commit"):
        subject.read_pack(folder, "linux-x86_64")


def test_any_other_extra_file_is_still_refused(subject, folder):
    put(folder, receipt())
    (folder / "notes.txt").write_text("x")
    with pytest.raises(release.ReleaseError, match="exactly its descriptor"):
        subject.read_pack(folder, "linux-x86_64")


def test_a_receipt_beside_a_macos_pack_is_refused(subject, tmp_path):
    folder = linux_pack(tmp_path / "mac", "macos-arm64").parent
    put(folder, receipt("macos-arm64"))
    with pytest.raises(release.ReleaseError, match="exactly its descriptor"):
        subject.read_pack(folder, "macos-arm64")


@pytest.mark.parametrize("present", [
    (),
    ("macos-arm64", "linux-x86_64"),
    ("macos-arm64", "linux-aarch64", "linux-x86_64", "linux-riscv64"),
])
def test_a_bundle_folder_without_exactly_the_three_packs_is_refused(subject, tmp_path, present):
    root = tmp_path / "bundle"
    root.mkdir()
    for name in present:
        (root / name).mkdir()
    subject.args.node_bundle_dir = str(root)
    with pytest.raises(release.ReleaseError, match="exactly the macos-arm64, linux-aarch64 and linux-x86_64 packs"):
        subject.verify_node_bundle()
