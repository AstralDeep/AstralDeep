"""summarize_url and compare_documents tests (HTTP + LLM fully stubbed)."""
import json
from unittest.mock import patch

import requests

from agents.summarizer.mcp_tools import (
    FETCH_MAX_BYTES,
    INPUT_CAP,
    _extract_text,
    compare_documents,
    summarize_url,
)
from shared.tests._http_mock import HttpMock

GOOD_JSON = json.dumps({
    "tldr": "A page about pythons.",
    "key_points": ["Pythons constrict"],
    "quotes": [],
})

PAGE_HTML = """<html><head><title>Python Facts</title>
<script>tracking()</script></head>
<body><nav>menu</nav><p>Pythons are large constricting snakes.</p></body></html>"""


# ---------------------------------------------------------------------------
# summarize_url
# ---------------------------------------------------------------------------


def test_summarize_url_happy_path(rmock: HttpMock, fake_openai) -> None:
    rmock.add("GET", "https://example.com/pythons", status=200,
              body=PAGE_HTML.encode("utf-8"),
              headers={"Content-Type": "text/html"})
    fake_cls = fake_openai(GOOD_JSON)
    result = summarize_url(url="https://example.com/pythons")
    source = result["_ui_components"][0]
    assert source["type"] == "text" and source["variant"] == "markdown"
    assert "https://example.com/pythons" in source["content"]
    tabs = result["_ui_components"][1]
    assert tabs["type"] == "tabs"
    assert result["_data"]["url"] == "https://example.com/pythons"
    assert result["_data"]["title"] == "Python Facts"
    # The fetched page's readable text (not raw HTML) went to the LLM.
    sent = fake_cls.calls_log[-1]["messages"][1]["content"]
    assert "Pythons are large constricting snakes." in sent
    assert "tracking()" not in sent
    assert "<html" not in sent


def test_summarize_url_sends_content_menu_to_model(rmock: HttpMock, fake_openai) -> None:
    html = (
        '<html><body><h1>Seasonal food guide</h1>'
        '<ul id="menu"><li>Roasted squash with rice</li><li>Fresh pear tart</li></ul>'
        '<div role="menubar">Account navigation</div>'
        '<div class="global-menu">Global navigation</div>'
        '<script>tracking()</script></body></html>'
    )
    rmock.add("GET", "https://example.com/food", status=200,
              body=html.encode("utf-8"), headers={"Content-Type": "text/html"})
    fake_cls = fake_openai(GOOD_JSON)
    result = summarize_url(url="https://example.com/food")
    assert result["_data"]["url"] == "https://example.com/food"
    assert len(fake_cls.calls_log) == 1
    sent = fake_cls.calls_log[0]["messages"][1]["content"]
    assert "Roasted squash with rice" in sent and "Fresh pear tart" in sent
    assert "Account navigation" not in sent and "Global navigation" not in sent
    assert "tracking()" not in sent


def test_summarize_url_egress_refusal_on_private_host() -> None:
    result = summarize_url(url="https://internal.example.com/wiki")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "egress is blocked" in alert["message"]


def test_summarize_url_over_one_megabyte_is_refused(rmock: HttpMock) -> None:
    rmock.add("GET", "https://example.com/huge", status=200,
              body=b"y" * (FETCH_MAX_BYTES + 1))
    result = summarize_url(url="https://example.com/huge")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "1 MB" in alert["message"]


def test_summarize_url_unreachable_is_error() -> None:
    with patch("requests.request", side_effect=requests.ConnectionError("down")):
        result = summarize_url(url="https://example.com/pythons")
    assert result["_ui_components"][0]["variant"] == "error"


def test_summarize_url_no_readable_text_is_error(rmock: HttpMock) -> None:
    rmock.add("GET", "https://example.com/blank", status=200, body=b"")
    result = summarize_url(url="https://example.com/blank")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "No readable text" in alert["message"]


def test_summarize_url_empty_url_is_error() -> None:
    result = summarize_url(url="")
    assert result["_ui_components"][0]["variant"] == "error"


def test_summarize_url_follows_redirect(rmock: HttpMock, fake_openai) -> None:
    rmock.add("GET", "https://redirect.example.com/old", status=301, body=b"",
              headers={"Location": "https://example.com/pythons"})
    rmock.add("GET", "https://example.com/pythons", status=200,
              body=PAGE_HTML.encode("utf-8"),
              headers={"Content-Type": "text/html"})
    fake_openai(GOOD_JSON)
    result = summarize_url(url="https://redirect.example.com/old")
    assert result["_ui_components"][0]["type"] == "text"  # source link
    assert result["_ui_components"][1]["type"] == "tabs"
    assert result["_data"]["title"] == "Python Facts"


def test_summarize_url_redirect_without_location_is_error(rmock: HttpMock) -> None:
    rmock.add("GET", "https://redirect.example.com/nowhere", status=302, body=b"")
    result = summarize_url(url="https://redirect.example.com/nowhere")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "Location" in alert["message"]


