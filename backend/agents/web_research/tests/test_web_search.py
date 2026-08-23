"""web_search tests: DDG path, Tavily-compatible provider path, failure paths."""
from unittest.mock import patch

import requests

from agents.web_research import mcp_tools
from agents.web_research.mcp_tools import (
    DDG_HTML_URL,
    DDG_LITE_URL,
    DDGLiteResultParser,
    _parse_ddg_html,
    research_brief,
    web_search,
)
from agents.web_research.tests.test_ddg_parsing import DDG_HTML, EMPTY_HTML
from shared.tests._http_mock import HttpMock

PROVIDER_URL = "https://search.example.com/api"
PROVIDER_CREDS = {"SEARCH_API_URL": PROVIDER_URL, "SEARCH_API_KEY": "sk-sentinel"}

# Trimmed from the live 202 "bots use DuckDuckGo too" puzzle page DuckDuckGo
# serves to non-browser traffic from datacenter addresses: no result anchors.
CHALLENGE_HTML = """<!DOCTYPE html>
<html lang="en"><head><title>DuckDuckGo</title></head><body>
<div class="anomaly-modal__mask">
  <div class="anomaly-modal__modal" data-testid="anomaly-modal">
    <div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div>
    <div class="anomaly-modal__description">Please complete the following
      challenge to confirm this search was made by a human.</div>
    <form class="challenge-form" method="POST">
      <button class="anomaly-modal__submit js-anomaly-modal-submit challenge-submit">Submit</button>
    </form>
  </div>
</div>
</body></html>"""

# Trimmed but structurally faithful lite.duckduckgo.com/lite page: a results
# table with a sponsored row (whose "more info" anchor is ALSO a result-link),
# then organic uddg-wrapped rows with their snippet rows.
LITE_HTML = """<!DOCTYPE html>
<html lang="en"><head><title>python tutorial at DuckDuckGo</title></head><body>
<table border="0">
  <tr class="result-sponsored">
    <td valign="top">1.&nbsp;</td>
    <td>
      <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fduckduckgo.com%2Fy.js%3Fad_domain%3Dads.example&amp;rut=ad1"
         class='result-link'>Sponsored Python Course</a>
      (Sponsored link - <a href="https://duckduckgo.com/duckduckgo-help-pages/company/ads/"
         rel="nofollow" class="result-link">more info</a>)
    </td>
  </tr>
  <tr class="result-sponsored">
    <td>&nbsp;&nbsp;&nbsp;</td>
    <td class='result-snippet'>Learn <b>Python</b> fast, sponsored.</td>
  </tr>
  <tr>
    <td valign="top">2.&nbsp;</td>
    <td>
      <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpython&amp;rut=abc"
         class='result-link'>Python <b>Tutorial</b> &mdash; Example</a>
    </td>
  </tr>
  <tr>
    <td>&nbsp;&nbsp;&nbsp;</td>
    <td class='result-snippet'>
      Learn <b>Python</b> from
      scratch.
    </td>
  </tr>
  <tr>
    <td>&nbsp;&nbsp;&nbsp;</td>
    <td><span class='link-text'>example.com/python</span></td>
  </tr>
  <tr>
    <td valign="top">3.&nbsp;</td>
    <td>
      <a rel="nofollow" href="https://direct.example.org/page" class='result-link'>Direct result</a>
    </td>
  </tr>
  <tr>
    <td>&nbsp;&nbsp;&nbsp;</td>
    <td class='result-snippet'>A direct, unwrapped link.</td>
  </tr>
</table>
</body></html>"""

# A well-formed lite page with no results at all (no challenge markers).
LITE_EMPTY_HTML = """<!DOCTYPE html>
<html lang="en"><head><title>zxqv at DuckDuckGo</title></head><body>
<table border="0"><tr><td>No results.</td></tr></table>
</body></html>"""


# ---------------------------------------------------------------------------
# Keyless DuckDuckGo path
# ---------------------------------------------------------------------------


