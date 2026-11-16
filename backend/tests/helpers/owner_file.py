"""Writes a node owner file the way an earlier build left it, so tests can stand up legacy and existing bindings that no product path writes any more.
It uses the data directory from shared/node_identity.py and returns the exact bytes written."""

from __future__ import annotations

import os

from shared import node_identity


def seed_owner(value: str) -> bytes:
    directory = node_identity.data_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / node_identity.OWNER_FILE
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
    return path.read_bytes()
