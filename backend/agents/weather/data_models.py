#!/usr/bin/env python3
"""Typed weather data models (extended conditions, historical ranges, alerts,
multi-location comparisons) that agents/weather/mcp_tools.py builds from Open-Meteo
API responses into UI components.
"""
from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ExtendedWeatherData:
    uv_index: Optional[float] = None
    pm2_5: Optional[float] = None
    pm10: Optional[float] = None
    carbon_monoxide: Optional[float] = None
    nitrogen_dioxide: Optional[float] = None
    sulphur_dioxide: Optional[float] = None
    ozone: Optional[float] = None
    sunrise: Optional[str] = None
    sunset: Optional[str] = None
    
    @classmethod
    def from_api_response(cls, current: Dict[str, Any], daily: Dict[str, Any]) -> 'ExtendedWeatherData':
        return cls(
            uv_index=current.get('uv_index'),
            pm2_5=current.get('pm2_5'),
            pm10=current.get('pm10'),
            carbon_monoxide=current.get('carbon_monoxide'),
            nitrogen_dioxide=current.get('nitrogen_dioxide'),
            sulphur_dioxide=current.get('sulphur_dioxide'),
            ozone=current.get('ozone'),
            sunrise=daily.get('sunrise', [None])[0] if daily.get('sunrise') else None,
            sunset=daily.get('sunset', [None])[0] if daily.get('sunset') else None,
        )


@dataclass
class HistoricalWeatherData:
    start_date: str
    end_date: str
    temperatures: list[float]
    precipitation: list[float]
    weather_codes: list[int]

    @classmethod
    def from_api_response(cls, daily: Dict[str, Any], start_date: str, end_date: str) -> 'HistoricalWeatherData':
        daily.get('time', [])
        temps = daily.get('temperature_2m_max', [])
        precip = daily.get('precipitation_sum', [])
        codes = daily.get('weather_code', [])
        return cls(
            start_date=start_date,
            end_date=end_date,
            temperatures=temps,
            precipitation=precip,
            weather_codes=codes
        )


@dataclass
class WeatherAlert:
    title: str
    severity: str
    description: str
    effective: datetime
    expires: datetime
    area: str


@dataclass
class LocationComparison:
    locations: list[str]
    temperatures: list[float]
    conditions: list[str]
    aqi: list[Optional[float]]
    uv_index: list[Optional[float]]
