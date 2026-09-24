"""Tests for personalization/service.py: empty-user fragment is empty, personality
renders last and subordinate to the compliance preamble, and a profile without
personality omits the style block.
"""

from __future__ import annotations

from personalization.service import PERSONALITY_PREAMBLE, PersonalizationService


class _FakeRepo:
    def __init__(self, profile=None, memory=None):
        self._profile = profile
        self._memory = memory or []

    def list_memory(self, user_id):
        return list(self._memory)

    def get_profile(self, user_id):
        return self._profile


def _service_with(profile=None, memory=None) -> PersonalizationService:
    svc = PersonalizationService(db=None)
    svc.repo = _FakeRepo(profile=profile, memory=memory)
    return svc


def test_empty_user_returns_empty_fragment():
    svc = _service_with()
    assert svc.build_prompt_fragment(None) == ""
    assert svc.build_prompt_fragment("u1") == ""


def test_personality_is_last_and_subordinate():
    profile = {
        "profession": "Clinical researcher",
        "goals": ["Track grant deadlines"],
        "personality": {"tone": "concise", "directness": "high", "notes": "No filler."},
    }
    memory = [{"category": "preference", "value": "Prefers bullet points"}]
    svc = _service_with(profile=profile, memory=memory)

    fragment = svc.build_prompt_fragment("u1", skill_lines=["grants:search_grants — find funding"])

    assert "WHAT YOU REMEMBER" in fragment
    assert "USER CONTEXT" in fragment
    assert "ENABLED SKILLS" in fragment
    assert PERSONALITY_PREAMBLE in fragment

    i_mem = fragment.index("WHAT YOU REMEMBER")
    i_ctx = fragment.index("USER CONTEXT")
    i_skill = fragment.index("ENABLED SKILLS")
    i_persona = fragment.index(PERSONALITY_PREAMBLE)
    assert i_mem < i_ctx < i_skill < i_persona, fragment

    assert fragment.index("concise") > i_persona


def test_profile_without_personality_omits_style_block():
    profile = {"profession": "Nurse", "goals": [], "personality": {}}
    svc = _service_with(profile=profile)
    fragment = svc.build_prompt_fragment("u1")
    assert "USER CONTEXT" in fragment
    assert PERSONALITY_PREAMBLE not in fragment
