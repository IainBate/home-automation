"""Light characterization tests for scripts/power_usage_analysis.py's pure
helper functions - this script has zero src/ imports and does no
network/hardware I/O at all, but is treated as lower priority per the test
plan's scope decision, so this covers the small pure utilities only, not the
large simulate_*/analyze_by_month functions.
"""

from __future__ import annotations

from datetime import datetime

from power_usage_analysis import (
    calculate_cost,
    detect_car_charging_periods,
    get_mode_at_time,
    get_month,
    get_octopus_intelligent_go_rate,
    is_car_charging_at_time,
)


def test_octopus_rate_off_peak_window():
    rate_period, rate_value = get_octopus_intelligent_go_rate(datetime(2026, 1, 1, 2, 0))
    assert rate_period == "off_peak"
    assert rate_value == 0.07


def test_octopus_rate_day_rate_outside_window():
    rate_period, rate_value = get_octopus_intelligent_go_rate(datetime(2026, 1, 1, 14, 0))
    assert rate_period == "day"
    assert rate_value == 0.25


def test_calculate_cost_import_uses_day_rate():
    cost, rate_period = calculate_cost(1.0, datetime(2026, 1, 1, 14, 0))
    assert rate_period == "day"
    assert cost == (1.0 * 0.25 * (5 / 60))


def test_calculate_cost_import_uses_off_peak_rate():
    cost, rate_period = calculate_cost(1.0, datetime(2026, 1, 1, 2, 0))
    assert rate_period == "off_peak"
    assert cost == (1.0 * 0.07 * (5 / 60))


def test_calculate_cost_export_gives_negative_cost():
    cost, rate_period = calculate_cost(-2.0, datetime(2026, 1, 1, 14, 0))
    assert rate_period == "export"
    assert cost < 0
    assert cost == -(2.0 * 0.15 * (5 / 60))


def test_detect_car_charging_periods_finds_sustained_high_load():
    data = [
        {"timestamp": "2026-01-01 18:00:00", "load_power_kw": 6.0, "battery_power_kw": 0.5},
        {"timestamp": "2026-01-01 18:05:00", "load_power_kw": 6.0, "battery_power_kw": 0.5},
        {"timestamp": "2026-01-01 18:10:00", "load_power_kw": 6.0, "battery_power_kw": 0.5},
        {"timestamp": "2026-01-01 18:15:00", "load_power_kw": 0.5, "battery_power_kw": 0.0},
    ]
    periods = detect_car_charging_periods(data, min_duration_minutes=10, min_power_kw=5.0)
    assert len(periods) == 1
    start, end = periods[0]
    assert start == datetime(2026, 1, 1, 18, 0, 0)
    assert end == datetime(2026, 1, 1, 18, 15, 0)


def test_detect_car_charging_periods_ignores_short_bursts():
    data = [
        {"timestamp": "2026-01-01 18:00:00", "load_power_kw": 6.0, "battery_power_kw": 0.5},
        {"timestamp": "2026-01-01 18:05:00", "load_power_kw": 0.5, "battery_power_kw": 0.0},
    ]
    periods = detect_car_charging_periods(data, min_duration_minutes=10, min_power_kw=5.0)
    assert periods == []


def test_detect_car_charging_periods_empty_data():
    assert detect_car_charging_periods([]) == []


def test_is_car_charging_at_time():
    periods = [(datetime(2026, 1, 1, 18, 0), datetime(2026, 1, 1, 19, 0))]
    assert is_car_charging_at_time(datetime(2026, 1, 1, 18, 30), periods) is True
    assert is_car_charging_at_time(datetime(2026, 1, 1, 20, 0), periods) is False


def test_get_mode_at_time_no_changes_defaults_self_use():
    assert get_mode_at_time(datetime(2026, 1, 1, 12, 0), []) == "SELF_USE"


def test_get_mode_at_time_returns_most_recent_applicable_change():
    mode_changes = [
        {"timestamp": datetime(2026, 1, 1, 0, 0), "mode": "FORCE_CHARGE"},
        {"timestamp": datetime(2026, 1, 1, 6, 0), "mode": "SELF_USE"},
    ]
    assert get_mode_at_time(datetime(2026, 1, 1, 3, 0), mode_changes) == "FORCE_CHARGE"
    assert get_mode_at_time(datetime(2026, 1, 1, 12, 0), mode_changes) == "SELF_USE"
    assert get_mode_at_time(datetime(2025, 12, 31, 0, 0), mode_changes) == "SELF_USE"


def test_get_month():
    assert get_month(datetime(2026, 3, 15, 10, 30)) == "2026-03"
