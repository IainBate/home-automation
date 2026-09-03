#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Evening Battery SoC Predictor (one-shot CLI).

Predicts what the battery's SoC will be a few hours after
hotwater_automation.trigger_hour (e.g. 21:30 -> 00:30), and writes the result
to a small status file that hotwater_automation_core.py's force-heat check
reads in preference to a live SoC snapshot - a multi-hour force-heat run
needs to know the surplus will still be there when it finishes, not just that
it's there right now.

Uses a simple "analog day" statistical method (src/core_logic/
battery_evening_prediction_logic.py), not a trained ML model: it looks at how
SoC has historically moved between two times of day in the same calendar
month, using the 5-minute log already collected by
scripts/solax_cloud_data_logger.py (data/solax_historical_data.json). No new
dependency, cheap enough to run once a day.

Also writes a handful of additional same-day SoC checkpoints
(DASHBOARD_CHECKPOINT_TIMES below) purely for the dashboard
(src/dashboard/status_collector.py) to display - hotwater_automation_core.py
only ever reads the original predicted_soc_percent/computed_at fields above,
unchanged by this addition.

Intended to run once daily via cron, shortly before trigger_hour (e.g.
"55 20 * * *"), so a fresh prediction is in place by the time
hotwater_automation_core.py needs it:

    55 20 * * * cd /path/to/repo && python3 scripts/battery_evening_predictor.py --quiet

Reads the battery's live SoC the same read-only way battery_mode_daemon.py
and hotwater_automation_core.py do (solax_modbus_soc()) and reads
solax_historical_data.json purely as input - it never writes to either the
battery daemon's state or that historical log, so it carries no risk to the
existing battery software.

Usage:
    python3 scripts/battery_evening_predictor.py
    python3 scripts/battery_evening_predictor.py --quiet
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytz

from hotwater_automation_core import get_battery_soc_percent, get_config_path

from src.config_manager.config_manager import load_static_config
from src.core_logic.battery_evening_prediction_logic import (
    extract_forecast_generation_kwh,
    predict_evening_soc,
)
from src.utils.historical_data import load_historical_records
from src.utils.paths import (
    get_battery_evening_prediction_path,
    get_solar_forecast_path,
)

logger = logging.getLogger(__name__)

DEFAULT_TRIGGER_HOUR = 21.5
DEFAULT_HORIZON_HOURS = 3.0
DEFAULT_MIN_SAMPLE_DAYS = 5
DEFAULT_TIMEZONE = "Europe/London"

# Extra same-day checkpoints for the dashboard (src/dashboard/status_collector.py) -
# additive to the main hotwater_automation prediction above, which
# hotwater_automation_core.py alone still reads. 23:30 is flagged as the
# priority checkpoint: it's the moment the schedule switches from evening
# FORCE_DISCHARGE to overnight FORCE_CHARGE (see battery_mode_daemon_config.json),
# so it's the most useful single number for "how much battery is left before
# cheap-rate charging kicks in".
DASHBOARD_CHECKPOINT_TIMES = [
    (18, 0, "6:00 PM", False),
    (20, 0, "8:00 PM", False),
    (22, 0, "10:00 PM", False),
    (23, 30, "11:30 PM", True),
]


def _create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Predict evening battery SoC from historical data and write it for "
            "the hot water automation to read"
        ),
        epilog="Example:\n  python3 scripts/battery_evening_predictor.py --quiet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument(
        "--log-level",
        type=str,
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set logging level (default: WARNING)",
    )
    return parser


def load_historical_records() -> list[dict[str, Any]] | None:
    """Load the "data" list from solax_historical_data.json, or None on failure."""
    path = Path(get_solax_historical_data_path())
    if not path.exists():
        logger.error(
            "Historical data file not found at %s - run scripts/solax_cloud_data_logger.py "
            "first",
            path,
        )
        return None
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to read/parse historical data file at %s", path)
        return None
    return content.get("data", [])


