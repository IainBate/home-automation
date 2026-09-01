"""Unit tests for src/core_logic/hotwater_decision_logic.py.

Covers determine_hotwater_decision's branches directly (the module's own
docstring examples cover a few of these too, via --doctest-modules, but
those are easy to read past without noticing if they stop being exercised -
these tests are the ones that actually fail a build).
"""

from __future__ import annotations

from datetime import time

from src.core_logic.hotwater_decision_logic import (
    HotWaterDecisionContext,
    determine_hotwater_decision,
    hour_float_to_time,
    is_in_evening_window,
    is_in_offpeak_window,
)


def _context(**overrides):
    defaults = {
        "tank_temperature_c": 30.0,
        "tank_temp_threshold_c": 45.0,
        "car_is_charging": False,
        "battery_soc_percent": None,
        "battery_soc_min_percent": 50.0,
        "grid_is_cheap": False,
        "in_evening_window": False,
    }
    defaults.update(overrides)
    return HotWaterDecisionContext(**defaults)


def test_tank_temperature_unavailable_never_heats():
    decision = determine_hotwater_decision(_context(tank_temperature_c=None))
    assert decision.should_force_heat is False


def test_tank_already_at_or_above_threshold_never_heats():
    decision = determine_hotwater_decision(
        _context(tank_temperature_c=45.0, tank_temp_threshold_c=45.0)
    )
    assert decision.should_force_heat is False


def test_car_charging_dominates_even_in_daytime():
    decision = determine_hotwater_decision(
        _context(car_is_charging=True, in_evening_window=False, grid_is_cheap=False)
    )
    assert decision.should_force_heat is True


def test_daytime_not_evening_never_heats_without_car_charging():
    decision = determine_hotwater_decision(_context(in_evening_window=False))
    assert decision.should_force_heat is False


def test_evening_with_battery_surplus_heats():
    decision = determine_hotwater_decision(
        _context(in_evening_window=True, battery_soc_percent=90.0, battery_soc_min_percent=50.0)
    )
    assert decision.should_force_heat is True


def test_evening_with_battery_below_minimum_does_not_heat_alone():
    decision = determine_hotwater_decision(
        _context(in_evening_window=True, battery_soc_percent=10.0, battery_soc_min_percent=50.0)
    )
    assert decision.should_force_heat is False


def test_evening_with_cheap_grid_heats():
    decision = determine_hotwater_decision(
        _context(in_evening_window=True, battery_soc_percent=None, grid_is_cheap=True)
    )
    assert decision.should_force_heat is True


def test_evening_without_battery_surplus_or_cheap_grid_waits():
    decision = determine_hotwater_decision(
        _context(in_evening_window=True, battery_soc_percent=10.0, grid_is_cheap=False)
    )
    assert decision.should_force_heat is False


def test_offpeak_window_wraps_midnight():
    window_start, window_end = time(23, 30), time(5, 30)
    assert is_in_offpeak_window(time(0, 0), window_start, window_end) is True
    assert is_in_offpeak_window(time(23, 45), window_start, window_end) is True
    assert is_in_offpeak_window(time(12, 0), window_start, window_end) is False
    # end is exclusive
    assert is_in_offpeak_window(time(5, 30), window_start, window_end) is False


def test_offpeak_window_same_day():
    window_start, window_end = time(9, 0), time(17, 0)
    assert is_in_offpeak_window(time(12, 0), window_start, window_end) is True
    assert is_in_offpeak_window(time(8, 59), window_start, window_end) is False
    assert is_in_offpeak_window(time(17, 0), window_start, window_end) is False


def test_evening_window_normal_trigger_hour_wraps_midnight():
    trigger_hour_time, window_end = time(18, 0), time(5, 30)
    assert is_in_evening_window(time(20, 0), trigger_hour_time, window_end) is True  # evening
    assert is_in_evening_window(time(2, 0), trigger_hour_time, window_end) is True  # after midnight
    assert is_in_evening_window(time(12, 0), trigger_hour_time, window_end) is False  # daytime


def test_evening_window_low_trigger_hour_still_wraps_instead_of_becoming_same_day_only():
    """Regression test: is_in_offpeak_window's start<=end heuristic would
    wrongly treat a low trigger_hour as a same-day-only window (true only
    between trigger_hour and offpeak_end), inverting the automation's
    behaviour for a plausible config (e.g. trigger_hour temporarily lowered
    to test without waiting for evening). is_in_evening_window must not do
    that - real evening hours must still count as "in the evening window"
    even when trigger_hour is numerically small.
    """
    trigger_hour_time, window_end = time(2, 0), time(5, 30)
    # Real evening hours must still be "in the evening window" here.
    assert is_in_evening_window(time(20, 0), trigger_hour_time, window_end) is True
    assert is_in_evening_window(time(23, 0), trigger_hour_time, window_end) is True
    # And the early-morning hours right after trigger_hour too.
    assert is_in_evening_window(time(3, 0), trigger_hour_time, window_end) is True


def test_hour_float_to_time_converts_fractional_hour():
    assert hour_float_to_time(21.5) == time(21, 30)
    assert hour_float_to_time(18) == time(18, 0)
    assert hour_float_to_time(0) == time(0, 0)


def test_hour_float_to_time_rounds_to_nearest_minute():
    assert hour_float_to_time(21.999) == time(22, 0)
    assert hour_float_to_time(21.0001) == time(21, 0)
