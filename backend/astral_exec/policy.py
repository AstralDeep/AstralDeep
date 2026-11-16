"""The execution policy model: strict parsing and validation in the contract's order, the LETS-CJ/1 digest and the loader that checks canonical on-disk spellings.
select.py and the backend renderers take a Policy from here, and the reference vectors pin every outcome.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from . import canonjson, paths
from .constants import (
    HOST_FIELDS,
    HOST_PORTS,
    LIMITS,
    MAX_HOSTS,
    MAX_READS,
    TIME_FIELDS,
    TOP_FIELDS,
    WALL_MS,
)
from .errors import ExecRefused, PolicyError, refuse
from .fold import assigned, contains, fold
from .paths import path_problem

_HOST_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _object(value: Any, fields: tuple[str, ...], where: str) -> None:
    if not isinstance(value, dict):
        raise PolicyError("policy.malformed", f"{where} must be an object")
    if set(value) - set(fields):
        raise PolicyError("policy.unknown_field", where)
    if set(fields) - set(value):
        raise PolicyError("policy.malformed", f"{where} is missing a field")


def _checked_path(value: Any, where: str, platform: str | None) -> str:
    if not isinstance(value, str):
        raise PolicyError("policy.malformed", f"{where} must be a string")
    problem = path_problem(value)
    if problem is None and platform == "darwin" and not all(map(assigned, value)):
        problem = "unassigned code point"
    if problem:
        raise PolicyError("policy.path_invalid", f"{where}: {problem}")
    return value


def _checked_host(value: Any, where: str) -> None:
    _object(value, HOST_FIELDS, where)
    host, port = value["host"], value["port"]
    if not isinstance(host, str) or not _is_int(port):
        raise PolicyError("policy.malformed", f"{where} has a wrong type")
    labels = host.split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or not all(_HOST_LABEL.fullmatch(label) for label in labels)
        or _NUMERIC_LABEL.fullmatch(labels[-1])
    ):
        raise PolicyError("policy.host_invalid", f"{where}.host")
    if port not in HOST_PORTS:
        raise PolicyError("policy.host_invalid", f"{where}.port")


def _validate_reads(policy: dict[str, Any], platform: str | None) -> None:
    reads = policy["reads"]
    if not isinstance(reads, list):
        raise PolicyError("policy.malformed", "reads must be a list")
    if len(reads) > MAX_READS:
        raise PolicyError("policy.too_many_entries", "reads")
    previous = None
    seen: set[str] = set()
    for index, path in enumerate(reads):
        _checked_path(path, f"reads[{index}]", platform)
        if previous is not None and path <= previous:
            raise PolicyError(
                "policy.path_invalid", f"reads[{index}] not sorted and unique"
            )
        folded = fold(path, platform)
        if folded in seen:
            raise PolicyError(
                "policy.path_invalid", f"reads[{index}] repeats a read root"
            )
        seen.add(folded)
        previous = path


def _validate_workspace(policy: dict[str, Any], platform: str | None) -> None:
    workspace = _checked_path(policy["workspace"], "workspace", platform)
    for path in policy["reads"]:
        if contains(workspace, path, platform) or contains(path, workspace, platform):
            raise PolicyError(
                "policy.path_invalid", "a read root overlaps the workspace"
            )


def _validate_hosts(policy: dict[str, Any]) -> None:
    hosts = policy["hosts"]
    if not isinstance(hosts, list):
        raise PolicyError("policy.malformed", "hosts must be a list")
    if len(hosts) > MAX_HOSTS:
        raise PolicyError("policy.too_many_entries", "hosts")
    previous_key = None
    for index, entry in enumerate(hosts):
        _checked_host(entry, f"hosts[{index}]")
        key = (entry["host"], entry["port"])
        if previous_key is not None and key <= previous_key:
            raise PolicyError(
                "policy.host_invalid", f"hosts[{index}] not sorted and unique"
            )
        previous_key = key


def _validate_limits(policy: dict[str, Any]) -> None:
    limits = policy["limits"]
    _object(limits, tuple(LIMITS), "limits")
    for name, (low, high) in LIMITS.items():
        if not _is_int(limits[name]):
            raise PolicyError("policy.malformed", f"limits.{name} must be an integer")
        if not low <= limits[name] <= high:
            raise PolicyError("policy.limit_out_of_range", name)
    wall = policy["time"]
    _object(wall, TIME_FIELDS, "time")
    if not _is_int(wall["wall_ms"]):
        raise PolicyError("policy.malformed", "time.wall_ms must be an integer")
    if not WALL_MS[0] <= wall["wall_ms"] <= WALL_MS[1]:
        raise PolicyError("policy.time_out_of_range", "wall_ms")


def validate(document: Any, platform: str | None) -> dict[str, Any]:
    _object(document, TOP_FIELDS, "policy")
    if not _is_int(document["v"]) or document["v"] != 1:
        raise PolicyError("policy.malformed", "v must be 1")
    _validate_reads(document, platform)
    _validate_workspace(document, platform)
    _validate_hosts(document)
    _validate_limits(document)
    return document


def _document(data: Any) -> Any:
    if isinstance(data, (bytes, str)):
        return canonjson.strict_loads(data)
    if isinstance(data, Mapping):
        return data
    raise canonjson.CanonicalError("a policy is JSON text or an object")


_VALIDATED = object()


class Policy:
    __slots__ = ("_canonical",)

    def __init__(self, token: object, canonical_bytes: bytes) -> None:
        if token is not _VALIDATED:
            raise TypeError("build a Policy with Policy.parse or Policy.load")
        self._canonical = canonical_bytes

    @classmethod
    def parse(
        cls, data: bytes | str | Mapping[str, Any], platform: str | None
    ) -> Policy:
        try:
            document = _document(data)
        except canonjson.CanonicalError as error:
            raise PolicyError("policy.malformed", str(error)) from error
        validate(document, platform)
        return cls(_VALIDATED, canonjson.canonical(document))

    @classmethod
    def load(
        cls,
        data: bytes | str | Mapping[str, Any],
        platform: str,
        data_dir: str | None = None,
    ) -> Policy:
        return cls._load(data, platform, data_dir, paths.canonical_path, os.stat)

    @classmethod
    def _load(
        cls,
        data: bytes | str | Mapping[str, Any],
        platform: str,
        data_dir: str | None,
        resolve: Callable[[str, str], str],
        stat: Callable[[str], Any],
    ) -> Policy:
        policy = cls.parse(data, platform)
        document = policy.document
        named = [(f"reads[{i}]", path) for i, path in enumerate(document["reads"])]
        for where, path in [*named, ("workspace", document["workspace"])]:
            try:
                resolved = resolve(path, platform)
            except (OSError, ValueError) as error:
                raise PolicyError(
                    "policy.path_invalid", f"{where}: cannot resolve ({error})"
                ) from error
            if resolved != path:
                raise PolicyError(
                    "policy.path_invalid", f"{where}: not the canonical path"
                )
        if data_dir is not None:
            checked = paths._check_context_path(data_dir, "data_dir", platform, resolve)
            if paths.aliases_data_dir(document, platform, checked, stat):
                raise ExecRefused(refuse("exec.policy_overlaps_data"))
        return policy

    @property
    def document(self) -> dict[str, Any]:
        return json.loads(self._canonical)

    def canonical_bytes(self) -> bytes:
        return self._canonical

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self._canonical).hexdigest()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Policy) and other._canonical == self._canonical

    def __hash__(self) -> int:
        return hash(self._canonical)

    def __repr__(self) -> str:
        return f"Policy({self.digest()})"
