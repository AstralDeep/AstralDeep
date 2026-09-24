"""Tests for journal_review/mcp_tools.py's _parse_openalex_source: field extraction,
citedness rounding, and confirmation that no impact-factor approximation survives in
the parsed record.
"""

from agents.journal_review.mcp_tools import _fmt_citedness, _parse_openalex_source
from agents.journal_review.tests.conftest import make_source


def test_extracts_core_fields() -> None:
    j = _parse_openalex_source(make_source())
    assert j["name"] == "European Heart Journal"
    assert j["issn"] == "0195-668X"
    assert j["issn_l"] == "0195-668X"
    assert j["publisher"] == "Oxford University Press"
    assert j["h_index"] == 350
    assert j["i10_index"] == 12000
    assert j["recent_works"] == 1200
    assert j["recent_cited"] == 90000
    assert j["topics"] == ["Cardiology", "Heart Failure"]


def test_two_year_mean_citedness_rounded_to_two_decimals() -> None:
    j = _parse_openalex_source(make_source())
    assert j["two_year_mean_citedness"] == 35.46


def test_citedness_integer_value_is_kept() -> None:
    j = _parse_openalex_source(
        make_source(summary_stats={"2yr_mean_citedness": 4}))
    assert j["two_year_mean_citedness"] == 4


def test_citedness_zero_is_a_real_value() -> None:
    j = _parse_openalex_source(
        make_source(summary_stats={"2yr_mean_citedness": 0.0}))
    assert j["two_year_mean_citedness"] == 0.0


def test_citedness_absent_from_summary_stats_is_none() -> None:
    j = _parse_openalex_source(make_source(summary_stats={"h_index": 5}))
    assert j["two_year_mean_citedness"] is None


def test_summary_stats_missing_entirely_is_none() -> None:
    src = make_source()
    del src["summary_stats"]
    j = _parse_openalex_source(src)
    assert j["two_year_mean_citedness"] is None


def test_citedness_non_numeric_is_none() -> None:
    j = _parse_openalex_source(
        make_source(summary_stats={"2yr_mean_citedness": "not-a-number"}))
    assert j["two_year_mean_citedness"] is None


def test_no_impact_factor_key_survives() -> None:
    j = _parse_openalex_source(make_source())
    assert "approx_impact_factor" not in j
    assert not [k for k in j if "impact" in k.lower()]


def test_fmt_citedness_display() -> None:
    assert _fmt_citedness(None) == "N/A"
    assert _fmt_citedness(35.46) == "35.46"
    assert _fmt_citedness(0.0) == "0.0"
