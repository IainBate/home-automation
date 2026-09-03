"""Tests for get_battery_soc_percent (per-battery minimum) and
get_battery_prediction_to_deadline in hotwater_automation_core.py - the
battery-prediction trigger path (see HotWaterDecisionContext.
battery_prediction_trigger_active).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest import mock

import hotwater_automation_core as core
from src.core_logic.battery_evening_prediction_logic import EveningSocPrediction


def test_get_battery_soc_percent_returns_the_lower_of_master_and_slave():
    with mock.patch.object(
        core, "solax_modbus_soc", lambda cfg: {"master": 80.0, "slave": 55.0}
    ):
        assert core.get_battery_soc_percent({}) == 55.0


def test_get_battery_soc_percent_returns_none_when_soc_unavailable():
    with mock.patch.object(core, "solax_modbus_soc", lambda cfg: None):
        assert core.get_battery_soc_percent({}) is None


def test_battery_prediction_returns_the_lower_of_the_two_predictions():
    now_local = datetime(2026, 9, 3, 17, 0, tzinfo=UTC)

    def fake_predict(*, current_soc_percent, **kwargs):
        # Same drift (-10pp) applied to each battery's own live reading.
        return EveningSocPrediction(
            predicted_soc_percent=current_soc_percent - 10.0,
            sample_count=10,
            average_drift_percent=-10.0,
            applied_drift_percent=-10.0,
            reason="stub",
        )

    with mock.patch.object(
        core, "solax_modbus_soc", lambda cfg: {"master": 80.0, "slave": 40.0}
    ), mock.patch.object(
        core, "load_historical_records", lambda: [{"timestamp": "2026-09-01 17:00:00", "soc_percent": 50}]
    ), mock.patch.object(
        core, "predict_evening_soc", side_effect=fake_predict
    ):
        predicted_min, reason = core.get_battery_prediction_to_deadline({}, {}, now_local, 23.5)

    # slave: 40 - 10 = 30; master: 80 - 10 = 70 -> min is the slave's 30.
    assert predicted_min == 30.0
    assert "slave" in reason


def test_battery_prediction_unavailable_when_live_soc_missing():
    now_local = datetime(2026, 9, 3, 17, 0, tzinfo=UTC)

    with mock.patch.object(core, "solax_modbus_soc", lambda cfg: None):
        predicted_min, reason = core.get_battery_prediction_to_deadline({}, {}, now_local, 23.5)

    assert predicted_min is None
    assert "live" in reason.lower()


def test_battery_prediction_unavailable_when_historical_data_missing():
    now_local = datetime(2026, 9, 3, 17, 0, tzinfo=UTC)

    with mock.patch.object(
        core, "solax_modbus_soc", lambda cfg: {"master": 80.0, "slave": 40.0}
    ), mock.patch.object(core, "load_historical_records", lambda: None):
        predicted_min, reason = core.get_battery_prediction_to_deadline({}, {}, now_local, 23.5)

    assert predicted_min is None
    assert "historical" in reason.lower()


def test_battery_prediction_unavailable_when_either_battery_lacks_enough_samples():
    now_local = datetime(2026, 9, 3, 17, 0, tzinfo=UTC)

    def fake_predict(*, current_soc_percent, **kwargs):
        return EveningSocPrediction(
            predicted_soc_percent=None,
            sample_count=1,
            average_drift_percent=None,
            applied_drift_percent=None,
            reason="not enough historical days",
        )

    with mock.patch.object(
        core, "solax_modbus_soc", lambda cfg: {"master": 80.0, "slave": 40.0}
    ), mock.patch.object(
        core, "load_historical_records", lambda: [{"timestamp": "2026-09-01 17:00:00", "soc_percent": 50}]
    ), mock.patch.object(
        core, "predict_evening_soc", side_effect=fake_predict
    ):
        predicted_min, reason = core.get_battery_prediction_to_deadline({}, {}, now_local, 23.5)

    assert predicted_min is None


def test_battery_prediction_none_when_already_past_deadline():
    now_local = datetime(2026, 9, 3, 23, 45, tzinfo=UTC)  # past the 23.5 (11:30pm) deadline

    predicted_min, reason = core.get_battery_prediction_to_deadline({}, {}, now_local, 23.5)

    assert predicted_min is None
    assert "deadline" in reason.lower()
