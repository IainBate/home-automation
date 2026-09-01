"""Tests for battery_evening_predictor.py's dashboard-only checkpoint predictions.

Regression-safety note: these checkpoints are additive to the existing
predicted_soc_percent/computed_at fields hotwater_automation_core.py reads -
see test_hotwater_battery_soc_source.py for that consumer's own tests, which
this file must not need to touch.
"""

from __future__ import annotations

from datetime import datetime

import battery_evening_predictor as predictor


def _historical_records_with_flat_drift(drift_percent: float) -> list[dict]:
    """Historical days where SoC at 18:00 is 70% and drifts by drift_percent per hour either side."""
    records = []
    for day in range(1, 10):
        for hour in range(17, 24):
            soc = 70.0 + drift_percent * (hour - 18)
            records.append({"timestamp": f"2026-06-{day:02d} {hour:02d}:00:00", "soc_percent": soc})
    return records


def test_checkpoints_omit_times_already_passed_today():
    now_local = datetime(2026, 6, 15, 21, 0)  # 9:00 PM - 6 PM and 8 PM have passed
    records = _historical_records_with_flat_drift(-5.0)

    checkpoints = predictor._compute_dashboard_checkpoints(70.0, records, now_local, min_sample_days=5)

    times = [c["time"] for c in checkpoints]
    assert times == ["22:00", "23:30"]


def test_priority_flag_is_only_set_on_11_30pm():
    now_local = datetime(2026, 6, 15, 12, 0)
    records = _historical_records_with_flat_drift(-5.0)

    checkpoints = predictor._compute_dashboard_checkpoints(70.0, records, now_local, min_sample_days=5)

    priority_times = [c["time"] for c in checkpoints if c["priority"]]
    assert priority_times == ["23:30"]


def test_checkpoint_prediction_applies_historical_drift():
    now_local = datetime(2026, 6, 15, 18, 0)
    records = _historical_records_with_flat_drift(-5.0)

    checkpoints = predictor._compute_dashboard_checkpoints(70.0, records, now_local, min_sample_days=5)

    checkpoint_22 = next(c for c in checkpoints if c["time"] == "22:00")
    assert checkpoint_22["predicted_soc_percent"] == 50.0  # 70 - 5*4h


def test_checkpoint_prediction_uses_fractional_current_time_not_truncated_hour():
    """Regression test: running a few minutes before the hour must not apply a
    too-large historical drift profile (see the module docstring on
    _compute_dashboard_checkpoints).

    Historical data has readings at both 17:00 (soc 75) and 18:00 (soc 70).
    Run at 17:55, 5 minutes from the 18:00 checkpoint: the closest historical
    reading to "now" (17:55) is 18:00 (5 min away - 17:00 is 55 min away,
    outside predict_evening_soc's 15-minute match tolerance), so the correct
    drift is 70->70 = 0, giving 70.0. The truncated-hour bug instead anchored
    "now" to 17:00 exactly, using the full 17:00->18:00 drift (75->70 = -5)
    on top of today's live reading, giving 65.0 - a full 5pp too low from a
    single 5-minute-late cron run.
    """
    now_local = datetime(2026, 6, 15, 17, 55)  # 5 minutes before the 18:00 checkpoint
    records = _historical_records_with_flat_drift(-5.0)

    checkpoints = predictor._compute_dashboard_checkpoints(70.0, records, now_local, min_sample_days=5)

    checkpoint_18 = next(c for c in checkpoints if c["time"] == "18:00")
    assert checkpoint_18["predicted_soc_percent"] == 70.0


def test_no_checkpoints_left_after_11_30pm():
    now_local = datetime(2026, 6, 15, 23, 45)
    records = _historical_records_with_flat_drift(-5.0)

    checkpoints = predictor._compute_dashboard_checkpoints(70.0, records, now_local, min_sample_days=5)

    assert checkpoints == []
