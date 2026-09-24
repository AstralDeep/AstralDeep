"""Tests for orchestrator/onboarding_submit.py: ParamPicker submit interpretation for
profile/personality persistence and scope-gated skills submission.
"""

import asyncio
import sys
import types
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import onboarding_submit  # noqa: E402


class _FakeRepo:
    def __init__(self):
        self.profile = {}

    def upsert_profile(self, user_id, *, profession=None, goals=None,
                       personality=None, dreaming_enabled=None):
        if profession is not None:
            self.profile["profession"] = profession
        if goals is not None:
            self.profile["goals"] = goals
        if personality is not None:
            self.profile["personality"] = personality
        return self.profile


class _FakeTP:
    def __init__(self, authorized=True):
        self._authorized = authorized
        self.enabled = []

    def get_tool_scope(self, agent_id, tool_name):
        return "tools:read"

    def is_scope_enabled(self, user_id, agent_id, scope):
        return self._authorized

    def set_skill_enabled(self, user_id, agent_id, tool_name, enabled):
        self.enabled.append((agent_id, tool_name, enabled))


def _fake_orch(repo, tp):
    renders = []

    async def send_ui_render(ws, components, target="canvas"):
        renders.append((components, target))

    orch = types.SimpleNamespace(
        personalization_service=types.SimpleNamespace(repo=repo),
        tool_permissions=tp,
        send_ui_render=send_ui_render,
    )
    orch._renders = renders
    return orch


def test_detection():
    assert onboarding_submit.is_onboarding_submit("Save my personalization profile — profession: x; goals: y")
    assert onboarding_submit.is_onboarding_submit("Enable these skills for me: a:b (read)")
    assert onboarding_submit.is_onboarding_submit("Set my assistant personality — tone: warm")
    assert not onboarding_submit.is_onboarding_submit("hello there")


def test_profile_submit_persists():
    repo, tp = _FakeRepo(), _FakeTP()
    orch = _fake_orch(repo, tp)
    handled = asyncio.run(onboarding_submit.handle_submit(
        orch, object(), "u1",
        "Save my personalization profile — profession: Researcher; goals: grants, papers",
        "c1"))
    assert handled is True
    assert repo.profile["profession"] == "Researcher"
    assert repo.profile["goals"] == ["grants", "papers"]


def test_personality_submit_persists():
    repo, tp = _FakeRepo(), _FakeTP()
    orch = _fake_orch(repo, tp)
    handled = asyncio.run(onboarding_submit.handle_submit(
        orch, object(), "u1",
        "Set my assistant personality — tone: warm; directness: high; verbosity: low; notes: none",
        "c1"))
    assert handled is True
    assert repo.profile["personality"]["tone"] == "warm"
    assert repo.profile["personality"]["directness"] == "high"


def test_skills_submit_scope_gated():
    repo, tp = _FakeRepo(), _FakeTP(authorized=True)
    orch = _fake_orch(repo, tp)
    handled = asyncio.run(onboarding_submit.handle_submit(
        orch, object(), "u1",
        "Enable these skills for me: web-research-1:web_search (read), summarizer-1:summarize_text (read)",
        "c1"))
    assert handled is True
    assert ("web-research-1", "web_search", True) in tp.enabled
    assert len(tp.enabled) == 2


def test_skills_submit_denied_when_unauthorized():
    repo, tp = _FakeRepo(), _FakeTP(authorized=False)
    orch = _fake_orch(repo, tp)
    handled = asyncio.run(onboarding_submit.handle_submit(
        orch, object(), "u1",
        "Enable these skills for me: web-research-1:web_search (write)",
        "c1"))
    assert handled is True
    assert tp.enabled == []
