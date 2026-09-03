#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Solar Forecast Model Trainer (one-shot CLI).

Trains an ExtraTreesRegressor to predict daily PV generation (kWh) from
historical weather (Open-Meteo, free, no API key) for the configured
location, and saves it for scripts/solar_forecast_predictor.py to use.
Deliberately not Solcast's generic forecast - see config.yaml's
solar_forecast section for why.

Ground truth is data/solax_cloud_daily_history.csv (a static, bundled
dataset manually collected from the SolaX Cloud web portal) merged with
newly-accumulating real readings from data/solax_historical_data.json - see
solar_forecast_logic.merge_daily_pv_history()'s docstring for why the merge
works this way (that JSON file was found to be synthetic for
2025-01-01..2026-08-31; the CSV is the trustworthy source for that period,
and the JSON only becomes trustworthy again from 2026-09-01 onward).

Meant to be re-run periodically (e.g. weekly via cron) as more historical
data accumulates - retraining more often than that isn't worth it, since the
input data barely changes day to day:

    0 3 * * 0 cd /path/to/repo && python3 scripts/solar_forecast_trainer.py --quiet

Entirely decoupled from battery_mode_daemon.py and hotwater_mode_daemon.py:
only reads solax_historical_data.json/solax_cloud_daily_history.csv and the
weather API, and only writes its own model file - it never touches either
daemon's state.

Usage:
    python3 scripts/solar_forecast_trainer.py [--config config.yaml]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import csv
import logging
from typing import Any

import joblib
from battery_evening_predictor import load_historical_records
from hotwater_automation_core import get_config_path

from src.api_clients.weather_client import fetch_historical_weather_hourly
from src.config_manager.config_manager import load_static_config
from src.core_logic.solar_forecast_logic import build_daily_training_rows, merge_daily_pv_history, train_model
from src.utils.logging_setup import configure_cron_safe_logging
from src.utils.paths import get_solar_forecast_model_path, get_solax_cloud_daily_history_path

logger = logging.getLogger(__name__)


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


def _load_seed_daily_kwh(path: str) -> dict[str, float]:
    """Load data/solax_cloud_daily_history.csv's "date,pv_kwh" rows into a dict.

    Missing file or malformed rows degrade to an empty/partial dict rather
    than raising - merge_daily_pv_history() still works from local telemetry
    alone (e.g. in a fresh install without the bundled seed file), just with
    less history to train on.
    """
    seed_path = Path(path)
    if not seed_path.exists():
        logger.warning("No seed dataset at %s - training on local telemetry only", seed_path)
        return {}

    seed: dict[str, float] = {}
    try:
        with seed_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    seed[row["date"]] = float(row["pv_kwh"])
                except (KeyError, ValueError):
                    continue
    except OSError:
        logger.exception("Failed to read seed dataset at %s", seed_path)
        return {}
    return seed


def run(config: dict[str, Any], *, quiet: bool) -> int:
    """Train and save the solar forecast model. Returns 0 on success, 1 on failure."""
    location = config.get("location", {})
    latitude = location.get("latitude", 0.0)
    longitude = location.get("longitude", 0.0)
    if latitude == 0.0 and longitude == 0.0:
        msg = "location.latitude/longitude are not set in config.yaml - see solar_forecast comments"
        logger.error(msg)
        if not quiet:
            print(msg)
        return 1

    seed_daily_kwh = _load_seed_daily_kwh(get_solax_cloud_daily_history_path())
    pv_history_records = load_historical_records() or []
    daily_pv_kwh = merge_daily_pv_history(seed_daily_kwh, pv_history_records)
    if not daily_pv_kwh:
        msg = (
            "No historical PV data available (neither data/solax_cloud_daily_history.csv nor "
            "data/solax_historical_data.json) - run scripts/solax_realtime_logger.py for a while first"
        )
        logger.error(msg)
        if not quiet:
            print(msg)
        return 1

    start_date = min(daily_pv_kwh)
    end_date = max(daily_pv_kwh)
    timezone = location.get("default_timezone_str", "Europe/London")

    if not quiet:
        print(f"Fetching historical weather for {start_date}..{end_date} ({len(daily_pv_kwh)} days of PV data)...")
    weather_records = fetch_historical_weather_hourly(latitude, longitude, start_date, end_date, timezone)
    if weather_records is None:
        msg = "Failed to fetch historical weather (see logs above)"
        logger.error(msg)
        if not quiet:
            print(msg)
        return 1

    training_rows = build_daily_training_rows(daily_pv_kwh, weather_records, latitude, longitude, timezone)
    if not quiet:
        print(f"Built {len(training_rows)} training rows - training model...")

    try:
        result = train_model(training_rows)
    except ValueError as e:
        logger.error(str(e))
        if not quiet:
            print(str(e))
        return 1

    model_path = Path(get_solar_forecast_model_path())
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(result.model, model_path)

    summary = (
        f"Model trained on {result.train_rows} rows, validated on {result.holdout_rows} "
        f"held-out rows: MAE {result.mae_kwh:.2f} kWh/day, R2 {result.r2:.3f}. Saved to {model_path}."
    )
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
        log_filename="solar_forecast_trainer.log",
    )

    config_path = args.config or get_config_path()
    config = load_static_config(config_path)
    if config is None:
        print("Failed to load config.yaml (see logs above)")
        sys.exit(1)

    sys.exit(run(config, quiet=args.quiet))


if __name__ == "__main__":
    main()
