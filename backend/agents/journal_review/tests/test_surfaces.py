"""Tests for journal_review/mcp_tools.py: every rendered surface (results table, profile
card, comparison table, landscape table) carries OpenAlex citedness attribution and
never the term Impact Factor; also covers SSRF/DNS egress gating.
"""

import json
import socket
from unittest.mock import patch

import requests

from agents.journal_review import mcp_tools
from agents.journal_review.mcp_tools import (
    CITEDNESS_LABEL,
    CROSSREF_JOURNALS_URL,
    OPENALEX_SOURCES_URL,
    OPENALEX_WORKS_URL,
    compare_journals,
    find_matching_journals,
    get_field_landscape,
    get_journal_profile,
)
from agents.journal_review.tests.conftest import EHJ_ID, make_source, make_works_payload
from shared.tests._http_mock import HttpMock

CROSSREF_EHJ_URL = f"{CROSSREF_JOURNALS_URL}/0195-668X"


def _mock_openalex(rmock: HttpMock, sources=None) -> None:
    rmock.add("GET", OPENALEX_WORKS_URL, status=200,
              json=make_works_payload([EHJ_ID, EHJ_ID, EHJ_ID]))
    rmock.add("GET", OPENALEX_SOURCES_URL, status=200,
              json={"results": sources if sources is not None else [make_source()]})


def _find_table(components):
    for comp in components:
        if comp.get("type") == "table":
            return comp
        for key in ("content", "children"):
            found = _find_table(comp.get(key) or [])
            if found is not None:
                return found
    return None


def test_results_table_header_carries_openalex_attribution(rmock: HttpMock) -> None:
    _mock_openalex(rmock)
    result = find_matching_journals(query="cardiology")
    table = _find_table(result["_ui_components"])
    assert table is not None
    assert CITEDNESS_LABEL in table["headers"]
    assert CITEDNESS_LABEL == "2-yr Mean Citedness (OpenAlex)"


def test_results_table_shows_rounded_openalex_citedness(rmock: HttpMock) -> None:
    _mock_openalex(rmock)
    result = find_matching_journals(query="cardiology")
    table = _find_table(result["_ui_components"])
    idx = table["headers"].index(CITEDNESS_LABEL)
    assert table["rows"][0][idx] == "35.46"


def test_results_data_payload_uses_citedness_key(rmock: HttpMock) -> None:
    _mock_openalex(rmock)
    result = find_matching_journals(query="cardiology")
    entry = result["_data"]["journals"][0]
    assert entry["two_year_mean_citedness"] == 35.46
    assert "approx_impact_factor" not in entry


def test_results_table_missing_citedness_shows_na(rmock: HttpMock) -> None:
    _mock_openalex(rmock, sources=[make_source(summary_stats={"h_index": 10})])
    result = find_matching_journals(query="cardiology")
    table = _find_table(result["_ui_components"])
    idx = table["headers"].index(CITEDNESS_LABEL)
    assert table["rows"][0][idx] == "N/A"


def _mock_profile(rmock: HttpMock, source=None) -> None:
    rmock.add("GET", OPENALEX_SOURCES_URL, status=200,
              json={"results": [source if source is not None else make_source()]})
    rmock.add("GET", CROSSREF_EHJ_URL, status=200, json={
        "status": "ok",
        "message": {"subjects": [{"name": "Cardiology"}],
                    "counts": {"total-dois": 30000, "current-dois": 1200}},
    })


def test_profile_metric_card_is_citedness_not_impact_factor(rmock: HttpMock) -> None:
    _mock_profile(rmock)
    result = get_journal_profile(journal_name="European Heart Journal")
    grid = result["_ui_components"][0]
    titles = [m["title"] for m in grid["children"]]
    assert "2-yr Mean Citedness" in titles
    metric = grid["children"][titles.index("2-yr Mean Citedness")]
    assert metric["value"] == "35.46"
    assert metric["subtitle"] == "OpenAlex"


def test_profile_metric_card_absent_citedness_is_na(rmock: HttpMock) -> None:
    _mock_profile(rmock, source=make_source(summary_stats={"h_index": 10}))
    result = get_journal_profile(journal_name="European Heart Journal")
    grid = result["_ui_components"][0]
    metric = next(m for m in grid["children"] if m["title"] == "2-yr Mean Citedness")
    assert metric["value"] == "N/A"


