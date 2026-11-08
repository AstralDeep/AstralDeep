"""Tests that get_weather_alerts decides US coverage from the geocoder's country code."""

from unittest.mock import MagicMock, patch

from agents.weather import mcp_tools


def _place(city, country, country_code, admin1):
    return {"results": [{
        "name": city, "latitude": 38.04, "longitude": -84.5, "country": country,
        "country_code": country_code, "admin1": admin1, "timezone": "America/New_York",
    }]}


def _alerts(place, features=()):
    city = place["results"][0]["name"]
    response = MagicMock()
    response.json.return_value = {"features": list(features)}
    with patch.object(mcp_tools, "_make_api_request", return_value=place), \
            patch.object(mcp_tools.requests, "get", return_value=response) as get:
        result = mcp_tools.get_weather_alerts(city=city, state=place["results"][0]["admin1"])
    return result, get


def test_a_us_place_reaches_the_national_weather_service():
    result, get = _alerts(_place("Lexington", "United States", "US", "Kentucky"))
    assert get.call_args.args[0] == "https://api.weather.gov/alerts/active?area=KY"
    assert "No active weather alerts for Kentucky" in str(result["_ui_components"])


def test_a_place_outside_the_us_is_refused_by_country_name():
    result, get = _alerts(_place("Toronto", "Canada", "CA", "Ontario"))
    get.assert_not_called()
    assert "only available for US locations. Country: Canada" in str(result["_ui_components"])


def test_active_alerts_render_when_the_place_has_no_state_name():
    feature = {"properties": {"headline": "Freeze Warning", "severity": "Severe"}}
    place = _place("Testville", "United States", "US", "")
    with patch.object(mcp_tools, "_make_api_request", return_value=place), \
            patch.object(mcp_tools.requests, "get") as get:
        get.return_value.json.return_value = {"features": [feature]}
        result = mcp_tools.get_weather_alerts(city="Testville", state="KY")
    assert "Freeze Warning" in str(result["_ui_components"])
    get.return_value.json.return_value = {"features": [feature]}
    place = _place("Noville", "United States", "US", "Kentucky")
    with patch.object(mcp_tools, "_make_api_request", return_value=place), \
            patch.object(mcp_tools.requests, "get", return_value=get.return_value):
        result = mcp_tools.get_weather_alerts(city="Noville", state=None)
    assert "Noville, Kentucky" in str(result["_ui_components"])
