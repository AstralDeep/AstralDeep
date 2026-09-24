"""Tests for authored skill packs (orchestrator/skill_packs.py, knowledge_synthesis.py):
authored packs take precedence over synthesized knowledge, and the per-turn digest is
relevance-scoped, bounded, and fail-open.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def test_authored_pack_is_loaded():
    from orchestrator.knowledge_synthesis import KnowledgeIndex

    ki = KnowledgeIndex()
    content = ki.get_techniques_for_agent("summarizer-1")
    assert "summarize_url" in content


def test_authored_pack_precedence_over_synthesized(tmp_path, monkeypatch):
    from orchestrator import knowledge_synthesis
    from orchestrator.knowledge_synthesis import KnowledgeIndex

    synth = tmp_path / "knowledge" / "techniques"
    synth.mkdir(parents=True)
    (synth / "demo.md").write_text("---\nname: x\n---\n\nSYNTHESIZED-BODY\n", encoding="utf-8")

    authored = tmp_path / "packs" / "techniques"
    authored.mkdir(parents=True)
    (authored / "demo.md").write_text("---\nname: x\nauthored: true\n---\n\nAUTHORED-BODY\n", encoding="utf-8")

    monkeypatch.setattr(knowledge_synthesis, "AUTHORED_KNOWLEDGE_DIR", str(tmp_path / "packs"))
    ki = KnowledgeIndex(knowledge_dir=str(tmp_path / "knowledge"))

    out = ki.get_techniques_for_agent("demo-1")
    assert "AUTHORED-BODY" in out
    assert "SYNTHESIZED-BODY" not in out


class _FakeIndex:
    def __init__(self, mapping, raises=False):
        self._mapping = mapping
        self._raises = raises

    def get_techniques_for_agent(self, agent_id):
        if self._raises:
            raise RuntimeError("boom")
        return self._mapping.get(agent_id, "")


def test_digest_is_bounded_to_max_packs():
    from orchestrator import skill_packs

    mapping = {f"agent-{i}": ("X" * 800) for i in range(6)}
    digest = skill_packs.build_skill_digest(_FakeIndex(mapping), mapping.keys())
    assert digest.count("### ") <= skill_packs.MAX_PACKS
    assert len(digest) <= skill_packs.MAX_DIGEST_CHARS + 400


def test_digest_only_includes_agents_with_packs():
    from orchestrator import skill_packs

    mapping = {"weather-1": "use geocode first"}
    digest = skill_packs.build_skill_digest(_FakeIndex(mapping), ["weather-1", "ghost-1"])
    assert "weather-1" in digest
    assert "ghost-1" not in digest


def test_digest_empty_when_no_packs():
    from orchestrator import skill_packs

    digest = skill_packs.build_skill_digest(_FakeIndex({}), ["a-1", "b-1"])
    assert digest == ""


def test_digest_fail_open_on_error():
    from orchestrator import skill_packs

    digest = skill_packs.build_skill_digest(_FakeIndex({}, raises=True), ["a-1"])
    assert digest == ""
