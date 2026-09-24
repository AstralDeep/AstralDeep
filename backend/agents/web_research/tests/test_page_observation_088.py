"""Tests that fetch_page's page-observation metadata reflects only the actual
fixed-reader response, never invented transport facts, using deterministic synthetic
responses.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from agents.web_research import mcp_tools
from persistent_agents.research_result import (
    legacy_page_response,
    read_page_observation,
)
from persistent_agents.runtime_values import extract_result


@pytest.mark.parametrize(
    "media,profile,body",
    [
        (
            "text/html; charset=UTF-8",
            "html_readable_v1",
            b"<html><title>Source</title><p>Visible</p></html>",
        ),
        ("application/xhtml+xml", "html_readable_v1", b"<html><p>Visible</p></html>"),
        ("text/plain", "plain_text_v1", b"Visible plain text"),
    ],
)
def test_actual_redirect_response_identity_and_extraction(rmock, media, profile, body):
    original, final = "https://redirect.example.com/first", "https://example.com/final"
    rmock.add("GET", original, status=302, headers={"Location": final})
    rmock.add("GET", final, body=body, headers={"Content-Type": media})
    for _, url, response in rmock.routes:
        response.url = url
    result = mcp_tools.fetch_page(original)
    value = read_page_observation(
        SimpleNamespace(result=result), requested_url=original
    )
    assert value["final_url"] == final and value["requested_url"] == original
    assert value["media_type"] == media.split(";", 1)[0]
    assert value["extraction_profile"] == profile
    assert (
        value["body_complete"]
        and value["extraction_complete"]
        and value["excerpt_complete"]
    )
    assert datetime.fromisoformat(value["retrieved_at"]).utcoffset() == timedelta(0)
    assert [call["url"] for call in rmock.calls] == [original, final]
    assert all("Authorization" not in call["headers"] for call in rmock.calls)


def test_partial_body_and_excerpt_are_distinct_from_completed_extractor(rmock):
    url = "https://example.com/partial"
    rmock.add(
        "GET",
        url,
        status=206,
        body=b"word " * 5000,
        headers={"Content-Type": "text/plain", "Content-Range": "bytes 0-24999/40000"},
    )
    rmock.routes[0][2].url = url
    result = mcp_tools.fetch_page(url)
    value = read_page_observation(SimpleNamespace(result=result), requested_url=url)
    assert value["body_complete"] is False
    assert (
        value["extraction_complete"] is True
    )
    assert value["excerpt_complete"] is False and value["redacted"] is False
    assert len(value["text"]) == mcp_tools.PAGE_TEXT_CAP
    old = {
        **result,
        "_data": {k: v for k, v in result["_data"].items() if k != "page_observation"},
    }
    assert extract_result(
        legacy_page_response(SimpleNamespace(result=result))
    ) == extract_result(SimpleNamespace(result=old))


def test_missing_actual_transport_url_is_not_invented(rmock):
    url = "https://example.com/missing"
    rmock.add("GET", url, body=b"Visible", headers={"Content-Type": "text/plain"})
    result = mcp_tools.fetch_page(url)
    assert result["_data"]["page_observation"]["final_url"] is None
    assert result["_ui_components"]
    with pytest.raises(ValueError, match="^assignment_source_observation_invalid$"):
        read_page_observation(SimpleNamespace(result=result), requested_url=url)
