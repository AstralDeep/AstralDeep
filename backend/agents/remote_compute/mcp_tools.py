#!/usr/bin/env python3
"""Unified verb registry for remote-compute-1 (feature 063).

ONE agent exposes BOTH the read-only verbs and the mutating verbs. The two verb
sets still live in their own modules, split by risk tier so each stays small and
reviewable:

- ``agents.remote_observe.mcp_tools`` — the 8 read-only verbs (``tools:read``).
- ``agents.remote_control.mcp_tools``  — the 8 mutating verbs (``tools:write`` /
  ``tools:system``).

This module unions them into the single registered agent. Merging the AGENTS does
NOT merge the SAFETY classes: every mutating verb keeps its declared destructive
classification and is gated per-verb by the durable confirmation mechanism
(``orchestrator/remote_confirmation.py``), now keyed on this agent's id. A read
verb (classification ``None``) passes the gate untouched; a destructive verb still
requires a fresh, single-use, user-issued approval before it runs.
"""
from __future__ import annotations

from agents.remote_control import mcp_tools as _control
from agents.remote_observe import mcp_tools as _observe


def register_deps(db, credmgr) -> None:
    """Wire the shared Database + CredentialManager into BOTH verb libraries."""
    _observe.register_deps(db, credmgr)
    _control.register_deps(db, credmgr)


# Union of the two risk-tiered registries. The verb names are disjoint (8 read +
# 8 mutating = 16), and each entry dict is the SAME object the source module
# built — so remote_control's ``destructive`` values remain identical (``is``) to
# ``remote_confirmation.DESTRUCTIVE_CLASSIFICATION`` (FR-028 no-drift holds).
TOOL_REGISTRY = {**_observe.TOOL_REGISTRY, **_control.TOOL_REGISTRY}
