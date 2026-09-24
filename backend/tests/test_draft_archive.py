"""Tests for the evolutionary draft archive (orchestrator/draft_archive.py): the
archive-enabled flag, the surrogate code-quality scorer, Jaccard-ranked exemplar
retrieval, prompt conditioning, and the self-test skip threshold.
"""

from __future__ import annotations
import sys
from pathlib import Path
import pytest
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
from orchestrator import draft_archive as da  # noqa: E402
from orchestrator.draft_archive import ArchivedDraft  # noqa: E402


GOOD_CODE = '''
"""A small example agent."""

TOOL_REGISTRY = {}


def register_tool(name):
    """Register a tool."""
    def deco(fn):
        TOOL_REGISTRY[name] = fn
        return fn
    return deco


@register_tool("do_thing")
def do_thing(params):
    """Do the thing and return a component dict."""
    try:
        value = params.get("x", 0) + 1
        return {"type": "text", "text": str(value)}
    except Exception:
        return {"type": "text", "text": "error"}
'''


def test_archive_default_off(monkeypatch):
    monkeypatch.delenv("FF_DRAFT_ARCHIVE", raising=False)
    assert da.archive_enabled() is False


@pytest.mark.parametrize("v", ["true", "1", "yes", "on", "TRUE", "  On  "])
def test_archive_on(monkeypatch, v):
    monkeypatch.setenv("FF_DRAFT_ARCHIVE", v)
    assert da.archive_enabled() is True


@pytest.mark.parametrize("v", ["false", "0", "no", "off", "", "maybe"])
def test_archive_off_values(monkeypatch, v):
    monkeypatch.setenv("FF_DRAFT_ARCHIVE", v)
    assert da.archive_enabled() is False


def test_surrogate_good_beats_empty():
    assert da.surrogate_score(GOOD_CODE) > da.surrogate_score("")


def test_surrogate_empty_is_zero():
    assert da.surrogate_score("") == 0.0
    assert da.surrogate_score("   ") == 0.0
    assert da.surrogate_score("x = 1") == 0.0


def test_surrogate_good_beats_eval():
    bad = (
        '"""bad agent"""\n'
        "TOOL_REGISTRY = {}\n"
        "def do_thing(params):\n"
        "    return {\"r\": eval(params['expr'])}\n"
    )
    assert da.surrogate_score(GOOD_CODE) > da.surrogate_score(bad)


def test_surrogate_good_beats_subprocess():
    bad = (
        '"""bad agent"""\n'
        "import subprocess\n"
        "TOOL_REGISTRY = {}\n"
        "def do_thing(params):\n"
        "    return {\"r\": subprocess.run(params['cmd'])}\n"
    )
    assert da.surrogate_score(GOOD_CODE) > da.surrogate_score(bad)


def test_surrogate_red_flags_compound():
    one_flag = (
        '"""x"""\nTOOL_REGISTRY = {}\n'
        "def f(p):\n    return {'r': eval(p)}\n"
    )
    two_flags = (
        '"""x"""\nTOOL_REGISTRY = {}\n'
        "import socket\n"
        "def f(p):\n    return {'r': eval(p)}\n"
    )
    assert da.surrogate_score(two_flags) < da.surrogate_score(one_flag)


def test_surrogate_clamped_unit_interval():
    for code in ("", "x", GOOD_CODE, GOOD_CODE * 5, "eval(" * 50):
        s = da.surrogate_score(code)
        assert 0.0 <= s <= 1.0


def test_surrogate_deterministic():
    assert da.surrogate_score(GOOD_CODE) == da.surrogate_score(GOOD_CODE)


def test_surrogate_rewards_registration_and_docstring():
    plain = "a = 1\nb = 2\nc = a + b\nd = c * 3\ne = d - 1\n" * 2
    assert da.surrogate_score(GOOD_CODE) > da.surrogate_score(plain)


def _draft(fp, score, code="x"):
    return ArchivedDraft(fingerprint=fp, code=code, score=score)


def test_top_exemplars_ranks_by_overlap_then_score():
    archive = [
        _draft("read pdf table", 0.9),
        _draft("send email smtp", 0.95),
        _draft("read pdf", 0.5),
    ]
    out = da.top_exemplars(archive, "read pdf table extract", k=3)
    assert out[0].fingerprint == "read pdf table"
    assert out[1].fingerprint == "read pdf"
    assert out[2].fingerprint == "send email smtp"


def test_top_exemplars_overlap_tie_broken_by_score():
    archive = [
        _draft("read pdf", 0.4, code="low"),
        _draft("read pdf", 0.8, code="high"),
    ]
    out = da.top_exemplars(archive, "read pdf", k=2)
    assert [d.score for d in out] == [0.8, 0.4]


def test_top_exemplars_respects_k():
    archive = [_draft(f"read pdf {i}", 0.5 + i * 0.01) for i in range(10)]
    out = da.top_exemplars(archive, "read pdf", k=3)
    assert len(out) == 3


def test_top_exemplars_excludes_non_positive_score():
    archive = [
        _draft("read pdf", 0.0),
        _draft("read pdf", -0.5),
        _draft("read pdf", 0.7),
    ]
    out = da.top_exemplars(archive, "read pdf", k=5)
    assert len(out) == 1
    assert out[0].score == 0.7


def test_top_exemplars_empty_archive():
    assert da.top_exemplars([], "anything", k=3) == []


def test_top_exemplars_non_positive_k():
    archive = [_draft("read pdf", 0.9)]
    assert da.top_exemplars(archive, "read pdf", k=0) == []
    assert da.top_exemplars(archive, "read pdf", k=-1) == []


def test_condition_prompt_no_exemplars_returns_base():
    base = "GENERATE AN AGENT"
    assert da.condition_prompt(base, []) == base


def test_condition_prompt_appends_section():
    base = "GENERATE AN AGENT"
    ex = [_draft("read pdf", 0.9, code="def f():\n    return {}\n")]
    out = da.condition_prompt(base, ex)
    assert out.startswith(base)
    assert "## Exemplars from past successful agents" in out
    assert "def f()" in out
    assert len(out) > len(base)


def test_condition_prompt_within_max_chars():
    base = "BASE"
    big = "X" * 50_000
    ex = [_draft("a", 0.9, code=big), _draft("b", 0.8, code=big)]
    out = da.condition_prompt(base, ex, max_chars=1000)
    appended = out[len(base):]
    assert len(appended) <= 1000
    assert "## Exemplars from past successful agents" in out


def test_condition_prompt_zero_budget_returns_base():
    base = "BASE"
    ex = [_draft("a", 0.9, code="something")]
    assert da.condition_prompt(base, ex, max_chars=0) == base


def test_condition_prompt_embeds_multiple_exemplars():
    base = "BASE"
    ex = [
        _draft("a", 0.9, code="AAA_CODE"),
        _draft("b", 0.8, code="BBB_CODE"),
    ]
    out = da.condition_prompt(base, ex, max_chars=4000)
    assert "AAA_CODE" in out
    assert "BBB_CODE" in out


def test_should_skip_self_test_true_for_empty():
    assert da.should_skip_self_test("") is True
    assert da.should_skip_self_test("   ") is True


def test_should_skip_self_test_true_for_red_flag_stub():
    assert da.should_skip_self_test("eval(x)") is True


def test_should_skip_self_test_false_for_good_code():
    assert da.should_skip_self_test(GOOD_CODE) is False


def test_should_skip_self_test_threshold_boundary():
    assert da.should_skip_self_test(GOOD_CODE, min_score=1.01) is True
    assert da.should_skip_self_test(GOOD_CODE, min_score=0.0) is False
