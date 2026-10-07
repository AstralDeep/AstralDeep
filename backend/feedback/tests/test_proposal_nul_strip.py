"""Regression test for the NUL-character bug in feedback/proposals.py.

On sandbox deployments, generate_for_underperforming() was failing to
insert proposals for some tools because the existing artifact, the
generated proposal, or the LLM-refined proposal could contain a NUL
character (\\x00). PostgreSQL rejects NUL in text values with
"invalid character ... 0x00", the surrounding try/except caught the
error and just logged it, and the proposal was silently dropped.

The fix strips NUL (replacing with U+FFFD REPLACEMENT CHARACTER) from
both inputs before _make_unified_diff, so the diff and the insert
proposal succeed even when an input originally had a NUL.

This test mocks the surrounding machinery (artifact read,
_make_unified_diff, repo.insert_proposal) and asserts that a NUL in
either input does not leak into the diff payload.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from feedback import proposals as proposals_mod  # noqa: E402


class _FakeRepo:
    """Minimal stand-in for FeedbackRepository for these regression tests."""

    def __init__(self, existing_content: str, proposed_content: str):
        self._existing = existing_content
        self._proposed = proposed_content
        self.inserted_payload = None

    def list_underperforming(self, limit):
        snap = SimpleNamespace(
            agent_id="test-agent",
            tool_name="test-tool",
            window_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 1, 8, tzinfo=timezone.utc),
            dispatch_count=5,
            failure_count=3,
            negative_feedback_count=2,
            failure_rate=0.6,
            negative_feedback_rate=0.4,
        )
        return [snap], []

    def category_breakdown(self, *a, **kw):
        return {}

    def collect_clean_comment_samples(self, *a, **kw):
        return []

    def evidence_ids(self, *a, **kw):
        return [], []

    def insert_proposal(self, **kwargs):
        self.inserted_payload = kwargs.get("diff_payload")
        return SimpleNamespace(id=1, **kwargs)


def _drive(repo, existing_text, tmp_path, monkeypatch):
    """Patch artifact I/O and audit emission, then run the function."""

    def fake_artifact_path(agent_id, tool_name):
        return Path(tmp_path) / agent_id / f"{tool_name}.md"

    def fake_ensure(p):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(existing_text, encoding="utf-8")
        return p

    monkeypatch.setattr(proposals_mod, "_artifact_path_for_tool", fake_artifact_path)
    monkeypatch.setattr(proposals_mod, "_ensure_within_knowledge_root", fake_ensure)
    monkeypatch.setattr(proposals_mod, "_emit_proposal_audit", lambda **kw: None)


@pytest.mark.asyncio
async def test_strips_nul_from_existing_artifact(tmp_path, monkeypatch):
    existing_with_nul = "hello\x00world\n"
    proposed = "hello world\nupdated\n"

    repo = _FakeRepo(existing_with_nul, proposed)
    _drive(repo, existing_with_nul, tmp_path, monkeypatch)

    result = await proposals_mod.generate_for_underperforming(repo)

    assert len(result) == 1, "expected exactly one proposal to be generated"
    assert repo.inserted_payload is not None
    assert "\x00" not in repo.inserted_payload, (
        "diff payload must not contain NUL; PostgreSQL would reject the insert"
    )


@pytest.mark.asyncio
async def test_strips_nul_from_proposed_content(tmp_path, monkeypatch):
    existing = "hello world\n"
    proposed_with_nul = "hello\x00world\nupdated\n"

    repo = _FakeRepo(existing, proposed_with_nul)
    _drive(repo, existing, tmp_path, monkeypatch)

    result = await proposals_mod.generate_for_underperforming(repo)

    assert len(result) == 1
    assert repo.inserted_payload is not None
    assert "\x00" not in repo.inserted_payload


@pytest.mark.asyncio
async def test_no_nul_passthrough(tmp_path, monkeypatch):
    existing = "hello world\n"
    proposed = "hello world\nupdated\n"

    repo = _FakeRepo(existing, proposed)
    _drive(repo, existing, tmp_path, monkeypatch)

    result = await proposals_mod.generate_for_underperforming(repo)

    assert len(result) == 1
    assert repo.inserted_payload is not None
    assert "hello world" in repo.inserted_payload
    assert "updated" in repo.inserted_payload
