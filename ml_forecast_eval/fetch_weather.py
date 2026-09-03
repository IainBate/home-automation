"""Fetch a year of historical hourly weather for the eval dataset (Open-Meteo archive API)."""
from __future__ import annotations

import json
from pathlib import Path

import requests

LATITUDE = 53.8804244
LONGITUDE = -1.0435382
TIMEZONE = "Europe/London"
START_DATE = "2025-09-01"
END_DATE = "2026-08-31"

# Baseline (current production) fields + candidates to test whether they help.
HOURLY_FIELDS = ",".join([
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "cloud_cover",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
])

OUT_PATH = Path(__file__).parent / "data" / "weather_hourly.json"

def main() -> None:
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "hourly": HOURLY_FIELDS,
        "timezone": TIMEZONE,
    }
    resp = requests.get("https://archive-api.open-meteo.com/v1/archive", params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()

    hourly = payload.get("hourly", {})
    n = len(hourly.get("time", []))
    print(f"Fetched {n} hourly records from {START_DATE} to {END_DATE}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload))
    print(f"Saved to {OUT_PATH}")


if __name__ == "__main__":
    main()
