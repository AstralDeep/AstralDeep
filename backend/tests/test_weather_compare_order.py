"""Tests that compare_locations keeps each location's weather under its own name."""

import json
import time
from unittest.mock import patch

from agents.weather import mcp_tools


def _current(city, **_kwargs):
    if city == "Lexington":
        time.sleep(0.2)
    temperature = {"Lexington": 51, "Miami": 83}[city]
    return {"_data": {"current": {"temperature_2m": temperature}}}


def _card(result, title):
    return next(
        json.dumps(component) for component in result["_ui_components"][0]["content"]
        if component.get("title") == title
    )


def test_a_slower_first_location_keeps_its_own_result():
    with patch.object(mcp_tools, "get_current_weather", side_effect=_current):
        result = mcp_tools.compare_locations(
            city1="Lexington", state1="KY", city2="Miami", state2="FL")
    assert "51" in _card(result, "Lexington, KY")
    assert "83" in _card(result, "Miami, FL")


def test_a_location_with_no_data_is_reported_as_a_failure():
    refused = {"_ui_components": [{"type": "alert", "message": "Unknown place"}], "_data": None}
    with patch.object(mcp_tools, "get_current_weather", return_value=refused):
        result = mcp_tools.compare_locations(city1="Nowhere", city2="Elsewhere")
    assert "Failed to fetch weather for some locations" in str(result["_ui_components"])
