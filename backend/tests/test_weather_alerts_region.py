"""Tests that get_weather_alerts decides US coverage from the geocoder's country code."""

from unittest.mock import MagicMock, patch

from agents.weather import mcp_tools


def _place(country, country_code, admin1):
    return {"results": [{
        "name": "Lexington", "latitude": 38.04, "longitude": -84.5, "country": country,
        "country_code": country_code, "admin1": admin1, "timezone": "America/New_York",
    }]}


def _alerts(place):
    response = MagicMock()
    response.json.return_value = {"features": []}
    with patch.object(mcp_tools, "_make_api_request", return_value=place), \
            patch.object(mcp_tools.requests, "get", return_value=response) as get:
        result = mcp_tools.get_weather_alerts(city="Lexington", state="Kentucky")
    return result, get


def test_a_us_place_reaches_the_national_weather_service():
    result, get = _alerts(_place("United States", "US", "Kentucky"))
    assert get.call_args.args[0] == "https://api.weather.gov/alerts/active?area=KY"
    assert "No active weather alerts for Kentucky" in str(result["_ui_components"])


def test_a_place_outside_the_us_is_refused_by_country_name():
    result, get = _alerts(_place("Canada", "CA", "Ontario"))
    get.assert_not_called()
    assert "only available for US locations. Country: Canada" in str(result["_ui_components"])
