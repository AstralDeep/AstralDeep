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


def _cards(value):
    if isinstance(value, dict):
        yield value
        value = list(value.values())
    if isinstance(value, list):
        for child in value:
            yield from _cards(child)


def _card(result, title):
    return next(json.dumps(card) for card in _cards(result["_ui_components"]) if card.get("title") == title)


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


def test_failed_locations_are_named_in_plain_text(monkeypatch):
    mcp_tools._geocoding_cache.clear()
    monkeypatch.setattr(mcp_tools, "_make_api_request", lambda url, params=None, **kwargs: {})
    result = mcp_tools.compare_locations(city1="Atlantisville", city2="Elsewhereton")
    message = result["_ui_components"][0]["message"]
    assert message.startswith("Failed to fetch weather for some locations: ")
    assert "Atlantisville" in message and "Elsewhereton" in message
    assert "; " in message and "{'type'" not in message