def write_prediction(prediction_record: dict[str, Any]) -> None:
    """Write the prediction status file atomically (same pattern as hotwater state)."""
    path = Path(get_battery_evening_prediction_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(prediction_record, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _compute_dashboard_checkpoints(
    current_soc_percent: float,
    historical_records: list[dict[str, Any]],
    now_local: datetime,
    min_sample_days: int,
) -> list[dict[str, Any]]:
    """Predict SoC at each still-upcoming DASHBOARD_CHECKPOINT_TIMES entry today.

    Reuses predict_evening_soc() unmodified, anchored to the exact current
    time (fractional trigger_hour - not truncated to the hour, which would
    apply a too-large historical drift profile whenever this runs off the
    hour) with a horizon computed to land exactly on each checkpoint's clock
    time. A checkpoint already passed today is omitted rather than
    predicting backwards.
    """
    now_hour_float = now_local.hour + now_local.minute / 60.0
    checkpoints = []
    for hour, minute, label, is_priority in DASHBOARD_CHECKPOINT_TIMES:
        target_hour_float = hour + minute / 60.0
        if target_hour_float <= now_hour_float:
            continue

        result = predict_evening_soc(
            current_soc_percent=current_soc_percent,
            historical_records=historical_records,
            trigger_hour=now_hour_float,
            horizon_hours=target_hour_float - now_hour_float,
            reference_day_of_year=now_local.timetuple().tm_yday,
            min_sample_days=min_sample_days,
        )
        checkpoints.append(
            {
                "time": f"{hour:02d}:{minute:02d}",
                "label": label,
                "priority": is_priority,
                "predicted_soc_percent": result.predicted_soc_percent,
                "sample_count": result.sample_count,
            }
        )
    return checkpoints


def _read_forecast_generation_kwh(
    config: dict[str, Any], trigger_ts: datetime, horizon_ts: datetime
) -> float | None:
    """Return today's forecast solar generation (kWh) for [trigger_ts, horizon_ts), or None.

    Best-effort: solar_forecast is a separate, optional subsystem
    (scripts/solar_forecast_predictor.py) - if it's disabled, hasn't
    produced a forecast file yet, or the file can't be read/doesn't cover
    this window, this returns None so predict_evening_soc falls back to the
    plain historical average, exactly as if this correction didn't exist.
    """
    if not config.get("solar_forecast", {}).get("enabled", False):
        return None

    path = Path(get_solar_forecast_path())
    if not path.exists():
        return None

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to read solar forecast file, skipping generation-based correction")
        return None

    hourly_kw = record.get("hourly_kw")
    if not hourly_kw:
        return None

    return extract_forecast_generation_kwh(hourly_kw, trigger_ts, horizon_ts)


def run(config: dict[str, Any], *, quiet: bool) -> int:
    """Compute and write today's evening SoC prediction.

    Returns:
        0 on success (including "not enough historical data to predict" -
        that's a legitimate outcome the caller falls back from, not an
        error), 1 if the live SoC or historical data couldn't be read at all.

    """
    hw_config = config.get("hotwater_automation", {})
    prediction_config = config.get("battery_evening_prediction", {})

    trigger_hour = prediction_config.get(
        "trigger_hour", hw_config.get("trigger_hour", DEFAULT_TRIGGER_HOUR)
    )
    horizon_hours = prediction_config.get(
        "horizon_hours",
        hw_config.get("force_heat_max_duration_hours", DEFAULT_HORIZON_HOURS),
    )
    min_sample_days = prediction_config.get("min_sample_days", DEFAULT_MIN_SAMPLE_DAYS)

    current_soc_percent = get_battery_soc_percent(config)
    if current_soc_percent is None:
        logger.error("Could not read live battery SoC, cannot predict")
        if not quiet:
            print("Failed to read live battery SoC (see logs above)")
        return 1

    historical_records = load_historical_records()
    if historical_records is None:
        if not quiet:
            print("Failed to load historical data (see logs above)")
        return 1

    tz_name = config.get("location", {}).get("default_timezone_str", DEFAULT_TIMEZONE)
    now_local = datetime.now(tz=UTC).astimezone(pytz.timezone(tz_name))

    # Naive (tzinfo stripped): solar_forecast.json's hourly_kw timestamps are
    # plain local wall-clock strings (see solar_forecast_predictor.py's
    # now_local.strftime("%Y-%m-%d %H:00")), not timezone-aware - comparing
    # them against an aware datetime raises TypeError.
    #
    # Built via timedelta addition from midnight, not hour=int(trigger_hour),
    # minute=round((trigger_hour % 1) * 60) - that rounds e.g. 20.995 to
    # minute=60, which datetime.replace() rejects with ValueError (config.yaml's
    # schema only bounds trigger_hour to [0, 24), not its fractional part).
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    trigger_ts = midnight + timedelta(hours=trigger_hour)
    horizon_ts = trigger_ts + timedelta(hours=horizon_hours)
    forecast_generation_kwh = _read_forecast_generation_kwh(config, trigger_ts, horizon_ts)

    result = predict_evening_soc(
        current_soc_percent=current_soc_percent,
        historical_records=historical_records,
        trigger_hour=trigger_hour,
        horizon_hours=horizon_hours,
        reference_day_of_year=now_local.timetuple().tm_yday,
        min_sample_days=min_sample_days,
        forecast_generation_kwh=forecast_generation_kwh,
    )

    prediction_record = {
        "computed_at": datetime.now(tz=UTC).isoformat(),
        "trigger_hour": trigger_hour,
        "horizon_hours": horizon_hours,
        "current_soc_percent": current_soc_percent,
        "predicted_soc_percent": result.predicted_soc_percent,
        "sample_count": result.sample_count,
        "average_drift_percent": result.average_drift_percent,
        "applied_drift_percent": result.applied_drift_percent,
        "forecast_generation_kwh": forecast_generation_kwh,
        "reason": result.reason,
        # Additional dashboard-only checkpoints - hotwater_automation_core.py
        # only ever reads the fields above, unchanged.
        "dashboard_checkpoints": _compute_dashboard_checkpoints(
            current_soc_percent, historical_records, now_local, min_sample_days
        ),
    }
    write_prediction(prediction_record)

    logger.info(
        "Evening SoC prediction: current %.1f%% -> predicted %s (%s)",
        current_soc_percent,
        (
            f"{result.predicted_soc_percent:.1f}%"
            if result.predicted_soc_percent is not None
            else "unavailable"
        ),
        result.reason,
    )
    if not quiet:
        print(f"Current SoC: {current_soc_percent:.1f}%")
        print(
            "Predicted SoC at +{:.1f}h: {}".format(
                horizon_hours,
                (
                    f"{result.predicted_soc_percent:.1f}%"
                    if result.predicted_soc_percent is not None
                    else "unavailable"
                ),
            )
        )
        print(result.reason)

    return 0


def main() -> None:
    """Execute main entry point."""
    parser = _create_argument_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    config = load_static_config(get_config_path())
    if config is None:
        print("ERROR" if args.quiet else "Failed to load config.yaml (see logs above)")
        sys.exit(1)

    if not config.get("battery_evening_prediction", {}).get("enabled", False):
        if not args.quiet:
            print(
                "Battery evening prediction is disabled "
                "(battery_evening_prediction.enabled: false)"
            )
        sys.exit(0)

    try:
        sys.exit(run(config, quiet=args.quiet))
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(130)


if __name__ == "__main__":
    main()
