"""Backend selection for a validated policy: the data directory overlap check, the shadow refusal and the platform rows of the contract's table, in order.
It returns a Backend or a Refusal, reads no environment and has no override, so nothing turns a refusal into a run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .constants import (
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


def select(
    policy: Policy, facts: Mapping[str, Any], data_dir: str
) -> Backend | Refusal:
    if not isinstance(policy, Policy):
        raise TypeError("select takes a Policy, parse the document first")
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
        return Backend("linux", getattr(facts, "bwrap_path", None))
    if platform == "darwin":
        if not checked["sandbox_exec"]:
            return refuse("exec.backend_unavailable", "sandbox_exec_missing")
        if checked["app_sandboxed"]:
            return refuse("exec.backend_unavailable", "app_sandboxed")
        return Backend("seatbelt")
    return refuse("exec.backend_unavailable", "platform_unsupported", platform=platform)