def test_ddg_search_renders_card_with_detailed_list(rmock: HttpMock) -> None:
    rmock.add("GET", DDG_HTML_URL, status=200, body=DDG_HTML.encode("utf-8"))
    result = web_search(query="python")
    card = result["_ui_components"][0]
    assert card["type"] == "card"
    assert card["title"] == "Search: python"
    listing = card["content"][0]
    assert listing["type"] == "list"
    assert listing["variant"] == "detailed"
    assert listing["items"][0] == {
        "title": "Python Tutorial — Example",
        "url": "https://example.com/python",
        "subtitle": "Learn Python from scratch.",
    }
    assert result["_data"]["backend"] == mcp_tools.DDG_BACKEND


def test_ddg_request_uses_desktop_user_agent_and_query_param(rmock: HttpMock) -> None:
    rmock.add("GET", DDG_HTML_URL, status=200, body=DDG_HTML.encode("utf-8"))
    web_search(query="python tutorials")
    call = rmock.calls[-1]
    assert call["url"] == DDG_HTML_URL
    assert call["params"] == {"q": "python tutorials"}
    assert call["headers"]["User-Agent"].startswith("Mozilla/5.0")


def test_ddg_empty_results_is_info_alert_never_fabricated(rmock: HttpMock) -> None:
    rmock.add("GET", DDG_HTML_URL, status=200, body=EMPTY_HTML.encode("utf-8"))
    result = web_search(query="zxqv-nothing")
    alert = result["_ui_components"][0]
    assert alert["type"] == "alert"
    assert alert["variant"] == "info"
    assert result["_data"]["results"] == []


def test_ddg_unreachable_names_backend_and_remedy() -> None:
    with patch("requests.request", side_effect=requests.ConnectionError("nope")):
        result = web_search(query="python")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "DuckDuckGo" in alert["message"]
    assert "SEARCH_API_URL" in alert["message"]


# ---------------------------------------------------------------------------
# DuckDuckGo bot challenge (HTTP 202) -> Lite retry -> honest failure
# ---------------------------------------------------------------------------


def _ddg_calls(rmock: HttpMock):
    return [c["url"] for c in rmock.calls if c["method"] == "GET"]


def test_ddg_202_challenge_retries_once_via_lite_and_succeeds(rmock: HttpMock) -> None:
    rmock.add("GET", DDG_HTML_URL, status=202, body=CHALLENGE_HTML.encode("utf-8"))
    rmock.add("GET", DDG_LITE_URL, status=200, body=LITE_HTML.encode("utf-8"))
    result = web_search(query="python tutorial")
    assert _ddg_calls(rmock) == [DDG_HTML_URL, DDG_LITE_URL]
    lite_call = rmock.calls[-1]
    assert lite_call["params"] == {"q": "python tutorial"}
    assert lite_call["headers"]["User-Agent"].startswith("Mozilla/5.0")
    assert lite_call["allow_redirects"] is False
    card = result["_ui_components"][0]
    assert card["type"] == "card"
    listing = card["content"][0]
    assert listing["items"] == [
        {"title": "Python Tutorial — Example",
         "url": "https://example.com/python",
         "subtitle": "Learn Python from scratch."},
        {"title": "Direct result",
         "url": "https://direct.example.org/page",
         "subtitle": "A direct, unwrapped link."},
    ]
    assert result["_data"]["backend"] == mcp_tools.DDG_LITE_BACKEND


def test_ddg_200_challenge_body_without_anchors_is_treated_as_challenge(rmock: HttpMock) -> None:
    """A 200 that is really the puzzle page (markers, zero anchors) is not 'no results'."""
    rmock.add("GET", DDG_HTML_URL, status=200, body=CHALLENGE_HTML.encode("utf-8"))
    rmock.add("GET", DDG_LITE_URL, status=200, body=LITE_HTML.encode("utf-8"))
    result = web_search(query="python tutorial")
    assert _ddg_calls(rmock) == [DDG_HTML_URL, DDG_LITE_URL]
    assert result["_ui_components"][0]["type"] == "card"
    assert result["_data"]["backend"] == mcp_tools.DDG_LITE_BACKEND


