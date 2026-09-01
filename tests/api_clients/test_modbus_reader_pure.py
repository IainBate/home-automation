"""Unit tests for the pure (no Modbus I/O) functions in src/api_clients/_modbus_reader.py.

_convert_uint16_to_int16, _interpret_battery_mode_from_power,
_validate_power_physical_limits (log-only, smoke-tested for no-raise),
_extract_input_register_data and _process_bulk_register_data all take
already-fetched register lists/ints - no mocking or fake server needed.

_interpret_work_mode's non-manual branch used to hardcode BatteryMode.SELF_USE
for every value instead of delegating to the correct per-value mapping - see
test_interpret_work_mode_non_manual_values_map_correctly below, which pins
down the fix.
"""

from __future__ import annotations

import pytest

from src.api_clients import _modbus_reader as reader
from src.core_logic.battery_simulation import BatteryMode

MASSIVE_BLOCK_SIZE = 79


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (32767, 32767),  # max positive signed value, no conversion
        (32768, -32768),  # smallest value needing conversion
        (65535, -1),  # max uint16 -> -1
        (65036, -500),
    ],
)
def test_convert_uint16_to_int16(value, expected):
    assert reader._convert_uint16_to_int16(value) == expected


@pytest.mark.parametrize(
    ("power_watts", "expected_mode"),
    [
        (500, BatteryMode.FORCE_CHARGE),
        (-500, BatteryMode.FORCE_DISCHARGE),
        (0, BatteryMode.IDLE),
    ],
)
def test_interpret_battery_mode_from_power(power_watts, expected_mode):
    assert reader._interpret_battery_mode_from_power(power_watts) == expected_mode


def test_validate_power_physical_limits_does_not_raise_on_any_input():
    """Log-only function (no return value) - just needs to survive all three bands."""
    reader._validate_power_physical_limits(
        [("Within limits", 5.0), ("Warning band", 21.0), ("Over limit", 30.0)]
    )


def _build_massive_block(**overrides: int) -> list[int]:
    block = [0] * MASSIVE_BLOCK_SIZE
    offsets = {
        "ac_power": 0,
        "run_mode": 7,
        "pv1_power": 8,
        "pv2_power": 9,
        "battery_power_raw": 20,
        "battery_temperature_celsius": 22,
        "battery_soc": 26,
        "battery_cap_lsb": 36,
        "battery_cap_msb": 37,
        "grid_power": 68,
        "feedin_lsb": 70,
        "feedin_msb": 71,
        "consum_lsb": 72,
        "consum_msb": 73,
        "daily_yield": 78,
    }
    for key, value in overrides.items():
        block[offsets[key]] = value
    return block


def test_extract_input_register_data_wrong_size_returns_none():
    assert reader._extract_input_register_data([1, 2, 3]) is None
    assert reader._extract_input_register_data([]) is None


def test_extract_input_register_data_full_extraction():
    block = _build_massive_block(
        ac_power=3000,
        run_mode=2,
        pv1_power=1200,
        pv2_power=800,
        battery_power_raw=65036,  # -500W after signed conversion
        battery_temperature_celsius=25,
        battery_soc=75,
        battery_cap_lsb=10000,
        battery_cap_msb=0,
        grid_power=200,
        feedin_lsb=12345,
        feedin_msb=0,
        consum_lsb=6789,
        consum_msb=0,
        daily_yield=523,
    )

    extracted = reader._extract_input_register_data(block)

    assert extracted["ac_power"] == 3000
    assert extracted["run_mode"] == 2
    assert extracted["run_mode_str"] == "Normal Mode"
    assert extracted["pv1_power"] == 1200
    assert extracted["pv2_power"] == 800
    assert extracted["battery_power_raw"] == 65036
    assert extracted["battery_power"] == -500
    assert extracted["battery_mode"] == BatteryMode.FORCE_DISCHARGE
    assert extracted["battery_temperature_celsius"] == 25
    assert extracted["battery_soc"] == 75
    assert extracted["battery_capacity_kwh"] == 10.0
    assert extracted["grid_power_watts"] == 200
    assert extracted["daily_yield_kwh"] == 5.23
    assert extracted["grid_export_total_kwh"] == pytest.approx(123.45)
    assert extracted["grid_import_total_kwh"] == pytest.approx(67.89)


def test_process_bulk_register_data_combines_all_three_blocks():
    input_block = _build_massive_block(ac_power=1000, battery_soc=50)
    serial_block = [
        (ord("S") << 8) | ord("O"),
        (ord("L") << 8) | ord("A"),
        (ord("X") << 8) | ord("1"),
        (ord("2") << 8) | ord("3"),
        (ord("4") << 8) | ord("5"),
        (ord("6") << 8) | ord("7"),
        (ord("A") << 8) | ord("B"),
    ]
    # RTC (6 regs) + work mode (0x008B) + manual mode (0x008C)
    timestamp_work_block = [30, 15, 10, 20, 3, 25, 0, 1]  # work_mode=0 (SELF_USE)

    result = reader._process_bulk_register_data(input_block, serial_block, timestamp_work_block)

    assert result is not None
    assert result["serial_number"] == "SOLAX1234567AB"
    assert result["rtc_timestamp"] == "2025-03-20 10:15:30"
    assert result["work_mode"] == BatteryMode.SELF_USE
    assert result["battery_soc"] == 50


def test_process_bulk_register_data_returns_none_on_bad_input_block():
    result = reader._process_bulk_register_data([1, 2, 3], [0] * 7, [0] * 8)
    assert result is None


@pytest.mark.parametrize(
    ("work_mode_raw", "expected"),
    [
        (0, BatteryMode.SELF_USE),
        (1, BatteryMode.FEED_IN_PRIORITY),
        (2, BatteryMode.BACKUP),
        (4, BatteryMode.PEAK_SHAVING),
        (5, BatteryMode.TOU_MODE),
        (6, BatteryMode.SMART_SCHEDULE),
        (99, BatteryMode.UNKNOWN_WORK_MODE),
    ],
)
def test_interpret_work_mode_non_manual_values_map_correctly(work_mode_raw, expected):
    """Regression test: every non-manual value used to collapse to SELF_USE."""
    assert reader._interpret_work_mode(work_mode_raw, manual_mode_raw=0) == expected


def test_interpret_work_mode_manual_delegates_to_manual_mode_register():
    assert (
        reader._interpret_work_mode(work_mode_raw=3, manual_mode_raw=1)
        == BatteryMode.FORCE_CHARGE
    )
