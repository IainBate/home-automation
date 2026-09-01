"""Open-Meteo weather client - historical and forecast data for solar forecasting.

Open-Meteo's archive/forecast APIs are free and require no API key or
account, unlike Solcast (whose generic forecast doesn't account for this
roof's specific shading/orientation - see config.yaml's solar_forecast
section). Read-only, synchronous (matches this project's other simple HTTP
clients, e.g. solax_cloud_client.py) - no hardware/automation impact either
way, so this fails the same way the rest of the codebase does: log and
return None, never raise.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_HOURLY_FIELDS = "shortwave_radiation,cloud_cover,temperature_2m"


def fetch_historical_weather_hourly(
    latitude: float,
    longitude: float,
    start_date: str,
    end_date: str,
    timezone: str,
    timeout_seconds: float = 30.0,
) -> list[dict[str, Any]] | None:
    """Fetch hourly historical weather for training the solar forecast model.

    Args:
        latitude: Site latitude (decimal degrees).
        longitude: Site longitude (decimal degrees).
        start_date: "YYYY-MM-DD", inclusive.
        end_date: "YYYY-MM-DD", inclusive.
        timezone: IANA timezone name (e.g. "Europe/London") - returned
            timestamps are in this local time, matching solax_historical_data.json.
        timeout_seconds: HTTP request timeout.

    Returns:
        List of {"timestamp": "YYYY-MM-DD HH:MM", "shortwave_radiation": float,
        "cloud_cover": float, "temperature_2m": float}, or None on error.

    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": _HOURLY_FIELDS,
        "timezone": timezone,
    }
    return _fetch_hourly(_ARCHIVE_URL, params, timeout_seconds)


def fetch_forecast_weather_hourly(
    latitude: float,
    longitude: float,
    timezone: str,
    forecast_days: int = 2,
    timeout_seconds: float = 30.0,
) -> list[dict[str, Any]] | None:
    """Fetch hourly weather forecast (default: today + tomorrow) for solar prediction.

    Args:
        latitude: Site latitude (decimal degrees).
        longitude: Site longitude (decimal degrees).
        timezone: IANA timezone name (e.g. "Europe/London").
        forecast_days: Number of days ahead to fetch (Open-Meteo starts from today).
        timeout_seconds: HTTP request timeout.

    Returns:
        Same shape as fetch_historical_weather_hourly, or None on error.

    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": _HOURLY_FIELDS,
        "forecast_days": forecast_days,
        "timezone": timezone,
    }
    return _fetch_hourly(_FORECAST_URL, params, timeout_seconds)


def _fetch_hourly(
    url: str, params: dict[str, Any], timeout_seconds: float
) -> list[dict[str, Any]] | None:
    try:
        response = requests.get(url, params=params, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Failed to fetch weather from %s: %s", url, e)
        return None

    hourly = payload.get("hourly")
    if not hourly or "time" not in hourly:
        logger.warning("Unexpected weather API response shape from %s: missing hourly.time", url)
        return None

    times = hourly["time"]
    radiation = hourly.get("shortwave_radiation", [])
    cloud_cover = hourly.get("cloud_cover", [])
    temperature = hourly.get("temperature_2m", [])

    records = []
    for i, timestamp in enumerate(times):
        records.append(
            {
                "timestamp": timestamp,
                "shortwave_radiation": radiation[i] if i < len(radiation) else None,
                "cloud_cover": cloud_cover[i] if i < len(cloud_cover) else None,
                "temperature_2m": temperature[i] if i < len(temperature) else None,
            }
        )
    return records
