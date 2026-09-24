"""Tests for personalization/skills_reco.py and panels.py: relevance ranking,
authorized-tools-win-ties ordering, limit handling, the profession/skills/personality
onboarding panels, and profile-update schema validation.
"""

from __future__ import annotations

from personalization.panels import (
    build_personality_panel,
    build_profession_panel,
    build_skills_panel,
)
from personalization.skills_reco import recommend_skills
from personalization.schemas import ProfileUpdateRequest

import pytest


_TOOLS = [
    {"agent_id": "grants", "tool_name": "search_grants", "description": "Find NSF and NIH research grant funding opportunities", "scope": "tools:search", "available": True},
    {"agent_id": "general", "tool_name": "graph_patient_data", "description": "Plot charts and graphs of data", "scope": "tools:read", "available": True},
    {"agent_id": "classify", "tool_name": "start_training_job", "description": "Train a machine learning classification model", "scope": "tools:write", "available": False},
]


def test_recommend_ranks_by_relevance():
    ranked = recommend_skills("Research grant administrator", ["track grant funding deadlines"], _TOOLS)
    assert ranked[0]["tool_name"] == "search_grants"
    assert ranked[0]["score"] >= 1


def test_recommend_prefers_authorized_on_ties():
    ranked = recommend_skills(None, None, _TOOLS)
    assert ranked[0].get("available", True) is True
    assert ranked[-1]["tool_name"] == "start_training_job"


def test_recommend_respects_limit():
    ranked = recommend_skills("x", [], _TOOLS, limit=2)
    assert len(ranked) == 2


def test_profession_panel_is_param_picker():
    resp = build_profession_panel({"profession": "Nurse", "goals": ["a", "b"]})
    comps = resp["_ui_components"]
    assert comps[0]["type"] == "param_picker"
    names = [f["name"] for f in comps[0]["fields"]]
    assert "profession" in names and "goals" in names


def test_skills_panel_marks_unauthorized():
    ranked = recommend_skills("ml engineer", ["train models"], _TOOLS)
    resp = build_skills_panel(ranked)
    options = resp["_ui_components"][0]["fields"][0]["options"]
    assert any("needs permission" in o for o in options)


def test_skills_panel_empty_shows_alert():
    resp = build_skills_panel([])
    assert resp["_ui_components"][0]["type"] == "alert"


def test_personality_panel_has_style_fields():
    resp = build_personality_panel()
    names = [f["name"] for f in resp["_ui_components"][0]["fields"]]
    assert names == ["tone", "directness", "verbosity", "notes"]


def test_profile_update_schema_rejects_too_many_goals():
    with pytest.raises(Exception):
        ProfileUpdateRequest(goals=[f"g{i}" for i in range(21)])


def test_profile_update_schema_accepts_valid():
    req = ProfileUpdateRequest(profession="Clinician", goals=["x"], dreaming_enabled=False)
    assert req.profession == "Clinician"
    assert req.dreaming_enabled is False
