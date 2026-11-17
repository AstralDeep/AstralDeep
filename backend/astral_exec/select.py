"""Backend selection for a validated policy: the data directory overlap check, the shadow refusal and the platform rows of the contract's table, in order.
It returns a Backend or a Refusal, reads no environment and has no override, so nothing turns a refusal into a run.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .constants import (
    BWRAP_PATHS,
    DARWIN_BASE,
    EXEC_SUBDIRECTORY,
    FACT_KEYS,
    LANDLOCK_ABI,
    LINUX_BASE,
)
from .errors import ContextError, Refusal, refuse
from .fold import assigned, contains, fold
from .paths import path_problem
from .policy import Policy

MOUNTINFO = "/proc/self/mountinfo"
_OCTAL_ESCAPES = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}


@dataclass(frozen=True)
class Backend:
    name: str
    bwrap: str | None = field(default=None, compare=False)

    @property
    def ok(self) -> bool:
        return True


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _facts(facts: Any) -> dict[str, Any]:
    if not isinstance(facts, Mapping):
        raise ContextError("facts must be an object")
    unknown = set(facts) - set(FACT_KEYS)
    if unknown:
        raise ContextError(f"unknown facts: {sorted(unknown)}")
    out = {"mode": "enforce", **facts}
    if not isinstance(out.get("platform"), str):
        raise ContextError("facts.platform must be a string")
    if out["mode"] not in ("enforce", "shadow"):
        raise ContextError("facts.mode must be enforce or shadow")
    if not _is_int(out.get("landlock_abi")) or out["landlock_abi"] < 0:
        raise ContextError("facts.landlock_abi must be a non-negative integer")
    if not _is_int(out.get("euid")) or out["euid"] < 0:
        raise ContextError("facts.euid must be a non-negative integer")
    for key in ("bwrap", "userns", "sandbox_exec", "app_sandboxed"):
        if not isinstance(out.get(key), bool):
            raise ContextError(f"facts.{key} must be a boolean")
    return out


def _data_dir(value: Any, platform: str) -> str:
    if not isinstance(value, str) or path_problem(value):
        raise ContextError("data_dir must be an absolute normalized path")
    if platform == "darwin" and not all(map(assigned, value)):
        raise ContextError("data_dir holds an unassigned code point")
    return value


def _overlaps_data(document: dict[str, Any], platform: str, data_dir: str) -> bool:
    base = {"darwin": DARWIN_BASE, "linux": LINUX_BASE}.get(platform, ())
    workspace = document["workspace"]
    roots = (*base, *document["reads"])
    if any(
        contains(root, data_dir, platform) or contains(data_dir, root, platform)
        for root in roots
    ):
        return True
    if contains(workspace, data_dir, platform):
        return True
    if contains(data_dir, workspace, platform):
        exec_root = fold(f"{data_dir}/{EXEC_SUBDIRECTORY}/", platform)
        return not fold(workspace, platform).startswith(exec_root)
    return False


def _unescape(field_value: str) -> str:
    out = []
    index = 0
    while index < len(field_value):
        char = field_value[index]
        if char == "\\" and field_value[index + 1 : index + 4] in _OCTAL_ESCAPES:
            out.append(_OCTAL_ESCAPES[field_value[index + 1 : index + 4]])
            index += 4
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _under(root: str, path: str) -> bool:
    return root == "/" or path == root or path.startswith(root + "/")


def _overlaps(one: str, other: str) -> bool:
    return _under(one, other) or _under(other, one)


def _parse_mountinfo(text: str) -> list[tuple[str, str, str]]:
    mounts = []
    for line in text.splitlines():
        parts = line.split(" ")
        if len(parts) < 5:
            continue
        mounts.append((parts[2], _unescape(parts[3]), _unescape(parts[4])))
    return mounts


def _translate(path: str, mounts: list[tuple[str, str, str]]) -> tuple[str, str]:
    covering = None
    for mount in mounts:
        if _under(mount[2], path) and (
            covering is None or len(mount[2]) >= len(covering[2])
        ):
            covering = mount
    if covering is None:
        raise OSError("no mount covers the path")
    device, root, point = covering
    remainder = path if point == "/" else path[len(point) :]
    in_fs = (root.rstrip("/") + "/" + remainder.lstrip("/")).rstrip("/") or "/"
    return device, in_fs


def mount_exposes_data_dir(
    text: str, roots: tuple[str, ...], data_dir: str, workspace: str | None = None
) -> bool:
    mounts = _parse_mountinfo(text)
    device, data_fs = _translate(data_dir, mounts)
    every = (*roots, workspace) if workspace else roots
    for mount_device, mount_root, mount_point in mounts:
        if (
            mount_device == device
            and any(_under(root, mount_point) for root in every)
            and _overlaps(mount_root, data_fs)
        ):
            return True
    for root in roots:
        root_device, root_fs = _translate(root, mounts)
        if root_device == device and _overlaps(root_fs, data_fs):
            return True
    if workspace:
        root_device, root_fs = _translate(workspace, mounts)
        exec_root = data_fs.rstrip("/") + f"/{EXEC_SUBDIRECTORY}/"
        return (
            root_device == device
            and _overlaps(root_fs, data_fs)
            and not root_fs.startswith(exec_root)
        )
    return False


def _mountinfo_refuses(document: dict[str, Any], data_dir: str) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        with open(MOUNTINFO, encoding="utf-8") as handle:
            text = handle.read()
        roots = (*LINUX_BASE, *document["reads"])
        return mount_exposes_data_dir(text, roots, data_dir, document["workspace"])
    except (OSError, ValueError):
        return True


def select(
    policy: Policy, facts: Mapping[str, Any], data_dir: str
) -> Backend | Refusal:
    if not isinstance(policy, Policy):
        raise TypeError("select takes a Policy loaded with Policy.load")
    if policy.loaded_for is None:
        raise ContextError("select needs a policy loaded with Policy.load")
    loaded_platform, loaded_dir, _home = policy.loaded_for
    if not isinstance(facts, Mapping) or facts.get("platform") != loaded_platform:
        raise ContextError("the policy was loaded for another platform")
    if data_dir != loaded_dir:
        raise ContextError("the policy was loaded for another data directory")
    outcome = _decide(policy, facts, data_dir)
    if isinstance(outcome, Backend) and outcome.name == "linux":
        path = getattr(facts, "bwrap_path", None)
        if path not in BWRAP_PATHS:
            return refuse("exec.backend_unavailable", "bwrap_missing")
        document = Policy.parse(policy.canonical_bytes(), "linux").document
        if _mountinfo_refuses(document, data_dir):
            return refuse("exec.policy_overlaps_data")
        return Backend("linux", path)
    return outcome


def _decide(
    policy: Policy, facts: Mapping[str, Any], data_dir: str
) -> Backend | Refusal:
    checked = _facts(facts)
    platform = checked["platform"]
    document = Policy.parse(policy.canonical_bytes(), platform).document
    data_dir = _data_dir(data_dir, platform)
    if _overlaps_data(document, platform, data_dir):
        return refuse("exec.policy_overlaps_data")
    if checked["mode"] == "shadow":
        return refuse("exec.shadow_refused")
    if platform == "linux":
        if checked["landlock_abi"] < LANDLOCK_ABI:
            return refuse(
                "exec.backend_unavailable",
                "landlock_abi",
                observed=checked["landlock_abi"],
                required=LANDLOCK_ABI,
            )
        if not checked["bwrap"]:
            return refuse("exec.backend_unavailable", "bwrap_missing")
        if not checked["userns"]:
            return refuse("exec.backend_unavailable", "userns_denied")
        if checked["euid"] == 0:
            return refuse("exec.backend_unavailable", "root_node")
        return Backend("linux")
    if platform == "darwin":
        if not checked["sandbox_exec"]:
            return refuse("exec.backend_unavailable", "sandbox_exec_missing")
        if checked["app_sandboxed"]:
            return refuse("exec.backend_unavailable", "app_sandboxed")
        return Backend("seatbelt")
    return refuse("exec.backend_unavailable", "platform_unsupported", platform=platform)
