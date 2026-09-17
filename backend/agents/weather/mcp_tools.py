#!/usr/bin/env python3
"""
MCP Tools for Weather Agent — tool functions that return UI Primitives.

Includes:
- Geocoding tools: geocode_location
- Weather tools: get_current_weather, get_hourly_forecast, get_daily_forecast, get_weekly_forecast
- Visualization tools: generate_weather_charts
"""
import asyncio
import os
import sys
import logging
import concurrent.futures
from typing import Dict, Any, Optional, Tuple, AsyncIterator
from datetime import datetime

import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from astralprims import (
    Text, Card, Table, MetricCard, Alert, Grid, PlotlyChart, create_ui_response,
    Gauge, StatGroup
)
from shared.stream_sdk import streaming_tool, StreamComponents
from .data_models import ExtendedWeatherData, HistoricalWeatherData, WeatherAlert

logger = logging.getLogger(__name__)

# Weather code mapping (WMO codes)
WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow fall", 73: "Moderate snow fall", 75: "Heavy snow fall",
    77: "Snow grains", 80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail"
}

# Open-Meteo API configuration
OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1"
GEOCODING_API_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Rate limiting configuration
RATE_LIMIT_REQUESTS = 100  # Open-Meteo free tier limit per day
RATE_LIMIT_WINDOW = 86400  # 24 hours in seconds

# Cache for geocoding results to reduce API calls
_geocoding_cache = {}


def _make_api_request(url: str, params: Dict, timeout: int = 10) -> Dict:
    """Make HTTP request to Open-Meteo API with error handling."""
    try:
        headers = {
            "User-Agent": "AstralDeep/1.0 (Weather Agent)"
        }
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        raise Exception(f"API request timed out after {timeout} seconds")
    except requests.exceptions.HTTPError as e:
        if response.status_code == 429:
            raise Exception("Rate limit exceeded. Please try again later.")
        else:
            raise Exception(f"API error: {e}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Network error: {e}")


def _build_weather_params(latitude: float, longitude: float, extra_current: str = "", extra_daily: str = "") -> Dict[str, Any]:
    """
    Build standard Open-Meteo forecast API parameters.
    
    Args:
        latitude: Latitude coordinate
        longitude: Longitude coordinate
        extra_current: Additional current parameters to include (comma-separated)
        extra_daily: Additional daily parameters to include (comma-separated)
        
    Returns:
        Dictionary of API parameters
    """
    current_params = "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m,pressure_msl,uv_index,pm2_5,pm10,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone"
    daily_params = "sunrise,sunset"
    
    if extra_current:
        current_params += "," + extra_current
    if extra_daily:
        daily_params += "," + extra_daily
        
    return {
        "latitude": latitude,
        "longitude": longitude,
        "current": current_params,
        "daily": daily_params,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto"
    }


def _validate_date_range(start_date: str, end_date: Optional[str] = None, max_days: int = 92) -> Tuple[datetime.date, datetime.date]:
    """
    Validate date range for historical weather queries.
    
    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format (optional, defaults to start_date)
        max_days: Maximum allowed date range (default: 92)
        
    Returns:
        Tuple of (start_date_obj, end_date_obj) as date objects
        
    Raises:
        ValueError: If dates are invalid, in the future, or range too large
    """
    from datetime import datetime, date
    today = date.today()
    
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Invalid start_date format: {start_date}. Use YYYY-MM-DD.")
    
    if end_date is None:
        end = start
    else:
        try:
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"Invalid end_date format: {end_date}. Use YYYY-MM-DD.")
    
    if start > end:
        raise ValueError(f"start_date ({start_date}) cannot be after end_date ({end_date or start_date}).")
    
    # Historical API only supports past dates (up to yesterday)
    if start > today or end > today:
        raise ValueError(f"Historical dates cannot be in the future. Today is {today}.")
    
    delta = (end - start).days + 1
    if delta > max_days:
        raise ValueError(f"Date range too large: {delta} days. Maximum is {max_days} days.")
    
    return start, end


