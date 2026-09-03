"""Shared loader for solax_historical_data.json.

Moved out of scripts/battery_evening_predictor.py (which still re-exports it,
for scripts/solar_forecast_trainer.py's `from battery_evening_predictor import
load_historical_records` and the existing test monkeypatches of
`predictor.load_historical_records`) so scripts/hotwater_automation_core.py
can also read it directly, for the per-battery force-heat trigger prediction
(see get_battery_prediction_to_deadline) - hotwater_automation_core.py is
itself imported by battery_evening_predictor.py, so importing the other way
round would be a circular import.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src.utils.paths import get_solax_historical_data_path

logger = logging.getLogger(__name__)


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
