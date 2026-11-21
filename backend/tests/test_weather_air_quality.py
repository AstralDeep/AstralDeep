"""Tests that air quality comes from the air-quality API and never blocks the forecast."""

from unittest.mock import patch

from agents.weather import mcp_tools


def _responses(url, params):
    if url == mcp_tools.AIR_QUALITY_API_URL:
        return {"current": {"pm2_5": 12.5, "pm10": 20.0, "ozone": 73.0}}
    return {"current": {"temperature_2m": 51, "uv_index": 2}, "daily": {}}


def _air_down(url, params):
    if url == mcp_tools.AIR_QUALITY_API_URL:
        raise Exception("Network error")
    return _responses(url, params)


def test_forecast_request_has_no_air_quality_fields():
    current = mcp_tools._build_weather_params(38.04, -84.5)["current"].split(",")
    assert not set(current) & set(mcp_tools.AIR_QUALITY_FIELDS)
    assert "temperature_2m" in current


def test_extended_weather_shows_air_quality_from_its_own_api():
    with patch.object(mcp_tools, "_make_api_request", side_effect=_responses) as request:
        result = mcp_tools.get_extended_weather(latitude=38.04, longitude=-84.5)
    air_call = next(c for c in request.call_args_list if c.args[0] == mcp_tools.AIR_QUALITY_API_URL)
    assert air_call.args[1]["latitude"] == 38.04
    assert "12.5 μg/m³" in str(result["_ui_components"])


def test_extended_weather_still_works_when_air_quality_is_down():
    with patch.object(mcp_tools, "_make_api_request", side_effect=_air_down):
        result = mcp_tools.get_extended_weather(latitude=38.04, longitude=-84.5)
    assert "N/A μg/m³" in str(result["_ui_components"])
    assert "UV Index" in str(result["_ui_components"])
