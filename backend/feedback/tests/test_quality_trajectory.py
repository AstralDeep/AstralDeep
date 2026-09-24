"""Tests for feedback/quality.py's trajectory scoring: with FF_AGENT_EVAL on, a
per-agent tool-call trajectory summary from orchestrator/agent_eval.py is folded into
the job's output and audited; off, behavior is unchanged.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import audit.recorder as audit_recorder  # noqa: E402
from feedback import quality  # noqa: E402


class _FakeAuditRepository:
    def __init__(self, traj_rows):
        self._traj_rows = tuple(SimpleNamespace(**row) for row in traj_rows)

    def list_tool_trajectory_events_for_administration(
        self,
        _transaction,
        *,
        from_ts,
        to_ts,
        limit,
    ):
        assert from_ts < to_ts
        assert limit > 0
        return self._traj_rows


class _FakeAuditContext:
    def __init__(self, traj_rows):
        self.repository = _FakeAuditRepository(traj_rows)

    @contextmanager
    def transaction(self):
        yield object()

    def call(self, operation, /, *args, **kwargs):
        with self.transaction() as transaction:
            return operation(transaction, *args, **kwargs)


class _FakeRepo:
    def __init__(self, agg_rows, traj_rows):
        self._audit = _FakeAuditContext(traj_rows)
        self._agg_rows = agg_rows
        self.inserted = []

    def aggregate_window(self, window_start, window_end):
        return [dict(r) for r in self._agg_rows]

    def latest_quality_signal(self, agent_id, tool_name):
        return None

    def insert_quality_signal(self, dto):
        self.inserted.append(dto)
        return dto


class _FakeRecorder:
    def __init__(self):
        self.events = []

    async def record(self, ev):
        self.events.append(ev)


def _row(d):
    return d


@pytest.fixture
def _recorder():
    prev = audit_recorder.get_recorder()
    rec = _FakeRecorder()
    audit_recorder.set_recorder(rec)
    yield rec
    audit_recorder.set_recorder(prev)


def _make_repo():
    window_end = datetime.now(timezone.utc)
    agg_rows = [
        _row({"agent_id": "web-research-1", "tool_name": "web_search",
              "dispatch_count": 30, "failure_count": 0,
              "negative_feedback_count": 0}),
    ]
    traj_rows = []

    def add_turn(corr, tools):
        for t in tools:
            traj_rows.append(_row({"agent_id": "web-research-1",
                                   "correlation_id": corr, "tool_name": t}))

    add_turn("t1", ["web_search", "fetch_page", "summarize"])
    add_turn("t2", ["web_search", "fetch_page", "summarize"])
    add_turn("t3", ["web_search", "fetch_page", "summarize"])
    add_turn("t4", ["fetch_page", "web_search", "summarize"])
    add_turn("t5", ["web_search", "summarize"])
    return _FakeRepo(agg_rows, traj_rows), window_end


@pytest.mark.asyncio
async def test_trajectory_flag_off_is_no_op(monkeypatch, _recorder):
    monkeypatch.delenv("FF_AGENT_EVAL", raising=False)
    from orchestrator.agent_eval import agent_eval_enabled
    assert agent_eval_enabled() is False

    repo, window_end = _make_repo()
    snapshots = await quality.compute_for_window(repo, now=window_end)

    assert len(snapshots) == 1
    assert not hasattr(snapshots[0], "trajectory_quality")
    assert all(ev.action_type != "trajectory_evaluated" for ev in _recorder.events)


@pytest.mark.asyncio
async def test_trajectory_flag_on_folds_score(monkeypatch, _recorder):
    monkeypatch.setenv("FF_AGENT_EVAL", "true")
    from orchestrator.agent_eval import agent_eval_enabled
    assert agent_eval_enabled() is True

    repo, window_end = _make_repo()
    snapshots = await quality.compute_for_window(repo, now=window_end)

    assert len(snapshots) == 1
    tq = getattr(snapshots[0], "trajectory_quality", None)
    assert tq is not None, "flag ON must fold the trajectory quality onto the DTO"
    assert tq["trajectory_count"] == 5
    assert tq["consensus_match_rate"] == pytest.approx(0.6)
    assert 0.0 <= tq["mean_quality"] <= 1.0
    assert set(tq["metric_means"]) == {
        "exact_match", "in_order_match", "any_order_match", "precision", "recall"}

    traj_events = [ev for ev in _recorder.events
                   if ev.action_type == "trajectory_evaluated"]
    assert len(traj_events) == 1
    ev = traj_events[0]
    assert ev.event_class == "tool_quality"
    assert ev.agent_id == "web-research-1"
    assert ev.inputs_meta["trajectory_count"] == 5
    assert ev.inputs_meta["consensus_match_rate"] == pytest.approx(0.6)
    assert "pass_k" in ev.inputs_meta


@pytest.mark.asyncio
async def test_trajectory_scores_match_backbone(monkeypatch, _recorder):
    monkeypatch.setenv("FF_AGENT_EVAL", "true")
    repo, window_end = _make_repo()

    expected = quality.evaluate_trajectories(
        repo, window_end - quality.timedelta(days=14), window_end)
    assert "web-research-1" in expected
    exp = expected["web-research-1"]

    snapshots = await quality.compute_for_window(repo, now=window_end)
    tq = getattr(snapshots[0], "trajectory_quality")
    assert tq["mean_quality"] == exp["mean_quality"]
    assert tq["pass_k"] == exp["pass_k"]
    assert tq["consensus_match_rate"] == exp["consensus_match_rate"]