def test_profile_info_table_row_carries_attribution(rmock: HttpMock) -> None:
    _mock_profile(rmock)
    result = get_journal_profile(journal_name="European Heart Journal")
    table = _find_table(result["_ui_components"][1:])
    assert [CITEDNESS_LABEL, "35.46"] in table["rows"]


def test_profile_data_payload_has_citedness_only(rmock: HttpMock) -> None:
    _mock_profile(rmock)
    result = get_journal_profile(journal_name="European Heart Journal")
    assert result["_data"]["two_year_mean_citedness"] == 35.46
    assert "approx_impact_factor" not in result["_data"]


def test_compare_table_has_citedness_row_no_impact_factor(rmock: HttpMock) -> None:
    rmock.add("GET", OPENALEX_SOURCES_URL, status=200,
              json={"results": [make_source()]})
    result = compare_journals(journal_names="European Heart Journal;The Lancet")
    table = _find_table(result["_ui_components"])
    labels = [row[0] for row in table["rows"]]
    assert CITEDNESS_LABEL in labels
    assert not any("impact factor" in str(label).lower() for label in labels)
    row = table["rows"][labels.index(CITEDNESS_LABEL)]
    assert row[1:] == ["35.46", "35.46"]


def test_compare_data_payload_uses_citedness_key(rmock: HttpMock) -> None:
    rmock.add("GET", OPENALEX_SOURCES_URL, status=200,
              json={"results": [make_source()]})
    result = compare_journals(journal_names="European Heart Journal;The Lancet")
    for entry in result["_data"]["journals"]:
        assert entry["two_year_mean_citedness"] == 35.46
        assert "approx_impact_factor" not in entry


def test_landscape_header_and_value(rmock: HttpMock) -> None:
    _mock_openalex(rmock)
    result = get_field_landscape(field="cardiology")
    table = _find_table(result["_ui_components"])
    assert CITEDNESS_LABEL in table["headers"]
    idx = table["headers"].index(CITEDNESS_LABEL)
    assert table["rows"][0][idx] == "35.46"
    entry = result["_data"]["journals"][0]
    assert entry["two_year_mean_citedness"] == 35.46
    assert "approx_impact_factor" not in entry


def test_no_rendered_output_ever_says_impact_factor(rmock: HttpMock) -> None:
    _mock_openalex(rmock)
    rmock.add("GET", CROSSREF_EHJ_URL, status=200,
              json={"status": "ok", "message": {}})
    outputs = [
        find_matching_journals(query="cardiology"),
        get_journal_profile(journal_name="European Heart Journal"),
        compare_journals(journal_names="European Heart Journal;The Lancet"),
        get_field_landscape(field="cardiology"),
    ]
    for out in outputs:
        text = json.dumps(out, default=str).lower()
        assert "impact factor" not in text
        assert "impact_factor" not in text
        assert "~if" not in text


def test_tool_registry_descriptions_never_say_impact_factor() -> None:
    for name, info in mcp_tools.TOOL_REGISTRY.items():
        assert "impact factor" not in str(info["description"]).lower(), name


def test_agent_card_never_says_impact_factor() -> None:
    from agents.journal_review.journal_review_agent import JournalReviewAgent
    assert "impact factor" not in JournalReviewAgent.description.lower()
    assert "impact-factor" not in JournalReviewAgent.skill_tags


def test_openalex_calls_carry_polite_headers_and_bounds(rmock: HttpMock) -> None:
    _mock_openalex(rmock)
    find_matching_journals(query="cardiology")
    assert rmock.calls, "expected calls intercepted at requests.request (external_http)"
    for call in rmock.calls:
        assert call["headers"]["User-Agent"].startswith("AstralDeep/1.0")
        assert call["allow_redirects"] is False
        assert call["timeout"] == 15


def test_private_dns_resolution_is_blocked_before_any_request(rmock: HttpMock) -> None:
    private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
    with patch("socket.getaddrinfo", return_value=private):
        result = find_matching_journals(query="cardiology")
    assert result["_ui_components"][0]["type"] == "alert"
    assert rmock.calls == []


def test_api_failure_yields_warning_alert_never_fabricated_data() -> None:
    with patch("requests.request", side_effect=requests.ConnectionError("down")):
        result = find_matching_journals(query="cardiology")
    alert = result["_ui_components"][0]
    assert alert["type"] == "alert"
    assert alert["variant"] == "warning"
