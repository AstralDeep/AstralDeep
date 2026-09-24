#!/usr/bin/env python3
"""Unions remote_observe's 9 read-only verbs and remote_control's 9 mutating verbs into
the registry the remote-compute-1 agent serves; register_deps() wires both libraries'
shared dependencies.
"""
from __future__ import annotations

from agents.remote_control import mcp_tools as _control
from agents.remote_observe import mcp_tools as _observe


def register_deps(plane_source, credmgr, blob_store) -> None:
    _observe.register_deps(plane_source, credmgr)
    _control.register_deps(plane_source, credmgr, blob_store)


# Same dict objects as the source registries — not copies
TOOL_REGISTRY = {**_observe.TOOL_REGISTRY, **_control.TOOL_REGISTRY}
