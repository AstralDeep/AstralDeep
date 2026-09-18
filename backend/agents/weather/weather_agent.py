#!/usr/bin/env python3
"""
Weather Agent — A2A-compliant specialist agent for weather data and forecasts.

Provides tools for:
- Geocoding (city/state to coordinates)
- Current weather conditions
- Hourly, daily, and weekly forecasts
- Weather data visualization
"""
import asyncio
import os
import sys
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.weather.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class WeatherAgent(BaseA2AAgent):
    """Specialist agent for weather data and forecasts."""

    agent_id = "weather-1"
    service_name = "Weather Agent"
    description = (
        "Current conditions, hourly and daily forecasts, historical readings "
        "and severe-weather alerts for any place, charted rather than "
        "described. Open-Meteo, no key required."
    )
    examples = [
        {"title": "A week ahead",
         "prompt": "What's the weather forecast for Lexington, KY this week? "
                   "Show it with charts"},
        {"title": "Two cities",
         "prompt": "Compare this week's forecast for Denver and Miami side by side"},
        {"title": "Today, hour by hour",
         "prompt": "Chart today's hourly temperature and precipitation for Chicago"},
    ]
    skill_tags = ["weather", "forecast", "geocoding", "visualization"]

    def __init__(self, port: int = None):
        super().__init__(MCPServer(), port=port, port_env_var="WEATHER_AGENT_PORT")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Weather Agent')
    parser.add_argument('--port', type=int, default=None, help='Port to run the agent on (overrides dynamic discovery)')
    args = parser.parse_args()

    agent = WeatherAgent(port=args.port)
    asyncio.run(agent.run())
