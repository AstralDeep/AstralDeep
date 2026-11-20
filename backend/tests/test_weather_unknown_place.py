"""An unknown place must produce a clear message, not a Python error."""
from __future__ import annotations

from agents.weather import mcp_tools


def test_current_weather_names_a_place_it_cannot_find(monkeypatch):
    mcp_tools._geocoding_cache.clear()
    monkeypatch.setattr(mcp_tools, "_make_api_request", lambda url, params=None, **kwargs: {})

    result = mcp_tools.get_current_weather(city="Atlantisville")

    message = result["_ui_components"][0]["message"]
    assert "Could not find a place named Atlantisville" in message
    assert "NoneType" not in message


def test_weekly_forecast_names_a_place_it_cannot_find(monkeypatch):
    mcp_tools._geocoding_cache.clear()
    monkeypatch.setattr(mcp_tools, "_make_api_request", lambda url, params=None, **kwargs: {})

    result = mcp_tools.get_weekly_forecast(city="Atlantisville")

    message = result["_ui_components"][0]["message"]
    assert "Could not find a place named Atlantisville" in message
    assert "NoneType" not in message


def test_weather_alerts_report_coordinates_that_cannot_be_placed(monkeypatch):
    monkeypatch.setattr(mcp_tools, "_make_api_request", lambda url, params=None, **kwargs: {})

    result = mcp_tools.get_weather_alerts(latitude=10.0, longitude=20.0)

    message = result["_ui_components"][0]["message"]
    assert message == "Could not geocode location to determine state."
