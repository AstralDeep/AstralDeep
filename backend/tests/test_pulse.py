"""Tests for backend/dreaming/pulse.py and its chrome surface
(orchestrator/projection_surfaces/pulse.py): digest grouping, deduping and bounding,
schedule-cadence proposals, and the real render() producing escaped astralprims
cards.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orchestrator.projection_surfaces.pulse as pulse_surface  # noqa: E402

from dreaming import pulse  # noqa: E402
from orchestrator.projection_surfaces import get_surface  # noqa: E402


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("FF_PULSE_DIGEST", raising=False)
    assert pulse.pulse_enabled() is False
    monkeypatch.setenv("FF_PULSE_DIGEST", "on")
    assert pulse.pulse_enabled() is True


def test_build_digest_groups_by_category():
    items = [
        {"category": "goal", "title": "Ship 033", "salience": 0.9},
        {"category": "goal", "title": "Write tests", "salience": 0.5},
        {"category": "preference", "value": "dark mode", "salience": 0.3},
    ]
    cards = pulse.build_digest(items)
    titles = [c["title"] for c in cards]
    assert "Goal" in titles and "Preference" in titles
    goal_card = next(c for c in cards if c["title"] == "Goal")
    assert goal_card["content"][0]["content"] == "• Ship 033"


def test_build_digest_dedups_and_bounds():
    items = [{"category": "goal", "title": "X", "salience": 0.5} for _ in range(4)]
    cards = pulse.build_digest(items)
    goal = next(c for c in cards if c["title"] == "Goal")
    assert len(goal["content"]) == 1


def test_build_digest_max_cards():
    items = [{"category": f"c{i}", "title": f"t{i}", "salience": 0.5} for i in range(10)]
    assert len(pulse.build_digest(items, max_cards=3)) == 3


def test_build_digest_empty_and_junk():
    assert pulse.build_digest([]) == []
    assert pulse.build_digest([None, "x", {"category": "g"}]) == []


def test_digest_cards_are_astralprims_shaped():
    cards = pulse.build_digest([{"category": "goal", "title": "Y", "salience": 0.5}])
    c = cards[0]
    assert c["type"] == "card" and isinstance(c["content"], list)
    assert c["content"][0]["type"] == "text"


@pytest.mark.parametrize("req,cadence", [
    ("remind me every morning", "daily"),
    ("send me a summary daily", "daily"),
    ("every week on monday", "weekly"),
    ("weekly digest please", "weekly"),
    ("every weekday at 9:00", "weekday"),
    ("every hour", "hourly"),
    ("in 2 hours", "once"),
    ("just do the thing", "unknown"),
])
def test_propose_schedule_cadence(req, cadence):
    assert pulse.propose_schedule(req).cadence == cadence


def test_propose_schedule_extracts_time():
    assert pulse.propose_schedule("every weekday at 09:30").at == "09:30"
    assert pulse.propose_schedule("every morning").at == "morning"
    assert pulse.propose_schedule("every week on friday").at == "friday"


def test_propose_schedule_relative():
    p = pulse.propose_schedule("in 30 minutes")
    assert p.cadence == "once" and p.at == "+30m"
    assert pulse.propose_schedule("in 3 days").at == "+3d"


def test_proposal_needs_confirmation_and_schedulability():
    assert pulse.propose_schedule("daily").confirm_needed is True
    assert pulse.is_schedulable(pulse.propose_schedule("daily")) is True
    assert pulse.is_schedulable(pulse.propose_schedule("do the thing")) is False


class _FakeRepo:
    def __init__(self, memories=None, signals=None):
        self._memories = memories or []
        self._signals = signals or []

    def list_memory(self, user_id):
        return [dict(m) for m in self._memories]

    def list_signals(self, user_id):
        return [dict(s) for s in self._signals]


def _orch(repo):
    return SimpleNamespace(personalization_service=SimpleNamespace(repo=repo))


def _render(orch, user_id="u1"):
    return asyncio.run(pulse_surface.render(orch, user_id, ["user"], {}))


def test_surface_is_registered():
    mod = get_surface("pulse")
    assert mod is not None
    assert mod.TITLE
    assert callable(mod.render)


def test_surface_off_when_flag_disabled(monkeypatch):
    monkeypatch.delenv("FF_PULSE_DIGEST", raising=False)
    repo = _FakeRepo(memories=[{"category": "goal", "value": "ship 033", "salience": 0.9}])
    html = _render(_orch(repo))
    assert "turned off" in html.lower()
    assert "astral-card" not in html


def test_surface_renders_real_digest_cards_when_enabled(monkeypatch):
    monkeypatch.setenv("FF_PULSE_DIGEST", "on")
    repo = _FakeRepo(
        memories=[
            {"category": "goal", "value": "ship 033", "salience": 0.9},
            {"category": "preference", "value": "dark mode", "salience": 0.3},
        ],
        signals=[{"category": "context", "value": "Kubernetes scaling", "recall_count": 2}],
    )
    html = _render(_orch(repo))
    assert "astral-card" in html
    assert "Goal" in html
    assert "ship 033" in html and "dark mode" in html
    assert "schedule" in html.lower()


def test_surface_empty_state_when_no_items(monkeypatch):
    monkeypatch.setenv("FF_PULSE_DIGEST", "on")
    html = _render(_orch(_FakeRepo()))
    assert "Nothing to show yet" in html
    assert "astral-card" not in html


def test_surface_escapes_user_values(monkeypatch):
    monkeypatch.setenv("FF_PULSE_DIGEST", "on")
    repo = _FakeRepo(memories=[
        {"category": "goal", "value": "<script>alert(1)</script>", "salience": 0.9},
    ])
    html = _render(_orch(repo))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_surface_handles_missing_subsystem(monkeypatch):
    monkeypatch.setenv("FF_PULSE_DIGEST", "on")
    orch = SimpleNamespace(personalization_service=None)
    html = _render(orch)
    assert "not available" in html.lower()