def test_summarize_url_redirect_loop_is_error(rmock: HttpMock) -> None:
    rmock.add("GET", "https://redirect.example.com/loop", status=301, body=b"",
              headers={"Location": "https://redirect.example.com/loop"})
    result = summarize_url(url="https://redirect.example.com/loop")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "Too many redirects" in alert["message"]


def test_extract_text_non_html_passthrough() -> None:
    class _Resp:
        text = "plain body"
        headers = {"Content-Type": "text/plain"}
    title, text = _extract_text(_Resp())
    assert title == ""
    assert text == "plain body"


# ---------------------------------------------------------------------------
# compare_documents
# ---------------------------------------------------------------------------

SUMMARY_A = json.dumps({"tldr": "Doc A says X.", "key_points": ["A1"], "quotes": []})
SUMMARY_B = json.dumps({"tldr": "Doc B says Y.", "key_points": ["B1"], "quotes": []})
COMPARISON = json.dumps({
    "differences": [
        {"aspect": "Conclusion", "a": "supports X", "b": "supports Y"},
        {"aspect": "Tone", "a": "formal", "b": "casual"},
    ],
})


def test_compare_documents_grid_and_table(fake_openai) -> None:
    fake_cls = fake_openai(SUMMARY_A, SUMMARY_B, COMPARISON)
    result = compare_documents(
        text_a="Document text A.", text_b="Document text B.",
        labels=["Spec v1", "Spec v2"],
    )
    grid, table = result["_ui_components"][0], result["_ui_components"][1]
    assert grid["type"] == "grid"
    assert grid["columns"] == 2
    cards = grid["children"]
    assert [card["title"] for card in cards] == ["Spec v1", "Spec v2"]
    assert cards[0]["content"][0]["content"] == "Doc A says X."
    assert cards[0]["content"][1]["items"] == ["A1"]

    assert table["type"] == "table"
    assert table["headers"] == ["Aspect", "Spec v1", "Spec v2"]
    assert table["rows"] == [
        ["Conclusion", "supports X", "supports Y"],
        ["Tone", "formal", "casual"],
    ]
    # Exactly three LLM calls: two summaries + ONE comparison.
    assert len(fake_cls.calls_log) == 3


def test_compare_documents_default_labels(fake_openai) -> None:
    fake_openai(SUMMARY_A, SUMMARY_B, COMPARISON)
    result = compare_documents(text_a="aaa", text_b="bbb")
    table = result["_ui_components"][1]
    assert table["headers"] == ["Aspect", "Document A", "Document B"]


def test_compare_documents_truncation_notices(fake_openai) -> None:
    fake_openai(SUMMARY_A, SUMMARY_B, COMPARISON)
    result = compare_documents(
        text_a="a" * (INPUT_CAP + 1), text_b="b" * (INPUT_CAP + 1),
        labels=["Left", "Right"],
    )
    alerts = [c for c in result["_ui_components"]
              if c["type"] == "alert" and c["variant"] == "info"]
    assert len(alerts) == 2
    assert any("Left" in a["message"] for a in alerts)
    assert any("Right" in a["message"] for a in alerts)


def test_compare_documents_no_differences_placeholder_row(fake_openai) -> None:
    fake_openai(SUMMARY_A, SUMMARY_B, json.dumps({"differences": []}))
    result = compare_documents(text_a="same", text_b="same")
    table = result["_ui_components"][1]
    assert table["rows"] == [["(no notable differences identified)", "—", "—"]]


def test_compare_documents_malformed_comparison_is_tolerated(fake_openai) -> None:
    fake_openai(SUMMARY_A, SUMMARY_B, "not json")
    result = compare_documents(text_a="aaa", text_b="bbb")
    table = result["_ui_components"][1]
    assert table["rows"][0][0] == "(no notable differences identified)"


def test_compare_documents_skips_malformed_difference_entries(fake_openai) -> None:
    comparison = json.dumps({"differences": [
        42,
        {"a": "missing aspect"},
        {"aspect": "Tone", "a": "formal", "b": "casual"},
    ]})
    fake_openai(SUMMARY_A, SUMMARY_B, comparison)
    result = compare_documents(text_a="aaa", text_b="bbb")
    table = result["_ui_components"][1]
    assert table["rows"] == [["Tone", "formal", "casual"]]


def test_compare_documents_missing_input_is_error() -> None:
    result = compare_documents(text_a="present", text_b="  ")
    assert result["_ui_components"][0]["variant"] == "error"


def test_compare_documents_llm_unavailable(no_llm_credentials) -> None:
    result = compare_documents(text_a="aaa", text_b="bbb")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "LLM" in alert["message"]


def test_comparison_llm_raises_when_unconfigured(no_llm_credentials) -> None:
    import pytest
    from agents.summarizer.mcp_tools import LlmUnavailableError, _call_comparison_llm
    with pytest.raises(LlmUnavailableError):
        _call_comparison_llm(("A", "aaa"), ("B", "bbb"), {})


def test_compare_documents_llm_exception(fake_openai) -> None:
    fake_openai(RuntimeError("boom"))
    result = compare_documents(text_a="aaa", text_b="bbb")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "boom" in alert["message"]
