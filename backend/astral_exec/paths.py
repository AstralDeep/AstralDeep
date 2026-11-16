"""Disk canonicalization of paths: F_GETPATH on an open descriptor on macOS and a strict realpath elsewhere.
policy.py uses it so a stated spelling that differs from the disk's spelling is refused, and callers use it for the home and data directories.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any

from .constants import DARWIN_BASE, EXEC_SUBDIRECTORY, LINUX_BASE
from .errors import ContextError
from .fold import assigned

_PATH_MAX = 1024
_O_EVTONLY = getattr(os, "O_EVTONLY", 0x8000)
_MAX_PATH_BYTES = 4096


def path_problem(path: str) -> str | None:
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        return "not UTF-8"
    if len(encoded) > _MAX_PATH_BYTES:
        return "too long"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        return "control character"
    if not path.startswith("/"):
        return "must be absolute"
    if any(part in ("", ".", "..") for part in path[1:].split("/")):
        return "not normalized"
    return None


def canonical_path(path: str, platform: str) -> str:
    if platform == "darwin" and sys.platform == "darwin":
        import fcntl

        fd = os.open(path, _O_EVTONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(_PATH_MAX))
        finally:
            os.close(fd)
        return raw.split(b"\0", 1)[0].decode("utf-8")
    return os.path.realpath(path, strict=True)


def _check_context_path(
    value: Any, name: str, platform: str, resolve: Callable[[str, str], str]
) -> str:
    if not isinstance(value, str) or path_problem(value):
        raise ContextError(f"{name} must be an absolute normalized path")
    if platform == "darwin" and not all(map(assigned, value)):
        raise ContextError(f"{name} holds an unassigned code point")
    try:
        resolved = resolve(value, platform)
    except (OSError, ValueError) as error:
        raise ContextError(f"{name} cannot be resolved: {error}") from error
    if resolved != value:
        raise ContextError(f"{name} is not the canonical path")
    return value


def canonical_context_path(value: Any, name: str, platform: str) -> str:
    return _check_context_path(value, name, platform, canonical_path)


def _identities(path: str, stat: Callable[[str], Any]) -> list[tuple[int, int]]:
    parts = path.split("/")[1:]
    chain = []
    for end in range(len(parts) + 1):
        try:
            info = stat("/" + "/".join(parts[:end]))
        except OSError:
            if end == len(parts):
                return []
            continue
        chain.append((info.st_dev, info.st_ino))
    return chain


def aliases_data_dir(
    document: dict[str, Any],
    platform: str,
    data_dir: str,
    stat: Callable[[str], Any] = os.stat,
) -> bool:
    data_chain = _identities(data_dir, stat)
    if not data_chain:
        return False
    data_id = data_chain[-1]
    base = {"darwin": DARWIN_BASE, "linux": LINUX_BASE}.get(platform, ())
    for root in (*base, *document["reads"]):
        chain = _identities(root, stat)
        if chain and (data_id in chain or chain[-1] in data_chain):
            return True
    chain = _identities(document["workspace"], stat)
    if not chain:
        return False
    if chain[-1] in data_chain:
        return True
    if data_id not in chain:
        return False
    exec_chain = _identities(f"{data_dir}/{EXEC_SUBDIRECTORY}", stat)
    exec_id = exec_chain[-1] if len(exec_chain) > len(data_chain) else None
    return exec_id is None or exec_id not in chain or exec_id == chain[-1]
