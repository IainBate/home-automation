"""Unit tests for src/api_clients/_modbus_validator.py - pure power-data validation.

validate_power_data_physical_limits() takes an already-assembled dict (no I/O),
checking against the 26kW hard limit / 20kW warning threshold documented in its
own docstring - which already carries a doctest exercised via --doctest-modules;
these tests cover the branches the doctest doesn't (warnings, missing/nested
data, multiple simultaneous violations).
"""

from __future__ import annotations

from src.api_clients._modbus_validator import (
    PowerValidationResult,
    validate_power_data_physical_limits,
)


def test_power_validation_result_starts_possible_with_no_messages():
    result = PowerValidationResult()
    assert result.physically_possible is True
    assert result.errors == []
    assert result.warnings == []


def test_add_error_flips_physically_possible_false():
    result = PowerValidationResult()
    result.add_error("boom")
    assert result.physically_possible is False
    assert result.errors == ["boom"]


def test_add_warning_does_not_flip_physically_possible():
    result = PowerValidationResult()
    result.add_warning("careful")
    assert result.physically_possible is True
    assert result.warnings == ["careful"]


def test_within_limits_is_physically_possible_with_no_messages():
    data = {
        "ac_power": {"master": 3000, "slave": 2000},
        "battery_power": {"master": {"power": 1000}, "slave": {"power": -500}},
        "grid_power": {"master": 500, "slave": 0},
    }
    result = validate_power_data_physical_limits(data)
    assert result.physically_possible is True
    assert result.errors == []
    assert result.warnings == []


def test_above_warning_threshold_below_hard_limit_adds_warning_only():
    data = {"ac_power": {"master": 21000}}  # 21kW: > 20kW warning, < 26kW limit
    result = validate_power_data_physical_limits(data)
    assert result.physically_possible is True
    assert result.warnings == ["AC Power Master: 21.00kW approaching 26.0kW limit"]
    assert result.errors == []


def test_above_hard_limit_adds_error():
    data = {"ac_power": {"master": 50000}}
    result = validate_power_data_physical_limits(data)
    assert result.physically_possible is False
    assert result.errors == [
        "AC Power Master: 50.00kW exceeds 26.0kW physical limit (100A supply)"
    ]


def test_negative_power_uses_absolute_value():
    data = {"grid_power": {"master": -50000}}
    result = validate_power_data_physical_limits(data)
    assert result.physically_possible is False
    assert "Grid Power Master" in result.errors[0]


def test_battery_power_handles_nested_dict_shape():
    data = {"battery_power": {"master": {"power": 50000, "mode": "FORCE_CHARGE"}}}
    result = validate_power_data_physical_limits(data)
    assert result.physically_possible is False
    assert "Battery Power Master" in result.errors[0]


def test_battery_power_handles_plain_int_shape():
    data = {"battery_power": {"master": 50000}}
    result = validate_power_data_physical_limits(data)
    assert result.physically_possible is False
    assert "Battery Power Master" in result.errors[0]


def test_missing_categories_default_to_zero_and_pass():
    result = validate_power_data_physical_limits({})
    assert result.physically_possible is True
    assert result.errors == []


def test_none_power_value_is_skipped_not_treated_as_violation():
    data = {"ac_power": {"master": None, "slave": 1000}}
    result = validate_power_data_physical_limits(data)
    assert result.physically_possible is True


def test_multiple_simultaneous_violations_all_reported():
    data = {
        "ac_power": {"master": 50000, "slave": 50000},
        "grid_power": {"master": 50000},
    }
    result = validate_power_data_physical_limits(data)
    assert result.physically_possible is False
    assert len(result.errors) == 3