def test_ddg_202_then_lite_202_is_actionable_error_never_fabricated(rmock: HttpMock) -> None:
    rmock.add("GET", DDG_HTML_URL, status=202, body=CHALLENGE_HTML.encode("utf-8"))
    rmock.add("GET", DDG_LITE_URL, status=202, body=CHALLENGE_HTML.encode("utf-8"))
    result = web_search(query="python tutorial")
    assert _ddg_calls(rmock) == [DDG_HTML_URL, DDG_LITE_URL]  # exactly one retry
    alert = result["_ui_components"][0]
    assert alert["type"] == "alert"
    assert alert["variant"] == "error"
    assert "bot challenge" in alert["title"]
    assert "bot challenge" in alert["message"]
    assert "DuckDuckGo" in alert["message"]
    assert "202" in alert["message"]
    assert "SEARCH_API_URL" in alert["message"]
    assert "No results were fabricated" in alert["message"]
    assert "No results found" not in alert["message"]


def test_ddg_202_then_lite_transport_failure_is_actionable_error(rmock: HttpMock) -> None:
    rmock.add("GET", DDG_HTML_URL, status=202, body=CHALLENGE_HTML.encode("utf-8"))
    rmock.add("GET", DDG_LITE_URL, status=503, body=b"upstream down")
    result = web_search(query="python tutorial")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "bot challenge" in alert["message"]
    assert "Lite endpoint retry failed" in alert["message"]
    assert "SEARCH_API_URL" in alert["message"]


def test_ddg_challenge_surfaces_in_research_brief_as_challenge_error(rmock: HttpMock) -> None:
    """research_brief must not report the challenge as 'returned no results'."""
    rmock.add("GET", DDG_HTML_URL, status=202, body=CHALLENGE_HTML.encode("utf-8"))
    rmock.add("GET", DDG_LITE_URL, status=202, body=CHALLENGE_HTML.encode("utf-8"))
    result = research_brief(topic="python tutorial")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "bot challenge" in alert["message"]
    assert "returned no results" not in alert["message"]
    assert "SEARCH_API_URL" in alert["message"]


def test_ddg_genuine_empty_page_is_still_no_results_without_lite_retry(rmock: HttpMock) -> None:
    """A well-formed 200 page with zero results is an honest empty set, not a challenge."""
    rmock.add("GET", DDG_HTML_URL, status=200, body=EMPTY_HTML.encode("utf-8"))
    rmock.add("GET", DDG_LITE_URL, status=200, body=LITE_HTML.encode("utf-8"))
    result = web_search(query="zxqv-nothing")
    assert _ddg_calls(rmock) == [DDG_HTML_URL]
    alert = result["_ui_components"][0]
    assert alert["variant"] == "info"
    assert result["_data"]["results"] == []
    assert result["_data"]["backend"] == mcp_tools.DDG_BACKEND


def test_ddg_empty_page_echoing_challenge_words_in_query_is_no_results(rmock: HttpMock) -> None:
    """The bare words anomaly/challenge/bots are not markers: the query is echoed on the page."""
    body = EMPTY_HTML.replace(
        "<body>", '<body><input name="q" value="anomaly detection challenge bots">')
    rmock.add("GET", DDG_HTML_URL, status=200, body=body.encode("utf-8"))
    result = web_search(query="anomaly detection challenge bots")
    assert _ddg_calls(rmock) == [DDG_HTML_URL]
    assert result["_ui_components"][0]["variant"] == "info"
    assert result["_data"]["results"] == []


def test_ddg_202_then_lite_genuinely_empty_is_no_results(rmock: HttpMock) -> None:
    rmock.add("GET", DDG_HTML_URL, status=202, body=CHALLENGE_HTML.encode("utf-8"))
    rmock.add("GET", DDG_LITE_URL, status=200, body=LITE_EMPTY_HTML.encode("utf-8"))
    result = web_search(query="zxqv-nothing")
    assert _ddg_calls(rmock) == [DDG_HTML_URL, DDG_LITE_URL]
    assert result["_ui_components"][0]["variant"] == "info"
    assert result["_data"]["results"] == []
    assert result["_data"]["backend"] == mcp_tools.DDG_LITE_BACKEND


