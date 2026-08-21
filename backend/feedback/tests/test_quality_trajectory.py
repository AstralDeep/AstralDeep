"""C-N5 (033) — trajectory-evaluation *wiring* tests for the feedback-quality job.

Feeds REAL tool-call trajectories (reconstructed from canned ``agent_tool_call``
audit rows) through the real ``feedback.quality.compute_for_window`` path and
asserts:

* flag ON  → a trajectory-quality summary is folded into the job output
  (stamped onto the returned snapshot DTOs AND emitted as an ``agent_eval``
  audit event), computed by the real ``orchestrator.agent_eval`` backbone;
* flag OFF → byte-identical behaviour to before (no trajectory work, no
  trajectory audit event, no DTO stamp).

DB-free: a fake Plane audit repository serves canned typed trajectory records
through the same repository context production uses and records the inserted
quality snapshots in memory; a fake audit recorder captures emitted events.
This exercises the production code path end to end without a live Postgres.
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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeAuditRepository:
    """Typed Plane audit boundary used by trajectory reconstruction."""

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
    """Typed audit context plus in-memory aggregate/snapshot behaviour."""

    def __init__(self, agg_rows, traj_rows):
        self._audit = _FakeAuditContext(traj_rows)
        self._agg_rows = agg_rows
        self.inserted = []

    def aggregate_window(self, window_start, window_end):
        return [dict(r) for r in self._agg_rows]

    def latest_quality_signal(self, agent_id, tool_name):
        return None  # no prior → no transition events

    def insert_quality_signal(self, dto):
        self.inserted.append(dto)
        return dto


class _FakeRecorder:
    def __init__(self):
        self.events = []

    async def record(self, ev):
        self.events.append(ev)


def _row(d):
    """A dict-row that supports both __getitem__ (used by quality.py)."""
    return d


@pytest.fixture
def _recorder():
    prev = audit_recorder.get_recorder()
    rec = _FakeRecorder()
    audit_recorder.set_recorder(rec)
    yield rec
    audit_recorder.set_recorder(prev)


def _make_repo():
    """One agent with a clear MODAL trajectory of [search, fetch_page, summarize].

    5 turns total: 3 match the modal sequence exactly, 1 reorders, 1 drops a
    tool — so consensus_match_rate and pass^k are < 1.0 and deterministic.
    """
    window_end = datetime.now(timezone.utc)
    agg_rows = [
        _row({"agent_id": "web-research-1", "tool_name": "web_search",
              "dispatch_count": 30, "failure_count": 0,
              "negative_feedback_count": 0}),
    ]
    # Each (agent_id, correlation_id, tool_name) row is one *.end audit event,
    # ordered as quality.py orders them (agent, corr, recorded_at).
    traj_rows = []

    def add_turn(corr, tools):
        for t in tools:
            traj_rows.append(_row({"agent_id": "web-research-1",
                                   "correlation_id": corr, "tool_name": t}))

    add_turn("t1", ["web_search", "fetch_page", "summarize"])   # modal
    add_turn("t2", ["web_search", "fetch_page", "summarize"])   # modal
    add_turn("t3", ["web_search", "fetch_page", "summarize"])   # modal
    add_turn("t4", ["fetch_page", "web_search", "summarize"])   # reordered
    add_turn("t5", ["web_search", "summarize"])                 # dropped a tool
    return _FakeRepo(agg_rows, traj_rows), window_end


# ---------------------------------------------------------------------------
# Flag OFF (default) — unchanged behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trajectory_flag_off_is_no_op(monkeypatch, _recorder):
    monkeypatch.delenv("FF_AGENT_EVAL", raising=False)
    from orchestrator.agent_eval import agent_eval_enabled
    assert agent_eval_enabled() is False

    repo, window_end = _make_repo()
    snapshots = await quality.compute_for_window(repo, now=window_end)

    # Snapshot still produced (the base job is unchanged)...
    assert len(snapshots) == 1
    # ...but NO trajectory_quality stamped and NO trajectory audit event.
    assert not hasattr(snapshots[0], "trajectory_quality")
    assert all(ev.action_type != "trajectory_evaluated" for ev in _recorder.events)


# ---------------------------------------------------------------------------
# Flag ON — trajectory score folded into the job output
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trajectory_flag_on_folds_score(monkeypatch, _recorder):
    monkeypatch.setenv("FF_AGENT_EVAL", "true")
    from orchestrator.agent_eval import agent_eval_enabled
    assert agent_eval_enabled() is True

    repo, window_end = _make_repo()
    snapshots = await quality.compute_for_window(repo, now=window_end)

    # The trajectory summary is stamped onto the returned snapshot DTO.
    assert len(snapshots) == 1
    tq = getattr(snapshots[0], "trajectory_quality", None)
    assert tq is not None, "flag ON must fold the trajectory quality onto the DTO"
    assert tq["trajectory_count"] == 5
    # 3 of 5 turns match the modal trajectory exactly.
    assert tq["consensus_match_rate"] == pytest.approx(0.6)
    assert 0.0 <= tq["mean_quality"] <= 1.0
    # The five named ADK/Vertex metrics are present in the per-agent means.
    assert set(tq["metric_means"]) == {
        "exact_match", "in_order_match", "any_order_match", "precision", "recall"}

    # ...and emitted as a real agent_eval audit event the job output exposes.
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
    """The folded numbers are exactly what the agent_eval backbone computes for
    the same trajectories scored against their modal reference — i.e. this is a
    REAL run through the deterministic backbone, not a stub."""
    monkeypatch.setenv("FF_AGENT_EVAL", "true")
    repo, window_end = _make_repo()

    # Independently compute the expectation via evaluate_trajectories.
    expected = quality.evaluate_trajectories(
        repo, window_end - quality.timedelta(days=14), window_end)
    assert "web-research-1" in expected
    exp = expected["web-research-1"]

    snapshots = await quality.compute_for_window(repo, now=window_end)
    tq = getattr(snapshots[0], "trajectory_quality")
    assert tq["mean_quality"] == exp["mean_quality"]
    assert tq["pass_k"] == exp["pass_k"]
    assert tq["consensus_match_rate"] == exp["consensus_match_rate"]
