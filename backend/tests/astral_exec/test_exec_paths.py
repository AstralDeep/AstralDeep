"""Covers the disk canonical form check that the policy loader and the context path helper apply, with an injected resolver and with real symlinks.
The reference validator cannot read a disk, so this is where a stated spelling that differs from the disk's spelling is refused.
"""

from __future__ import annotations

import os
import sys

import pytest
from astral_exec import ContextError, Policy, PolicyError
from astral_exec.paths import canonical_context_path, canonical_path
from exec_vectors import minimal_policy

IS_DARWIN = sys.platform == "darwin"


def _policy(reads, workspace="/w/ws"):
    return minimal_policy(reads=reads, workspace=workspace)


def test_load_accepts_paths_that_are_their_own_canonical_form():
    policy = Policy.load(
        _policy(["/a/b", "/c"]), "linux", resolve=lambda path, platform: path
    )
    assert policy.document["reads"] == ["/a/b", "/c"]


def test_load_refuses_a_read_root_whose_spelling_differs():
    resolve = lambda path, platform: path.replace("/link", "/real")  # noqa: E731
    with pytest.raises(PolicyError) as caught:
        Policy.load(_policy(["/link/x"]), "linux", resolve=resolve)
    assert caught.value.code == "policy.path_invalid"
    assert "reads[0]" in caught.value.detail


def test_load_refuses_a_workspace_whose_spelling_differs():
    resolve = lambda path, platform: "/elsewhere" if path == "/w/ws" else path  # noqa: E731
    with pytest.raises(PolicyError) as caught:
        Policy.load(_policy([]), "linux", resolve=resolve)
    assert caught.value.code == "policy.path_invalid"
    assert "workspace" in caught.value.detail


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("x"), PermissionError("x"), OSError("x"), ValueError("x")],
)
def test_load_refuses_a_path_the_disk_cannot_resolve(error):
    def resolve(path, platform):
        raise error

    with pytest.raises(PolicyError) as caught:
        Policy.load(_policy(["/a"]), "linux", resolve=resolve)
    assert caught.value.code == "policy.path_invalid"


def test_load_reports_a_validation_error_before_touching_the_disk():
    def resolve(path, platform):
        raise AssertionError("disk must not be read for an invalid policy")

    with pytest.raises(PolicyError) as caught:
        Policy.load(minimal_policy(v=2), "linux", resolve=resolve)
    assert caught.value.code == "policy.malformed"


def test_load_resolves_each_path_with_the_platform_given():
    seen = []

    def resolve(path, platform):
        seen.append((path, platform))
        return path

    Policy.load(_policy(["/a"]), "darwin", resolve=resolve)
    assert seen == [("/a", "darwin"), ("/w/ws", "darwin")]


def test_real_symlinks_are_refused_and_real_directories_accepted(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    work = tmp_path / "work"
    work.mkdir()
    platform = "darwin" if IS_DARWIN else "linux"
    ok = Policy.load(_policy([str(real)], str(work)), platform)
    assert ok.document["reads"] == [str(real)]
    with pytest.raises(PolicyError) as caught:
        Policy.load(_policy([str(link)], str(work)), platform)
    assert caught.value.code == "policy.path_invalid"
    with pytest.raises(PolicyError):
        Policy.load(_policy([str(tmp_path / "missing")], str(work)), platform)


def test_canonical_path_resolves_symlinks_and_dotdot_free_paths(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    platform = "darwin" if IS_DARWIN else "linux"
    assert canonical_path(str(tmp_path / "link"), platform) == str(real)
    assert canonical_path(str(real), platform) == str(real)


def test_canonical_path_resolves_a_regular_file(tmp_path):
    target = tmp_path / "tool.py"
    target.write_text("x")
    platform = "darwin" if IS_DARWIN else "linux"
    assert canonical_path(str(target), platform) == str(target)


def test_canonical_path_does_not_block_on_a_fifo(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    platform = "darwin" if IS_DARWIN else "linux"
    assert canonical_path(str(fifo), platform) == str(fifo)


def test_canonical_path_of_a_missing_path_is_an_os_error(tmp_path):
    with pytest.raises(OSError):
        canonical_path(str(tmp_path / "nope"), "linux")


def test_context_path_accepts_a_canonical_directory(tmp_path):
    platform = "darwin" if IS_DARWIN else "linux"
    assert canonical_context_path(str(tmp_path), "home", platform) == str(tmp_path)


def test_context_path_refuses_a_symlink_a_relative_path_and_a_missing_path(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "l").symlink_to(tmp_path / "t")
    platform = "darwin" if IS_DARWIN else "linux"
    for bad in (
        str(tmp_path / "l"),
        "relative/dir",
        str(tmp_path / "gone"),
        "/",
        5,
        None,
    ):
        with pytest.raises(ContextError):
            canonical_context_path(bad, "data_dir", platform)  # type: ignore[arg-type]


def test_context_path_uses_an_injected_resolver():
    with pytest.raises(ContextError, match="home"):
        canonical_context_path("/a", "home", "linux", resolve=lambda p, pl: "/b")
    assert (
        canonical_context_path("/a", "home", "linux", resolve=lambda p, pl: p) == "/a"
    )


def test_context_path_refuses_text_that_is_not_utf8_or_is_too_long():
    for bad in ("/a/\ud800", "/" + "a" * 5000):
        with pytest.raises(ContextError):
            canonical_context_path(bad, "home", "linux", resolve=lambda p, pl: p)


def test_context_path_refuses_an_unassigned_code_point_on_darwin():
    with pytest.raises(ContextError):
        canonical_context_path(
            "/Users/t/\u0378", "home", "darwin", resolve=lambda p, pl: p
        )
    assert canonical_context_path(
        "/Users/t/\u0378", "home", "linux", resolve=lambda p, pl: p
    )