def geocode_location(
    city: str,
    state: Optional[str] = None,
    country: str = "US",
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    """
    Convert city and state names to latitude/longitude coordinates.
    
    Args:
        city: City name (required)
        state: State/province name (optional)
        country: Country code (default: "US")
        
    Returns:
        Dict with _ui_components and _data keys.
    """
    cache_key = f"{city},{state},{country}".lower()
    if cache_key in _geocoding_cache:
        logger.info(f"Using cached geocoding result for {cache_key}")
        cached_result = _geocoding_cache[cache_key]
        return {
            "_ui_components": [
                Card(
                    title="Location Coordinates",
                    id="geocode-card",
                    content=[
                        Alert(
                            message=f"Found coordinates for {city}, {state or country} (cached)",
                            variant="info"
                        ),
                        Grid(
                            columns=2,
                            children=[
                                MetricCard(
                                    title="Latitude",
                                    value=f"{cached_result['latitude']:.4f}°",
                                    id="lat-metric"
                                ),
                                MetricCard(
                                    title="Longitude",
                                    value=f"{cached_result['longitude']:.4f}°",
                                    id="lon-metric"
                                ),
                            ]
                        ),
                        Text(
                            content=f"Location: {cached_result['name']}, {cached_result.get('admin1', '')}, {cached_result.get('country', '')}",
                            variant="caption"
                        )
                    ]
                ).to_dict()
            ],
            "_data": cached_result
        }
    
    try:
        # Build search queries with fallback strategy
        queries_to_try = []
        
        # 1. Full query: city, state, country
        query_parts = [city]
        if state:
            query_parts.append(state)
        query_parts.append(country)
        queries_to_try.append(", ".join(query_parts))
        
        # 2. City and country only
        queries_to_try.append(f"{city}, {country}")
        
        # 3. City and state only (if state provided)
        if state:
            queries_to_try.append(f"{city}, {state}")
        
        # 4. City only
        queries_to_try.append(city)
        
        # Try each query until we get results
        data = None
        successful_query = None
        
        for query in queries_to_try:
            params = {
                "name": query,
                "count": 5,
                "language": "en",
                "format": "json"
            }
            
            try:
                logger.info(f"Geocoding location with query: {query}")
                data = _make_api_request(GEOCODING_API_URL, params)
                
                if data.get("results"):
                    successful_query = query
                    break
            except Exception as e:
                logger.warning(f"Query '{query}' failed: {e}")
                continue
        
        if not data or not data.get("results"):
            error_msg = f"No results found for '{city}'"
            if state:
                error_msg += f", {state}"
            error_msg += f", {country}. Please check the city and state names."
            
            suggestions = []
            if "," in city:
                suggestions.append("Try removing commas from the city name.")
            suggestions.append("Try using just the city name.")
            if country != "US":
                suggestions.append("Try using the local name for the city.")
            
            suggestion_text = " ".join(suggestions)
            
            return create_ui_response([
                Alert(
                    message=error_msg,
                    variant="error",
                    title="Geocoding Failed"
                ),
                Text(
                    content=f"Suggestions: {suggestion_text}",
                    variant="caption"
                )
            ])
        
        # Use the first result (most relevant)
        result = data["results"][0]
        location_data = {
            "name": result.get("name", ""),
            "latitude": result.get("latitude", 0),
            "longitude": result.get("longitude", 0),
            "elevation": result.get("elevation", 0),
            "feature_code": result.get("feature_code", ""),
            "country": result.get("country", ""),
            "admin1": result.get("admin1", ""),  # State/province
            "timezone": result.get("timezone", ""),
            "population": result.get("population", 0)
        }
        
        # Cache the result
        _geocoding_cache[cache_key] = location_data
        
        # Update success message to show which query worked
        success_msg = f"Successfully geocoded {city}"
        if successful_query != city:  # If we used a different query
            success_msg += f" (using query: {successful_query})"
        
        components = [
            Card(
                title="Location Coordinates",
                id="geocode-card",
                content=[
                    Alert(
                        message=success_msg,
                        variant="success"
                    ),
                    Grid(
                        columns=2,
                        children=[
                            MetricCard(
                                title="Latitude",
                                value=f"{location_data['latitude']:.4f}°",
                                id="lat-metric"
                            ),
                            MetricCard(
                                title="Longitude",
                                value=f"{location_data['longitude']:.4f}°",
                                id="lon-metric"
                            ),
                        ]
                    ),
                    Text(
                        content=f"Location: {location_data['name']}, {location_data.get('admin1', '')}, {location_data.get('country', '')}",
                        variant="caption"
                    ),
                    Text(
                        content=f"Timezone: {location_data.get('timezone', 'Unknown')}",
                        variant="caption"
                    )
                ]
            )
        ]
        
        return {
            "_ui_components": [c.to_dict() for c in components],
            "_data": location_data
        }
        
    except Exception as e:
        return create_ui_response([
            Alert(
                message=f"Geocoding failed: {str(e)}",
                variant="error"
            )
        ])


def _get_coordinates_from_args(**kwargs) -> Tuple[float, float]:
    """Extract latitude and longitude from arguments."""
    # Check if latitude and longitude are provided and not None
    latitude = kwargs.get("latitude")
    longitude = kwargs.get("longitude")
    
    if latitude is not None and longitude is not None:
        try:
            return float(latitude), float(longitude)
        except (TypeError, ValueError) as e:
            raise ValueError(f"Invalid coordinates: {latitude}, {longitude}. Error: {e}")
    
    # Otherwise, geocode using city/state
    city = kwargs.get("city")
    state = kwargs.get("state")
    country = kwargs.get("country", "US")
    
    if not city:
        raise ValueError("Either provide latitude/longitude or city name")
    
    # Use geocoding function
    geocode_result = geocode_location(city, state, country)
    if "_data" not in geocode_result:
        raise ValueError(f"Could not geocode location: {city}, {state}")
    
    data = geocode_result["_data"]
    return data["latitude"], data["longitude"]



def _fraction(percent) -> float:
    """A 0-100 reading as the 0-1 fraction ``Gauge`` and ``ProgressBar`` use.

    Returns 0.0 for a missing or unparseable reading rather than raising: a
    weather panel that fails because one field was absent is worse than a
    gauge reading zero beside the number that says otherwise.
    """
    try:
        value = float(percent)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return max(0.0, min(1.0, value / 100.0))


def get_current_weather(
    city: Optional[str] = None,
    state: Optional[str] = None,
    country: str = "US",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    """
    Get current weather conditions for a location.
    
    Args:
        city: City name (optional if latitude/longitude provided)
        state: State/province name (optional)
        country: Country code (default: "US")
        latitude: Latitude coordinate (optional if city provided)
        longitude: Longitude coordinate (optional if city provided)
        
    Returns:
        Dict with _ui_components and _data keys.
    """
    try:
        # Get coordinates
        lat, lon = _get_coordinates_from_args(
            city=city, state=state, country=country,
            latitude=latitude, longitude=longitude
        )
        
        # Build API request
        params = _build_weather_params(lat, lon)
        
        logger.info(f"Fetching current weather for coordinates: {lat}, {lon}")
        data = _make_api_request(f"{OPEN_METEO_BASE_URL}/forecast", params)
        
        current = data.get("current", {})
        
        # Map weather codes to human-readable descriptions
        weather_code = current.get("weather_code", 0)
        weather_desc = WEATHER_CODES.get(weather_code, "Unknown")
        
        # Determine variant based on conditions
        variant = "default"
        if weather_code in [95, 96, 99, 65, 67, 75, 82, 86]:
            variant = "error"  # Severe weather
        elif weather_code in [61, 63, 71, 73, 80, 81, 85]:
            variant = "warning"  # Moderate precipitation
        
        # Build UI components
        location_str = f"{city or 'Unknown'}, {state or country}" if city else f"{lat:.4f}°, {lon:.4f}°"
        
        components = [
            Card(
                title=f"Current Weather - {location_str}",
                id="current-weather-card",
                content=[
                    # Feature 089: current conditions are a set of readings,
                    # which is what a stat group is. Non-web clients receive
                    # the ROTE fallback -- a grid of metric tiles -- so their
                    # rendering is unchanged.
                    StatGroup(
                        title="Current conditions",
                        columns=4,
                        id="current-conditions",
                        items=[
                            {
                                "label": "Temperature",
                                "value": f"{current.get('temperature_2m', 'N/A')}°F",
                                "hint": (
                                    "Feels like "
                                    f"{current.get('apparent_temperature', 'N/A')}°F"
                                ),
                                "variant": variant,
                            },
                            {
                                "label": "Wind",
                                "value": f"{current.get('wind_speed_10m', 'N/A')} mph",
                                "hint": (
                                    "Direction "
                                    f"{current.get('wind_direction_10m', 'N/A')}°"
                                ),
                            },
                            {
                                "label": "Pressure",
                                "value": f"{current.get('pressure_msl', 'N/A')} hPa",
                            },
                        ],
                    ),
                    # Humidity is a bounded percentage, which is the one thing
                    # a gauge reads better than a number does.
                    Gauge(
                        label="Humidity",
                        value=_fraction(current.get("relative_humidity_2m")),
                        display_value=f"{current.get('relative_humidity_2m', 'N/A')}%",
                        id="humidity-gauge",
                        thresholds=[
                            {"at": 0.0, "variant": "default"},
                            {"at": 0.70, "variant": "warning"},
                            {"at": 0.90, "variant": "error"},
                        ],
                    ),
                    Alert(
                        message=weather_desc,
                        variant=variant,
                        title="Conditions"
                    ),
                    Text(
                        content=f"Precipitation: {current.get('precipitation', '0')} in",
                        variant="body"
                    ),
                    Text(
                        content=f"Last updated: {current.get('time', 'Unknown')}",
                        variant="caption"
                    )
                ]
            )
        ]
        
        return {
            "_ui_components": [c.to_dict() for c in components],
            "_data": {
                "location": location_str,
                "coordinates": {"latitude": lat, "longitude": lon},
                "current": current,
                "weather_description": weather_desc
            }
        }
        
    except Exception as e:
        return create_ui_response([
            Alert(
                message=f"Failed to get current weather: {str(e)}",
                variant="error"
            )
        ])


def get_extended_weather(
    city: Optional[str] = None,
    state: Optional[str] = None,
    country: str = "US",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    """
    Get extended weather data including UV index, air quality, and sunrise/sunset.
    
    Args:
        city: City name (optional if latitude/longitude provided)
        state: State/province name (optional)
        country: Country code (default: "US")
        latitude: Latitude coordinate (optional if city provided)
        longitude: Longitude coordinate (optional if city provided)
        
    Returns:
        Dict with _ui_components and _data keys.
    """
    try:
        # Get coordinates
        lat, lon = _get_coordinates_from_args(
            city=city, state=state, country=country,
            latitude=latitude, longitude=longitude
        )
        
        # Build API request (same as get_current_weather but we already added extra parameters)
        params = _build_weather_params(lat, lon)
        
        logger.info(f"Fetching extended weather for coordinates: {lat}, {lon}")
        data = _make_api_request(f"{OPEN_METEO_BASE_URL}/forecast", params)
        
        current = data.get("current", {})
        daily = data.get("daily", {})
        
        # Create extended data model
        extended = ExtendedWeatherData.from_api_response(current, daily)
        
        # Build UI components
        location_str = f"{city or 'Unknown'}, {state or country}" if city else f"{lat:.4f}°, {lon:.4f}°"
        
        # Metric cards for UV, AQI, sunrise, sunset
        metric_grid = Grid(
            columns=4,
            children=[
                MetricCard(
                    title="UV Index",
                    value=f"{extended.uv_index or 'N/A'}",
                    subtitle="0-11+ scale",
                    variant="warning" if extended.uv_index and extended.uv_index > 5 else "default",
                    id="uv-metric"
                ),
                MetricCard(
                    title="Air Quality (PM2.5)",
                    value=f"{extended.pm2_5 or 'N/A'} μg/m³",
                    subtitle="PM2.5 concentration",
                    variant="error" if extended.pm2_5 and extended.pm2_5 > 35 else "default",
                    id="aqi-metric"
                ),
                MetricCard(
                    title="Sunrise",
                    value=extended.sunrise.split('T')[1][:5] if extended.sunrise else "N/A",
                    subtitle="Local time",
                    id="sunrise-metric"
                ),
                MetricCard(
                    title="Sunset",
                    value=extended.sunset.split('T')[1][:5] if extended.sunset else "N/A",
                    subtitle="Local time",
                    id="sunset-metric"
                ),
            ]
        )
        
        # Additional air quality metrics if available
        extra_metrics = []
        if extended.pm10 is not None:
            extra_metrics.append(MetricCard(title="PM10", value=f"{extended.pm10} μg/m³", id="pm10-metric"))
        if extended.carbon_monoxide is not None:
            extra_metrics.append(MetricCard(title="CO", value=f"{extended.carbon_monoxide} μg/m³", id="co-metric"))
        if extended.nitrogen_dioxide is not None:
            extra_metrics.append(MetricCard(title="NO₂", value=f"{extended.nitrogen_dioxide} μg/m³", id="no2-metric"))
        
        extra_grid = None
        if extra_metrics:
            extra_grid = Grid(columns=len(extra_metrics), children=extra_metrics)
        
        components = [
            Card(
                title=f"Extended Weather - {location_str}",
                id="extended-weather-card",
                content=[
                    metric_grid,
                    *([extra_grid] if extra_grid else []),
                    Alert(
                        message="Data provided by Open-Meteo. UV index and air quality may not be available for all locations.",
                        variant="info"
                    )
                ]
            )
        ]
        
        return {
            "_ui_components": [c.to_dict() for c in components],
            "_data": {
                "location": location_str,
                "coordinates": {"latitude": lat, "longitude": lon},
                "extended": extended,
                "current": current,
                "daily": daily
            }
        }
        
    except Exception as e:
        return create_ui_response([
            Alert(
                message=f"Failed to get extended weather: {str(e)}",
                variant="error"
            )
        ])


def get_historical_weather(
    city: Optional[str] = None,
    state: Optional[str] = None,
    country: str = "US",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    start_date: str = "2025-01-01",
    end_date: Optional[str] = None,
    daily: str = "temperature_2m_max,precipitation_sum,weather_code",
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    """
    Get historical weather data for a location and date range.
    
    Args:
        city: City name (optional if latitude/longitude provided)
        state: State/province name (optional)
        country: Country code (default: "US")
        latitude: Latitude coordinate (optional if city provided)
        longitude: Longitude coordinate (optional if city provided)
        start_date: Start date in YYYY-MM-DD format (required)
        end_date: End date in YYYY-MM-DD format (optional, defaults to start_date)
        daily: Comma-separated daily variables (default: temperature_2m_max,precipitation_sum,weather_code)
        
    Returns:
        Dict with _ui_components and _data keys.
    """
    try:
        # Get coordinates
        lat, lon = _get_coordinates_from_args(
            city=city, state=state, country=country,
            latitude=latitude, longitude=longitude
        )
        
        # Validate dates
        start_obj, end_obj = _validate_date_range(start_date, end_date)
        
        # Build API request
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": start_date,
            "end_date": end_date or start_date,
            "daily": daily,
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "timezone": "auto"
        }
        
        logger.info(f"Fetching historical weather for coordinates: {lat}, {lon} from {start_date} to {end_date or start_date}")
        data = _make_api_request(f"{OPEN_METEO_ARCHIVE_URL}/archive", params)
        
        daily_data = data.get("daily", {})
        times = daily_data.get("time", [])
        temperatures = daily_data.get("temperature_2m_max", [])
        precipitation = daily_data.get("precipitation_sum", [])
        weather_codes = daily_data.get("weather_code", [])
        
        if not times:
            return create_ui_response([
                Alert(
                    message="No historical weather data available for the specified date range.",
                    variant="warning"
                )
            ])
        
        # Create historical data model
        historical = HistoricalWeatherData.from_api_response(daily_data, start_date, end_date or start_date)
        
        # Map weather codes to descriptions
        weather_descriptions = [WEATHER_CODES.get(code, "Unknown") for code in weather_codes]
        
        # Build UI components
        location_str = f"{city or 'Unknown'}, {state or country}" if city else f"{lat:.4f}°, {lon:.4f}°"
        
        # Line chart for temperature trend
        chart_data = [{
            "x": times,
            "y": temperatures,
            "type": "scatter",
            "mode": "lines+markers",
            "name": "Max Temperature (°F)",
            "line": {"color": "#FF6B6B", "width": 3}
        }]
        
        # Table data
        table_headers = ["Date", "Max Temp (°F)", "Precipitation (in)", "Conditions"]
        table_rows = []
        for i in range(min(10, len(times))):  # Show first 10 days
            table_rows.append([
                times[i],
                f"{temperatures[i]}",
                f"{precipitation[i]}",
                weather_descriptions[i]
            ])
        
        # Summary metrics
        avg_temp = sum(temperatures) / len(temperatures) if temperatures else 0
        total_precip = sum(precipitation) if precipitation else 0
        
        components = [
            Card(
                title=f"Historical Weather - {location_str}",
                id="historical-weather-card",
                content=[
                    PlotlyChart(
                        title="Temperature Trend",
                        data=chart_data,
                        layout={
                            "xaxis": {"title": "Date"},
                            "yaxis": {"title": "Temperature (°F)"},
                            "showlegend": True
                        },
                        id="historical-chart"
                    ),
                    Grid(
                        columns=2,
                        children=[
                            MetricCard(
                                title="Average Temperature",
                                value=f"{avg_temp:.1f}°F",
                                id="avg-temp-metric"
                            ),
                            MetricCard(
                                title="Total Precipitation",
                                value=f"{total_precip:.2f} in",
                                id="total-precip-metric"
                            )
                        ]
                    ),
                    Table(
                        headers=table_headers,
                        rows=table_rows,
                        id="historical-table"
                    ),
                    Alert(
                        message=f"Data from {start_date} to {end_date or start_date}. Provided by Open-Meteo Historical API.",
                        variant="info"
                    )
                ]
            )
        ]
        
        return {
            "_ui_components": [c.to_dict() for c in components],
            "_data": {
                "location": location_str,
                "coordinates": {"latitude": lat, "longitude": lon},
                "historical": historical,
                "daily_data": daily_data,
                "summary": {
                    "average_temperature": avg_temp,
                    "total_precipitation": total_precip,
                    "days": len(times)
                }
            }
        }
        
    except Exception as e:
        return create_ui_response([
            Alert(
                message=f"Failed to get historical weather: {str(e)}",
                variant="error"
            )
        ])


def get_weather_alerts(
    city: Optional[str] = None,
    state: Optional[str] = None,
    country: str = "US",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    """
    Get severe weather alerts for a location (US only).
    
    Args:
        city: City name (optional if latitude/longitude provided)
        state: State/province name (optional)
        country: Country code (default: "US")
        latitude: Latitude coordinate (optional if city provided)
        longitude: Longitude coordinate (optional if city provided)
        
    Returns:
        Dict with _ui_components and _data keys.
    """
    try:
        # Get coordinates and location details
        lat, lon = _get_coordinates_from_args(
            city=city, state=state, country=country,
            latitude=latitude, longitude=longitude
        )
        
        # Geocode to get state code (admin1)
        geocode_result = geocode_location(
            city=city, state=state, country=country,
            latitude=latitude, longitude=longitude
        )
        if "_data" not in geocode_result:
            return create_ui_response([
                Alert(
                    message="Could not geocode location to determine state.",
                    variant="error"
                )
            ])
        location_data = geocode_result["_data"]
        admin1 = location_data.get("admin1", "")
        country_code = location_data.get("country", "")
        
        # Only US locations supported for NWS alerts
        if country_code != "US":
            return create_ui_response([
                Alert(
                    message=f"Weather alerts are currently only available for US locations. Country: {country_code}",
                    variant="warning"
                )
            ])
        
        # Map state name to abbreviation (simple mapping for common states)
        state_abbr_map = {
            "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
            "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
            "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
            "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
            "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
            "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
            "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
            "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
            "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
            "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
            "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
            "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
            "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC"
        }
        state_abbr = state_abbr_map.get(admin1, admin1[:2].upper() if len(admin1) >= 2 else "")
        if not state_abbr:
            state_abbr = state[:2].upper() if state else ""
        
        if not state_abbr:
            return create_ui_response([
                Alert(
                    message="Could not determine state abbreviation for location.",
                    variant="error"
                )
            ])
        
        # Fetch alerts from NWS API
        nws_url = f"https://api.weather.gov/alerts/active?area={state_abbr}"
        headers = {"User-Agent": "AstralDeep/1.0 (Weather Agent)"}
        response = requests.get(nws_url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        features = data.get("features", [])
        if not features:
            return create_ui_response([
                Alert(
                    message=f"No active weather alerts for {admin1} ({state_abbr}).",
                    variant="info"
                )
            ])
        
        # Parse alerts
        alerts = []
        for feature in features[:5]:  # Limit to 5 alerts
            props = feature.get("properties", {})
            title = props.get("headline", "No title")
            severity = props.get("severity", "unknown").lower()
            description = props.get("description", "No description provided.")
            effective = props.get("effective")
            expires = props.get("expires")
            area = props.get("areaDesc", "Unknown area")
            
            # Convert ISO datetime strings to datetime objects
            # (datetime is imported at module level — no local import needed)
            try:
                effective_dt = datetime.fromisoformat(effective.replace('Z', '+00:00')) if effective else datetime.now()
                expires_dt = datetime.fromisoformat(expires.replace('Z', '+00:00')) if expires else datetime.now()
            except Exception:
                effective_dt = datetime.now()
                expires_dt = datetime.now()
            
            alerts.append(WeatherAlert(
                title=title,
                severity=severity,
                description=description,
                effective=effective_dt,
                expires=expires_dt,
                area=area
            ))
        
        # Build UI components
        location_str = f"{city or 'Unknown'}, {state or admin1 or country_code}" if city else f"{lat:.4f}°, {lon:.4f}°"
        
        alert_components = []
        for alert in alerts:
            # Map severity to variant
            variant_map = {
                "extreme": "error",
                "severe": "error",
                "moderate": "warning",
                "minor": "info",
                "unknown": "default"
            }
            variant = variant_map.get(alert.severity, "default")
            alert_components.append(
                Alert(
                    message=f"{alert.title} - {alert.area}",
                    variant=variant,
                    title=f"Severity: {alert.severity.capitalize()}",
                    id=f"alert-{alert.title[:20]}"
                )
            )
            alert_components.append(
                Text(
                    content=alert.description[:200] + ("..." if len(alert.description) > 200 else ""),
                    variant="caption"
                )
            )
        
        components = [
            Card(
                title=f"Weather Alerts - {location_str}",
                id="weather-alerts-card",
                content=alert_components
            )
        ]
        
        return {
            "_ui_components": [c.to_dict() for c in components],
            "_data": {
                "location": location_str,
                "coordinates": {"latitude": lat, "longitude": lon},
                "state": state_abbr,
                "alerts": [{"title": a.title, "severity": a.severity, "area": a.area} for a in alerts],
                "total_alerts": len(features)
            }
        }
        
    except Exception as e:
        return create_ui_response([
            Alert(
                message=f"Failed to get weather alerts: {str(e)}",
                variant="error"
            )
        ])


def compare_locations(
    city1: Optional[str] = None,
    state1: Optional[str] = None,
    country1: str = "US",
    latitude1: Optional[float] = None,
    longitude1: Optional[float] = None,
    city2: Optional[str] = None,
    state2: Optional[str] = None,
    country2: str = "US",
    latitude2: Optional[float] = None,
    longitude2: Optional[float] = None,
    city3: Optional[str] = None,
    state3: Optional[str] = None,
    country3: str = "US",
    latitude3: Optional[float] = None,
    longitude3: Optional[float] = None,
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    """
    Compare current weather across multiple locations (up to 3).
    
    Args:
        city1, state1, country1, latitude1, longitude1: First location
        city2, state2, country2, latitude2, longitude2: Second location (required)
        city3, state3, country3, latitude3, longitude3: Third location (optional)
        
    Returns:
        Dict with _ui_components and _data keys.
    """
    try:
        # Build list of location arguments
        locations = []
        if city1 or latitude1 is not None:
            locations.append({
                "city": city1,
                "state": state1,
                "country": country1,
                "latitude": latitude1,
                "longitude": longitude1
            })
        if city2 or latitude2 is not None:
            locations.append({
                "city": city2,
                "state": state2,
                "country": country2,
                "latitude": latitude2,
                "longitude": longitude2
            })
        if city3 or latitude3 is not None:
            locations.append({
                "city": city3,
                "state": state3,
                "country": country3,
                "latitude": latitude3,
                "longitude": longitude3
            })
        
        if len(locations) < 2:
            return create_ui_response([
                Alert(
                    message="At least two locations are required for comparison.",
                    variant="error"
                )
            ])
        
        # Fetch weather for each location in parallel
        def fetch_one(loc):
            try:
                result = get_current_weather(
                    city=loc["city"],
                    state=loc["state"],
                    country=loc["country"],
                    latitude=loc["latitude"],
                    longitude=loc["longitude"]
                )
                if "_data" in result:
                    return result["_data"]
                else:
                    # Error case
                    return {"error": result.get("_ui_components", [])}
            except Exception as e:
                return {"error": str(e)}
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            future_to_loc = {executor.submit(fetch_one, loc): i for i, loc in enumerate(locations)}
            results = []
            for future in concurrent.futures.as_completed(future_to_loc):
                results.append(future.result())
        
        # Check for errors
        errors = [r for r in results if "error" in r]
        if errors:
            error_messages = [e["error"] for e in errors]
            return create_ui_response([
                Alert(
                    message=f"Failed to fetch weather for some locations: {error_messages}",
                    variant="error"
                )
            ])
        
        # Build comparison UI
        location_names = []
        for i, loc in enumerate(locations):
            if loc["city"]:
                name = f"{loc['city']}, {loc['state'] or loc['country']}"
            else:
                name = f"{results[i].get('coordinates', {}).get('latitude', 'N/A')}°, {results[i].get('coordinates', {}).get('longitude', 'N/A')}°"
            location_names.append(name)
        
        # Metric cards for each location
        metric_grids = []
        for i, (name, data) in enumerate(zip(location_names, results)):
            current = data.get("current", {})
            metric_grids.append(
                Card(
                    title=f"{name}",
                    id=f"location-{i}-card",
                    content=[
                        Grid(
                            columns=2,
                            children=[
                                MetricCard(
                                    title="Temperature",
                                    value=f"{current.get('temperature_2m', 'N/A')}°F",
                                    subtitle=f"Feels like {current.get('apparent_temperature', 'N/A')}°F",
                                    id=f"temp-{i}"
                                ),
                                MetricCard(
                                    title="Humidity",
                                    value=f"{current.get('relative_humidity_2m', 'N/A')}%",
                                    id=f"humidity-{i}"
                                ),
                                MetricCard(
                                    title="Wind",
                                    value=f"{current.get('wind_speed_10m', 'N/A')} mph",
                                    subtitle=f"Direction {current.get('wind_direction_10m', 'N/A')}°",
                                    id=f"wind-{i}"
                                ),
                                MetricCard(
                                    title="Pressure",
                                    value=f"{current.get('pressure_msl', 'N/A')} hPa",
                                    id=f"pressure-{i}"
                                ),
                            ]
                        ),
                        Alert(
                            message=data.get("weather_description", "No description"),
                            variant="default"
                        )
                    ]
                )
            )
        
        # Bar chart comparing temperatures
        chart_data = [{
            "x": location_names,
            "y": [r.get("current", {}).get("temperature_2m", 0) for r in results],
            "type": "bar",
            "name": "Temperature (°F)",
            "marker": {"color": "#FF6B6B"}
        }]
        
        # Table summary
        table_headers = ["Location", "Temp (°F)", "Humidity (%)", "Wind (mph)", "Pressure (hPa)", "Conditions"]
        table_rows = []
        for name, data in zip(location_names, results):
            current = data.get("current", {})
            table_rows.append([
                name,
                f"{current.get('temperature_2m', 'N/A')}",
                f"{current.get('relative_humidity_2m', 'N/A')}",
                f"{current.get('wind_speed_10m', 'N/A')}",
                f"{current.get('pressure_msl', 'N/A')}",
                data.get("weather_description", "N/A")
            ])
        
        components = [
            Card(
                title="Weather Comparison",
                id="comparison-card",
                content=[
                    PlotlyChart(
                        title="Temperature Comparison",
                        data=chart_data,
                        layout={
                            "xaxis": {"title": "Location"},
                            "yaxis": {"title": "Temperature (°F)"},
                            "showlegend": False
                        },
                        id="comparison-chart"
                    ),
                    Grid(
                        columns=len(location_names),
                        children=metric_grids
                    ),
                    Table(
                        headers=table_headers,
                        rows=table_rows,
                        id="comparison-table"
                    ),
                    Alert(
                        message=f"Comparing {len(location_names)} locations. Data fetched at {datetime.now().strftime('%H:%M:%S')}.",
                        variant="info"
                    )
                ]
            )
        ]
        
        return {
            "_ui_components": [c.to_dict() for c in components],
            "_data": {
                "locations": location_names,
                "results": results,
                "comparison": {
                    "temperatures": [r.get("current", {}).get("temperature_2m") for r in results],
                    "humidities": [r.get("current", {}).get("relative_humidity_2m") for r in results],
                    "wind_speeds": [r.get("current", {}).get("wind_speed_10m") for r in results]
                }
            }
        }
        
    except Exception as e:
        return create_ui_response([
            Alert(
                message=f"Failed to compare locations: {str(e)}",
                variant="error"
            )
        ])


def get_hourly_forecast(
    city: Optional[str] = None,
    state: Optional[str] = None,
    country: str = "US",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    hours: int = 24,
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    """
    Get hourly forecast for a location.
    
    Args:
        city: City name (optional if latitude/longitude provided)
        state: State/province name (optional)
        country: Country code (default: "US")
        latitude: Latitude coordinate (optional if city provided)
        longitude: Longitude coordinate (optional if city provided)
        hours: Number of hours to forecast (default: 24, max: 168)
        
    Returns:
        Dict with _ui_components and _data keys.
    """
    try:
        # Get coordinates
        lat, lon = _get_coordinates_from_args(
            city=city, state=state, country=country,
            latitude=latitude, longitude=longitude
        )
        
        # Limit hours to API maximum
        hours = min(max(hours, 1), 168)
        
        # Build API request
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "forecast_hours": hours,
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "timezone": "auto"
        }
        
        logger.info(f"Fetching {hours}-hour forecast for coordinates: {lat}, {lon}")
        data = _make_api_request(f"{OPEN_METEO_BASE_URL}/forecast", params)
        
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])[:hours]
        temperatures = hourly.get("temperature_2m", [])[:hours]
        precip_probs = hourly.get("precipitation_probability", [])[:hours]
        
        if not times:
            return create_ui_response([
                Alert(
                    message="No hourly forecast data available",
                    variant="warning"
                )
            ])
        
        # Create line chart data
        chart_data = [{
            "x": times,
            "y": temperatures,
            "type": "scatter",
            "mode": "lines+markers",
            "name": "Temperature (°F)",
            "line": {"color": "#FF6B6B", "width": 3}
        }]
        
        # Create table data
        table_headers = ["Time", "Temperature", "Precipitation %"]
        table_rows = []
        for i in range(min(12, len(times))):  # Show first 12 hours in table
            time_str = times[i].replace("T", " ")
            table_rows.append([
                time_str,
                f"{temperatures[i]}°F",
                f"{precip_probs[i]}%"
            ])
        
        location_str = f"{city or 'Unknown'}, {state or country}" if city else f"{lat:.4f}°, {lon:.4f}°"
        
        components = [
            Card(
                title=f"{hours}-Hour Forecast - {location_str}",
                id="hourly-forecast-card",
                content=[
                    PlotlyChart(
                        title="Temperature Trend",
                        data=chart_data,
                        layout={
                            "xaxis": {"title": "Time"},
                            "yaxis": {"title": "Temperature (°F)"},
                            "showlegend": True
                        },
                        id="hourly-chart"
                    ),
                    Table(
                        headers=table_headers,
                        rows=table_rows,
                        id="hourly-table"
                    )
                ]
            )
        ]
        
        return {
            "_ui_components": [c.to_dict() for c in components],
            "_data": {
                "location": location_str,
                "coordinates": {"latitude": lat, "longitude": lon},
                "hours": hours,
                "hourly_data": {
                    "times": times,
                    "temperatures": temperatures,
                    "precipitation_probabilities": precip_probs
                }
            }
        }
        
    except Exception as e:
        return create_ui_response([
            Alert(
                message=f"Failed to get hourly forecast: {str(e)}",
                variant="error"
            )
        ])


def get_daily_forecast(
    city: Optional[str] = None,
    state: Optional[str] = None,
    country: str = "US",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    days: int = 7,
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    """
    Get daily forecast for a location.
    
    Args:
        city: City name (optional if latitude/longitude provided)
        state: State/province name (optional)
        country: Country code (default: "US")
        latitude: Latitude coordinate (optional if city provided)
        longitude: Longitude coordinate (optional if city provided)
        days: Number of days to forecast (default: 7, max: 16)
        
    Returns:
        Dict with _ui_components and _data keys.
    """
    try:
        # Get coordinates
        lat, lon = _get_coordinates_from_args(
            city=city, state=state, country=country,
            latitude=latitude, longitude=longitude
        )
        
        # Limit days to API maximum
        days = min(max(days, 1), 16)
        
        # Build API request
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code",
            "forecast_days": days,
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "timezone": "auto"
        }
        
        logger.info(f"Fetching {days}-day forecast for coordinates: {lat}, {lon}")
        data = _make_api_request(f"{OPEN_METEO_BASE_URL}/forecast", params)
        
        daily = data.get("daily", {})
        dates = daily.get("time", [])[:days]
        max_temps = daily.get("temperature_2m_max", [])[:days]
        min_temps = daily.get("temperature_2m_min", [])[:days]
        precip_sums = daily.get("precipitation_sum", [])[:days]
        
        if not dates:
            return create_ui_response([
                Alert(
                    message="No daily forecast data available",
                    variant="warning"
                )
            ])
        
        # Create bar chart data for temperature range
        chart_data = [
            {
                "x": dates,
                "y": max_temps,
                "type": "bar",
                "name": "High",
                "marker": {"color": "#FF6B6B"}
            },
            {
                "x": dates,
                "y": min_temps,
                "type": "bar",
                "name": "Low",
                "marker": {"color": "#4ECDC4"}
            }
        ]
        
        # Create table data
        table_headers = ["Date", "High", "Low", "Precipitation"]
        table_rows = []
        for i in range(len(dates)):
            table_rows.append([
                dates[i],
                f"{max_temps[i]}°F",
                f"{min_temps[i]}°F",
                f"{precip_sums[i]} in"
            ])
        
        location_str = f"{city or 'Unknown'}, {state or country}" if city else f"{lat:.4f}°, {lon:.4f}°"
        
        components = [
            Card(
                title=f"{days}-Day Forecast - {location_str}",
                id="daily-forecast-card",
                content=[
                    PlotlyChart(
                        title="Daily Temperature Range",
                        data=chart_data,
                        layout={
                            "barmode": "group",
                            "xaxis": {"title": "Date"},
                            "yaxis": {"title": "Temperature (°F)"},
                            "showlegend": True
                        },
                        id="daily-chart"
                    ),
                    Table(
                        headers=table_headers,
                        rows=table_rows,
                        id="daily-table"
                    )
                ]
            )
        ]
        
        return {
            "_ui_components": [c.to_dict() for c in components],
            "_data": {
                "location": location_str,
                "coordinates": {"latitude": lat, "longitude": lon},
                "days": days,
                "daily_data": {
                    "dates": dates,
                    "max_temperatures": max_temps,
                    "min_temperatures": min_temps,
                    "precipitation_sums": precip_sums
                }
            }
        }
        
    except Exception as e:
        return create_ui_response([
            Alert(
                message=f"Failed to get daily forecast: {str(e)}",
                variant="error"
            )
        ])


def get_weekly_forecast(
    city: Optional[str] = None,
    state: Optional[str] = None,
    country: str = "US",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    """
    Get weekly forecast summary for a location.
    
    Args:
        city: City name (optional if latitude/longitude provided)
        state: State/province name (optional)
        country: Country code (default: "US")
        latitude: Latitude coordinate (optional if city provided)
        longitude: Longitude coordinate (optional if city provided)
        
    Returns:
        Dict with _ui_components and _data keys.
    """
    try:
        # Get 7-day forecast
        forecast_result = get_daily_forecast(
            city=city, state=state, country=country,
            latitude=latitude, longitude=longitude,
            days=7
        )
        
        if "_data" not in forecast_result:
            return forecast_result
        
        data = forecast_result["_data"]
        daily_data = data.get("daily_data", {})
        
        # Calculate weekly statistics
        max_temps = daily_data.get("max_temperatures", [])
        min_temps = daily_data.get("min_temperatures", [])
        precip_sums = daily_data.get("precipitation_sums", [])
        
        if not max_temps:
            return create_ui_response([
                Alert(
                    message="No weekly forecast data available",
                    variant="warning"
                )
            ])
        
        avg_high = sum(max_temps) / len(max_temps) if max_temps else 0
        avg_low = sum(min_temps) / len(min_temps) if min_temps else 0
        total_precip = sum(precip_sums) if precip_sums else 0
        max_high = max(max_temps) if max_temps else 0
        min_low = min(min_temps) if min_temps else 0
        
        # Determine overall weather trend
        trend = "stable"
        if len(max_temps) >= 3:
            if max_temps[-1] > max_temps[0] + 5:
                trend = "warming"
            elif max_temps[-1] < max_temps[0] - 5:
                trend = "cooling"
        
        location_str = data.get("location", "Unknown location")
        
        components = [
            Card(
                title=f"Weekly Forecast Summary - {location_str}",
                id="weekly-forecast-card",
                content=[
                    Grid(
                        columns=3,
                        children=[
                            MetricCard(
                                title="Average High",
                                value=f"{avg_high:.1f}°F",
                                subtitle=f"Max: {max_high}°F",
                                id="avg-high-metric"
                            ),
                            MetricCard(
                                title="Average Low",
                                value=f"{avg_low:.1f}°F",
                                subtitle=f"Min: {min_low}°F",
                                id="avg-low-metric"
                            ),
                            MetricCard(
                                title="Total Precipitation",
                                value=f"{total_precip:.2f} in",
                                id="precip-metric"
                            ),
                        ]
                    ),
                    Alert(
                        message=f"Overall trend: {trend.capitalize()}",
                        variant="info"
                    ),
                    Text(
                        content="This week's forecast shows daily temperature ranges and precipitation totals.",
                        variant="body"
                    )
                ]
            )
        ]
        
        return {
            "_ui_components": [c.to_dict() for c in components],
            "_data": {
                "location": location_str,
                "weekly_stats": {
                    "average_high": avg_high,
                    "average_low": avg_low,
                    "total_precipitation": total_precip,
                    "maximum_high": max_high,
                    "minimum_low": min_low,
                    "trend": trend
                },
                "daily_data": daily_data
            }
        }
        
    except Exception as e:
        return create_ui_response([
            Alert(
                message=f"Failed to get weekly forecast: {str(e)}",
                variant="error"
            )
        ])


# =============================================================================
# STREAMING TOOLS (001-tool-stream-ui reference implementation)
# =============================================================================

@streaming_tool(
    name="live_temperature",
    description=(
        "Stream live temperature for a location, updating every interval_s "
        "seconds. Returns a Metric component with the latest reading."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "latitude": {
                "type": "number",
                "description": "Latitude coordinate",
            },
            "longitude": {
                "type": "number",
                "description": "Longitude coordinate",
            },
            "interval_s": {
                "type": "integer",
                "minimum": 1,
                "maximum": 60,
                "default": 5,
                "description": "Polling interval in seconds (clamped 1..60)",
            },
        },
        "required": ["latitude", "longitude"],
    },
    max_fps=10,
    min_fps=5,
)
async def live_temperature(args: Dict[str, Any], credentials: Dict[str, Any]) -> AsyncIterator[StreamComponents]:
    """Yield a Metric component every ``interval_s`` seconds with the latest
    temperature reading from Open-Meteo.

    Reference implementation for the 001-tool-stream-ui feature. The
    cleanup pattern (try/finally) is REQUIRED of every streaming tool —
    when the orchestrator sends ToolStreamCancel, the SDK propagates
    GeneratorExit through this generator and the finally block runs.
    """
    interval = max(1, min(60, int(args.get("interval_s", 5))))
    lat = float(args["latitude"])
    lon = float(args["longitude"])
    last_temp: Optional[float] = None

    try:
        while True:
            try:
                # Run the blocking HTTP call in a thread so we don't stall
                # the event loop. Open-Meteo's free tier supports the
                # current_weather=true short form.
                data = await asyncio.to_thread(
                    _make_api_request,
                    f"{OPEN_METEO_BASE_URL}/forecast",
                    {"latitude": lat, "longitude": lon, "current_weather": True},
                    10,
                )
                current = data.get("current_weather") or {}
                temp = current.get("temperature")
                if temp is None:
                    raise Exception("Open-Meteo response missing current_weather.temperature")

                delta_str = ""
                if last_temp is not None:
                    delta = temp - last_temp
                    delta_str = f"{delta:+.1f}°C"
                last_temp = float(temp)

                yield StreamComponents(
                    components=[{
                        "type": "metric",
                        "label": "Live Temperature",
                        "value": f"{temp:.1f}°C",
                        "subtitle": f"{lat:.2f}, {lon:.2f}",
                        "delta": delta_str,
                    }],
                    raw={
                        "temperature_c": temp,
                        "wind_kph": current.get("windspeed"),
                        "wind_dir": current.get("winddirection"),
                        "weather_code": current.get("weathercode"),
                        "ts": current.get("time"),
                    },
                )

            except asyncio.CancelledError:
                # Reraise so the outer finally runs
                raise
            except Exception as e:
                # Surface as a transient error chunk; the orchestrator's
                # _classify_error will route this to RECONNECTING (when US5
                # lands) and the auto-retry kicks in.
                logger.warning(f"live_temperature poll failed: {e}")
                yield StreamComponents(
                    components=[],
                    error={
                        "code": "upstream_unavailable",
                        "message": f"Open-Meteo error: {e}",
                        "phase": "reconnecting",
                        "retryable": False,
                    },
                )

            await asyncio.sleep(interval)
    finally:
        logger.info(f"live_temperature stream stopping (lat={lat}, lon={lon})")


# =============================================================================
# TOOL REGISTRY
# =============================================================================

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "live_temperature": {
        "function": live_temperature,
        "scope": "tools:read",
        "description": (
            "Stream live temperature updates for a location. Pushes a new "
            "Metric component every interval_s seconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "latitude": {"type": "number"},
                "longitude": {"type": "number"},
                "interval_s": {"type": "integer", "minimum": 1, "maximum": 60, "default": 5},
            },
            "required": ["latitude", "longitude"],
        },
        # 001-tool-stream-ui: marks this tool as push-streamable so the
        # orchestrator routes stream_subscribe to StreamManager rather than
        # the legacy poll path. validate_streaming_metadata enforces the
        # shape at register_agent time.
        "metadata": {
            "streamable": True,
            "streaming_kind": "push",
            "max_fps": 10,
            "min_fps": 5,
            "max_chunk_bytes": 65536,
        },
    },
    "geocode_location": {
        "function": geocode_location,
        "scope": "tools:search",
        "description": "Convert city and state names to latitude/longitude coordinates using Open-Meteo's geocoding API.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (required)"
                },
                "state": {
                    "type": "string",
                    "description": "State/province name (optional)"
                },
                "country": {
                    "type": "string",
                    "description": "Country code (default: 'US')",
                    "default": "US"
                }
            },
            "required": ["city"]
        }
    },
    "get_current_weather": {
        "function": get_current_weather,
        "scope": "tools:search",
        "description": "Get current weather conditions for a location, including temperature, humidity, wind, and conditions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (optional if latitude/longitude provided)"
                },
                "state": {
                    "type": "string",
                    "description": "State/province name (optional)"
                },
                "country": {
                    "type": "string",
                    "description": "Country code (default: 'US')",
                    "default": "US"
                },
                "latitude": {
                    "type": "number",
                    "description": "Latitude coordinate (optional if city provided)"
                },
                "longitude": {
                    "type": "number",
                    "description": "Longitude coordinate (optional if city provided)"
                }
            }
        }
    },
    "get_hourly_forecast": {
        "function": get_hourly_forecast,
        "scope": "tools:search",
        "description": "Get hourly forecast for a location, including temperature trends and precipitation probability.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (optional if latitude/longitude provided)"
                },
                "state": {
                    "type": "string",
                    "description": "State/province name (optional)"
                },
                "country": {
                    "type": "string",
                    "description": "Country code (default: 'US')",
                    "default": "US"
                },
                "latitude": {
                    "type": "number",
                    "description": "Latitude coordinate (optional if city provided)"
                },
                "longitude": {
                    "type": "number",
                    "description": "Longitude coordinate (optional if city provided)"
                },
                "hours": {
                    "type": "integer",
                    "description": "Number of hours to forecast (default: 24, max: 168)",
                    "default": 24
                }
            }
        }
    },
    "get_daily_forecast": {
        "function": get_daily_forecast,
        "scope": "tools:search",
        "description": "Get daily forecast for a location, including high/low temperatures and precipitation totals.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (optional if latitude/longitude provided)"
                },
                "state": {
                    "type": "string",
                    "description": "State/province name (optional)"
                },
                "country": {
                    "type": "string",
                    "description": "Country code (default: 'US')",
                    "default": "US"
                },
                "latitude": {
                    "type": "number",
                    "description": "Latitude coordinate (optional if city provided)"
                },
                "longitude": {
                    "type": "number",
                    "description": "Longitude coordinate (optional if city provided)"
                },
                "days": {
                    "type": "integer",
                    "description": "Number of days to forecast (default: 7, max: 16)",
                    "default": 7
                }
            }
        }
    },
    "get_weekly_forecast": {
        "function": get_weekly_forecast,
        "scope": "tools:search",
        "description": "Get weekly forecast summary for a location, including averages, totals, and trends.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (optional if latitude/longitude provided)"
                },
                "state": {
                    "type": "string",
                    "description": "State/province name (optional)"
                },
                "country": {
                    "type": "string",
                    "description": "Country code (default: 'US')",
                    "default": "US"
                },
                "latitude": {
                    "type": "number",
                    "description": "Latitude coordinate (optional if city provided)"
                },
                "longitude": {
                    "type": "number",
                    "description": "Longitude coordinate (optional if city provided)"
                }
            }
        }
    },
    "get_extended_weather": {
        "function": get_extended_weather,
        "scope": "tools:search",
        "description": "Get extended weather data including UV index, air quality, and sunrise/sunset times.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (optional if latitude/longitude provided)"
                },
                "state": {
                    "type": "string",
                    "description": "State/province name (optional)"
                },
                "country": {
                    "type": "string",
                    "description": "Country code (default: 'US')",
                    "default": "US"
                },
                "latitude": {
                    "type": "number",
                    "description": "Latitude coordinate (optional if city provided)"
                },
                "longitude": {
                    "type": "number",
                    "description": "Longitude coordinate (optional if city provided)"
                }
            }
        }
    },
    "get_historical_weather": {
        "function": get_historical_weather,
        "scope": "tools:search",
        "description": "Get historical weather data for a location and date range, including temperature, precipitation, and weather conditions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (optional if latitude/longitude provided)"
                },
                "state": {
                    "type": "string",
                    "description": "State/province name (optional)"
                },
                "country": {
                    "type": "string",
                    "description": "Country code (default: 'US')",
                    "default": "US"
                },
                "latitude": {
                    "type": "number",
                    "description": "Latitude coordinate (optional if city provided)"
                },
                "longitude": {
                    "type": "number",
                    "description": "Longitude coordinate (optional if city provided)"
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format (required)"
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format (optional, defaults to start_date)"
                },
                "daily": {
                    "type": "string",
                    "description": "Comma-separated daily variables (default: temperature_2m_max,precipitation_sum,weather_code)",
                    "default": "temperature_2m_max,precipitation_sum,weather_code"
                }
            },
            "required": ["start_date"]
        }
    },
    "get_weather_alerts": {
        "function": get_weather_alerts,
        "scope": "tools:search",
        "description": "Get severe weather alerts for a location (US only) using National Weather Service API.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (optional if latitude/longitude provided)"
                },
                "state": {
                    "type": "string",
                    "description": "State/province name (optional)"
                },
                "country": {
                    "type": "string",
                    "description": "Country code (default: 'US')",
                    "default": "US"
                },
                "latitude": {
                    "type": "number",
                    "description": "Latitude coordinate (optional if city provided)"
                },
                "longitude": {
                    "type": "number",
                    "description": "Longitude coordinate (optional if city provided)"
                }
            }
        }
    },
    "compare_locations": {
        "function": compare_locations,
        "scope": "tools:search",
        "description": "Compare current weather across multiple locations (up to 3) with parallel API calls and visual comparison.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city1": {
                    "type": "string",
                    "description": "City name for first location (optional if latitude1/longitude1 provided)"
                },
                "state1": {
                    "type": "string",
                    "description": "State/province name for first location (optional)"
                },
                "country1": {
                    "type": "string",
                    "description": "Country code for first location (default: 'US')",
                    "default": "US"
                },
                "latitude1": {
                    "type": "number",
                    "description": "Latitude coordinate for first location (optional if city1 provided)"
                },
                "longitude1": {
                    "type": "number",
                    "description": "Longitude coordinate for first location (optional if city1 provided)"
                },
                "city2": {
                    "type": "string",
                    "description": "City name for second location (required unless latitude2/longitude2 provided)"
                },
                "state2": {
                    "type": "string",
                    "description": "State/province name for second location (optional)"
                },
                "country2": {
                    "type": "string",
                    "description": "Country code for second location (default: 'US')",
                    "default": "US"
                },
                "latitude2": {
                    "type": "number",
                    "description": "Latitude coordinate for second location (optional if city2 provided)"
                },
                "longitude2": {
                    "type": "number",
                    "description": "Longitude coordinate for second location (optional if city2 provided)"
                },
                "city3": {
                    "type": "string",
                    "description": "City name for third location (optional)"
                },
                "state3": {
                    "type": "string",
                    "description": "State/province name for third location (optional)"
                },
                "country3": {
                    "type": "string",
                    "description": "Country code for third location (default: 'US')",
                    "default": "US"
                },
                "latitude3": {
                    "type": "number",
                    "description": "Latitude coordinate for third location (optional if city3 provided)"
                },
                "longitude3": {
                    "type": "number",
                    "description": "Longitude coordinate for third location (optional if city3 provided)"
                }
            }
        }
    }
}