def test_ddg_normal_200_with_results_never_touches_lite(rmock: HttpMock) -> None:
    rmock.add("GET", DDG_HTML_URL, status=200, body=DDG_HTML.encode("utf-8"))
    rmock.add("GET", DDG_LITE_URL, status=200, body=LITE_HTML.encode("utf-8"))
    result = web_search(query="python")
    assert _ddg_calls(rmock) == [DDG_HTML_URL]
    assert result["_ui_components"][0]["type"] == "card"
    assert result["_data"]["backend"] == mcp_tools.DDG_BACKEND
    assert [r["url"] for r in result["_data"]["results"]] == [
        "https://example.com/python", "https://direct.example.org/page",
    ]


def test_lite_parser_skips_sponsored_rows_and_decodes_uddg() -> None:
    results = _parse_ddg_html(LITE_HTML, max_results=10, parser_cls=DDGLiteResultParser)
    assert results == [
        {"title": "Python Tutorial — Example",
         "url": "https://example.com/python",
         "snippet": "Learn Python from scratch."},
        {"title": "Direct result",
         "url": "https://direct.example.org/page",
         "snippet": "A direct, unwrapped link."},
    ]


def test_lite_parser_respects_max_results_and_tolerates_garbage() -> None:
    assert len(_parse_ddg_html(LITE_HTML, max_results=1, parser_cls=DDGLiteResultParser)) == 1
    assert _parse_ddg_html("<<<<not html", max_results=5, parser_cls=DDGLiteResultParser) == []
    assert _parse_ddg_html(CHALLENGE_HTML, max_results=5, parser_cls=DDGLiteResultParser) == []


# ---------------------------------------------------------------------------
# Configured provider (Tavily-compatible) path
# ---------------------------------------------------------------------------


def test_provider_path_posts_tavily_compatible_json(rmock: HttpMock) -> None:
    rmock.add("POST", PROVIDER_URL, status=200, json={
        "results": [
            {"title": "Result One", "url": "https://example.com/one",
             "content": "snippet one"},
            {"title": "Result Two", "url": "https://example.com/two",
             "content": "snippet two"},
        ],
    })
    result = web_search(query="python", max_results=5, _credentials=PROVIDER_CREDS)
    call = rmock.calls[-1]
    assert call["method"] == "POST"
    assert call["json"] == {"query": "python", "max_results": 5}
    assert call["headers"]["Authorization"] == "Bearer sk-sentinel"
    listing = result["_ui_components"][0]["content"][0]
    assert [item["url"] for item in listing["items"]] == [
        "https://example.com/one", "https://example.com/two",
    ]
    assert result["_data"]["backend"] == mcp_tools.PROVIDER_BACKEND


def test_provider_max_results_is_clamped_to_twenty(rmock: HttpMock) -> None:
    rmock.add("POST", PROVIDER_URL, status=200, json={"results": []})
    web_search(query="python", max_results=99, _credentials=PROVIDER_CREDS)
    assert rmock.calls[-1]["json"]["max_results"] == 20


def test_provider_malformed_payload_yields_no_results(rmock: HttpMock) -> None:
    rmock.add("POST", PROVIDER_URL, status=200, json={"unexpected": True})
    result = web_search(query="python", _credentials=PROVIDER_CREDS)
    assert result["_ui_components"][0]["variant"] == "info"
    assert result["_data"]["results"] == []


def test_provider_invalid_json_body_yields_no_results(rmock: HttpMock) -> None:
    rmock.add("POST", PROVIDER_URL, status=200, body=b"definitely not json")
    result = web_search(query="python", _credentials=PROVIDER_CREDS)
    assert result["_ui_components"][0]["variant"] == "info"
    assert result["_data"]["results"] == []


