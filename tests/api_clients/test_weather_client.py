"""Tests for weather_client.py's Open-Meteo HTTP handling."""

from __future__ import annotations

from unittest import mock

import requests

from src.api_clients import weather_client


def _fake_response(payload):
    response = mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_fetch_historical_weather_hourly_parses_response():
    payload = {
        "hourly": {
            "time": ["2026-01-01T00:00", "2026-01-01T01:00"],
            "shortwave_radiation": [0.0, 10.0],
            "cloud_cover": [80.0, 75.0],
            "temperature_2m": [5.0, 5.2],
        }
    }
    with mock.patch.object(weather_client.requests, "get", return_value=_fake_response(payload)) as get:
        records = weather_client.fetch_historical_weather_hourly(
            51.5, -0.1, "2026-01-01", "2026-01-01", "Europe/London"
        )

    assert records == [
        {
            "timestamp": "2026-01-01T00:00",
            "shortwave_radiation": 0.0,
            "cloud_cover": 80.0,
            "temperature_2m": 5.0,
        },
        {
            "timestamp": "2026-01-01T01:00",
            "shortwave_radiation": 10.0,
            "cloud_cover": 75.0,
            "temperature_2m": 5.2,
        },
    ]
    assert get.call_args.kwargs["params"]["latitude"] == 51.5


def test_fetch_forecast_weather_hourly_returns_none_on_request_error():
    with mock.patch.object(weather_client.requests, "get", side_effect=requests.ConnectionError("boom")):
        result = weather_client.fetch_forecast_weather_hourly(51.5, -0.1, "Europe/London")

    assert result is None


def test_fetch_returns_none_on_unexpected_response_shape():
    with mock.patch.object(weather_client.requests, "get", return_value=_fake_response({"not_hourly": {}})):
        result = weather_client.fetch_historical_weather_hourly(
            51.5, -0.1, "2026-01-01", "2026-01-02", "Europe/London"
        )

    assert result is None
