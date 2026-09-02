#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Solar Forecast Predictor (one-shot CLI).

Loads the model trained by scripts/solar_forecast_trainer.py, fetches
today/tomorrow's weather forecast, and writes a cached prediction for the
dashboard (src/dashboard/status_collector.py) to display. Display-only -
nothing reads this to make automation decisions.

Meant to run periodically via cron (e.g. hourly - weather forecasts don't
change fast enough to justify more often than that):

    0 * * * * cd /path/to/repo && python3 scripts/solar_forecast_predictor.py --quiet

Usage:
    python3 scripts/solar_forecast_predictor.py [--config config.yaml]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import joblib
import pytz
from hotwater_automation_core import get_config_path

from src.api_clients.weather_client import fetch_forecast_weather_hourly
from src.config_manager.config_manager import load_static_config
from src.core_logic.solar_forecast_logic import (
    build_forecast_rows,
    compute_actual_daily_kwh,
    predict_hourly_kw,
)
from src.utils.logging_setup import configure_cron_safe_logging
from src.utils.paths import (
    get_solar_forecast_model_path,
    get_solar_forecast_path,
    get_solax_historical_data_path,
)
from src.utils.state_store import read_json_state, write_json_atomic

logger = logging.getLogger(__name__)

_CLOUD_COVER_CLEAR_MAX = 20
_CLOUD_COVER_PARTLY_CLOUDY_MAX = 60


def _create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser


def _weather_description(cloud_cover_percent: float) -> str:
    """A simple, human-readable label from cloud cover - not meteorologically rigorous."""
    if cloud_cover_percent <= _CLOUD_COVER_CLEAR_MAX:
        return "Clear"
    if cloud_cover_percent <= _CLOUD_COVER_PARTLY_CLOUDY_MAX:
        return "Partly cloudy"
    return "Overcast"


def _carry_forward_yesterday_forecast(
    previous_record: dict[str, Any], today_str: str, yesterday_str: str
) -> float | None:
    """The forecast to score yesterday's actual generation against.

    solar_forecast.json is overwritten on every run, so nothing normally
    remembers what was predicted for a day once it's over - by the time
    "today" becomes "yesterday", its "today_kwh" is gone. This captures it
    exactly once, at the first run after midnight, from the previous (now-
    completed) day's own "today_kwh" (identified via that record's
    "for_date"); every later run that same day then carries the already-
    captured value forward via its own "yesterday_forecast_kwh" field, since
    by then "for_date" has already rolled over to today. Returns None across
    a >1-day gap (the predictor didn't run at all yesterday, e.g. the Pi was
    down) - there's then no meaningful forecast to compare against.
    """
    if previous_record.get("for_date") == yesterday_str:
        return previous_record.get("today_kwh")
    if previous_record.get("for_date") == today_str:
        return previous_record.get("yesterday_forecast_kwh")
    return None


