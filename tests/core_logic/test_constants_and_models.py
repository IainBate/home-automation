"""Unit tests for src/core_logic/battery_simulation/constants_and_models.py -
the BatteryMode enum and its display-string/dataclass boundary functions.

battery_mode_to_display_string's dict.get(mode, "Self-Use") fallback silently
masks any BatteryMode member missing from display_mapping - the enum-repr/
doctest-drift pattern already found twice this session (ohme_charging_logic.py,
_modbus_reader.py). test_every_battery_mode_has_a_display_string is a
regression guard against a typo silently falling back instead of erroring.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.core_logic.battery_simulation.constants_and_models import (
    BatteryMode,
    BatterySimulationPeriod,
    BatterySimulationResult,
    battery_mode_to_display_string,
)
from src.utils.exceptions import ValidationError


def test_every_battery_mode_has_a_display_string():
    """A member missing from display_mapping would silently fall back to "Self-Use"."""
    for mode in BatteryMode:
        if mode == BatteryMode.PARTIAL_CHARGE:
            continue  # handled by its own dedicated branch, not the mapping dict
        assert battery_mode_to_display_string(mode) != "Self-Use" or mode == BatteryMode.SELF_USE


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (BatteryMode.SELF_USE, "Self-Use"),
        (BatteryMode.FORCE_CHARGE, "Charging"),
        (BatteryMode.FORCE_DISCHARGE, "Discharging"),
        (BatteryMode.MANUAL_STOP, "Holding"),
        (BatteryMode.FEED_IN_PRIORITY, "Feed-in Priority"),
        (BatteryMode.BACKUP, "Backup"),
        (BatteryMode.PEAK_SHAVING, "Peak Shaving"),
        (BatteryMode.TOU_MODE, "TOU Mode"),
        (BatteryMode.SMART_SCHEDULE, "Smart Schedule"),
        (BatteryMode.UNKNOWN_WORK_MODE, "Unknown Mode"),
        (BatteryMode.IDLE, "Idle"),
    ],
)
def test_battery_mode_to_display_string_known_values(mode, expected):
    assert battery_mode_to_display_string(mode) == expected


def test_partial_charge_with_minutes():
    assert (
        battery_mode_to_display_string(BatteryMode.PARTIAL_CHARGE, partial_charge_minutes=15)
        == "Partial Charge (15 mins)"
    )


def test_partial_charge_without_minutes_falls_back():
    assert battery_mode_to_display_string(BatteryMode.PARTIAL_CHARGE) == "Partial Charge"


def _period_kwargs(**overrides):
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
    defaults = {
        "start_time_utc": start,
        "end_time_utc": end,
        "battery_mode": BatteryMode.SELF_USE,
        "pv_generation_kw": 1.0,
        "house_background_load_kw": 0.5,
        "appliance_load_kw": 0.2,
        "electricity_price_gbp_per_kwh": 0.25,
    }
    defaults.update(overrides)
    return defaults


def test_battery_simulation_period_valid_construction():
    period = BatterySimulationPeriod(**_period_kwargs())
    assert period.duration_hours == pytest.approx(0.5)


def test_battery_simulation_period_rejects_end_before_start():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="End time must be after start time"):
        BatterySimulationPeriod(**_period_kwargs(start_time_utc=start, end_time_utc=end))


def test_battery_simulation_period_rejects_negative_pv():
    with pytest.raises(ValidationError, match="PV generation cannot be negative"):
        BatterySimulationPeriod(**_period_kwargs(pv_generation_kw=-1.0))


def test_battery_simulation_period_rejects_negative_house_load():
    with pytest.raises(ValidationError, match="House background load cannot be negative"):
        BatterySimulationPeriod(**_period_kwargs(house_background_load_kw=-1.0))


def test_battery_simulation_period_rejects_negative_appliance_load():
    with pytest.raises(ValidationError, match="Appliance load cannot be negative"):
        BatterySimulationPeriod(**_period_kwargs(appliance_load_kw=-1.0))


def _result_kwargs(**overrides):
    defaults = {
        "period": BatterySimulationPeriod(**_period_kwargs()),
        "starting_soc_percent": 50.0,
        "ending_soc_percent": 55.0,
        "soc_change_percent": 5.0,
        "battery_charge_kw": 1.0,
        "battery_discharge_kw": 0.0,
        "grid_import_kw": 0.5,
        "battery_efficiency_used": 0.95,
        "energy_stored_kwh": 0.5,
        "energy_discharged_kwh": 0.0,
        "energy_balance_kwh": 0.5,
        "grid_cost_gbp": 0.1,
        "is_valid": True,
    }
    defaults.update(overrides)
    return defaults


def test_battery_simulation_result_defaults_warnings_to_empty_list():
    result = BatterySimulationResult(**_result_kwargs())
    assert result.warnings == []


def test_battery_simulation_result_warnings_default_is_not_a_shared_mutable_list():
    """A shared class-level default list would leak mutations across instances."""
    result_a = BatterySimulationResult(**_result_kwargs())
    result_a.warnings.append("uh oh")

    result_b = BatterySimulationResult(**_result_kwargs())
    assert result_b.warnings == []


def test_battery_simulation_result_preserves_explicit_warnings():
    result = BatterySimulationResult(**_result_kwargs(warnings=["a warning"]))
    assert result.warnings == ["a warning"]
