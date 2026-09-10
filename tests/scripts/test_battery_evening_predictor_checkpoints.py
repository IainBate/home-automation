"""Tests for battery_evening_predictor.py's dashboard-only checkpoint predictions.

Checkpoints are derived from the same battery-prediction logic the force-heat
decision itself uses (hotwater_automation_core.resolve_battery_prediction_eligibility_end_hour
and DEFAULT_BATTERY_PREDICTION_WINDOW_START_HOUR) rather than hardcoded clock
times, confirmed with the project owner 2026-09-10: the dashboard should show
"will the battery-prediction path still fire today" (the eligibility cutoff),
not arbitrary round numbers.

Regression-safety note: these checkpoints are additive to the existing
predicted_soc_percent/computed_at fields hotwater_automation_core.py reads -
see test_hotwater_battery_soc_source.py for that consumer's own tests, which
this file must not need to touch.
"""

from __future__ import annotations

from datetime import datetime
from unittest import mock

import battery_evening_predictor as predictor
import hotwater_automation_core as core


def _historical_records_with_flat_drift(drift_percent: float) -> list[dict]:
    """Historical days where SoC at 18:00 is 70% and drifts by drift_percent per hour either side."""
    records = []
    for day in range(1, 10):
        for hour in range(17, 24):
            soc = 70.0 + drift_percent * (hour - 18)
            records.append({"timestamp": f"2026-06-{day:02d} {hour:02d}:00:00", "soc_percent": soc})
    return records


def _hw_config(**overrides) -> dict:
    base = {
        "battery_prediction_window_start_hour": 18.0,
        "battery_prediction_deadline_hour": 23.5,
        "forced_discharge_start_hour": 22.5,
        "force_heat_max_duration_hours": 1.0,
        "legionella_max_cycle_duration_hours": 1.0,
    }
    base.update(overrides)
    return base


def _no_schedule_file():
    """No battery_mode_daemon_config.json - resolve_battery_prediction_eligibility_end_hour
    falls back to hw_config's own forced_discharge_start_hour (22.5), giving an
    eligibility cutoff of 21:30 for the default _hw_config() above."""
    return mock.patch.object(core, "get_battery_mode_daemon_config_path", lambda: "/nonexistent/path.json")


def test_checkpoints_are_window_start_and_eligibility_end():
    now_local = datetime(2026, 6, 15, 12, 0)  # noon - both checkpoints still ahead
    records = _historical_records_with_flat_drift(-5.0)

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    times = [c["time"] for c in checkpoints]
    assert times == ["18:00", "21:30"]


def test_eligibility_end_checkpoint_is_the_priority_one():
    now_local = datetime(2026, 6, 15, 12, 0)
    records = _historical_records_with_flat_drift(-5.0)

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    priority_times = [c["time"] for c in checkpoints if c["priority"]]
    assert priority_times == ["21:30"]
    non_priority_times = [c["time"] for c in checkpoints if not c["priority"]]
    assert non_priority_times == ["18:00"]


def test_window_start_checkpoint_omitted_once_it_has_passed():
    now_local = datetime(2026, 6, 15, 19, 0)  # 18:00 has passed, 21:30 hasn't
    records = _historical_records_with_flat_drift(-5.0)

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    assert [c["time"] for c in checkpoints] == ["21:30"]


def test_no_checkpoints_left_once_eligibility_end_has_passed():
    now_local = datetime(2026, 6, 15, 22, 0)  # past both 18:00 and 21:30
    records = _historical_records_with_flat_drift(-5.0)

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    assert checkpoints == []


def test_eligibility_end_derived_from_the_real_schedule_when_available(tmp_path):
    """The battery daemon's real FORCE_DISCHARGE start (22:00) - not hw_config's
    stale forced_discharge_start_hour (22.5) - drives the checkpoint, exactly
    like the force-heat decision itself (see resolve_battery_prediction_eligibility_end_hour)."""
    config_path = tmp_path / "battery_mode_daemon_config.json"
    config_path.write_text(
        '{"schedule": {"enabled": true, "time_ranges": '
        '[{"start_time": "22:00", "end_time": "23:30", "battery_mode": "FORCE_DISCHARGE"}]}}',
        encoding="utf-8",
    )
    now_local = datetime(2026, 6, 15, 12, 0)
    records = _historical_records_with_flat_drift(-5.0)

    with mock.patch.object(core, "get_battery_mode_daemon_config_path", lambda: str(config_path)):
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    assert [c["time"] for c in checkpoints] == ["18:00", "21:00"]


def test_checkpoint_prediction_applies_historical_drift():
    now_local = datetime(2026, 6, 15, 18, 0)
    records = _historical_records_with_flat_drift(-5.0)
    # _historical_records_with_flat_drift only has whole-hour readings, so the
    # 21:30 checkpoint below has no reading within predict_evening_soc's
    # 15-minute match tolerance. Add an explicit 21:30 reading (consistent
    # with the fixture's own -5.0pp/hour drift from the 18:00/70% baseline:
    # 70 + (-5.0 * 3.5) = 52.5) here, in this test's own local copy, rather
    # than in the shared helper - other tests reusing that helper rely on its
    # whole-hour-only closest-match behavior and must not shift.
    records = records + [
        {"timestamp": f"2026-06-{day:02d} 21:30:00", "soc_percent": 52.5} for day in range(1, 10)
    ]

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    checkpoint = next(c for c in checkpoints if c["time"] == "21:30")
    assert checkpoint["predicted_soc_percent"] == 52.5  # 70 - 5*3.5h


def test_checkpoint_prediction_uses_fractional_current_time_not_truncated_hour():
    """Regression test (carried over from the fixed-checkpoint version): running
    a few minutes before the hour must not apply a too-large historical drift
    profile (see _compute_dashboard_checkpoints's own docstring).

    Historical data has readings at both 17:00 (soc 75) and 18:00 (soc 70).
    Run at 17:55, 5 minutes from the 18:00 checkpoint: the closest historical
    reading to "now" (17:55) is 18:00 (5 min away - 17:00 is 55 min away,
    outside predict_evening_soc's 15-minute match tolerance), so the correct
    drift is 70->70 = 0, giving 70.0.
    """
    now_local = datetime(2026, 6, 15, 17, 55)
    records = _historical_records_with_flat_drift(-5.0)

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    checkpoint_18 = next(c for c in checkpoints if c["time"] == "18:00")
    assert checkpoint_18["predicted_soc_percent"] == 70.0


def test_window_start_checkpoint_omitted_when_it_is_not_before_eligibility_end():
    """A degenerate config where the eligibility cutoff falls at/before the
    window start (e.g. a very early forced_discharge_start_hour) must not show
    the window-start checkpoint out of order - only the (always-present)
    eligibility checkpoint is shown."""
    now_local = datetime(2026, 6, 15, 6, 0)
    records = _historical_records_with_flat_drift(-5.0)
    hw_config = _hw_config(forced_discharge_start_hour=18.5)  # eligibility_end = 17.5, before window_start=18.0

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=hw_config
        )

    assert [c["time"] for c in checkpoints] == ["17:30"]
