"""Disk canonicalization of paths: F_GETPATH on an open descriptor on macOS and a strict realpath elsewhere.
policy.py uses it so a stated spelling that differs from the disk's spelling is refused, and callers use it for the home and data directories.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any

from .errors import ContextError
from .fold import assigned

_PATH_MAX = 1024
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

        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(_PATH_MAX))
        finally:
            os.close(fd)
        return raw.split(b"\0", 1)[0].decode("utf-8")
    return os.path.realpath(path, strict=True)


def canonical_context_path(
    value: Any,
    name: str,
    platform: str,
    *,
    resolve: Callable[[str, str], str] = canonical_path,
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
