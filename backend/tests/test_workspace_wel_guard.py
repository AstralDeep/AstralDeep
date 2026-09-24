"""Tests for the workspace layer's refusal of ephemeral wel_ identities
(backend/orchestrator/workspace.py): a wel_-namespaced id is discarded and falls back
to fingerprint resolution.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.workspace import WorkspaceManager, fingerprint  # noqa: E402


def _wm() -> WorkspaceManager:
    return WorkspaceManager(SimpleNamespace(db=None))


def test_wel_component_id_is_refused_and_falls_back_to_fingerprint():
    comp = {"type": "card", "component_id": "wel_hero",
            "_source_agent": "a1", "_source_tool": "t1", "_source_params": {"x": 1}}
    cid = _wm().resolve_identity(comp)
    assert not cid.startswith("wel_")
    assert cid == fingerprint("a1", "t1", {"x": 1})
    assert comp["component_id"] == cid


def test_wel_author_id_is_refused_never_au_prefixed():
    comp = {"type": "card", "id": "wel_examples",
            "_source_agent": "a1", "_source_tool": "t1", "_source_params": None}
    cid = _wm().resolve_identity(comp)
    assert not cid.startswith(("wel_", "au_wel_"))
    assert cid.startswith("wc_")


def test_normal_identities_unchanged():
    wm = _wm()
    assert wm.resolve_identity({"component_id": "wc_abc123"}) == "wc_abc123"
    assert wm.resolve_identity({"id": "wc_echoed11"}) == "wc_echoed11"
    assert wm.resolve_identity({"id": "mychart"}) == "au_mychart"
