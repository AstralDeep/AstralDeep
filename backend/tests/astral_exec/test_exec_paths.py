"""Covers the disk canonical form check that the policy loader and the context path helper apply, with an injected resolver and with real symlinks.
The reference validator cannot read a disk, so this is where a stated spelling that differs from the disk's spelling is refused.
"""

from __future__ import annotations

import inspect
import os
import sys
import types

import pytest
from astral_exec import ContextError, Policy, PolicyError
from astral_exec import ExecRefused
from astral_exec.paths import (
    _check_context_path,
    aliases_data_dir,
    canonical_context_path,
    canonical_path,
)
from exec_vectors import minimal_policy

IS_DARWIN = sys.platform == "darwin"


def _load(data, platform, resolve, data_dir=None, stat=os.stat):
    return Policy._load(data, platform, data_dir, resolve, stat)


def _policy(reads, workspace="/w/ws"):
    return minimal_policy(reads=reads, workspace=workspace)


def test_load_accepts_paths_that_are_their_own_canonical_form():
    policy = _load(_policy(["/a/b", "/c"]), "linux", lambda path, platform: path)
    assert policy.document["reads"] == ["/a/b", "/c"]


def test_load_refuses_a_read_root_whose_spelling_differs():
    resolve = lambda path, platform: path.replace("/link", "/real")  # noqa: E731
    with pytest.raises(PolicyError) as caught:
        _load(_policy(["/link/x"]), "linux", resolve)
    assert caught.value.code == "policy.path_invalid"
    assert "reads[0]" in caught.value.detail


def test_load_refuses_a_workspace_whose_spelling_differs():
    resolve = lambda path, platform: "/elsewhere" if path == "/w/ws" else path  # noqa: E731
    with pytest.raises(PolicyError) as caught:
        _load(_policy([]), "linux", resolve)
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
        _load(_policy(["/a"]), "linux", resolve)
    assert caught.value.code == "policy.path_invalid"


def test_load_reports_a_validation_error_before_touching_the_disk():
    def resolve(path, platform):
        raise AssertionError("disk must not be read for an invalid policy")

    with pytest.raises(PolicyError) as caught:
        _load(minimal_policy(v=2), "linux", resolve)
    assert caught.value.code == "policy.malformed"


def test_load_resolves_each_path_with_the_platform_given():
    seen = []

    def resolve(path, platform):
        seen.append((path, platform))
        return path

    _load(_policy(["/a"]), "darwin", resolve)
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
        _check_context_path("/a", "home", "linux", lambda p, pl: "/b")
    assert _check_context_path("/a", "home", "linux", lambda p, pl: p) == "/a"


def test_context_path_refuses_text_that_is_not_utf8_or_is_too_long():
    for bad in ("/a/\ud800", "/" + "a" * 5000):
        with pytest.raises(ContextError):
            _check_context_path(bad, "home", "linux", lambda p, pl: p)


def test_context_path_refuses_an_unassigned_code_point_on_darwin():
    with pytest.raises(ContextError):
        _check_context_path("/Users/t/\u0378", "home", "darwin", lambda p, pl: p)
    assert _check_context_path("/Users/t/\u0378", "home", "linux", lambda p, pl: p)


def test_the_public_loader_and_context_helper_take_no_resolver():
    assert "resolve" not in inspect.signature(Policy.load).parameters
    assert "resolve" not in inspect.signature(canonical_context_path).parameters


def test_a_policy_cannot_be_built_without_validation():
    with pytest.raises(TypeError):
        Policy(object(), b"{}")
    with pytest.raises(TypeError):
        Policy(b"{}")  # type: ignore[call-arg]


class _Disk:
    def __init__(self, inodes):
        self.inodes = inodes

    def __call__(self, path):
        if path not in self.inodes:
            raise FileNotFoundError(path)
        return types.SimpleNamespace(st_dev=1, st_ino=self.inodes[path])


def _disk(**extra):
    inodes = {"/": 1, "/var": 2, "/var/lib": 3, "/var/lib/node": 4, "/w": 5, "/w/ws": 6}
    inodes.update(extra)
    return _Disk(inodes)


def test_identity_check_passes_for_unrelated_paths():
    doc = _policy([], "/w/ws")
    assert aliases_data_dir(doc, "linux", "/var/lib/node", _disk()) is False


