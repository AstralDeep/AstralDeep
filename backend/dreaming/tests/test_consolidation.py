"""Tests for dreaming/consolidation.py: scoring rewards frequency and recency, one-off
signals are excluded from promotion, PHI is blocked at the sweep, and sleeptime
precompute runs only when idle and enabled.
"""

from __future__ import annotations

from dreaming.consolidation import run_sweep, score_signal, select_promotions
from personalization.phi_gate import PHIGate

NOW = 1_748_300_000_000


class _CleanAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return []


def _clean_gate() -> PHIGate:
    return PHIGate(analyzer=_CleanAnalyzer())


class _FakeRepo:
    def __init__(self, signals):
        self._signals = {s["id"]: s for s in signals}
        self.memory = []
        self.sweeps = []

    def list_signals(self, user_id):
        return list(self._signals.values())

    def create_memory(self, user_id, category, value, *, source="explicit", salience=0.0):
        item = {"id": f"m{len(self.memory)}", "category": category, "value": value, "source": source}
        self.memory.append(item)
        return item

    def delete_signal(self, user_id, sig_id):
        self._signals.pop(sig_id, None)

    def record_sweep(self, sweep):
        self.sweeps.append(sweep)


def test_score_rewards_frequency_and_recency():
    recent = score_signal(3, NOW, NOW)
    stale = score_signal(3, NOW - 30 * 86_400_000, NOW)
    assert recent > stale


def test_select_excludes_one_offs():
    signals = [
        {"id": "a", "category": "preference", "value": "x", "recall_count": 1, "last_seen_at": NOW},
        {"id": "b", "category": "preference", "value": "y", "recall_count": 3, "last_seen_at": NOW},
    ]
    chosen = select_promotions(signals, NOW, min_recalls=2)
    assert [c["id"] for c in chosen] == ["b"]


def test_run_sweep_promotes_recurring_excludes_phi():
    signals = [
        {"id": "a", "category": "preference", "value": "prefers bullet points", "recall_count": 3, "last_seen_at": NOW},
        {"id": "b", "category": "context", "value": "one-off note", "recall_count": 1, "last_seen_at": NOW},
        {"id": "c", "category": "context", "value": "SSN 123-45-6789", "recall_count": 4, "last_seen_at": NOW},
    ]
    repo = _FakeRepo(signals)
    sweep = run_sweep(repo, _clean_gate(), "u1", now_ms=NOW, min_recalls=2)

    assert sweep["promoted_count"] == 1
    assert repo.memory[0]["value"] == "prefers bullet points"
    assert repo.memory[0]["source"] == "promoted"
    assert all(m["value"] != "SSN 123-45-6789" for m in repo.memory)
    assert "c" not in repo._signals
    assert repo.sweeps and repo.sweeps[0]["trigger"] == "scheduled"


class _ProfileRepo(_FakeRepo):
    def __init__(self, signals, memories=None):
        super().__init__(signals)
        self._profile = {"user_id": "u1", "personality": {"tone": "warm"}}
        self.memory = list(memories or [])

    def get_profile(self, user_id):
        return dict(self._profile)

    def upsert_profile(self, user_id, *, personality=None, **kwargs):
        if personality is not None:
            self._profile["personality"] = personality
        return dict(self._profile)

    def list_memory(self, user_id):
        return [dict(m) for m in self.memory]


def _idle_signals():
    return [
        {"id": "a", "category": "preference", "value": "prefers bullet points",
         "recall_count": 3, "last_seen_at": NOW},
        {"id": "b", "category": "context", "value": "Looking into Kubernetes scaling",
         "recall_count": 1, "last_seen_at": NOW},
    ]


def _goal_memory():
    return [{"category": "goal", "value": "ship the v2 release", "salience": 3.0}]


def test_sleeptime_off_by_default_no_precompute(monkeypatch):
    monkeypatch.delenv("FF_SLEEPTIME_COMPUTE", raising=False)
    repo = _ProfileRepo(_idle_signals(), memories=_goal_memory())
    sweep = run_sweep(repo, _clean_gate(), "u1", now_ms=NOW,
                      last_activity_ms=NOW - 10 * 60_000)
    assert sweep["precompute"] == []
    assert "_sleeptime_precompute" not in repo._profile["personality"]
    assert repo._profile["personality"] == {"tone": "warm"}


def test_sleeptime_on_idle_produces_and_persists_plan(monkeypatch):
    monkeypatch.setenv("FF_SLEEPTIME_COMPUTE", "on")
    repo = _ProfileRepo(_idle_signals(), memories=_goal_memory())
    sweep = run_sweep(repo, _clean_gate(), "u1", now_ms=NOW,
                      last_activity_ms=NOW - 10 * 60_000)

    assert sweep["precompute"], "expected anticipated questions when idle + enabled"
    qs = [q["question"] for q in sweep["precompute"]]
    assert any("Kubernetes" in q for q in qs) or any("v2 release" in q for q in qs)
    assert all({"question", "rationale", "priority"} <= set(q) for q in sweep["precompute"])

    plan = repo._profile["personality"].get("_sleeptime_precompute")
    assert plan is not None
    assert plan["trigger"] == "idle"
    assert plan["questions"] == sweep["precompute"]
    assert repo._profile["personality"]["tone"] == "warm"


def test_sleeptime_on_but_active_user_skips(monkeypatch):
    monkeypatch.setenv("FF_SLEEPTIME_COMPUTE", "on")
    repo = _ProfileRepo(_idle_signals(), memories=_goal_memory())
    sweep = run_sweep(repo, _clean_gate(), "u1", now_ms=NOW,
                      last_activity_ms=NOW)
    assert sweep["precompute"] == []
    assert "_sleeptime_precompute" not in repo._profile["personality"]


def test_sleeptime_on_no_last_activity_treats_sweep_as_idle(monkeypatch):
    monkeypatch.setenv("FF_SLEEPTIME_COMPUTE", "on")
    repo = _ProfileRepo(_idle_signals(), memories=_goal_memory())
    sweep = run_sweep(repo, _clean_gate(), "u1", now_ms=NOW)
    assert sweep["precompute"]
    assert "_sleeptime_precompute" in repo._profile["personality"]


def test_sleeptime_promotion_path_unaffected(monkeypatch):
    monkeypatch.setenv("FF_SLEEPTIME_COMPUTE", "on")
    repo = _ProfileRepo(_idle_signals(), memories=_goal_memory())
    sweep = run_sweep(repo, _clean_gate(), "u1", now_ms=NOW,
                      last_activity_ms=NOW - 10 * 60_000, min_recalls=2)
    assert sweep["promoted_count"] == 1
    assert any(m["value"] == "prefers bullet points" for m in repo.memory)
