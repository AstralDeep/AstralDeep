"""Tests for personalization/retrieval_scoring.py: flag defaults, the
recency/relevance/importance component functions, composite score bounds and
weighting, and multi-signal ranking wired into memory_tools.py's memory_search.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from personalization import retrieval_scoring as rs  # noqa: E402
from personalization import project_scope as ps  # noqa: E402
from personalization.memory_tools import MemoryTools  # noqa: E402


def test_multisignal_flag_default_on(monkeypatch):
    monkeypatch.delenv("FF_MEMORY_MULTISIGNAL", raising=False)
    assert rs.multisignal_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_multisignal_flag_off(monkeypatch, value):
    monkeypatch.setenv("FF_MEMORY_MULTISIGNAL", value)
    assert rs.multisignal_enabled() is False


def test_recency_from_rank():
    assert rs.recency_from_rank(0, 3) == 1.0
    assert rs.recency_from_rank(2, 3) == 0.0
    assert rs.recency_from_rank(1, 3) == 0.5
    assert rs.recency_from_rank(0, 1) == 1.0
    assert rs.recency_from_rank(0, 0) == 1.0


def test_relevance_from_overlap():
    assert rs.relevance_from_overlap(1, 2) == 0.5
    assert rs.relevance_from_overlap(5, 2) == 1.0
    assert rs.relevance_from_overlap(3, 0) == 0.0


def test_importance_signal_salience_then_source():
    assert rs.importance_signal(0.8) == 0.8
    assert rs.importance_signal(1.5) == 1.0
    assert rs.importance_signal(0.0, "explicit") == 0.7
    assert rs.importance_signal(0.0, "promoted") == 0.5
    assert rs.importance_signal(0.0, None) == 0.5


def test_multi_signal_score_bounds_and_weights():
    assert rs.multi_signal_score(recency=1, importance=1, relevance=1) == 1.0
    assert rs.multi_signal_score(recency=0, importance=0, relevance=0) == 0.0
    assert rs.multi_signal_score(recency=2, importance=-1, relevance=0.5) == pytest.approx(
        round((0.34 * 1.0 + 0.33 * 0.0 + 0.33 * 0.5) / 1.0, 6))
    assert rs.multi_signal_score(recency=1, importance=1, relevance=1,
                                 weights={"recency": 0.0}) == 0.0


class _Gate:
    def contains_phi(self, _value):  # noqa: D401
        return False


class _Repo:
    def __init__(self, items):
        self._items = items

    def list_memory(self, _user_id, *, project_id=None, include_global=True):
        rows = [dict(item) for item in self._items]
        if project_id is None:
            return rows
        return ps.filter_to_project(rows, project_id, include_global=include_global)


_A = {"id": "a", "category": "context", "value": "python", "source": "promoted", "salience": 0.0}
_B = {"id": "b", "category": "context", "value": "python advanced guide", "source": "explicit", "salience": 0.0}
_ITEMS = [_A, _B]


def test_memory_search_multisignal_lifts_recent_relevant(monkeypatch):
    monkeypatch.setenv("FF_MEMORY_MULTISIGNAL", "true")
    mt = MemoryTools(_Repo(_ITEMS), phi_gate=_Gate())
    out = mt.memory_search("u", "python advanced")
    assert [i["id"] for i in out] == ["a", "b"]


def test_memory_search_flag_off_is_overlap_only(monkeypatch):
    monkeypatch.setenv("FF_MEMORY_MULTISIGNAL", "false")
    mt = MemoryTools(_Repo(_ITEMS), phi_gate=_Gate())
    out = mt.memory_search("u", "python advanced")
    assert [i["id"] for i in out] == ["b", "a"]


def test_memory_search_empty_query_returns_recency(monkeypatch):
    mt = MemoryTools(_Repo(_ITEMS), phi_gate=_Gate())
    out = mt.memory_search("u", "")
    assert [i["id"] for i in out] == ["a", "b"]
