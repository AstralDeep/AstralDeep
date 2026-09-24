"""Verifies closed source disclosures in transient tool results and saved conversation presentation.
Research briefs, errors, source identities, and complete source text remain available.
"""

import copy
from types import SimpleNamespace

import pytest

from orchestrator.canvas_consolidation import consolidate_canvas
from orchestrator.history import _content_parts, _rail_parts
from orchestrator.orchestrator import _tag_tool_result_source
from orchestrator.source_details import present_source_details


def source(title="Source A", tool="fetch_page", **values):
    return {"type": "card", "title": title, "_source_agent": "web-research-1", "_source_tool": tool,
            "content": [{"type": "text", "variant": "markdown", "content": "Source: [A](https://example.org/a)"},
                        {"type": "text", "content": "Full source material " * 1000}], **values}


@pytest.mark.parametrize("tool", ["fetch_page", "web_search", "web-research-1__fetch_page", "web-research-1__web_search"])
def test_source_detail_is_closed_without_losing_content_or_identity(tool):
    original = source(tool=tool, component_id="stored-source", _source_params={"url": "https://example.org/a"})
    before = copy.deepcopy(original)
    result = present_source_details(original)
    assert result == {**original, "type": "collapsible", "default_open": False}
    assert original == before
    assert present_source_details(result) == result


@pytest.mark.parametrize("tool,title", [("web_search", "Search results"), ("fetch_page", "Source details")])
def test_missing_source_title_gets_specific_disclosure_label(tool, title):
    assert present_source_details(source(title="", tool=tool))["title"] == title


@pytest.mark.parametrize("change", [
    {"_source_agent": "other-agent"}, {"_source_tool": "research_brief"},
    {"type": "alert", "variant": "error", "title": "Fetch failed", "message": "Provider unavailable"},
    {"type": "alert", "title": "Content truncated", "message": "Only the first 20,000 characters were fetched"},
])
def test_brief_failure_and_truncation_are_not_hidden(change):
    original = source(**change)
    assert present_source_details(original) == original


def test_tool_tagging_closes_source_before_transient_delivery():
    original = {"type": "card", "title": "Page", "content": [{"type": "text", "content": "Full article"}]}
    _tag_tool_result_source(original, SimpleNamespace(result={}), "web-research-1", "web-research-1__fetch_page", {"url": "https://example.org/a"}, "fetch-1")
    assert original["type"] == "collapsible"
    assert original["default_open"] is False
    assert original["_source_params"]["url"] == "https://example.org/a"
    assert original["content"][0]["content"] == "Full article"


def test_malformed_tool_component_does_not_raise():
    _tag_tool_result_source(None, SimpleNamespace(result={}), "web-research-1", "fetch_page", {})


def test_distinct_historical_sources_stay_closed_in_canvas_and_rail():
    first = source(component_id="old-source-a")
    second = source(title="Source B", component_id="old-source-b")
    consolidated = consolidate_canvas([first, second])
    assert len(consolidated) == 2
    assert all(c["type"] == "collapsible" and c["default_open"] is False for c in consolidated)
    parts = _rail_parts(_content_parts([first, second]))
    assert [p["components"][0]["title"] for p in parts] == ["Source A", "Source B"]
    assert all(p["type"] == "components" for p in parts)
    assert _rail_parts(_content_parts([first]), canvas_component_ids=frozenset({"old-source-a"})) == []


def test_nested_historical_source_is_not_lifted_as_raw_text():
    parts = _rail_parts(_content_parts([{"type": "container", "content": [source()]}]))
    assert len(parts) == 1
    assert parts[0]["components"][0]["type"] == "collapsible"


def forecast(city, **values):
    return {"type": "card", "title": f"Weekly Forecast Summary - {city}", "component_id": f"forecast-{city}",
            "_source_agent": "weather-1", "_source_tool": "weather-1__get_weekly_forecast", "_source_params": {"location": city},
            "content": [{"type": "metric", "title": "Average High", "value": "70°F"},
                        {"type": "alert", "variant": "info", "message": "Overall trend: Stable"}], **values}


def artifact():
    return {"type": "table", "title": "Weekly comparison", "component_id": "comparison", "headers": ["City", "High"],
            "rows": [["Denver", "70°F"], ["Miami", "70°F"]], "_source_agent": "connectors-1", "_source_tool": "connectors-1__interactive_artifacts"}


def test_weather_comparison_is_first_and_original_forecasts_are_closed_after_it():
    originals = [forecast("Denver"), forecast("Miami")]
    components = [*originals, artifact()]
    result = consolidate_canvas(components)
    assert result[0] == artifact()
    assert result[1:] == [{**item, "type": "collapsible", "default_open": False} for item in originals]
    assert consolidate_canvas(result) == result
    assert components == [*originals, artifact()]


def test_forecast_alone_or_after_old_dashboard_stays_prominent():
    original = forecast("Denver")
    assert consolidate_canvas([original]) == [original]
    assert consolidate_canvas([artifact(), original]) == [artifact(), original]


def test_forecast_warning_stays_visible_and_other_agent_cards_are_untouched():
    warning = forecast("Denver", content=[{"type": "alert", "variant": "warning", "message": "Severe weather expected"}])
    other = forecast("Miami", _source_agent="other-agent")
    components = [warning, other, artifact()]
    assert consolidate_canvas(components) == components


def test_nested_dashboard_is_detected_without_reparenting_forecast_identity():
    dashboard = {"type": "grid", "children": [artifact()]}
    result = consolidate_canvas([forecast("Denver"), dashboard])
    assert result[0] == dashboard
    assert result[1]["component_id"] == "forecast-Denver"
    assert result[1]["type"] == "collapsible"


def test_research_comparison_precedes_closed_sources_with_original_order_and_ids():
    sources = [source(title=f"Source {index}", component_id=f"source-{index}", tool="web_search" if index == 0 else "fetch_page") for index in range(4)]
    result = consolidate_canvas([*sources[:3], artifact(), sources[3]])
    assert result[0] == artifact()
    assert result[1:] == [{**item, "type": "collapsible", "default_open": False} for item in sources]
    assert consolidate_canvas(result) == result


def test_research_source_order_stays_unchanged_without_completed_dashboard():
    sources = [source(title="Source A", component_id="source-a"), source(title="Source B", component_id="source-b")]
    result = consolidate_canvas(sources)
    assert result == [{**item, "type": "collapsible", "default_open": False} for item in sources]


def test_forecast_details_keep_real_renderer_action_identity():
    from orchestrator.orchestrator import Orchestrator
    from rote.rote import ROTE

    fake = SimpleNamespace(rote=ROTE())
    socket = object()
    fake.rote.register_device(socket, {})
    snapshot = {"transcript": [], "canvas": {"components": [forecast("Denver"), forecast("Miami"), artifact()]}}
    adapted = Orchestrator._adapt_conversation_snapshot(fake, socket, snapshot)
    assert adapted["canvas"]["components"][0]["component_id"] == "comparison"
    for node in adapted["canvas"]["components"][1:]:
        assert node["component_id"] in node["_presentation"]["html"]
        assert "<details" in node["_presentation"]["html"]
        assert " open" not in node["_presentation"]["html"].split("<summary", 1)[0]
