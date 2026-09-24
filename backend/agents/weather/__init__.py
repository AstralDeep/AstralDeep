"""Weather agent package: exposes WeatherAgent (weather_agent.py) and its MCP dispatch
server/tools (mcp_server.py, mcp_tools.py) for Open-Meteo current/historical/forecast
data.
"""

from agents.weather.weather_agent import WeatherAgent
from agents.weather.mcp_server import MCPServer
from agents.weather.mcp_tools import (
    geocode_location,
    get_current_weather,
    get_hourly_forecast,
    get_daily_forecast,
    get_weekly_forecast,
    TOOL_REGISTRY
)

__all__ = [
    'WeatherAgent',
    'MCPServer',
    'geocode_location',
    'get_current_weather',
    'get_hourly_forecast',
    'get_daily_forecast',
    'get_weekly_forecast',
    'TOOL_REGISTRY'
]

__version__ = '1.0.0'