def test_provider_non_dict_payload_yields_no_results(rmock: HttpMock) -> None:
    rmock.add("POST", PROVIDER_URL, status=200, json=[1, 2, 3])
    result = web_search(query="python", _credentials=PROVIDER_CREDS)
    assert result["_data"]["results"] == []


def test_provider_skips_malformed_result_items(rmock: HttpMock) -> None:
    rmock.add("POST", PROVIDER_URL, status=200, json={"results": [
        "not-a-dict",
        {"title": "missing url", "content": "x"},
        {"title": "Good", "url": "https://example.com/good", "content": "ok"},
    ]})
    result = web_search(query="python", _credentials=PROVIDER_CREDS)
    assert [r["url"] for r in result["_data"]["results"]] == [
        "https://example.com/good",
    ]


def test_provider_auth_failure_is_error_alert(rmock: HttpMock) -> None:
    rmock.add("POST", PROVIDER_URL, status=401, body=b"{}")
    result = web_search(query="python", _credentials=PROVIDER_CREDS)
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "SEARCH_API_URL" in alert["message"]


def test_provider_on_private_host_is_refused() -> None:
    """Egress gate: a provider URL resolving into RFC1918 space is blocked."""
    creds = {"SEARCH_API_URL": "https://internal.example.com/api",
             "SEARCH_API_KEY": "sk"}
    result = web_search(query="python", _credentials=creds)
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "egress is blocked" in alert["message"]


# ---------------------------------------------------------------------------
# Input validation + credentials check
# ---------------------------------------------------------------------------


def test_empty_query_is_error() -> None:
    result = web_search(query="   ")
    assert result["_ui_components"][0]["variant"] == "error"


def test_bad_max_results_falls_back_to_default(rmock: HttpMock) -> None:
    rmock.add("POST", PROVIDER_URL, status=200, json={"results": []})
    web_search(query="python", max_results="lots", _credentials=PROVIDER_CREDS)
    assert rmock.calls[-1]["json"]["max_results"] == mcp_tools.DEFAULT_MAX_RESULTS


def test_credentials_check_unconfigured_is_ok() -> None:
    result = mcp_tools._credentials_check()
    assert result["credential_test"] == "ok"
    assert "DuckDuckGo" in result["detail"]


def test_credentials_check_ok_on_200(rmock: HttpMock) -> None:
    rmock.add("POST", PROVIDER_URL, status=200, json={"results": []})
    result = mcp_tools._credentials_check(_credentials=PROVIDER_CREDS)
    assert result == {"credential_test": "ok"}


def test_credentials_check_auth_failed(rmock: HttpMock) -> None:
    rmock.add("POST", PROVIDER_URL, status=403, body=b"{}")
    result = mcp_tools._credentials_check(_credentials=PROVIDER_CREDS)
    assert result["credential_test"] == "auth_failed"


def test_credentials_check_unreachable() -> None:
    with patch("requests.request", side_effect=requests.ConnectionError("down")):
        result = mcp_tools._credentials_check(_credentials=PROVIDER_CREDS)
    assert result["credential_test"] == "unreachable"


def test_credentials_check_unexpected_error() -> None:
    with patch("requests.request", side_effect=RuntimeError("surprise")):
        result = mcp_tools._credentials_check(_credentials=PROVIDER_CREDS)
    assert result["credential_test"] == "unexpected"


def test_web_search_unexpected_error_is_error_alert() -> None:
    """Non-HTTP exceptions also surface as actionable error Alerts."""
    with patch("requests.request", side_effect=RuntimeError("surprise")):
        result = web_search(query="python")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "SEARCH_API_URL" in alert["message"]


def test_no_api_key_echoed_in_responses(rmock: HttpMock) -> None:
    import json as json_module
    rmock.add("POST", PROVIDER_URL, status=200, json={"results": []})
    result = web_search(query="python", _credentials=PROVIDER_CREDS)
    assert "sk-sentinel" not in json_module.dumps(result)
