"""Tests confirming the etf_tracker_1 agent is absent from every surface
(orchestrator/history_surface.py, knowledge_synthesis.py, local_agents.py,
orchestrator.py): directory, public catalog, retirement set, and history rows.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


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


def test_history_rows_name_no_agent_at_all():
    from orchestrator.history_surface import history_surface_components
    items = history_surface_components([
        {"id": "c1", "title": "Anything", "agent_id": "etf_tracker_1", "updated_at": 0},
    ])[0]["items"]
    assert items and all("icon" not in item for item in items)
    assert all("etf" not in str(value).lower() for item in items for value in item.values())
