"""Feature 040 (US3) — etf_tracker_1 retirement.

Verifies the agent is removed from every surface (directory, first-party public
catalog, retirement sets, history glyphs, and knowledge index. Historical
schema cleanup is part of Plane's supported 066.001 predecessor baseline and
is not re-run by Deep at application startup.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# Static surface checks (no DB) — FR-017
# ---------------------------------------------------------------------------

def test_etf_agent_directory_removed():
    assert not (BACKEND_DIR / "agents" / "etf_tracker_1").exists()


def test_etf_not_in_first_party_public_catalog():
    from orchestrator.local_agents import FIRST_PARTY_PUBLIC_AGENT_IDS

    assert "etf-tracker-1-1" not in FIRST_PARTY_PUBLIC_AGENT_IDS


def test_etf_in_retired_agent_ids():
    from orchestrator.orchestrator import RETIRED_AGENT_IDS
    assert "etf-tracker-1-1" in RETIRED_AGENT_IDS
    assert "etf_tracker_1" in RETIRED_AGENT_IDS


def test_etf_knowledge_stem_retired():
    from orchestrator.knowledge_synthesis import RETIRED_KNOWLEDGE_STEMS
    assert "etf_tracker" in RETIRED_KNOWLEDGE_STEMS


def test_etf_history_icon_removed():
    from orchestrator.history_surface import _AGENT_ICONS
    assert "etf_tracker_1" not in _AGENT_ICONS
