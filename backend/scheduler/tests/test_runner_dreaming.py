"""Tests for backend/scheduler/runner.py: a dreaming job routes straight to the
consolidation sweep without needing a grant, and is skipped when disabled.
"""

import asyncio
import sys
import types
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scheduler.runner import JobRunner  # noqa: E402


class _Gate:
    def contains_phi(self, value):
        return False


class _Repo:
    def __init__(self, enabled=True, signals=None):
        self._enabled = enabled
        self._signals = signals or []
        self.memory = []
        self.sweeps = []

    def get_profile(self, user_id):
        return {"dreaming_enabled": self._enabled}

    def list_signals(self, user_id):
        return list(self._signals)

    def create_memory(self, user_id, category, value, *, source="explicit", salience=0.0):
        item = {"id": f"m{len(self.memory)}", "category": category, "value": value}
        self.memory.append(item)
        return item

    def delete_signal(self, user_id, sid):
        self._signals = [s for s in self._signals if s.get("id") != sid]

    def record_sweep(self, sweep):
        self.sweeps.append(sweep)


class _Store:
    def __init__(self):
        self.runs = []
        self.finished = []
        self.statuses = []
        self.after = []

    def start_run(self, job_id, user_id, correlation_id):
        rid = f"run{len(self.runs)}"
        self.runs.append(rid)
        return rid

    def finish_run(self, run_id, *, outcome, summary=None, auth_ref=None):
        self.finished.append((run_id, outcome, summary))

    def set_status(self, user_id, job_id, status):
        self.statuses.append((job_id, status))
        return True

    def update_after_run(self, job_id, *, last_run_at, next_run_at, completed):
        self.after.append((job_id, next_run_at, completed))


def _runner(repo, store, monkeypatch):
    import personalization.phi_gate as pg
    monkeypatch.setattr(pg, "get_phi_gate", lambda: _Gate())
    orch = types.SimpleNamespace(
        personalization_service=types.SimpleNamespace(repo=repo),
    )
    return JobRunner(orch, store, offline_grants=None)


_DREAM_JOB = {
    "id": "j1", "user_id": "u1", "agent_id": "__dreaming__", "name": "Memory consolidation",
    "instruction": "(internal)", "schedule_kind": "cron", "schedule_expr": "0 3 * * *",
    "timezone": "UTC", "offline_grant_id": None,
}


def test_dreaming_job_runs_sweep_without_grant(monkeypatch):
    repo, store = _Repo(enabled=True), _Store()
    runner = _runner(repo, store, monkeypatch)
    outcome = asyncio.run(runner.run_job(dict(_DREAM_JOB)))
    assert outcome == "success"
    assert store.finished and store.finished[0][1] == "success"
    assert store.after and store.after[0][2] is False


def test_dreaming_job_skipped_when_disabled(monkeypatch):
    repo, store = _Repo(enabled=False), _Store()
    runner = _runner(repo, store, monkeypatch)
    outcome = asyncio.run(runner.run_job(dict(_DREAM_JOB)))
    assert outcome == "skipped"
    assert ("j1", "paused") in store.statuses