def test_a_bind_mounted_alias_of_the_data_dir_is_caught_as_a_read_root():
    disk = _disk(**{"/mnt": 7, "/mnt/alias": 4})
    doc = _policy(["/mnt/alias"], "/w/ws")
    assert aliases_data_dir(doc, "linux", "/var/lib/node", disk) is True


def test_a_bind_mount_of_an_ancestor_of_the_data_dir_is_caught():
    disk = _disk(**{"/mnt": 7, "/mnt/alias": 3})
    doc = _policy(["/mnt/alias"], "/w/ws")
    assert aliases_data_dir(doc, "linux", "/var/lib/node", disk) is True


def test_a_read_root_inside_the_data_dir_by_identity_is_caught():
    disk = _disk(**{"/mnt": 7, "/mnt/alias": 8, "/mnt/alias/inner": 9})
    doc = _policy(["/mnt/alias/inner"], "/w/ws")
    assert aliases_data_dir(doc, "linux", "/var/lib/node", disk) is False
    inside = _disk(**{"/mnt": 7, "/mnt/alias": 4, "/mnt/alias/inner": 9})
    assert aliases_data_dir(
        _policy(["/mnt/alias/inner"], "/w/ws"), "linux", "/var/lib/node", inside
    )


def test_a_base_root_that_aliases_the_data_dir_is_caught():
    disk = _disk(**{"/usr": 4})
    assert (
        aliases_data_dir(_policy([], "/w/ws"), "linux", "/var/lib/node", disk) is True
    )


def test_a_workspace_aliasing_or_containing_the_data_dir_is_caught():
    equal = _disk(**{"/mnt": 7, "/mnt/ws": 4})
    assert aliases_data_dir(_policy([], "/mnt/ws"), "linux", "/var/lib/node", equal)
    contains = _disk(**{"/mnt": 7, "/mnt/ws": 2})
    assert aliases_data_dir(_policy([], "/mnt/ws"), "linux", "/var/lib/node", contains)


def test_a_workspace_inside_the_data_dir_must_be_under_exec_by_identity():
    base = {
        "/mnt": 7,
        "/mnt/alias": 4,
        "/mnt/alias/exec": 10,
        "/mnt/alias/exec/job": 11,
        "/mnt/alias/other": 12,
        "/var/lib/node/exec": 10,
    }
    disk = _disk(**base)
    assert (
        aliases_data_dir(
            _policy([], "/mnt/alias/exec/job"), "linux", "/var/lib/node", disk
        )
        is False
    )
    assert (
        aliases_data_dir(
            _policy([], "/mnt/alias/other"), "linux", "/var/lib/node", disk
        )
        is True
    )
    assert (
        aliases_data_dir(_policy([], "/mnt/alias/exec"), "linux", "/var/lib/node", disk)
        is True
    )
    no_exec = _disk(**{k: v for k, v in base.items() if k != "/var/lib/node/exec"})
    assert (
        aliases_data_dir(
            _policy([], "/mnt/alias/exec/job"), "linux", "/var/lib/node", no_exec
        )
        is True
    )


def test_a_missing_data_dir_has_no_identity_to_alias():
    assert (
        aliases_data_dir(_policy([], "/w/ws"), "linux", "/nowhere/at/all", _disk())
        is False
    )


def test_load_with_a_data_dir_refuses_an_alias_with_the_overlap_refusal():
    disk = _disk(**{"/mnt": 7, "/mnt/alias": 4})
    with pytest.raises(ExecRefused) as caught:
        _load(_policy(["/mnt/alias"]), "linux", lambda p, pl: p, "/var/lib/node", disk)
    assert caught.value.code == "exec.policy_overlaps_data"
    ok = _load(_policy(["/a"]), "linux", lambda p, pl: p, "/var/lib/node", disk)
    assert ok.document["reads"] == ["/a"]


def test_load_with_a_non_canonical_data_dir_is_a_context_error():
    with pytest.raises(ContextError):
        _load(
            _policy([]),
            "linux",
            lambda p, pl: "/elsewhere" if p == "/var/lib/node" else p,
            "/var/lib/node",
        )


def test_real_load_with_a_data_dir_accepts_a_sibling_and_refuses_the_same_directory(
    tmp_path,
):
    platform = "darwin" if IS_DARWIN else "linux"
    data = tmp_path / "data"
    data.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    Policy.load(_policy([str(other)], str(work)), platform, str(data))
    with pytest.raises(ExecRefused):
        Policy.load(_policy([str(data)], str(work)), platform, str(data))