def run(config: dict[str, Any], *, quiet: bool) -> int:
    """Predict today/tomorrow's solar generation and write it for the dashboard. 0/1 exit code."""
    location = config.get("location", {})
    latitude = location.get("latitude", 0.0)
    longitude = location.get("longitude", 0.0)
    timezone_name = location.get("default_timezone_str", "Europe/London")
    if latitude == 0.0 and longitude == 0.0:
        msg = "location.latitude/longitude are not set in config.yaml - see solar_forecast comments"
        logger.error(msg)
        if not quiet:
            print(msg)
        return 1

    model_path = Path(get_solar_forecast_model_path())
    if not model_path.exists():
        msg = f"No trained model at {model_path} - run scripts/solar_forecast_trainer.py first"
        logger.error(msg)
        if not quiet:
            print(msg)
        return 1
    try:
        model = joblib.load(model_path)
    except Exception as e:  # noqa: BLE001  # Corrupt/partial file, or a joblib/scikit-learn version mismatch after an upgrade
        msg = f"Failed to load model at {model_path}: {e} - re-run scripts/solar_forecast_trainer.py"
        logger.error(msg)
        if not quiet:
            print(msg)
        return 1

    weather_records = fetch_forecast_weather_hourly(latitude, longitude, timezone_name)
    if weather_records is None:
        msg = "Failed to fetch weather forecast (see logs above)"
        logger.error(msg)
        if not quiet:
            print(msg)
        return 1

    forecast_rows = build_forecast_rows(weather_records)
    predicted_kw = predict_hourly_kw(model, forecast_rows)

    now_local = datetime.now(tz=UTC).astimezone(pytz.timezone(timezone_name))
    today_str = now_local.strftime("%Y-%m-%d")
    tomorrow_str = (now_local + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_str = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")

    hourly = [
        {"timestamp": row["timestamp"], "predicted_kw": round(kw, 3)}
        for row, kw in zip(forecast_rows, predicted_kw)
    ]
    today_kwh = sum(h["predicted_kw"] for h in hourly if h["timestamp"].startswith(today_str))
    tomorrow_kwh = sum(h["predicted_kw"] for h in hourly if h["timestamp"].startswith(tomorrow_str))

    current_hour_key = now_local.strftime("%Y-%m-%d %H:00")
    current_weather_row = next((r for r in forecast_rows if r["timestamp"] == current_hour_key), None)
    current_weather = None
    if current_weather_row is not None:
        current_weather = {
            "cloud_cover_percent": current_weather_row["cloud_cover"],
            "temperature_c": current_weather_row["temperature_2m"],
            "description": _weather_description(current_weather_row["cloud_cover"]),
        }

    previous_record = read_json_state(get_solar_forecast_path())
    yesterday_forecast_kwh = _carry_forward_yesterday_forecast(previous_record, today_str, yesterday_str)

    pv_history = read_json_state(get_solax_historical_data_path())
    yesterday_actual_kwh = compute_actual_daily_kwh(pv_history.get("data", []), yesterday_str)

    yesterday_error_kwh = (
        round(yesterday_actual_kwh - yesterday_forecast_kwh, 2)
        if yesterday_actual_kwh is not None and yesterday_forecast_kwh is not None
        else None
    )

    record = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "for_date": today_str,
        "model_trained_at": datetime.fromtimestamp(model_path.stat().st_mtime, tz=UTC).isoformat(),
        "today_kwh": round(today_kwh, 2),
        "tomorrow_kwh": round(tomorrow_kwh, 2),
        "yesterday_forecast_kwh": yesterday_forecast_kwh,
        "yesterday_actual_kwh": round(yesterday_actual_kwh, 2) if yesterday_actual_kwh is not None else None,
        "yesterday_error_kwh": yesterday_error_kwh,
        "current_weather": current_weather,
        "hourly_kw": hourly,
    }
    write_json_atomic(get_solar_forecast_path(), record)

    summary = f"Solar forecast: today {record['today_kwh']} kWh, tomorrow {record['tomorrow_kwh']} kWh"
    logger.info(summary)
    if not quiet:
        print(summary)

    return 0


def main() -> None:
    """Execute main entry point."""
    args = _create_argument_parser().parse_args()
    configure_cron_safe_logging(
        level=getattr(logging, args.log_level),
        quiet=args.quiet,
        log_filename="solar_forecast_predictor.log",
    )

    config_path = args.config or get_config_path()
    config = load_static_config(config_path)
    if config is None:
        print("Failed to load config.yaml (see logs above)")
        sys.exit(1)

    if not config.get("solar_forecast", {}).get("enabled", False):
        logger.info("Solar forecast disabled (solar_forecast.enabled: false in config.yaml)")
        if not args.quiet:
            print("Solar forecast is disabled (solar_forecast.enabled: false)")
        sys.exit(0)

    sys.exit(run(config, quiet=args.quiet))


if __name__ == "__main__":
    main()
