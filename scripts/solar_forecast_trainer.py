#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Solar Forecast Model Trainer (one-shot CLI).

Trains a RandomForestRegressor on this system's own historical PV output
(data/solax_historical_data.json) joined with historical weather (Open-Meteo,
free, no API key) for the configured location, and saves it for
scripts/solar_forecast_predictor.py to use. Deliberately not Solcast's
generic forecast - see config.yaml's solar_forecast section for why.

Meant to be re-run periodically (e.g. weekly via cron) as more historical
data accumulates - retraining more often than that isn't worth it, since the
input data barely changes hour to hour:

    0 3 * * 0 cd /path/to/repo && python3 scripts/solar_forecast_trainer.py --quiet

Entirely decoupled from battery_mode_daemon.py and hotwater_mode_daemon.py:
only reads solax_historical_data.json and the weather API, and only writes
its own model file - it never touches either daemon's state.

Usage:
    python3 scripts/solar_forecast_trainer.py [--config config.yaml]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging
from typing import Any

import joblib
from battery_evening_predictor import load_historical_records
from hotwater_automation_core import get_config_path

from src.api_clients.weather_client import fetch_historical_weather_hourly
from src.config_manager.config_manager import load_static_config
from src.core_logic.solar_forecast_logic import build_training_rows, train_model
from src.utils.logging_setup import configure_cron_safe_logging
from src.utils.paths import get_solar_forecast_model_path

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

    pv_records = load_historical_records()
    if not pv_records:
        msg = "No historical PV data available - run scripts/solax_cloud_data_logger.py first"
        logger.error(msg)
        if not quiet:
            print(msg)
        return 1

    start_date = pv_records[0]["timestamp"][:10]
    end_date = pv_records[-1]["timestamp"][:10]
    timezone = location.get("default_timezone_str", "Europe/London")

    if not quiet:
        print(f"Fetching historical weather for {start_date}..{end_date}...")
    weather_records = fetch_historical_weather_hourly(latitude, longitude, start_date, end_date, timezone)
    if weather_records is None:
        msg = "Failed to fetch historical weather (see logs above)"
        logger.error(msg)
        if not quiet:
            print(msg)
        return 1

    training_rows = build_training_rows(pv_records, weather_records)
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
        f"held-out rows: MAE {result.mae_kw:.3f} kW, R2 {result.r2:.3f}. Saved to {model_path}."
    )
    logger.info(summary)
    if not quiet:
        print(summary)

    return 0


def main() -> None:
    """Execute main entry point."""
    args = _create_argument_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    config_path = args.config or get_config_path()
    config = load_static_config(config_path)
    if config is None:
        print("Failed to load config.yaml (see logs above)")
        sys.exit(1)

    sys.exit(run(config, quiet=args.quiet))


if __name__ == "__main__":
    main()
